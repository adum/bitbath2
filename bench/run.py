"""The build phase: one model gets the starter kit in a container, time to
work, and nothing else. What it leaves in submission/ is checked, and then
sealed as an entry for the tournament. The time can be used in several
sittings: between them the entry is paused, and its workspace waits in a
Docker volume."""

import datetime as dt
import hashlib
import io
import json
import re
import tarfile
import time
import uuid
from pathlib import Path

from .harnesses import PROVIDERS, Harness, activity, auth_secrets, error_message, failure_kind, usage_records
from .process import run_process
from .sandbox import CLAUDE_SECCOMP, ROOT, BenchmarkError, Docker, extract_archive

ENTRIES = ROOT / "entries"
# A session isn't started with less time than this left.
MIN_SESSION_SECONDS = 300
# A run ends once this many sessions in a row have each ended themselves
# within IDLE_SECONDS and left submission/ as it was: the model has stopped
# working, and more sessions would only repeat that.
IDLE_SESSIONS = 3
IDLE_SECONDS = 120
# Waits before retrying a session that failed for a passing reason.
RETRY_DELAYS = (15, 30, 60, 120)
CHECK = ["target/release/bitbath", "check", "submission", "--config", "configs/micro.toml"]

TASK = """This is a competition. You are in /workspace, which holds the Bitbath starter \
kit, and your task is to build the strongest bot you can and leave it in submission/. \
Start by reading README.md, which describes the game and the task.

What you are up against:

- When your time is over, your bot plays a tournament against bots that other AI \
models built from this same kit, and against reference bots. Your bot's rating in \
that tournament is the only result of this work.
- The kit's bots/ directory holds six of the reference bots. They are simple \
samples, and greedy is the best of them.
- Four more reference bots are held back, so you can't see them. On the scale \
where greedy is rated 1000 they are rated from about 1400 to 1650. The weakest of \
them wins about nine games in ten against greedy, and the strongest nearly every \
game. None of them is close to perfect play: they are hand-written heuristics, \
and a better bot can beat them all.
- So a bot that only edges out greedy finishes in the bottom half. Winning nine \
games in ten against greedy is the first real milestone, and doing well means \
going well beyond that. A copy of a sample bot, or a sample bot with its numbers \
tuned, scores about what that bot scores: getting further takes a design of your own.

How the work is run:

- You have {hours} of working time in all. {remaining} of it is left, and this \
session is cut off at {deadline} UTC (`date -u` shows the time now).
- The time is there to be used, and ending a session early saves none of it. If \
you stop while time is left, a new session simply starts and has to find its \
bearings again. Keep working in this one for as long as you can.
- The work may still be split over several sessions. This is session {session}. A \
new session starts with the files as you left them, but remembers nothing of the \
sessions before it. Keep notes for yourself in /workspace/NOTES.md as you go, on \
what you tried, what the results were, and what to do next. Read them first if \
they exist.
{sitting}- This container has {cpus} CPUs, whatever `nproc` says: it reports the host's.
- Every tournament game is on a map like those of configs/micro.toml, on a seed \
you haven't seen.
- Nobody is there to answer questions, so don't ask any. Decide for yourself.
- There is no internet access. Work only inside /workspace.
- Keep submission/ working as you go, since the session can end at any moment. \
Whenever you change it, make sure this still passes: `{check}`
- Creating an empty file at /workspace/FINISHED ends the work for good and gives \
up the time that is left.

{history}"""

FIRST = "Where things stand: this is the first session, and submission/ holds the kit's placeholder bot.\n"

STANDING = """How submission/ stands now. After the last session the benchmark played it, as \
"mine", against the kit's six bots on micro maps, with greedy's rating fixed at 1000:

{table}

Read this table for what it is. These six are the weakest bots in the real \
tournament, and the four reference bots you can't see are rated about 1400 to \
1650 on this scale. Coming first here is not a good result yet: a strong bot wins \
nearly every game in this table. It is also only four seeds, so \
it is a rough guide and not a measurement.
"""

NO_STANDING = """The benchmark tried to play submission/ against the kit's bots after the last \
session, and couldn't:

{error}
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


def progress_report(label, elapsed, left, events):
    """The lines shown every few minutes while a session runs: the time, what
    the model has done so far, and what it is doing and last said."""
    try:
        seen = activity(events.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        seen = activity("")

    def cut(text, width=150):
        return text if len(text) <= width else text[:width - 3] + "..."

    lines = [f"  [{label}] {duration(elapsed)} in, {duration(left)} left: "
             f"{seen['commands']} command{'s' * (seen['commands'] != 1)}, "
             f"{seen['edits']} file edit{'s' * (seen['edits'] != 1)}"]
    if seen["command"]:
        lines.append(f"      running: {cut(seen['command'])}")
    if seen["said"]:
        lines.append(f"      said: {cut(seen['said'], 300)}")
    return "\n".join(lines)


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

        events, errors = directory / f"attempt-{attempt}.events.jsonl", directory / f"attempt-{attempt}.stderr.txt"

        def heartbeat(elapsed, size, left=left, events=events):
            print(progress_report(label, elapsed, left - elapsed, events), flush=True)

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
        if failure:
            outcome["error"] = error_message(stdout)
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


SAMPLES = ("random", "greedy", "turtle", "volley", "raider", "rush")


def standing(docker, archive, jobs):
    """Plays a submission against the kit's bots, in a fresh container
    with no network, and returns the ratings table. The model could run the
    same tournament itself: it learns nothing here that the kit doesn't hold."""
    container = docker.offline("standing-" + uuid.uuid4().hex[:6])
    try:
        docker.put(container, archive, "/workspace/submission")
        command = ["target/release/bitbath", "tournament", "--bot", "mine=submission"]
        for name in SAMPLES:
            command += ["--bot", f"{name}=python bots/{name}_bot.py"]
        command += ["--config", "configs/micro.toml", "--seeds", "1-4", "--jobs", str(jobs),
                    "--anchor", "greedy=1000", "--bootstrap", "0", "--out", "/tmp/standing"]
        output = docker.run(container, *command, workdir="/workspace", timeout=900)
        table = output[output.rindex("  #  bot"):].split("\n\n")[0]
        return STANDING.format(table=table.rstrip())
    except (BenchmarkError, ValueError) as e:
        return NO_STANDING.format(error=str(e)[-1500:])
    finally:
        docker.remove(container)


def idle(rounds, before):
    """Whether the last sessions did nothing: each ended itself quickly and
    left submission/ as it found it. `before` is its hash before session 1."""
    if len(rounds) < IDLE_SESSIONS:
        return False
    hashes = [before] + [r["contents_sha256"] for r in rounds]
    return all(not r["stop"] and r["seconds"] < IDLE_SECONDS and hashes[i] == hashes[i + 1]
               for i, r in list(enumerate(rounds))[-IDLE_SESSIONS:])


def contents_hash(archive):
    """A hash of the files in a tar archive, whatever their dates and order."""
    files = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isfile():
                files.append(f"{hashlib.sha256(tar.extractfile(member).read()).hexdigest()}  {member.name}\n")
    return hashlib.sha256("".join(sorted(files)).encode()).hexdigest()


def history(rounds, before, standing_text):
    """What a new session is told about the sessions before it."""
    if not rounds:
        return FIRST
    used = sum(r["seconds"] for r in rounds)
    last = rounds[-1]
    if len(rounds) > 1:
        text = f"Where things stand: {len(rounds)} sessions so far, which used {duration(used)} between them."
    else:
        text = f"Where things stand: one session so far, which used {duration(used)}."
    if not last["stop"]:
        text += f" The last one ended itself after {duration(last['seconds'])}, with time left."
    previous = rounds[-2]["contents_sha256"] if len(rounds) > 1 else before
    if last["contents_sha256"] == previous:
        text += " It left submission/ exactly as it found it."
    return f"{text}\n\n{standing_text}"


def sitting_note(number, left):
    """The prompt's line about sittings, for work that is done in more than
    one. `left` is the time left in this one, if it ends before the work does."""
    lasts = f"{duration(left)} of it is left" if left else "it lasts until the working time is used up"
    return (f"- The working time is spread over sittings, which can be days apart. This is sitting {number}, "
            f"and {lasts}. When a sitting ends the work is paused, and it goes on later in a new container "
            "that has /workspace as you left it. Everything outside /workspace is lost between sittings, and "
            "so is anything still running.\n")


def credentials_for(harness, auth_file):
    """The harness's credentials from the environment, the Codex login file,
    and every secret value to keep out of the logs."""
    credentials = harness.credentials()
    auth_file = Path(auth_file).expanduser() if auth_file else Path.home() / ".codex" / "auth.json"
    if harness.name == "codex" and not credentials and not auth_file.is_file():
        raise BenchmarkError("Codex needs OPENAI_API_KEY, or --auth-file naming a Codex auth.json")
    if harness.name != "codex" and not credentials:
        raise BenchmarkError(f"set one of these first: {', '.join(harness.credential_names())}")
    secrets = tuple(credentials.values())
    if harness.name == "codex" and not credentials:
        secrets += auth_secrets(auth_file)
    return credentials, auth_file, secrets


def sitting_seconds(at_a_time):
    if at_a_time is None:
        return None
    if at_a_time * 3600 < MIN_SESSION_SECONDS:
        raise BenchmarkError(f"--at-a-time must be at least {duration(MIN_SESSION_SECONDS)}")
    return at_a_time * 3600


def run(args):
    """Starts the build phase for one model. Returns the entry's directory."""
    harness = Harness(args.harness, args.model, args.effort)
    limit = sitting_seconds(args.at_a_time)
    access = credentials_for(harness, args.auth_file)
    docker = Docker(args.image, cpus=args.cpus, memory=args.memory)
    image = docker.preflight()
    name = bot_name(args.name or args.model)
    entry_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-") + name
    entry = ENTRIES / entry_id
    entry.mkdir(parents=True)
    record = {
        "id": entry_id, "name": name, "harness": harness.name, "model": harness.model, "effort": harness.effort,
        "image": image, "started": now(), "sealed": False, "state": "working", "volume": "bitbath-" + entry_id,
        "rounds": [], "sittings": [],
        "conditions": {"hours": args.hours, "max_sessions": args.max_sessions, "fix_minutes": args.fix_minutes,
                       "cpus": args.cpus, "memory": args.memory, "network": "provider hosts only",
                       "hosts": harness.hosts},
    }
    write_json(entry / "entry.json", record)
    print(f"Entry {entry_id}: {harness.name} / {harness.model}, {duration(args.hours * 3600)}, "
          f"kit {image['kit_commit'][:12]}")
    return sit(entry, record, harness, docker, image, access, limit=limit)


def resume(args):
    """Goes on with a paused entry, in a new container on its workspace."""
    entry = ENTRIES / args.entry
    try:
        record = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    except OSError:
        raise BenchmarkError(f"no entry {args.entry} in {ENTRIES}") from None
    if record.get("state") not in ("paused", "working"):
        raise BenchmarkError(f"{args.entry} isn't a paused entry")
    harness = Harness(record["harness"], record["model"], record["effort"])
    limit = sitting_seconds(args.at_a_time)
    access = credentials_for(harness, args.auth_file)
    conditions = record["conditions"]
    docker = Docker(args.image, cpus=conditions["cpus"], memory=conditions["memory"])
    image = docker.preflight()
    if image["kit_sha256"] != record["image"]["kit_sha256"]:
        raise BenchmarkError(f"{args.image} holds another kit than the one this entry started from "
                             f"(commit {record['image']['kit_commit'][:12]})")
    if not docker.volume_exists(record["volume"]):
        raise BenchmarkError(f"the entry's workspace, the Docker volume {record['volume']}, is gone")
    users = docker.volume_users(record["volume"])
    if users:
        raise BenchmarkError("the entry's workspace is in use, by another resume or by one that was killed: "
                             f"container {', '.join(users)}")
    if image["id"] != record["image"]["id"]:
        print("Note: the image was rebuilt since this entry started. The kit is the same, "
              "but the harness may be another version.")
    used = sum(r["seconds"] for r in record["rounds"] if not r.get("fix"))
    print(f"Entry {record['id']}: sitting {len(record['sittings']) + 1}, "
          f"{duration(conditions['hours'] * 3600 - used)} of {duration(conditions['hours'] * 3600)} left")
    return sit(entry, record, harness, docker, image, access, limit=limit, seal=args.seal)


def sit(entry, record, harness, docker, image, access, *, limit=None, seal=False):
    """One sitting of an entry's build phase: sessions until the working time
    or the sitting's own time is used up. The entry is then either paused,
    with its workspace kept in a Docker volume, or checked and sealed.
    Returns the entry's directory."""
    credentials, auth_file, secrets = access
    conditions, rounds, volume = record["conditions"], record["rounds"], record["volume"]
    budget = conditions["hours"] * 3600
    cpus = max(1, int(conditions["cpus"]))
    first = len(rounds)
    sitting = {"started": now(), "image_id": image["id"], "first_session": first + 1,
               "credential_names": sorted(credentials)}
    if limit:
        sitting["hours"] = limit / 3600
    record["sittings"].append(sitting)
    record["state"] = "working"
    record.pop("error", None)

    def save():
        write_json(entry / "entry.json", record)

    save()
    try:
        with docker:
            agent = docker.agent(hosts=harness.hosts, environment=harness.environment(), workspace=volume,
                                 seccomp=CLAUDE_SECCOMP if harness.name == "claude" else None)
            harness.preflight(docker, agent)
            harness.authenticate(docker, agent, auth_file)
            version = docker.run(agent, harness.name, "--version")
            sitting["harness_version"] = record["harness_version"] = version
            archive = docker.get(agent, "/workspace/submission")
            before = record.setdefault("placeholder_sha256", contents_hash(archive))
            start = contents_hash(archive)

            def keep(outcome, data):
                """Records submission/ as a session left it."""
                directory = entry / "rounds" / f"{outcome['number']:02d}"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "submission.tar").write_bytes(data)
                outcome.update(submission_sha256=hashlib.sha256(data).hexdigest(), contents_sha256=contents_hash(data))
                save()

            if rounds and rounds[-1]["contents_sha256"] is None:
                # The last sitting was interrupted in the middle of a session.
                keep(rounds[-1], archive)

            def work(number, prompt, timeout, label, **marks):
                """One session, then a copy of submission/ as it left it."""
                began = time.monotonic()
                try:
                    outcome = session(docker, agent, harness, prompt, entry / "rounds" / f"{number:02d}",
                                      timeout=timeout, secrets=secrets, version=version, label=label)
                except BaseException:
                    # Its time still counts, and the next sitting records what it left.
                    rounds.append({"number": number, "seconds": round(time.monotonic() - began), "stop": "interrupted",
                                   "failure": None, "attempts": 1, "usage": [], "contents_sha256": None, **marks})
                    save()
                    raise
                outcome.update(number=number, **marks)
                rounds.append(outcome)
                # This also stops whatever the session left running.
                data = docker.get(agent, "/workspace/submission")
                keep(outcome, data)
                if outcome["failure"] == "configuration":
                    raise BenchmarkError("the harness rejected the model or the credential: "
                                         f"{outcome.get('error') or 'see ' + str(entry / 'rounds')}")
                return data, outcome

            def end(reason):
                """Ends the sitting where a run in one piece would end for good."""
                if limit:
                    sitting["stop"] = reason
                else:
                    record["stop"] = reason

            if seal and "stop" not in record:
                record["stop"] = "sealed_early"
            while "stop" not in record and "stop" not in sitting:
                number = len(rounds) + 1
                # Only the sessions' own time counts: not the benchmark's
                # games between them.
                left = budget - sum(r["seconds"] for r in rounds if not r.get("fix"))
                here = left if not limit else min(left, limit - sum(r["seconds"] for r in rounds[first:]))
                if left < MIN_SESSION_SECONDS:
                    record["stop"] = "time"
                elif number > conditions["max_sessions"]:
                    record["stop"] = "max_sessions"
                elif finished(docker, agent):
                    record["stop"] = "finished"
                elif here < MIN_SESSION_SECONDS:
                    sitting["stop"] = "time"
                if "stop" in record or "stop" in sitting:
                    break
                if rounds and "standing" not in rounds[-1]:
                    # An unchanged bot isn't played again: the table would be the same.
                    same = len(rounds) > 1 and rounds[-2]["contents_sha256"] == rounds[-1]["contents_sha256"]
                    if same and "standing" in rounds[-2]:
                        rounds[-1]["standing"] = rounds[-2]["standing"]
                    else:
                        rounds[-1]["standing"] = standing(docker, archive, cpus)
                    save()
                    print(rounds[-1]["standing"], flush=True)
                several = limit or len(record["sittings"]) > 1
                prompt = TASK.format(hours=duration(budget), remaining=duration(left), deadline=clock_after(here),
                                     session=number, check=" ".join(CHECK), cpus=cpus,
                                     sitting=sitting_note(len(record["sittings"]), limit and here) if several else "",
                                     history=history(rounds, before, rounds[-1]["standing"] if rounds else ""))
                print(f"Session {number}: {duration(left)} left" + (f", {duration(here)} of it now" if limit else ""),
                      flush=True)
                archive, outcome = work(number, prompt, here, f"session {number}")
                if finished(docker, agent):
                    record["stop"] = "finished"
                elif outcome["failure"] == "harness" and outcome["seconds"] < 60:
                    end("harness_error")
                elif idle(rounds[first:], start):
                    end("idle")

            if "stop" in record:
                if not rounds:
                    raise BenchmarkError("no session ran, so there is no submission")
                if contents_hash(archive) == before:
                    # The kit's placeholder isn't anyone's entry.
                    record["state"] = "done"
                    error = rounds[-1].get("error")
                    raise BenchmarkError("the model left submission/ as the kit had it, so there is nothing to seal"
                                         + (f": {error}" if error else ""))

                passed, output = check(docker, archive, entry)
                print(f"Check: {'passed' if passed else 'failed'}", flush=True)
                fixed = any(r.get("fix") and r["stop"] != "interrupted" for r in rounds)
                if not passed and conditions["fix_minutes"] > 0 and not fixed:
                    # One more session, with the check's output and nothing else new.
                    record["fix_session"] = True
                    seconds = conditions["fix_minutes"] * 60
                    prompt = FIX.format(deadline=clock_after(seconds), check=" ".join(CHECK), output=output)
                    archive, _ = work(len(rounds) + 1, prompt, seconds, "fix session", fix=True)
                    passed, output = check(docker, archive, entry)
                    print(f"Check after the fix session: {'passed' if passed else 'failed'}", flush=True)
                record["check"] = {"passed": passed, "output": output}
                (entry / "submission.tar").write_bytes(archive)
                extract_archive(archive, entry / "submission")
                record.update(sealed=passed, state="done", submission_sha256=hashlib.sha256(archive).hexdigest())
    except BaseException as e:
        record["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        sitting["ended"] = now()
        if "stop" in record:
            sitting.setdefault("stop", record["stop"])
        if record["state"] == "done":
            record["finished"] = now()
            try:
                docker.remove_volume(volume)
            except BenchmarkError as e:
                print(f"Couldn't remove the entry's workspace: {e}")
        else:
            record["state"] = "paused"
            used = sum(r["seconds"] for r in rounds if not r.get("fix"))
            print(f"Paused with {duration(budget - used)} of working time left. Its workspace is the Docker "
                  f"volume {volume}.\n  To go on:      python benchmark.py resume {record['id']}\n"
                  f"  To seal it now: python benchmark.py resume {record['id']} --seal")
        save()
    if record["state"] == "done":
        print(f"Sealed: {entry}" if record["sealed"] else f"Not sealed, because the check failed: {entry}")
    return entry


def status(record):
    """One entry's state in a word or two."""
    state = record.get("state")
    if state == "paused":
        return "paused"
    if state == "working":
        return "working"
    if record.get("sealed"):
        return "sealed"
    if "check" in record:
        return "check failed"
    return "failed"


def clock(seconds):
    """Seconds as hours and minutes: "3:20"."""
    minutes = max(0, round(seconds / 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


def summary(record):
    """One entry as a row of the list: its id, harness, status, time used
    and left, sessions, sittings, and when it was last worked on."""
    rounds = record.get("rounds", [])
    used = sum(r["seconds"] for r in rounds if not r.get("fix"))
    budget = record.get("conditions", {}).get("hours", 0) * 3600
    open_ = status(record) in ("paused", "working")
    sittings = record.get("sittings") or [{}]
    last = sittings[-1].get("ended") or record.get("finished") or record.get("started") or ""
    return [record.get("id", "?"), record.get("harness", "?"), status(record), clock(used), clock(budget),
            clock(budget - used) if open_ else "-", str(len(rounds)), str(len(sittings)),
            last[:16].replace("T", " ")]


def listing(args):
    """Prints every entry, oldest first, with what to do about the paused ones."""
    records = []
    for path in sorted(ENTRIES.glob("*/entry.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            records.append({"id": path.parent.name})
    if not args.all:
        records = [r for r in records if status(r) in ("paused", "working", "sealed")]
    if not records:
        print("No entries." if args.all else "No sealed or paused entries. --all also lists the failed ones.")
        return
    head = ["entry", "harness", "status", "used", "of", "left", "sessions", "sittings", "last worked (UTC)"]
    rows = [head] + [summary(r) for r in records]
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    right = {3, 4, 5, 6, 7}
    for row in rows:
        print("  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(row, widths))).rstrip())
    counts = {}
    for r in records:
        counts[status(r)] = counts.get(status(r), 0) + 1
    print()
    print(", ".join(f"{n} {word}" for word, n in counts.items()))
    paused = [r["id"] for r in records if status(r) == "paused"]
    if paused:
        print("To go on with a paused entry: python benchmark.py resume " + (paused[-1] if len(paused) == 1 else "<entry>"))
    if any(status(r) == "working" for r in records):
        print("A working entry is in a sitting now, or its last one was killed: resume it if nothing is running.")


def finished(docker, agent):
    """Whether the model left the file that says it is done."""
    try:
        docker.run(agent, "test", "-e", "/workspace/FINISHED")
        return True
    except BenchmarkError:
        return False
