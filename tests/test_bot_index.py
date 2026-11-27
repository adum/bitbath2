"""Checks bots/index.json, the bot index (README: The bot index), against the
bots' files and imports, what the Play page needs, and the benchmark's tracks,
and checks bots/ratings.json (README: Ratings) against the index.

Usage: python tests/test_bot_index.py
"""

import ast
import contextlib
import importlib
import io
import json
import re
import shutil
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "bots" / "index.json"
RATINGS = ROOT / "bots" / "ratings.json"
# The web build's scripts and the ratings', the bots and kit they use, and the benchmark.
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "bots"), str(ROOT / "kits" / "python"), str(ROOT)]
import browser_bot  # noqa: E402
import build_web_bots  # noqa: E402
import ratings  # noqa: E402
from bench import tracks  # noqa: E402

FIELDS = {"name", "about", "kind", "made", "language", "files", "depends", "run", "build", "rules", "maps",
          "play_page", "benchmark"}
MADE = {"by", "how", "created", "commit"}
KINDS = {"reference", "sample", "entry"}
LANGUAGES = {"python", "rust", "c++"}
RULES = {"classic", "strategic"}
PLAY_PAGE = {None, "js", "pyodide", "wasm"}
# A bot's part in a benchmark track.
PARTS = {"kit", "held back", "entry"}
LADDER = {"about", "rules", "map", "config", "from", "date", "commit", "seeds", "games", "anchor", "field", "ratings"}
RATING = set(ratings.STATS) | {"source"}


def bots():
    return json.loads(INDEX.read_text(encoding="utf-8"))["bots"]


def text(path):
    return (ROOT / path).read_text(encoding="utf-8")


def map_names():
    """The maps a record can name: the presets in configs/, and the Play page's maps."""
    names = {p.stem for p in (ROOT / "configs").glob("*.toml")}
    return names | set(re.findall(r'\bid: "([^"]+)", name: "', text("web/play.js")))


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
                parts = b["benchmark"]
                self.assertIsInstance(parts, dict, "benchmark: its part in each track that uses it")
                self.assertTrue(set(parts) <= set(tracks.names()), f"benchmark: the tracks are {tracks.names()}")
                self.assertTrue(set(parts.values()) <= PARTS, f"benchmark: a part is one of {sorted(PARTS)}")
                self.assertEqual(b["kind"] == "entry", bool(parts) and set(parts.values()) == {"entry"},
                                 "a benchmark entry is kind entry, and the other way round")

    def test_files_exist(self):
        for bid, b in bots().items():
            for f in b["files"] + b["depends"]:
                self.assertTrue((ROOT / f).is_file(), f"{bid}: there's no {f}")

    def test_every_bot_file_is_indexed(self):
        listed = {f for b in bots().values() for f in b["files"]}
        for f in sorted((ROOT / "bots").rglob("*")):
            path = f.relative_to(ROOT).as_posix()
            if f.is_file() and "__pycache__" not in f.parts and f not in (INDEX, RATINGS):
                self.assertTrue(path in listed, f"{path} isn't in bots/index.json: give it a record, or add it to one")

    def test_python_depends(self):
        for bid, b in bots().items():
            if b["language"] == "python":
                self.assertEqual(sorted(b["depends"]), sorted(imported(b["files"])),
                                 f"{bid}: depends should list every repository file it imports, directly or not")

    def test_play_page(self):
        index = bots()
        ladders = json.loads(RATINGS.read_text(encoding="utf-8"))["ladders"]
        # The web build writes the page's bots and ratings into web/bot-index.js.
        page = json.loads(build_web_bots.page_list(index, ladders).split(" = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual([b["id"] for b in page["bots"]], [bid for bid, b in index.items() if b["play_page"]],
                         "web/bot-index.js should list the bots whose play_page isn't null, in the index's order")
        self.assertEqual(list(page["ladders"]), list(ladders), "web/bot-index.js should have every ladder")
        # The dialog names a ladder by its map, and picks it by the map's rules and size.
        maps = re.findall(r'\bid: "([^"]+)", name: "', text("web/play.js"))
        for name, ladder in ladders.items():
            self.assertIn(ladder["map"], maps, f"the Play page should have {name}'s map")
        # web/bots.js plays its own ports, and runs the others in a worker.
        own = re.search(r"const own = \{([^}]*)\}", text("web/bots.js")).group(1)
        self.assertEqual(sorted(re.findall(r'^\s*"?([\w-]+)"?\s*:', own, re.M)),
                         sorted(bid for bid, b in index.items() if b["play_page"] == "js"),
                         "web/bots.js should have a port for each bot whose play_page is js, and no others")
        build_web_bots.bundled(index)  # Raises if two files of the bundle share a name.
        worker = text("web/reference-worker.js")
        for bid, b in index.items():
            with self.subTest(bot=bid):
                if b["play_page"] == "pyodide":
                    # scripts/browser_bot.py runs the bot its first file defines.
                    self.assertEqual(b["language"], "python", "Pyodide runs Python")
                    browser_bot.bot_class(importlib.import_module(Path(b["files"][0]).stem))
                if b["play_page"] == "wasm":
                    self.assertTrue(f'"{bid}"' in worker, "web/reference-worker.js should load it")

    def test_benchmark(self):
        index = bots()
        for name in tracks.names():
            with self.subTest(track=name):
                track = tracks.load(name)
                part = {bid: b["benchmark"].get(name) for bid, b in index.items()}
                references = {bid: tracks.reference(spec) for bid, spec in track["references"].items()}
                self.assertEqual(set(references), {bid for bid in index if part[bid] in ("kit", "held back")},
                                 "track.json's references should be the bots whose part is kit or held back")
                for bid, (files, run, build) in references.items():
                    b = index[bid]
                    self.assertEqual(files[0], b["files"][0], f"{bid}: its own file comes first")
                    self.assertEqual(sorted(files), sorted(set(b["files"] + b["depends"]) - {track["python_kit"]}),
                                     f"{bid}: track.json should pack its files and everything they import")
                    self.assertEqual(build is None, b["build"] is None, f"{bid}: built, or not")
                    if b["language"] == "python":
                        self.assertEqual(run, "python " + Path(files[0]).name)
                in_kit = {line.strip() for line in text(track["allowlist"]).splitlines() if line.startswith("bots/")}
                self.assertEqual(in_kit, {f for bid, (files, _, _) in references.items() if part[bid] == "kit"
                                          for f in files},
                                 "kit.txt should hold the bots whose part is kit with what they import, and no others")
                self.assertEqual(set(track["practice"]["bots"]), {bid for bid in index if part[bid] == "kit"},
                                 "the practice games should be against the bots whose part is kit")

    def test_ratings(self):
        # Whether a rating is current isn't checked: a bot may change before
        # its ladders are played again. python scripts/ratings.py says which
        # ratings are stale.
        index, maps = bots(), map_names()
        for name, ladder in json.loads(RATINGS.read_text(encoding="utf-8"))["ladders"].items():
            with self.subTest(ladder=name):
                self.assertIn(name, ratings.LADDERS, "scripts/ratings.py should know the ladder")
                self.assertEqual(set(ladder), LADDER)
                self.assertIn(ladder["rules"], RULES)
                self.assertIn(ladder["map"], maps)
                self.assertTrue(ladder["config"] and all(re.fullmatch(r"[0-9a-f]{64}", h)
                                                         for h in ladder["config"].values()), "its config's hash")
                date.fromisoformat(ladder["date"])
                for bid, r in ladder["ratings"].items():
                    self.assertIn(bid, index, f"{bid} is rated, but isn't in the index")
                    self.assertTrue(ratings.plays(index[bid], ladder), f"{bid} doesn't play {name}'s rules and map")
                    self.assertIn(bid, ladder["field"])
                    self.assertEqual(set(r), RATING, f"{bid}'s rating")
                    self.assertEqual(r["wins"] + r["draws"] + r["losses"], r["games"], f"{bid}'s record")
                    self.assertTrue(r["source"] and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in r["source"].values()),
                                    f"{bid}: the hashes of the files it was rated with")
        with contextlib.redirect_stdout(io.StringIO()):
            ratings.report(index, ratings.load())

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
