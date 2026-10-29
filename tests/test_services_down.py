"""ComfyUI, the embedding server and bw when they are missing or wrong: one plain
sentence each, in its tool result or the status line, and nothing that hangs."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from celine import image_catalog
from celine.kb import embed
from celine.kb.retrieve import resolve, retrieve
from kb_fixtures import build
from test_comfyui import app  # noqa: F401  (the fixture: a fake ComfyUI and llama-swap)

pytestmark = pytest.mark.anyio


# ---- ComfyUI --------------------------------------------------------------------------

async def test_a_preset_whose_file_the_server_lacks_fails_before_queueing(app):
    fake = app.fake
    fake.lacks = {"UNETLoader": "unet_name"}
    b = await app.batches.create("images", [{"prompt": "a", "preset": "krea2-turbo"}])
    b = await app.batches.run(b["id"])
    item = b["items"][0]
    assert item["state"] == "failed"
    assert item["error"].startswith("The preset Krea 2 turbo needs ")
    assert item["error"].endswith(" (UNETLoader), which ComfyUI does not have.")
    assert fake.graphs == [], "nothing was queued"
    assert fake.count("comfy GET /object_info/UNETLoader") == 1


async def test_a_dead_comfyui_names_its_address_and_flips_the_host(app):
    fake = app.fake
    fake.dead = True
    b = await app.batches.create("images", [{"prompt": "a"}, {"prompt": "b"}])
    b = await app.batches.run(b["id"])
    assert [i["state"] for i in b["items"]] == ["failed", "failed"]
    assert b["items"][0]["error"] == "ComfyUI at comfy:8188 refused the connection."
    assert app.llm.router.hosts()["comfy"]["state"] == "down", "the Models page flips"
    assert app.llm.router.provider_state("comfy").error == b["items"][0]["error"]


async def test_lora_manager_absent_means_no_metadata_not_an_error():
    def comfy(request):
        path = request.url.path
        if path.startswith("/object_info/"):
            cls = path.rsplit("/", 1)[1]
            key = {"UNETLoader": "unet_name", "LoraLoaderModelOnly": "lora_name"}[cls]
            return httpx.Response(200, json={cls: {"input": {"required": {
                key: [["flux/krea.safetensors" if cls == "UNETLoader"
                       else "flux/style.safetensors"]]}}}})
        assert path.startswith("/api/lm/")
        return httpx.Response(404, text="Not Found")
    image_catalog._cache.clear()
    cat = await image_catalog.load("http://comfy:8188", transport=httpx.MockTransport(comfy))
    assert sorted(r.id for r in cat.resources) == ["flux/krea", "flux/style"]
    assert all(r.summary == "" and r.triggers == () for r in cat.resources)


# ---- the embedding server ----------------------------------------------------------------

def test_embedding_server_failures_are_one_sentence_each(monkeypatch):
    s = {"url": "http://emb:8081/v1/embeddings", "model": "qwen3-embed",
         "timeout": 20}
    answer = {}

    def post(url, json, headers, timeout):
        a = answer["a"]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(httpx, "post", post)

    answer["a"] = httpx.ConnectError("refused")
    with pytest.raises(embed.SemanticError, match=r"^the embedding server at emb:8081 refused "
                                                 r"the connection \(Settings, Models\)$"):
        embed.embed_sync(s, ["x"])
    answer["a"] = httpx.ReadTimeout("slow")
    with pytest.raises(embed.SemanticError, match="did not answer within 20 s$"):
        embed.embed_sync(s, ["x"])
    for status, words in ((401, "rejected the API key"), (404, "has no model qwen3-embed"),
                          (500, "answered 500")):
        answer["a"] = httpx.Response(status, text="<html>nope</html>")
        with pytest.raises(embed.SemanticError, match=words) as e:
            embed.embed_sync(s, ["x"])
        assert "<" not in str(e.value)
    answer["a"] = httpx.Response(200, text="<html>a web page</html>")
    with pytest.raises(embed.SemanticError, match="answered without vectors"):
        embed.embed_sync(s, ["x"])


def test_a_search_without_an_embedder_says_why_in_its_notes(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"), embed=None,
                   embed_why="the embedding server at emb:8081 refused the connection")
    assert ("Semantic topic search unavailable (the embedding server at emb:8081 refused "
            "the connection). Word search only.") in res.notes


# ---- bw -----------------------------------------------------------------------------------

async def test_a_missing_bw_is_said_once_and_her_turns_are_not_held(tmp_path, monkeypatch):
    from celine import vault as vault_mod
    from celine.vault import Vault
    monkeypatch.setenv("PATH", str(tmp_path))                 # no bw anywhere
    monkeypatch.setattr(vault_mod, "account", lambda cfg: SimpleNamespace(
        url="https://vault.example.com", client_id="id", client_secret="s", collection="c"))
    v = Vault(SimpleNamespace(data_dir=tmp_path))
    assert not v.ready.is_set()
    status = await v.connect()
    assert status == vault_mod.NO_BW
    assert v.ready.is_set(), "the dispatcher waits on this: it never waits forever"
    with pytest.raises(vault_mod.VaultError, match="no bw program"):
        await v.items()
