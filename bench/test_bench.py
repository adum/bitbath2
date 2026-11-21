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
from bench.harnesses import Harness, activity, error_message, failure_kind, usage_records
from bench.run import FIRST, FIX, TASK, bot_name, contents_hash, duration, history, idle
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

    def test_a_refused_account_is_a_configuration_error(self):
        inner = json.dumps({"type": "error", "status": 400, "error": {
            "message": "This organization cannot be used for Codex usage.", "code": "codex_byok_restricted_by_x"}})
        events = "".join(json.dumps(event) + "\n" for event in (
            {"type": "error", "message": inner}, {"type": "turn.failed", "error": {"message": inner}}))
        self.assertEqual(failure_kind(events, "", 0), "configuration")
        self.assertEqual(error_message(events), "This organization cannot be used for Codex usage.")
        self.assertEqual(error_message('{"type":"error","message":"plain"}\nnot json\n'), "plain")
        self.assertEqual(error_message('{"type":"result"}\n'), "")

    def test_activity_is_read_from_both_harnesses(self):
        claude = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Trying  a\nnew build order."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "bitbath play"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "bot.py"}}]}}]
        codex = [{"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
                 {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
                 {"type": "item.completed", "item": {"type": "file_change"}},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}}]
        lines = lambda events: "".join(json.dumps(event) + chr(10) for event in events) + "not json" + chr(10)
        self.assertEqual(activity(lines(claude)), {"commands": 1, "edits": 1, "command": "bitbath play",
                                                   "said": "Trying a new build order."})
        self.assertEqual(activity(lines(codex)), {"commands": 1, "edits": 1, "command": "ls", "said": "Done."})
        self.assertEqual(activity(""), {"commands": 0, "edits": 0, "command": "", "said": ""})

    def test_antigravity(self):
        harness = Harness("agy", "gemini-4-carbon-high", "high")
        self.assertEqual((harness.provider, harness.credential_names()), ("google", ["GEMINI_API_KEY"]))
        command, stdin = harness.command("build a bot")
        self.assertEqual(json.loads(stdin), {"event": "user", "message": {"content": "build a bot"}})
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(settings["modelProvider"], "gemini")
        self.assertEqual(command[-2:], ["--effort", "high"])
        step = lambda **fields: {"event": "step_update", "step_update": fields}
        events = [
            step(step_index=1, state="ACTIVE", step_type="agent_response", text_delta="Reading "),
            step(step_index=1, state="DONE", step_type="agent_response", text_delta="the kit."),
            step(step_index=2, state="ACTIVE", step_type="tool", tool_name="run_command",
                 tool_info={"parameters": {"CommandLine": "ls bots"}}),
            step(step_index=2, state="DONE", step_type="tool", tool_name="run_command",
                 tool_info={"parameters": {"CommandLine": "ls bots"}}),
            step(step_index=3, state="DONE", step_type="tool", tool_name="write_to_file"),
            {"event": "result", "result": {"status": "SUCCESS", "usage": {"total_tokens": 7}}},
        ]
        text = "".join(json.dumps(event) + chr(10) for event in events)
        self.assertEqual(activity(text), {"commands": 1, "edits": 1, "command": "ls bots", "said": "Reading the kit."})
        self.assertIsNone(failure_kind(text, "", 0))
        self.assertEqual(usage_records(text), [{"total_tokens": 7}])
        failed = json.dumps({"event": "result", "result": {"status": "ERROR", "error": "API key not valid."}})
        self.assertEqual(failure_kind(failed, "", 0), "configuration")
        self.assertEqual(error_message(failed), "API key not valid.")

    def test_antigravity_tool_policy(self):
        from bench.agy_policy import decision
        for tool in ("run_command", "read_file", "write_file", "edit_file"):
            self.assertEqual(decision({"toolCall": {"name": tool, "args": {}}})["decision"], "allow", tool)
        for tool in ("search_web", "read_url_content", "browser_subagent", "invoke_subagent", "call_mcp_tool", None):
            self.assertEqual(decision({"toolCall": {"name": tool, "args": {}}})["decision"], "deny", tool)
        self.assertEqual(decision({})["decision"], "deny")

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
        task = TASK.format(hours="4 hours", remaining="1 hour", deadline="12:00", session=2, check="bitbath check", cpus=16,
                           history=FIRST)
        fix = FIX.format(deadline="12:00", check="bitbath check", output="error: x")
        self.assertNotIn("{", task + fix)


class History(unittest.TestCase):
    def test_contents_hash_ignores_order(self):
        one = archive_of({"a.py": b"1", "b.py": b"2"})
        self.assertEqual(contents_hash(one), contents_hash(archive_of({"b.py": b"2", "a.py": b"1"})))
        self.assertNotEqual(contents_hash(one), contents_hash(archive_of({"a.py": b"1", "b.py": b"3"})))

    def test_a_new_session_hears_what_the_last_one_did(self):
        self.assertEqual(history([], "h0", ""), FIRST)
        idle = {"seconds": 95, "stop": None, "contents_sha256": "h0"}
        text = history([idle], "h0", "TABLE")
        self.assertIn("one session so far, which used 2 minutes.", text)
        self.assertIn("ended itself after 2 minutes", text)
        self.assertIn("exactly as it found it", text)
        self.assertTrue(text.endswith("TABLE"))
        busy = {"seconds": 3600, "stop": "timeout", "contents_sha256": "h1"}
        text = history([idle, busy], "h0", "TABLE")
        self.assertIn("2 sessions so far, which used 1 hour 2 minutes", text)
        self.assertNotIn("ended itself", text)
        self.assertNotIn("as it found it", text)

    def test_a_run_ends_when_sessions_stop_working(self):
        quick = {"seconds": 9, "stop": None, "contents_sha256": "h1"}
        work = {"seconds": 2876, "stop": None, "contents_sha256": "h1"}
        self.assertFalse(idle([work, quick, quick], "h0"))
        self.assertTrue(idle([work, quick, quick, quick], "h0"))
        self.assertTrue(idle([dict(quick, contents_sha256="h0")] * 3, "h0"))
        # A session that changed the bot, ran long, or was cut off isn't idle.
        self.assertFalse(idle([work, quick, quick, dict(quick, contents_sha256="h2")], "h0"))
        self.assertFalse(idle([work, quick, quick, dict(quick, seconds=600)], "h0"))
        self.assertFalse(idle([work, quick, quick, dict(quick, stop="timeout")], "h0"))


if __name__ == "__main__":
    unittest.main()
