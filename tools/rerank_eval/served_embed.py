"""An embedder as it is served: the labelled chat genres through a llama-server /v1/embeddings,
beside the same model through sentence-transformers.

    python served_embed.py <url> <hf model or -> <query prefix> <passage prefix>

Prints, over every answered description against all passages of all chat genres: right
passage first and in the top ten for the served file, the same for the reference, the mean
cosine between a served and a reference vector, and how often both pick the same passage.
"""
from __future__ import annotations

import json
import sys
import urllib.request

import numpy as np

from compare import GENRES


def served(url: str, texts: list[str]) -> np.ndarray:
    out = []
    for i in range(0, len(texts), 16):
        req = urllib.request.Request(url + "/v1/embeddings", json.dumps({"input": texts[i:i + 16]}).encode(),
                                     {"Content-Type": "application/json"})
        data = json.load(urllib.request.urlopen(req, timeout=600))["data"]
        out += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    v = np.array(out, dtype=np.float32)
    assert np.isfinite(v).all(), "the server returned a vector that is not finite"
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def main(url: str, model: str, qp: str, pp: str) -> None:
    passages, queries, right = [], [], []
    for g, d in GENRES.items():
        if g.startswith("cards_"):
            continue
        base = len(passages)
        passages += d["passages"]
        for a in d["answered"]:
            queries.append(a["q"])
            right.append(base + a["passage"])
    right = np.array(right)

    def report(name: str, q: np.ndarray, p: np.ndarray) -> np.ndarray:
        order = np.argsort(-(q @ p.T), axis=1)
        print(f"{name:10} {len(queries)} queries, {len(passages)} passages | top1 {np.mean(order[:, 0] == right):.3f} "
              f"top10 {np.mean((order[:, :10] == right[:, None]).any(1)):.3f} | dims {p.shape[1]}")
        return order[:, 0]

    sq, sp = served(url, [qp + q for q in queries]), served(url, [pp + p for p in passages])
    s1 = report("served", sq, sp)
    if model != "-":
        import torch
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(model, model_kwargs={"torch_dtype": torch.bfloat16}, device="cuda")
        enc = lambda t: m.encode(t, batch_size=8, normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)  # noqa: E731
        rq, rp = enc([qp + q for q in queries]), enc([pp + p for p in passages])
        r1 = report("reference", rq, rp)
        print(f"cosine served to reference: queries {np.mean(np.sum(sq * rq, 1)):.4f} passages {np.mean(np.sum(sp * rp, 1)):.4f} "
              f"| same first passage {np.mean(s1 == r1):.3f}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
