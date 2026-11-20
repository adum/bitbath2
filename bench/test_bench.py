"""Tests for the parts of the benchmark tools that need no Docker.

    python -m unittest bench.test_bench
"""

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from bench import kit
from bench.harnesses import Harness, failure_kind, usage_records
from bench.run import FIX, TASK, bot_name, duration
from bench.sandbox import BenchmarkError, archive_of, check_archive, extract_archive


def tar_with(member, content=b""):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


class Kit(unittest.TestCase):
    def test_allowlist(self):
        include, copies = kit.read_allowlist("# note\nengine\nkits/python  # the kit\n\nsub/b.py = kits/python/b.py\n")
        self.assertEqual(include, ["engine", "kits/python"])
        self.assertEqual(copies, {"sub/b.py": "kits/python/b.py"})
        with self.assertRaises(SystemExit):
            kit.read_allowlist("engine\nsub/b.py = bots/strong_bot.py\n")

    def test_the_kit_holds_the_sample_bots_only(self):
        include, _ = kit.read_allowlist((kit.ROOT / kit.ALLOWLIST).read_text())
        bots = sorted(Path(path).name for path in include if path.startswith("bots"))
        samples = ["greedy", "raider", "random", "rush", "turtle", "volley"]
        self.assertEqual(bots, [f"{name}_bot.py" for name in samples])
        # Every held-back bot is still a tournament opponent.
        references = json.loads((kit.ROOT / "bench" / "arena.json").read_text())["references"]
        held_back = sorted(set(references) - set(samples))
        self.assertEqual(held_back, ["bastion", "smart", "solid", "vanguard"])


class Archives(unittest.TestCase):
    def test_round_trip(self):
        data = archive_of({"bot.toml": b'run = "python bot.py"\n', "src/bot.py": b"pass\n"})
        check_archive(data)
        with tempfile.TemporaryDirectory() as directory:
            extract_archive(data, directory)
            self.assertEqual((Path(directory) / "src" / "bot.py").read_bytes(), b"pass\n")

    def test_unsafe_archives_are_refused(self):
        link = tarfile.TarInfo("notes")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        for member in (tarfile.TarInfo("../outside"), tarfile.TarInfo("/absolute"), link):
            with self.assertRaises(BenchmarkError, msg=member.name):
                check_archive(tar_with(member))
        with self.assertRaises(BenchmarkError):
            check_archive(tar_with(tarfile.TarInfo("big"), b"x" * 2000), limit=1000)


class Harnesses(unittest.TestCase):
    def test_providers(self):
        self.assertEqual(Harness("claude", "m").hosts, ["api.anthropic.com"])
        self.assertEqual(Harness("opencode", "xai/grok").provider, "xai")
        for model in ("grok", "nobody/model"):
            with self.assertRaises(BenchmarkError):
                Harness("opencode", model).provider

    def test_web_tools_are_off(self):
        claude, _ = Harness("claude", "m").command("p")
        self.assertIn("WebFetch,WebSearch,mcp__*", claude)
        codex, stdin = Harness("codex", "m", "high").command("p")
        self.assertIn('web_search="disabled"', codex)
        self.assertEqual(stdin, "p")
        opencode, stdin = Harness("opencode", "xai/grok").command("p")
        self.assertEqual((opencode[-1], stdin), ("p", None))

    def test_failures(self):
        self.assertIsNone(failure_kind('{"type":"result","subtype":"success"}\n', "", 0))
        self.assertEqual(failure_kind("", "Error: 503 overloaded", 1), "transient")
        self.assertEqual(failure_kind('{"type":"error","message":"model not found"}\n', "", 0), "configuration")
        self.assertEqual(failure_kind("", "segfault", 1), "harness")
        self.assertIsNone(failure_kind('{"type":"result","subtype":"error_max_turns"}\n', "", 1))
        events = json.dumps({"usage": {"a": 1}}) + "\nnot json\n" + json.dumps({"message": {"usage": {"b": 2}}})
        self.assertEqual(usage_records(events), [{"a": 1}, {"b": 2}])


class Run(unittest.TestCase):
    def test_names_and_times(self):
        self.assertEqual(bot_name("anthropic/claude-opus-5.5"), "claude-opus-5-5")
        self.assertEqual(len(bot_name("x" * 80)), 32)
        self.assertEqual(duration(4 * 3600), "4 hours")
        self.assertEqual(duration(3660), "1 hour 1 minute")
        self.assertEqual(duration(20), "0 minutes")

    def test_prompts_fill_in(self):
        task = TASK.format(hours="4 hours", remaining="1 hour", deadline="12:00", session=2, check="bitbath check")
        fix = FIX.format(deadline="12:00", check="bitbath check", output="error: x")
        self.assertNotIn("{", task + fix)


if __name__ == "__main__":
    unittest.main()
