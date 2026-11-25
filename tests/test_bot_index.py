"""Checks bots/index.json, the bot index (README: The bot index), against the
bots' files and imports, the Play page's list, and the benchmark's.

Usage: python tests/test_bot_index.py
"""

import ast
import json
import re
import shutil
import subprocess
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "bots" / "index.json"

FIELDS = {"name", "about", "kind", "made", "language", "files", "depends", "run", "build", "rules", "maps",
          "play_page", "benchmark"}
MADE = {"by", "how", "created", "commit"}
KINDS = {"reference", "sample", "entry"}
LANGUAGES = {"python", "rust", "c++"}
RULES = {"classic", "strategic"}
PLAY_PAGE = {None, "js", "pyodide", "wasm"}
BENCHMARK = {None, "kit", "held back", "entry"}


def bots():
    return json.loads(INDEX.read_text(encoding="utf-8"))["bots"]


def text(path):
    return (ROOT / path).read_text(encoding="utf-8")


def map_names():
    """The maps a record can name: the presets in configs/, and the Play page's maps."""
    names = {p.stem for p in (ROOT / "configs").glob("*.toml")}
    return names | set(re.findall(r'\bid: "([^"]+)", name: "', text("web/play.js")))


def play_page_list():
    """{id: {field: value}} for the bots in web/bots.js's list."""
    source = text("web/bots.js")
    block = source[source.index("const list = ["):]
    out = {}
    for line in block[:block.index("];")].splitlines():
        found = re.search(r'\bid: "([^"]+)"', line)
        if found:
            out[found.group(1)] = dict(re.findall(r'\b(name|about|rules|map): "([^"]*)"', line))
    return out


def local_imports(path):
    """The files in bots/ and kits/python/ that a Python file imports."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    found = (directory / (name + ".py") for name in names for directory in (ROOT / "bots", ROOT / "kits" / "python"))
    return {f for f in found if f.is_file()}


def imported(files):
    """Everything the files import from the repository, directly or not, as paths from the root."""
    seen, todo = set(), [ROOT / f for f in files]
    while todo:
        for f in local_imports(todo.pop()):
            path = f.relative_to(ROOT).as_posix()
            if path not in seen and path not in files:
                seen.add(path)
                todo.append(f)
    return seen


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


class BotIndex(unittest.TestCase):
    def test_records(self):
        maps = map_names()
        for bid, b in bots().items():
            with self.subTest(bot=bid):
                self.assertRegex(bid, r"^[a-z0-9][a-z0-9-]*$", "ids are lowercase, with dashes")
                self.assertEqual(set(b) - FIELDS, set(), "unknown fields")
                self.assertEqual(FIELDS - set(b), set(), "missing fields")
                self.assertTrue(b["name"] and b["about"], "a name and an about line")
                self.assertIn(b["kind"], KINDS)
                made = b["made"]
                self.assertEqual(MADE - set(made), set(), "made needs by, how, created, and commit")
                self.assertEqual(set(made) - MADE - {"session"}, set(), "unknown fields in made")
                self.assertTrue(made["by"] is None or (made["by"] and all(made["by"])), "by: null, or who")
                self.assertTrue(made["how"], "how it was made")
                if made["created"] is not None:
                    date.fromisoformat(made["created"])
                if made["commit"] is not None:
                    self.assertRegex(made["commit"], r"^[0-9a-f]{7,40}$")
                self.assertIn(b["language"], LANGUAGES)
                self.assertTrue(b["files"], "at least one file")
                for key in ("run", "build"):
                    self.assertTrue(b[key] is None or b[key].strip(), f"{key}: null, or a command")
                if b["language"] == "python":
                    self.assertTrue(b["run"] and b["run"].startswith("python "), "a Python bot runs with python")
                if b["language"] == "rust":
                    self.assertTrue(b["build"], "a Rust bot says how to build it")
                self.assertTrue(b["rules"] and set(b["rules"]) <= RULES and len(set(b["rules"])) == len(b["rules"]),
                                f"rules: some of {sorted(RULES)}")
                self.assertTrue(b["maps"] == "any" or (b["maps"] and set(b["maps"]) <= maps),
                                f"maps: \"any\", or some of {sorted(maps)}")
                self.assertIn(b["play_page"], PLAY_PAGE)
                self.assertIn(b["benchmark"], BENCHMARK)
                self.assertEqual(b["kind"] == "entry", b["benchmark"] == "entry",
                                 "a benchmark entry is kind entry, and the other way round")

    def test_files_exist(self):
        for bid, b in bots().items():
            for f in b["files"] + b["depends"]:
                self.assertTrue((ROOT / f).is_file(), f"{bid}: there's no {f}")

    def test_every_bot_file_is_indexed(self):
        listed = {f for b in bots().values() for f in b["files"]}
        for f in sorted((ROOT / "bots").rglob("*")):
            path = f.relative_to(ROOT).as_posix()
            if f.is_file() and "__pycache__" not in f.parts and f != INDEX:
                self.assertTrue(path in listed, f"{path} isn't in bots/index.json: give it a record, or add it to one")

    def test_python_depends(self):
        for bid, b in bots().items():
            if b["language"] == "python":
                self.assertEqual(sorted(b["depends"]), sorted(imported(b["files"])),
                                 f"{bid}: depends should list every repository file it imports, directly or not")

    def test_play_page(self):
        index, listed = bots(), play_page_list()
        self.assertEqual(sorted(listed), sorted(bid for bid, b in index.items() if b["play_page"]),
                         "web/bots.js's list should hold the bots whose play_page isn't null")
        for bid, fields in listed.items():
            b = index[bid]
            self.assertEqual(fields.get("name"), b["name"], f"{bid}: web/bots.js has another name")
            self.assertEqual(fields.get("about"), b["about"], f"{bid}: web/bots.js has another about line")
            self.assertIn(fields.get("rules", "classic"), b["rules"], f"{bid}: the Play page offers it for other rules")
            if "map" in fields:
                self.assertEqual(b["maps"], [fields["map"]], f"{bid}: the Play page offers it on {fields['map']} only")
        # A Pyodide bot needs a class in scripts/browser_bot.py, and its
        # files and the bots it imports in the bundle of reference bots.
        pyodide = sorted(bid for bid, b in index.items() if b["play_page"] == "pyodide")
        classes = re.search(r"bot = \{([^}]*)\}\[name\]", text("scripts/browser_bot.py")).group(1)
        self.assertEqual(sorted(re.findall(r'"([^"]+)":', classes)), pyodide,
                         "scripts/browser_bot.py should have a class for each Pyodide bot")
        names = re.search(r'_bot\.py" for name in \(([^)]*)\)', text("scripts/build_web_bots.py")).group(1)
        bundled = {f"bots/{name}_bot.py" for name in re.findall(r'"([^"]+)"', names)}
        for bid in pyodide:
            needs = {f for f in index[bid]["files"] + index[bid]["depends"] if f.startswith("bots/")}
            self.assertEqual(needs - bundled, set(), f"{bid}: scripts/build_web_bots.py should bundle these")
        worker = text("web/reference-worker.js")
        for bid, b in index.items():
            if b["play_page"] == "wasm":
                self.assertIn(f'"{bid}"', worker, f"{bid}: web/reference-worker.js should load it")

    def test_benchmark(self):
        index = bots()
        references = json.loads(text("bench/arena.json"))["references"]
        self.assertEqual(references, {bid: b["files"][0] for bid, b in index.items()
                                      if b["benchmark"] in ("kit", "held back")},
                         "bench/arena.json's references should be the bots whose benchmark is kit or held back")
        kit = {line.strip() for line in text("bench/starter.txt").splitlines() if line.startswith("bots/")}
        self.assertEqual(kit, {b["files"][0] for b in index.values() if b["benchmark"] == "kit"},
                         "bench/starter.txt should hold the bots whose benchmark is kit, and no others")

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs the git repository")
    def test_commits(self):
        for bid, b in bots().items():
            made = b["made"]
            if made["commit"] is None:
                continue
            added = git("log", "--diff-filter=A", "--format=%H", "--", *b["files"]).split()
            self.assertTrue(any(h.startswith(made["commit"]) for h in added),
                            f"{bid}: commit {made['commit']} didn't add any of its files; these did: {added}")
            if made["created"] is not None:
                day = git("log", "-1", "--date=short", "--format=%ad", made["commit"]).strip()
                self.assertLessEqual(made["created"], day, f"{bid}: created after the commit that added it")


if __name__ == "__main__":
    unittest.main()
