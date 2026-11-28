"""Score the labelled set (genres/*.json, checked by check_set.py) on a reranker and say how well it ranks and how well
its score tells "a passage answers this" from "none does".

    python compare.py run <model>     one model, writes results/<model>.json
    python compare.py table           every result as one table
    python compare.py doubts          labels the strong models agree are wrong

Runs in its own environment (torch, transformers, sentence-transformers); the app does
not import this. Each model runs in its own process, so the GPU is free for the next.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "results"
# genres/local/ holds the genres built from real character cards (cards.py); not in git
GENRES = {f.stem: json.loads(f.read_text()) for f in sorted((HERE / "genres").glob("*.json"))
          + sorted(f for f in (HERE / "genres" / "local").glob("cards_*.json")
                   if f.name.count(".") == 1)}
CARDS = tuple(g for g in GENRES if g.startswith("cards_"))
NSFW = ("flirting", "kink", "party", "fights", *CARDS)
KINDS = ("literal", "paraphrase", "deep", "xling", "name")
INSTRUCT = ("Given a description of chat messages, judge whether the chat excerpt contains "
            "messages that match the description")
YESNO = {"Qwen/Qwen3-Reranker-0.6B", "Qwen/Qwen3-Reranker-4B", "Qwen/Qwen3-Reranker-8B",
         "Yuanbao-Search/Yuanbao-Reranker-0.6B", "Yuanbao-Search/Yuanbao-Reranker-4B",
         "Yuanbao-Search/Yuanbao-Reranker-8B"}
TRUST = {"jinaai/jina-reranker-v2-base-multilingual", "Alibaba-NLP/gte-multilingual-reranker-base",
         "KaLM-Embedding/KaLM-Reranker-V1-Large-R2", "KaLM-Embedding/KaLM-Reranker-V1-Small-R2",
         "nvidia/Nemotron-3.5-Rerank-8B-BF16", "nvidia/Nemotron-3.5-Rerank-1B-BF16",
         "Querit/Querit-Reranker-4B"}


def yesno(name: str):
    """Qwen3-Reranker and the models built like it: P(yes) after the instruction."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(name, padding_side="left")
    model = AutoModelForCausalLM.from_pretrained(name, torch_dtype=torch.bfloat16,
                                                 device_map="cuda").eval()
    yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
    pre = ('<|im_start|>system\nJudge whether the Document meets the requirements based on '
           'the Query and the Instruct provided. Note that the answer can only be "yes" or '
           '"no".<|im_end|>\n<|im_start|>user\n')
    suf = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    @torch.inference_mode()
    def score(q: str, docs: list[str]) -> list[float]:
        out = []
        for i in range(0, len(docs), 16):
            texts = [f"{pre}<Instruct>: {INSTRUCT}\n<Query>: {q}\n<Document>: {d}{suf}"
                     for d in docs[i:i + 16]]
            b = tok(texts, padding=True, return_tensors="pt").to("cuda")
            last = model(**b).logits[:, -1, :]
            two = torch.stack([last[:, no], last[:, yes]], dim=1).float().log_softmax(dim=1)
            out += two[:, 1].exp().tolist()
        return out
    return score


def gemma(name: str):
    """bge-reranker-v2-gemma: the logit of Yes after its prompt."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(name)
    model = AutoModelForCausalLM.from_pretrained(name, torch_dtype=torch.bfloat16,
                                                 device_map="cuda").eval()
    yes = tok("Yes", add_special_tokens=False)["input_ids"][0]
    prompt = ("Given a query A and a passage B, determine whether the passage contains an "
              "answer to the query by providing a prediction of either 'Yes' or 'No'.")

    @torch.inference_mode()
    def score(q: str, docs: list[str]) -> list[float]:
        out = []
        for d in docs:
            ids = tok(f"A: {q}\nB: {d}\n{prompt}", return_tensors="pt").to("cuda")
            out.append(float(model(**ids).logits[0, -1, yes]))
        return out
    return score


def listwise(name: str):
    """jina-reranker-v3.5: one pass over the whole list; a score is relative to the list."""
    from transformers import AutoModel
    model = AutoModel.from_pretrained(name, dtype="auto", trust_remote_code=True).to("cuda").eval()

    def score(q: str, docs: list[str]) -> list[float]:
        out = [0.0] * len(docs)
        for r in model.rerank(q, docs):
            out[r["index"]] = float(r["relevance_score"])
        return out
    return score


def cross(name: str):
    from sentence_transformers import CrossEncoder
    model = CrossEncoder(name, device="cuda", trust_remote_code=name in TRUST)

    def score(q: str, docs: list[str]) -> list[float]:
        return [float(x) for x in model.predict([(q, d) for d in docs], batch_size=16)]
    return score


def load(name: str):
    if name in YESNO:
        return yesno(name)
    if name == "BAAI/bge-reranker-v2-gemma":
        return gemma(name)
    if name.startswith("jinaai/jina-reranker-v3"):
        return listwise(name)
    return cross(name)


def run(name: str) -> None:
    import torch
    t0 = time.time()
    score = load(name)
    loaded = time.time() - t0
    t0 = time.time()
    path = OUT / (name.replace("/", "__") + ".json")
    old = json.loads(path.read_text()) if path.exists() else {}
    out, pairs = old.get("genres", {}), 0                # a genre already scored stays
    for g, d in GENRES.items():
        if g in out:
            continue
        out[g] = {"answered": [score(a["q"], d["passages"]) for a in d["answered"]],
                  "unanswered": [score(u["q"], d["passages"]) for u in d["unanswered"]]}
        pairs += (len(d["answered"]) + len(d["unanswered"])) * len(d["passages"])
    took = time.time() - t0
    OUT.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "model": name, "genres": out, "load_s": loaded,
        "pairs_per_s": old.get("pairs_per_s") or pairs / took,
        "vram_gb": old.get("vram_gb") or torch.cuda.max_memory_allocated() / 2 ** 30}))
    print(name, f"{took:.0f}s")


def auroc(pos: list[float], neg: list[float]) -> float:
    """How often an answered description scores above an unanswered one."""
    if not pos or not neg:
        return float("nan")
    pos, neg = sorted(pos), sorted(neg)
    wins = j = 0
    for p in pos:                                  # a tie counts as a loss: rare with floats
        while j < len(neg) and neg[j] < p:
            j += 1
        wins += j
    return wins / (len(pos) * len(neg))


def lead(s: list[float]) -> float:
    """How far the best passage leads the second."""
    a, b = sorted(s)[-2:]
    return b - a


def rows(r: dict, only: tuple[str, ...] | None = None) -> dict:
    """One model's results as flat lists: for every answered description whether the
    right passage is first, its kind, its score and its lead; for every unanswered one
    its kind, its best score and that passage's lead."""
    right, kind, pos, pos_lead, neg, neg_kind, neg_lead = [], [], [], [], [], [], []
    for g, d in GENRES.items():
        if g not in r["genres"] or (only and g not in only):
            continue
        for s, a in zip(r["genres"][g]["answered"], d["answered"]):
            right.append(s[a["passage"]] == max(s))
            kind.append(a["kind"])
            pos.append(s[a["passage"]])
            pos_lead.append(lead(s))
        for s, u in zip(r["genres"][g]["unanswered"], d["unanswered"]):
            neg.append(max(s))
            neg_kind.append(u["kind"])
            neg_lead.append(lead(s))
    return {"right": right, "kind": kind, "pos": pos, "pos_lead": pos_lead, "neg": neg,
            "neg_kind": neg_kind, "neg_lead": neg_lead}


def results() -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted(OUT.glob("*.json"))]


def table() -> None:
    print(f"{'model':42} {'top1':>5} " + " ".join(f"{k[:5]:>5}" for k in KINDS)
          + f" {'nsfw':>5} {'worst':>12} | {'sep':>5} {'hard':>5} {'far':>5} {'gap':>5}"
          f" {'nsfw':>5} {'cut':>5} {'keep':>5} | {'pairs/s':>7} {'GB':>5}")
    lines = []
    for r in results():
        x = rows(r)
        n = rows(r, NSFW)
        share = lambda v: sum(v) / len(v) if v else float("nan")          # noqa: E731
        by = {k: [ok for ok, c in zip(x["right"], x["kind"]) if c == k] for k in KINDS}
        per = {g: share(rows(r, (g,))["right"]) for g in r["genres"]}
        worst = min(per, key=per.get)
        hard = [s for s, c in zip(x["neg"], x["neg_kind"]) if c == "hard"]
        far = [s for s, c in zip(x["neg"], x["neg_kind"]) if c == "far"]
        both = sorted(set(x["pos"] + x["neg"]))
        cut = max((sum(p >= t for p in x["pos"]) + sum(q < t for q in x["neg"]))
                  / (len(x["pos"]) + len(x["neg"])) for t in both[::max(1, len(both) // 400)])
        # a floor that loses 5% of the real answers: the share of no-answers it refuses
        floor = sorted(x["pos"])[len(x["pos"]) // 20]
        keep = share([q < floor for q in x["neg"]])
        sep = auroc(x["pos"], x["neg"])
        lines.append((sep, f"{r['model']:42} {share(x['right']):5.2f} "
                      + " ".join(f"{share(by[k]):5.2f}" for k in KINDS)
                      + f" {share(n['right']):5.2f} {worst[:7]:>7} {per[worst]:4.2f} |"
                      f" {sep:5.3f} {auroc(x['pos'], hard):5.3f} {auroc(x['pos'], far):5.3f}"
                      f" {auroc(x['pos_lead'], x['neg_lead']):5.3f}"
                      f" {auroc(n['pos'], n['neg']):5.3f} {cut:5.2f} {keep:5.2f} |"
                      f" {r['pairs_per_s']:7.0f} {r['vram_gb']:5.1f}"))
    for _, line in sorted(lines, reverse=True):
        print(line)
    n = sum(len(d["answered"]) + len(d["unanswered"]) for d in GENRES.values())
    print(f"\n{len(GENRES)} genres, {n} descriptions, each scored against the 20 passages "
          "of its genre.\nLeft: the right passage ranked first: overall, by what the "
          "description takes, on the four NSFW genres, and the genre it is worst on.\n"
          "Middle, telling an answer from none, over all genres at once (1.0 = always): "
          "sep by the score, against near misses (hard) and absent things (far); gap by "
          "the lead of the best passage over the second; nsfw: sep on the NSFW genres; "
          "cut: the share of all descriptions the best single floor gets right; keep: "
          "the share of no-answers refused by a floor that loses 5% of the real answers.")


def doubts() -> None:
    """Labels to read again: an answered description where most strong models put the
    same other passage first, and an unanswered one that most score like an answer."""
    strong = [r for r in results() if sum(rows(r)["right"]) / len(rows(r)["right"]) >= 0.85]
    print(f"{len(strong)} models with top1 of 0.85 or more vote")
    for g, d in GENRES.items():
        rs = [r for r in strong if g in r["genres"]]
        if not rs:
            continue
        for j, a in enumerate(d["answered"]):
            firsts = [max(range(20), key=r["genres"][g]["answered"][j].__getitem__) for r in rs]
            top = max(set(firsts), key=firsts.count)
            if top != a["passage"] and firsts.count(top) >= 0.7 * len(rs):
                print(f"{g} answered {j} ({a['kind']}): labelled {a['passage']}, "
                      f"{firsts.count(top)}/{len(rs)} say {top}: {a['q']}")
        for j, u in enumerate(d["unanswered"]):
            votes = 0
            for r in rs:
                pos = sorted(rows(r)["pos"])
                votes += max(r["genres"][g]["unanswered"][j]) >= pos[len(pos) // 2]
            if votes >= 0.7 * len(rs):
                s = rs[0]["genres"][g]["unanswered"][j]
                print(f"{g} unanswered {j} ({u['kind']}): {votes}/{len(rs)} score it like "
                      f"an answer, passage {max(range(20), key=s.__getitem__)}: {u['q']}")


if __name__ == "__main__":
    {"run": lambda: run(sys.argv[2]), "table": table, "doubts": doubts}[sys.argv[1]]()
