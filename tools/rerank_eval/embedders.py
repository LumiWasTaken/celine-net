"""The first stage on the same labelled set (genres/*.json): where an embedder ranks the
passage that answers a description, among the 20 passages of its genre (the same people,
other things) and among all passages of all genres at once.

    python embedders.py <model> [plain]      plain: the query goes out as it is, no instruction
"""
from __future__ import annotations

import json
import sys
import time

from compare import GENRES, HERE, KINDS, NSFW

INSTRUCT = "Given a description of chat messages, retrieve the chat excerpts that match it"
QWEN_LIKE = ("Qwen3-Embedding", "Octen", "F2LLM", "reason-embed", "RTriever")


def run(name: str, plain: bool = False) -> None:
    import torch
    from sentence_transformers import SentenceTransformer
    t0 = time.time()
    model = SentenceTransformer(name, device="cuda", trust_remote_code=True,
                                model_kwargs={"torch_dtype": torch.bfloat16})
    model.max_seq_length = 2048
    docs, start, qs, right, kinds, nsfw = [], {}, [], [], [], []
    for g, d in GENRES.items():
        start[g] = len(docs)
        docs += d["passages"]
        for a in d["answered"]:
            qs.append(a["q"])
            right.append(start[g] + a["passage"])
            kinds.append(a["kind"])
            nsfw.append(g in NSFW)
    hay = HERE / "genres" / "local" / "haystack.json"      # cards.py: thousands of other cards
    if hay.exists():
        have = set(docs)
        docs += [p for p in json.loads(hay.read_text()) if p not in have]
    if "Octen" in name:                   # its card: a document without instruction starts "- "
        docs = ["- " + d for d in docs]
    prompts = model.prompts or {}
    if plain:
        how, qv = "plain", model.encode(qs, normalize_embeddings=True, batch_size=16)
    elif any(k in name for k in QWEN_LIKE):
        how = "instruct"
        qv = model.encode(qs, prompt=f"Instruct: {INSTRUCT}\nQuery:", normalize_embeddings=True,
                          batch_size=16)
    elif "query" not in prompts and "web_search_query" in prompts:
        how = "web_search_query"
        qv = model.encode(qs, prompt_name=how, normalize_embeddings=True, batch_size=16)
    else:
        how = "its own query prompt" if "query" in prompts else "plain"
        qv = model.encode_query(qs, normalize_embeddings=True, batch_size=16)
    dv = model.encode(docs, normalize_embeddings=True, batch_size=8) if "Octen" in name \
        else model.encode_document(docs, normalize_embeddings=True, batch_size=8)
    sims = torch.tensor(qv).float() @ torch.tensor(dv).float().T
    assert torch.isfinite(sims).all(), "a vector is not finite"
    rank = [int((row > row[r]).sum()) + 1 for row, r in zip(sims, right)]          # among all
    own = [int((row[r - r % 20:r - r % 20 + 20] > row[r]).sum()) + 1                # in its genre
           for row, r in zip(sims, right)]
    share = lambda v: sum(v) / len(v)                                              # noqa: E731
    print(f"{name:46} {how:20} {len(qs)} queries, {len(docs)} passages | genre top1 {share([r == 1 for r in own]):.2f} | all: "
          f"top1 {share([r == 1 for r in rank]):.2f} top10 {share([r <= 10 for r in rank]):.2f} "
          f"top50 {share([r <= 50 for r in rank]):.2f} worst {max(rank):3} | "
          + " ".join(f"{k[:5]} {share([r == 1 for r, c in zip(rank, kinds) if c == k]):.2f}"
                     for k in KINDS)
          + f" | nsfw {share([r == 1 for r, x in zip(rank, nsfw) if x]):.2f}"
          f" | dims {dv.shape[1]} {torch.cuda.max_memory_allocated() / 2 ** 30:.1f} GB"
          f" {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    run(sys.argv[1], plain=sys.argv[2:] == ["plain"])
