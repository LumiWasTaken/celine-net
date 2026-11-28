"""The reranker as it is served: score the labelled set through a llama-server /v1/rerank,
and say how far one served file's scores are from another's.

    python served.py score <url> <label> [chat|all]   writes results/<label>.json (compare.py table reads it)
    python served.py agree <reference label> <label> [...]

agree, per description (a query and its 20 passages), against the reference:
  top1      the best passage is the same one
  top5      share of the reference's five best that are among the five best
  flips     pairs of passages in the other order although the reference had them 0.5 logit
            or more apart, as a share of such pairs
  rms, p99  difference of the logit of the score, over all pairs
  kld       KL divergence of the yes/no distribution, in nats, over the pairs the reference is
            undecided on (its logit within 3 of zero): elsewhere nothing can move
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request

from compare import GENRES, OUT


def rerank(url: str, q: str, docs: list[str]) -> list[float]:
    req = urllib.request.Request(url + "/v1/rerank", json.dumps({"query": q, "documents": docs}).encode(),
                                 {"Content-Type": "application/json"})
    out = [0.0] * len(docs)
    for r in json.load(urllib.request.urlopen(req, timeout=600))["results"]:
        out[r["index"]] = r["relevance_score"]
    return out


def score(url: str, label: str, which: str = "chat") -> None:
    t0, pairs, res = time.time(), 0, {}
    for g, d in GENRES.items():
        if which == "chat" and g.startswith("cards_"):
            continue
        res[g] = {k: [rerank(url, x["q"], d["passages"]) for x in d[k]] for k in ("answered", "unanswered")}
        pairs += 50 * len(d["passages"])
    took = time.time() - t0
    OUT.mkdir(exist_ok=True)
    (OUT / f"{label}.json").write_text(json.dumps({"model": label, "genres": res, "load_s": 0,
                                                   "pairs_per_s": pairs / took, "vram_gb": 0}))
    print(label, f"{pairs} pairs in {took:.0f}s")


def z(p: float) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return math.log(p / (1 - p))


def agree(ref: str, labels: list[str]) -> None:
    a = json.loads((OUT / f"{ref}.json").read_text())["genres"]
    print(f"{'against ' + ref:34} {'queries':>7} {'top1':>6} {'top5':>6} {'flips':>7} {'rms':>6} {'p99':>6} {'kld':>9}")
    for lab in labels:
        b = json.loads((OUT / f"{lab}.json").read_text())["genres"]
        top1, top5, flip, far, dz, kl = [], [], 0, 0, [], []
        for g in a:
            if g not in b:
                continue
            for k in ("answered", "unanswered"):
                for ra, rb in zip(a[g][k], b[g][k]):
                    za, zb = [z(p) for p in ra], [z(p) for p in rb]
                    oa = sorted(range(len(za)), key=za.__getitem__, reverse=True)
                    ob = sorted(range(len(zb)), key=zb.__getitem__, reverse=True)
                    top1.append(oa[0] == ob[0])
                    top5.append(len(set(oa[:5]) & set(ob[:5])) / 5)
                    for i in range(len(za)):
                        for j in range(i):
                            if abs(za[i] - za[j]) >= 0.5:
                                far += 1
                                flip += (za[i] - za[j]) * (zb[i] - zb[j]) < 0
                    for x, y, p, q in zip(za, zb, ra, rb):
                        dz.append(abs(x - y))
                        if abs(x) < 3:
                            q = min(max(q, 1e-12), 1 - 1e-12)
                            kl.append(p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q)))
        dz.sort()
        share = lambda v: sum(v) / len(v)                                          # noqa: E731
        print(f"{lab:34} {len(top1):7} {share(top1):6.3f} {share(top5):6.3f} {flip / max(far, 1):7.4f} "
              f"{math.sqrt(share([d * d for d in dz])):6.3f} {dz[int(.99 * len(dz))]:6.3f} "
              f"{share(kl) if kl else float('nan'):9.5f}")


if __name__ == "__main__":
    if sys.argv[1] == "score":
        score(*sys.argv[2:5])
    else:
        agree(sys.argv[2], sys.argv[3:])
