"""The arena: sealed entries and the reference bots play a tournament in one
container with no network. Each bot's files belong to a user of its own
there, so no bot can read another's code, working files, or the replays."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from .process import run_process
from .run import ENTRIES, now, write_json
from .sandbox import ROOT, BenchmarkError, Docker, archive_of, extract_archive

SETTINGS = ROOT / "bench" / "arena.json"
SEASONS = ROOT / "arena"
HOME = "/workspace/arena"
BITBATH = "/workspace/target/release/bitbath"
# What root needs in the arena container: to hand files to the bots' users,
# to start and stop their programs, and to read their directories.
CAPABILITIES = ("SETUID", "SETGID", "CHOWN", "KILL", "DAC_OVERRIDE", "FOWNER")
FIRST_UID = 2001
MAX_RESULTS_BYTES = 4 * 1024**3


def committed(commit, path):
    """A file as it is in a commit. The reference bots and the rule presets
    come from the image's kit commit, so they match its engine."""
    done = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=ROOT, capture_output=True)
    if done.returncode:
        raise BenchmarkError(f"{path} isn't in commit {commit[:12]}, which the image's kit was built from")
    return done.stdout


def reference_bots(settings, commit):
    """The reference bots, each as a bot directory with the Python kit beside it."""
    kit = committed(commit, settings["kit"])
    bots = {}
    for name, path in settings["references"].items():
        file = Path(path).name
        archive = archive_of({file: committed(commit, path), "bitbath.py": kit,
                              "bot.toml": f'run = "python {file}"\n'.encode()})
        bots[name] = {"kind": "reference", "source": path, "archive": archive}
    return bots


def sealed_entries(only, image, allow_other_kits):
    """The sealed entries, or those named in `only`, as bots by name."""
    bots = {}
    for path in sorted(ENTRIES.glob("*/entry.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if only and record["id"] not in only:
            continue
        if not record.get("sealed"):
            if only:
                raise BenchmarkError(f"entry {record['id']} isn't sealed: its check failed, or its run didn't finish")
            continue
        archive = (path.parent / "submission.tar").read_bytes()
        if hashlib.sha256(archive).hexdigest() != record["submission_sha256"]:
            raise BenchmarkError(f"entry {record['id']}: submission.tar isn't the file that was sealed")
        if record["image"]["kit_sha256"] != image["kit_sha256"] and not allow_other_kits:
            raise BenchmarkError(f"entry {record['id']} was built from another kit than this image's; "
                                 "pass --allow-other-kits to play it anyway")
        name = record["name"]
        if name in bots:
            raise BenchmarkError(f"entries {bots[name]['source']} and {record['id']} are both named '{name}': "
                                 "choose which to play with --entry")
        bots[name] = {"kind": "entry", "source": record["id"], "archive": archive}
    missing = set(only) - {bot["source"] for bot in bots.values()}
    if missing:
        raise BenchmarkError(f"no entry named {', '.join(sorted(missing))} in {ENTRIES}")
    return bots


def stream(container, command, log, *, timeout):
    """Runs a long command in the arena container, showing its output."""
    done = run_process(["docker", "exec", "--workdir", f"{HOME}/work", container, *command],
                       stdout=log, stderr=log.with_suffix(".stderr.txt"), timeout=timeout, echo=True)
    if done.stop or done.returncode:
        detail = log.with_suffix(".stderr.txt").read_text(errors="replace").strip()[-2000:]
        raise BenchmarkError(f"{' '.join(command[:2])} {'was cut off' if done.stop else 'failed'}: {detail}")


def download(container, season):
    """Copies the results out: the games, the ratings, and every replay.
    The bots' working directories stay behind."""
    script = ("cd \"$1\" && find . -name games.jsonl -o -name ratings.json -o -name replay.bbr "
              "| tar -cf - -T -")
    with tempfile.TemporaryDirectory(prefix="bb-results-") as temporary:
        path = Path(temporary) / "results.tar"
        done = run_process(["docker", "exec", container, "sh", "-c", script, "sh", f"{HOME}/out"],
                           stdout=path, stderr=Path(temporary) / "stderr", timeout=1800,
                           max_output=MAX_RESULTS_BYTES)
        if done.stop or done.returncode:
            raise BenchmarkError("couldn't copy the results out of the arena container")
        extract_archive(path.read_bytes(), season, limit=MAX_RESULTS_BYTES)


def arena(args):
    """Plays the tournament, or the games of it that the season doesn't have yet."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    jobs = args.jobs or max(1, int(args.cpus) // 2)
    docker = Docker(args.image, cpus=args.cpus, memory=args.memory)
    image = docker.preflight()
    commit = image["kit_commit"]

    bots = {} if args.no_references else reference_bots(settings, commit)
    for name, bot in sealed_entries(args.entry, image, args.allow_other_kits).items():
        if name in bots:
            raise BenchmarkError(f"entry {bot['source']} has the name of the reference bot '{name}'")
        bots[name] = bot
    if len(bots) < 2:
        raise BenchmarkError("a tournament needs at least two bots")
    for bot in bots.values():
        bot["sha256"] = hashlib.sha256(bot["archive"]).hexdigest()

    # A season's games are only comparable if each name always meant the
    # same bot, playing on the same engine.
    season = SEASONS / args.season
    state_path = season / "arena.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"bots": {}}
    if state.get("kit_sha256", image["kit_sha256"]) != image["kit_sha256"]:
        raise BenchmarkError(f"season '{args.season}' was played with another kit: start a new --season")
    for name, bot in bots.items():
        known = state["bots"].get(name)
        if known and known["sha256"] != bot["sha256"]:
            raise BenchmarkError(f"'{name}' isn't the bot that season '{args.season}' has games for: "
                                 "start a new --season")
    season.mkdir(parents=True, exist_ok=True)

    failed = {}
    with docker:
        container = docker.offline("arena", user="0:0", caps=CAPABILITIES, pids=4096)
        docker.run(container, "mkdir", "-p", f"{HOME}/work", f"{HOME}/out")
        configs = {Path(m["config"]).name: committed(commit, m["config"]) for m in settings["maps"]}
        configs[Path(settings["check_config"]).name] = committed(commit, settings["check_config"])
        docker.put(container, archive_of(configs), f"{HOME}/configs")
        games = season / "games.jsonl"
        if games.is_file():
            docker.put(container, archive_of({"games.jsonl": games.read_bytes()}), f"{HOME}/out")

        # A bot owns its directory while it is built. After that root does,
        # and the bot, which the runner then runs as the user numbered like
        # the directory's group, can read it but not write to it.
        own = 'chown -R "$1:$1" "$2" && chmod -R go-rwx "$2"'
        lock = 'chown -R "0:$1" "$2" && chmod -R u=rwX,g=rX,o= "$2"'
        for uid, (name, bot) in enumerate(bots.items(), start=FIRST_UID):
            directory = f"{HOME}/entries/{name}"
            docker.put(container, bot["archive"], directory)
            docker.run(container, "sh", "-c", own, "sh", str(uid), directory)
            try:
                print(f"Checking {name}", flush=True)
                check = [BITBATH, "check", directory, "--config", f"{HOME}/configs/{Path(settings['check_config']).name}"]
                docker.run(container, *check, workdir=f"{HOME}/work", timeout=1800)
                docker.run(container, "sh", "-c", lock, "sh", str(uid), directory)
            except BenchmarkError as e:
                if bot["kind"] == "reference":
                    raise BenchmarkError(f"the reference bot '{name}' fails its check: {e}") from None
                print(f"  {name} fails its check and sits this tournament out", flush=True)
                failed[name] = str(e)
        playing = [name for name in bots if name not in failed]
        if len(playing) < 2:
            raise BenchmarkError("fewer than two bots passed their checks")

        state.update(kit_sha256=image["kit_sha256"], kit_commit=commit, image=image["id"], updated=now(),
                     settings=settings, failed_checks=failed)
        state["bots"].update({name: {k: bots[name][k] for k in ("kind", "source", "sha256")} for name in playing})
        write_json(state_path, state)

        entrants = [arg for name in playing for arg in ("--bot", f"{name}={HOME}/entries/{name}")]
        anchor = ["--anchor", settings["anchor"]] if settings["anchor"].split("=")[0] in playing else []
        try:
            for number, maps in enumerate(settings["maps"], start=1):
                command = [BITBATH, "tournament", *entrants, "--config", f"{HOME}/configs/{Path(maps['config']).name}",
                           "--seeds", str(maps["seeds"]), "--out", f"{HOME}/out", "--jobs", str(jobs),
                           "--bootstrap", "0", *anchor]
                stream(container, command, season / f"tournament-{number}.log", timeout=args.hours * 3600)
            stream(container, [BITBATH, "rate", f"{HOME}/out/games.jsonl", *anchor,
                               "--json", f"{HOME}/out/ratings.json"], season / "ratings.txt", timeout=600)
        finally:
            # Whatever was played is kept, so the next run continues from it.
            download(container, season)
    print(f"Results: {season}")
    return season
