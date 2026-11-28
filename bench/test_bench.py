"""Tests for the parts of the benchmark tools that need no Docker.

    python -m unittest bench.test_bench
"""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench import arena, kit, tracks
from bench.arena import chosen_names, reference_files, sealed_entries
from bench.harnesses import Harness, activity, error_message, failure_kind, usage_records
from bench.run import (FIRST, FIX, STANDING, bot_name, check_command, contents_hash, duration, history, idle,
                       practice_command, sitting_note, sitting_seconds, status, summary, task)
from bench.sandbox import BenchmarkError, archive_of, check_archive, extract_archive

# Each track's practice bots, which are in its kit, and the reference bots it holds back.
TRACKS = {
    "micro": (["greedy", "raider", "random", "rush", "turtle", "volley"], ["bastion", "smart", "solid", "vanguard"]),
    "islets": (["forager", "naval", "supply"], ["armada", "breakwater", "brigantine", "trident"]),
}


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

    def test_a_kit_holds_its_practice_bots_and_no_held_back_bot(self):
        self.assertEqual(tracks.names(), sorted(TRACKS))
        for name, (practice, held_back) in TRACKS.items():
            with self.subTest(track=name):
                track = tracks.load(name)
                include, copies = kit.read_allowlist((kit.ROOT / track["allowlist"]).read_text())
                in_kit = {path for path in include if path.startswith("bots/")}
                files = {bot: tracks.reference(spec)[0] for bot, spec in track["references"].items()}
                self.assertEqual(sorted(track["practice"]["bots"]), practice)
                # The kit's bots are the practice bots, with the files they import.
                self.assertEqual(in_kit, {path for bot in practice for path in files[bot]})
                # Every other reference bot is a tournament opponent whose own file stays out.
                self.assertEqual(sorted(set(files) - set(practice)), held_back)
                for bot in held_back:
                    self.assertNotIn(files[bot][0], in_kit, bot)
                # The bots that set the ratings' scale play.
                self.assertIn(track["anchor"].split("=")[0], track["references"])
                self.assertIn(track["practice"]["anchor"].split("=")[0], track["practice"]["bots"])
                # Every game is played with a preset the kit has.
                for config in {track["check_config"], track["practice"]["config"]} | {m["config"] for m in track["maps"]}:
                    self.assertIn(config, include)
                self.assertTrue((kit.ROOT / track["overlay"] / "README.md").is_file())
                self.assertTrue((kit.ROOT / track["overlay"] / "submission" / "bot.toml").is_file())
                self.assertEqual(copies, {"submission/bitbath.py": track["python_kit"]})

    def test_reference_bots_are_packed_with_what_they_import(self):
        read = lambda path: path.encode()
        islets = tracks.load("islets")["references"]
        naval = reference_files(islets["naval"], read, "kits/python/bitbath.py")
        self.assertEqual(sorted(naval), ["bitbath.py", "bot.toml", "naval_bot.py", "supply_bot.py", "vanguard_bot.py"])
        self.assertEqual(naval["bot.toml"], b'run = "python naval_bot.py"\n')
        self.assertEqual(naval["supply_bot.py"], b"bots/supply_bot.py")
        # A bot in another language is built in its directory, and gets no Python kit.
        forager = reference_files(islets["forager"], read, "kits/python/bitbath.py")
        self.assertEqual(sorted(forager), ["bot.toml", "forager_bot.rs"])
        self.assertEqual(forager["bot.toml"],
                         b'run = "forager_bot"\nbuild = "rustc -O forager_bot.rs -o forager_bot"\n')
        # A Python bot in one file is named by its path alone.
        self.assertEqual(reference_files("bots/greedy_bot.py", read, "kits/python/bitbath.py"),
                         {"greedy_bot.py": b"bots/greedy_bot.py", "bitbath.py": b"kits/python/bitbath.py",
                          "bot.toml": b'run = "python greedy_bot.py"\n'})
        with self.assertRaises(BenchmarkError):
            reference_files({"files": ["bots/a.py", "old/a.py"], "run": "python a.py"}, read, "kit.py")


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


class Arena(unittest.TestCase):
    def test_two_entries_of_one_name_play_under_two(self):
        image = {"track": "islets", "kit_sha256": "k"}
        season = {"bots": {"naval": {"kind": "reference", "source": "bots/naval_bot.py"}}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(arena, "ENTRIES", Path(directory)):
            for entry in ("e1", "e2"):
                archive = archive_of({"bot.toml": f'run = "{entry}"\n'.encode()})
                record = {"id": entry, "name": "model", "track": "islets", "sealed": True, "image": image,
                          "submission_sha256": hashlib.sha256(archive).hexdigest()}
                (Path(directory) / entry).mkdir()
                (Path(directory) / entry / "submission.tar").write_bytes(archive)
                (Path(directory) / entry / "entry.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "--name ID=NAME"):
                sealed_entries([], image, False)
            bots = sealed_entries([], image, False, chosen_names(["e2=model-2"], season))
            self.assertEqual({name: bot["source"] for name, bot in bots.items()}, {"model": "e1", "model-2": "e2"})
            # The season knows its entries by their ids, so a name is given once.
            season["bots"].update({name: {"kind": "entry", "source": bot["source"]} for name, bot in bots.items()})
            self.assertEqual(chosen_names([], season), {"e1": "model", "e2": "model-2"})
            self.assertEqual(sorted(sealed_entries([], image, False, chosen_names([], season))), ["model", "model-2"])
        # A name can't change once a season has games under it, and it has to be one the runner takes.
        self.assertEqual(chosen_names(["e2=model-2"], season)["e2"], "model-2")
        with self.assertRaisesRegex(BenchmarkError, "as 'model-2'"):
            chosen_names(["e2=other"], season)
        for option in ("e3", "e3=", "=x", "e3=a b", "e3=" + "x" * 33):
            with self.assertRaises(BenchmarkError, msg=option):
                chosen_names([option], season)


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
        fields = dict(hours="4 hours", remaining="1 hour", deadline="12:00", session=2, cpus=16, history=FIRST)
        for name in TRACKS:
            with self.subTest(track=name):
                track = tracks.load(name)
                prompt = task(track, sitting="", **fields)
                fix = FIX.format(deadline="12:00", check=" ".join(check_command(track)), output="error: x")
                table = STANDING.format(table="  #  bot", practice=track["prompt"]["practice"],
                                        caveat=track["prompt"]["caveat"])
                self.assertNotIn("{", prompt + fix + table)
                self.assertNotIn("sitting", prompt)
                # The opposition is one list with the line before it, and the check is the track's.
                self.assertIn("only result of this work.\n- The kit's bots/ directory holds", prompt)
                self.assertIn(f"`target/release/bitbath check submission --config {track['check_config']}`", prompt)
                self.assertIn(f"on a map like those of {track['check_config']}, on a seed", prompt)
        micro, islets = (task(tracks.load(name), sitting="", **fields) for name in ("micro", "islets"))
        self.assertIn("greedy is the best of them", micro)
        self.assertIn("Each of them won all 40 of its games against naval", islets)
        for word in ("greedy", "micro"):
            self.assertNotIn(word, islets)
        # Work that is spread over sittings is told so, as one more line of the list.
        split = task(tracks.load("micro"), sitting=sitting_note(3, 7200), **fields)
        self.assertIn("they exist.\n- The working time is spread over sittings", split)
        self.assertIn("This is sitting 3, and 2 hours of it is left.", split)
        self.assertIn("lost between sittings, and so is anything still running.\n- This container has", split)
        self.assertIn("lasts until the working time is used up", sitting_note(2, None))

    def test_practice_games_are_the_tracks(self):
        command = practice_command(tracks.load("islets"), 16)
        self.assertEqual(command[:4], ["target/release/bitbath", "tournament", "--bot", "mine=submission"])
        for part in ("naval=python bots/naval_bot.py", "forager=target/release/forager_bot", "configs/islets.toml",
                     "supply=1000"):
            self.assertIn(part, command)
        self.assertIn("greedy=python bots/greedy_bot.py", practice_command(tracks.load("micro"), 4))

    def test_entries_are_listed_with_their_status(self):
        self.assertEqual(status({"sealed": True}), "sealed")
        self.assertEqual(status({"sealed": False, "check": {"passed": False}}), "check failed")
        self.assertEqual(status({"sealed": False, "error": "x"}), "failed")
        paused = {"id": "e1", "track": "islets", "harness": "codex", "state": "paused", "sealed": False,
                  "conditions": {"hours": 20}, "started": "2026-11-22T03:09:08+00:00",
                  "rounds": [{"seconds": 7000}, {"seconds": 200}, {"seconds": 900, "fix": True}],
                  "sittings": [{"ended": "2026-11-22T05:10:00+00:00"}, {"ended": "2026-11-23T09:30:00+00:00"}]}
        self.assertEqual(summary(paused), ["e1", "islets", "codex", "paused", "2:00", "20:00", "18:00", "3", "2",
                                           "2026-11-23 09:30"])
        # An entry from before there were tracks is the first track's.
        old = {"id": "e0", "harness": "claude", "sealed": True, "conditions": {"hours": 1},
               "rounds": [{"seconds": 3600}], "finished": "2026-11-21T02:45:00+00:00"}
        self.assertEqual(summary(old), ["e0", "micro", "claude", "sealed", "1:00", "1:00", "-", "1", "1",
                                        "2026-11-21 02:45"])

    def test_a_sitting_is_long_enough_for_a_session(self):
        self.assertIsNone(sitting_seconds(None))
        self.assertEqual(sitting_seconds(2), 7200)
        with self.assertRaises(BenchmarkError):
            sitting_seconds(0.01)


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
