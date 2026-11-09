"""The `words-banned` block of docs/ui/words.md holds for the UI's strings.

The web app: JSX text and quoted strings that read as words (they start with a capital
letter), comments left out; a `label` rule matches a whole string, a `word` rule a whole
word or phrase inside one. The server: a `label` rule against its string constants that
can be a label (not a docstring, a dict key or a piece of an f-string). Matching ignores
case."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUOTED = re.compile(r"""(?<![\w$])(['"`])((?:(?!\1)[^\\\n]|\\.)*)\1""")
JSX_TEXT = re.compile(r">([^<>{}\n]*[A-Za-z][^<>{}\n]*)<")
COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def rules() -> list[tuple[str, str, str]]:
    block = re.search(r"```words-banned\n(.*?)```", (ROOT / "docs/ui/words.md").read_text(), re.S)
    out = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            kind, rest = line.split(" ", 1)
            banned, _, instead = rest.partition(" => ")
            out.append((kind, banned.strip(), instead.strip()))
    return out


def reads_as_words(s: str) -> bool:
    s = s.strip()
    return bool(s) and s[0].isupper() and not re.search(r"[/\\${}=]|^[A-Z0-9_]+$|^[A-Z][a-z]+[A-Z]", s)


def broken(s: str, kinds: set[str]) -> list[str]:
    out = []
    for kind, banned, instead in rules():
        if kind not in kinds:
            continue
        if kind == "label" and s.strip().rstrip(".").lower() == banned.lower() or kind == "word" and re.search(
                rf"(?<![\w-]){re.escape(banned)}(?![\w-])", s, re.I):
            out.append(f"{kind} {banned!r} in {s!r}: {instead}")
    return out


def test_the_web_app_uses_no_banned_word():
    found = []
    for f in sorted((ROOT / "web/src").rglob("*.ts*")):
        if ".check." in f.name:
            continue
        text = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), f.read_text())
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("import "):
                continue
            for s in [m.group(2) for m in QUOTED.finditer(line)] + [m.group(1) for m in JSX_TEXT.finditer(line)]:
                if reads_as_words(s):
                    found += [f"{f.relative_to(ROOT)}:{n}: {b}" for b in broken(s, {"label", "word"})]
    assert not found, "\n".join(found)


def test_the_server_labels_no_banned_word():
    found = []
    for f in sorted((ROOT / "server/celine").rglob("*.py")):
        tree = ast.parse(f.read_text())
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body \
                    and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                skip.add(id(node.body[0].value))
            elif isinstance(node, ast.Dict):
                skip.update(id(k) for k in node.keys if k is not None)
            elif isinstance(node, ast.JoinedStr):
                skip.update(id(v) for v in node.values)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip \
                    and reads_as_words(node.value):
                found += [f"{f.relative_to(ROOT)}:{node.lineno}: {b}" for b in broken(node.value, {"label"})]
    assert not found, "\n".join(found)
