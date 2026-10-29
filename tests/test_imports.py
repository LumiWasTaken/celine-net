"""The package boundaries hold:
the contracts in .importlinter, every package named in them, and no new two-way import
between two packages of server/celine. TWO_WAY is today's debt; it only shrinks."""
from __future__ import annotations

import configparser
import logging
from pathlib import Path

import grimp
from importlinter import cli          # importing it configures import-linter

ROOT = Path(__file__).parent.parent
CONTRACTS = ROOT / ".importlinter"

# the pairs of packages that import each other. A change that
# breaks one deletes its line here; a change that makes a new one fails.
TWO_WAY = {
    ("api", "conversations"), ("api", "guard"), ("api", "terminals"),
    ("attachments", "conversations"), ("backends", "kb"), ("backends", "settings"),
    ("backends", "turns"), ("browser", "skills"),
    ("comfyui", "image_catalog"), ("comfyui", "settings"),
    ("conversations", "guard"), ("conversations", "intercom"),
    ("conversations", "settings"), ("conversations", "skills"),
    ("conversations", "sub_agents"), ("conversations", "turns"), ("events", "guard"),
    ("events", "intercom"), ("guard", "intercom"), ("guard", "settings"),
    ("guard", "skills"), ("guard", "turns"), ("intercom", "missions"),
    ("intercom", "sub_agents"), ("jobs", "turns"), ("missions", "plans"),
    ("settings", "skills"), ("skills", "turns"),
    ("skills", "vault"), ("sub_agents", "turns"),
}


def _top(module: str) -> str:
    return module.split(".")[1] if "." in module else ""


def _edges(graph) -> set[tuple[str, str]]:
    return {(a, b) for m in graph.modules
            for i in graph.find_modules_directly_imported_by(m)
            if (a := _top(m)) and (b := _top(i)) and a != b}


def test_the_contracts_hold():
    """Through use_cases, not cli.lint_imports: that one runs logging's dictConfig, which
    disables every logger this worker already made, and a later caplog test sees nothing."""
    made = logging.getLogger("celine.vault")
    assert cli.use_cases.lint_imports(config_filename=str(CONTRACTS), cache_dir=None)
    assert not made.disabled


def test_the_bottom_contract_names_every_package():
    """A new package is a feature unless it is added to the bottom on purpose."""
    c = configparser.ConfigParser()
    c.read(CONTRACTS)
    bottom = c["importlinter:contract:bottom"]
    named = {*bottom["source_modules"].split(), *bottom["forbidden_modules"].split()}
    graph = grimp.build_graph("celine")
    assert {f"celine.{t}" for m in graph.modules if (t := _top(m))} == named


def test_no_new_two_way_import_and_the_debt_only_shrinks():
    edges = _edges(grimp.build_graph("celine"))
    pairs = {tuple(sorted(e)) for e in edges if e[::-1] in edges}
    assert pairs - TWO_WAY == set(), "a new two-way import between packages"
    assert TWO_WAY - pairs == set(), "fixed: delete these lines from TWO_WAY"
