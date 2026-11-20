"""The build phase: one model gets the starter kit in a container, time to
work, and nothing else. What it leaves in submission/ is checked, and then
sealed as an entry for the tournament."""

import datetime as dt
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from .harnesses import PROVIDERS, Harness, auth_secrets, failure_kind, usage_records
from .process import run_process
from .sandbox import CLAUDE_SECCOMP, ROOT, BenchmarkError, Docker, extract_archive

ENTRIES = ROOT / "entries"
# A session isn't started with less time than this left.
MIN_SESSION_SECONDS = 300
# Waits before retrying a session that failed for a passing reason.
RETRY_DELAYS = (15, 30, 60, 120)
CHECK = ["target/release/bitbath", "check", "submission", "--config", "configs/micro.toml"]

TASK = """You are in /workspace, which holds the Bitbath starter kit. Start by reading \
README.md: it describes your task, which is to build the strongest bot you can and \
leave it in submission/.

How the work is run:

- You have {hours} of working time in all. {remaining} of it is left, and this \
session is cut off at {deadline} UTC (`date -u` shows the time now).
- The work may be split over several sessions. This is session {session}. A new \
session starts with the files as you left them, but remembers nothing of the \
sessions before it. Keep notes for yourself in /workspace/NOTES.md, and read them \
first if they exist.
- Use the time. A first working bot is the start: play it against the sample bots \
and against earlier versions of itself, read the replays to see why games were won \
or lost, and improve it. Many small tested improvements beat one big untested idea.
- Nobody is there to answer questions, so don't ask any. Decide for yourself.
- There is no internet access. Work only inside /workspace.
- Keep submission/ working as you go, since the session can end at any moment. \
Before you stop, make sure this passes: `{check}`
- If you are sure that more time wouldn't make the bot stronger, create an empty \
file at /workspace/FINISHED and stop. Otherwise the next session continues the work.
"""

FIX = """You are in /workspace, which holds the Bitbath starter kit and the bot you \
built in submission/. Your working time is over, but the bot failed the check that \
admits it to the tournament. This session is only for making it pass. It is cut off \
at {deadline} UTC, there is no internet access, and nobody is there to answer questions.

The check was `{check}`, run in a fresh copy of the kit with only your submission/ \
directory added. Its output:

{output}

Fix what makes it fail, change as little else as you can, and run the check \
yourself before you stop. README.md describes what a submission must be.
"""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def duration(seconds):
    """Seconds as words: "3 hours 20 minutes"."""
    minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours} hour{'s' * (hours != 1)}"] if hours else []
    if minutes or not hours:
        parts.append(f"{minutes} minute{'s' * (minutes != 1)}")
    return " ".join(parts)


def clock_after(seconds):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%H:%M")


def bot_name(model):
    """A name for the tournament from a model id: the runner allows up to 32
    letters, digits, '-' and '_'."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", model.split("/")[-1]).strip("-")
    return name[:32] or "bot"


def session(docker, agent, harness, prompt, directory, *, timeout, secrets, version, label):
    """Runs one harness session in the build container. Returns what happened."""
    directory.mkdir(parents=True)
    (directory / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    command, stdin = harness.command(prompt, version=version)
    deadline = time.monotonic() + timeout
    outcome = {"attempts": 0, "seconds": 0, "usage": []}
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        left = deadline - time.monotonic()
        if left <= 0:
            break

        def heartbeat(elapsed, size, left=left):
            print(f"  [{label}] {duration(elapsed)} in, {size / 1e6:.1f} MB of output, "
                  f"{duration(left - elapsed)} left", flush=True)

        events, errors = directory / f"attempt-{attempt}.events.jsonl", directory / f"attempt-{attempt}.stderr.txt"
        done = run_process(
            ["docker", "exec", "-i", "--workdir", "/workspace", agent, *command],
            stdout=events, stderr=errors, input_text=stdin, timeout=left, secrets=secrets,
            progress=heartbeat, progress_interval=300,
        )
        stdout = events.read_text(encoding="utf-8", errors="replace")
        failure = None if done.stop else failure_kind(
            stdout, errors.read_text(encoding="utf-8", errors="replace"), done.returncode)
        outcome["attempts"] = attempt
        outcome["seconds"] += round(done.elapsed)
        outcome["usage"] += usage_records(stdout)
        outcome["stop"] = done.stop
        outcome["failure"] = failure
        if failure != "transient" or delay is None:
            break
        print(f"  [{label}] the provider or the connection failed; trying again in {delay} seconds", flush=True)
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
    return outcome


def check(docker, archive, directory):
    """Checks a submission in a fresh container with no network: the kit as
    the image has it, and only the submission added. Returns whether it
    passed, and the check's output."""
    container = docker.offline("check-" + uuid.uuid4().hex[:6])
    try:
        docker.put(container, archive, "/workspace/submission")
        out, err = directory / "check.stdout.txt", directory / "check.stderr.txt"
        done = run_process(["docker", "exec", "--workdir", "/workspace", container, *CHECK],
                           stdout=out, stderr=err, timeout=1800)
        text = out.read_text(errors="replace") + err.read_text(errors="replace")
        if done.stop:
            text += f"\n[the check was cut off: {done.stop}]\n"
        return done.returncode == 0 and not done.stop, text[-6000:]
    finally:
        docker.remove(container)


def run(args):
    """Runs the build phase for one model. Returns the entry's directory."""
    harness = Harness(args.harness, args.model, args.effort)
    credentials = harness.credentials()
    auth_file = Path(args.auth_file).expanduser() if args.auth_file else Path.home() / ".codex" / "auth.json"
    if harness.name == "codex" and not credentials and not auth_file.is_file():
        raise BenchmarkError("Codex needs OPENAI_API_KEY, or --auth-file naming a Codex auth.json")
    if harness.name != "codex" and not credentials:
        raise BenchmarkError(f"set one of these first: {', '.join(PROVIDERS[harness.provider][1])}")
    secrets = tuple(credentials.values())
    if harness.name == "codex" and not credentials:
        secrets += auth_secrets(auth_file)

    docker = Docker(args.image, cpus=args.cpus, memory=args.memory)
    image = docker.preflight()
    name = bot_name(args.name or args.model)
    entry_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-") + name
    entry = ENTRIES / entry_id
    entry.mkdir(parents=True)
    budget = args.hours * 3600
    record = {
        "id": entry_id, "name": name, "harness": harness.name, "model": harness.model, "effort": harness.effort,
        "image": image, "started": now(), "sealed": False, "rounds": [],
        "conditions": {"hours": args.hours, "max_sessions": args.max_sessions, "fix_minutes": args.fix_minutes,
                       "cpus": args.cpus, "memory": args.memory, "network": "provider hosts only",
                       "hosts": harness.hosts},
        "credential_names": sorted(credentials),
    }
    write_json(entry / "entry.json", record)
    print(f"Entry {entry_id}: {harness.name} / {harness.model}, {duration(budget)}, kit {image['kit_commit'][:12]}")

    archive = None
    try:
        with docker:
            agent = docker.agent(hosts=harness.hosts, environment=harness.environment(),
                                 seccomp=CLAUDE_SECCOMP if harness.name == "claude" else None)
            harness.preflight(docker, agent)
            harness.authenticate(docker, agent, auth_file)
            version = docker.run(agent, harness.name, "--version")
            record["harness_version"] = version
            deadline = time.monotonic() + budget
            stop = "max_sessions"

            def work(number, prompt, timeout, label):
                """One session, then a copy of submission/ as it left it."""
                outcome = session(docker, agent, harness, prompt, entry / "rounds" / f"{number:02d}",
                                  timeout=timeout, secrets=secrets, version=version, label=label)
                # This also stops whatever the session left running.
                data = docker.get(agent, "/workspace/submission")
                (entry / "rounds" / f"{number:02d}" / "submission.tar").write_bytes(data)
                outcome.update(number=number, submission_sha256=hashlib.sha256(data).hexdigest())
                record["rounds"].append(outcome)
                write_json(entry / "entry.json", record)
                if outcome["failure"] == "configuration":
                    raise BenchmarkError(f"the harness rejected the model or the credential: see {entry / 'rounds'}")
                return data, outcome

            for number in range(1, args.max_sessions + 1):
                left = deadline - time.monotonic()
                if left < MIN_SESSION_SECONDS:
                    stop = "time"
                    break
                prompt = TASK.format(hours=duration(budget), remaining=duration(left), deadline=clock_after(left),
                                     session=number, check=" ".join(CHECK))
                print(f"Session {number}: {duration(left)} left", flush=True)
                archive, outcome = work(number, prompt, left, f"session {number}")
                if finished(docker, agent):
                    stop = "finished"
                    break
                if outcome["failure"] == "harness" and outcome["seconds"] < 60:
                    stop = "harness_error"
                    break
            record["stop"] = stop
            if archive is None:
                raise BenchmarkError("no session ran, so there is no submission")

            passed, output = check(docker, archive, entry)
            print(f"Check: {'passed' if passed else 'failed'}", flush=True)
            if not passed and args.fix_minutes > 0:
                # One more session, with the check's output and nothing else new.
                record["fix_session"] = True
                seconds = args.fix_minutes * 60
                prompt = FIX.format(deadline=clock_after(seconds), check=" ".join(CHECK), output=output)
                archive, _ = work(len(record["rounds"]) + 1, prompt, seconds, "fix session")
                passed, output = check(docker, archive, entry)
                print(f"Check after the fix session: {'passed' if passed else 'failed'}", flush=True)
            record["check"] = {"passed": passed, "output": output}

        (entry / "submission.tar").write_bytes(archive)
        extract_archive(archive, entry / "submission")
        record.update(sealed=passed, submission_sha256=hashlib.sha256(archive).hexdigest())
    except BaseException as e:
        record["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        record["finished"] = now()
        write_json(entry / "entry.json", record)
    if record["sealed"]:
        print(f"Sealed: {entry}")
    else:
        print(f"Not sealed, because the check failed: {entry}")
    return entry


def finished(docker, agent):
    """Whether the model left the file that says it is done."""
    try:
        docker.run(agent, "test", "-e", "/workspace/FINISHED")
        return True
    except BenchmarkError:
        return False
