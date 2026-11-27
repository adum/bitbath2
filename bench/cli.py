"""The benchmark's command line. See the README's benchmark section."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import kit, tracks
from .harnesses import HARNESSES
from .sandbox import ROOT, BenchmarkError


def cmd_export(args):
    manifest = kit.export(args.directory, args.rev, args.track)
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Starter kit of the {args.track} track: {args.directory}")
    print(f"  commit {manifest['commit']}")
    print(f"  sha256 {manifest['sha256']}")
    print(f"  {len(manifest['files'])} files")


def cmd_build(args):
    """Builds a track's benchmark image around its freshly exported kit."""
    if not shutil.which("docker"):
        raise BenchmarkError("Docker is required: install and start Docker Desktop or Docker Engine")
    image = args.image or tracks.image(args.track)
    with tempfile.TemporaryDirectory(prefix="bb-image-") as temporary:
        context = Path(temporary)
        manifest = kit.export(context / "kit", args.rev, args.track)
        (context / "kit.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        (context / "bench").mkdir()
        for name in ("proxy.py", "bwrap.py", "agy_policy.py"):
            # Written with Unix line endings: two of them are run by their first line.
            text = (ROOT / "bench" / name).read_text(encoding="utf-8")
            (context / "bench" / name).write_text(text, encoding="utf-8", newline="\n")
        command = ["docker", "build", "--file", str(ROOT / "containers" / "benchmark.Dockerfile"),
                   "--tag", image, "--label", f"bitbath.track={args.track}",
                   "--label", f"bitbath.kit.commit={manifest['commit']}",
                   "--label", f"bitbath.kit.sha256={manifest['sha256']}"]
        for pin in args.build_arg:
            command += ["--build-arg", pin]
        if subprocess.run([*command, str(context)]).returncode:
            raise BenchmarkError("docker build failed")
    print(f"Image {image}: the {args.track} track's kit, commit {manifest['commit'][:12]}, "
          f"sha256 {manifest['sha256'][:12]}")


def cmd_run(args):
    from .run import run
    run(args)


def cmd_resume(args):
    from .run import resume
    resume(args)


def cmd_list(args):
    from .run import listing
    listing(args)


def cmd_arena(args):
    from .arena import arena
    arena(args)


def main():
    parser = argparse.ArgumentParser(description="The Bitbath bot-building benchmark.")
    commands = parser.add_subparsers(dest="command", required=True)
    names = tracks.names()

    def track(p):
        p.add_argument("--track", required=True, choices=names,
                       help="the game the bots are built for: its kit, its practice bots, and its tournament")

    def image(p, about="the benchmark image"):
        p.add_argument("--image", help=f"{about} (default: {tracks.image('<track>')})")

    p = commands.add_parser("export", help="write a track's starter kit to a directory")
    track(p)
    p.add_argument("directory", help="where to write the kit; it must be empty or not exist yet")
    p.add_argument("--rev", default="HEAD", help="the commit to take the kit from (default: HEAD)")
    p.add_argument("--manifest", help="also write the kit's manifest, with every file's hash, to this file")
    p.set_defaults(run=cmd_export)

    p = commands.add_parser("build", help="build a track's benchmark image, with its starter kit inside")
    track(p)
    p.add_argument("--rev", default="HEAD", help="the commit to take the kit from (default: HEAD)")
    image(p, "the image's name")
    p.add_argument("--build-arg", action="append", default=[], metavar="NAME=VERSION",
                   help="pin a version in the Dockerfile, such as CLAUDE_VERSION=2.1.325; repeatable")
    p.set_defaults(run=cmd_build)

    p = commands.add_parser("run", help="have a model build a bot, and seal it as an entry")
    track(p)
    p.add_argument("--harness", required=True, choices=HARNESSES, help="the coding agent the model works through")
    p.add_argument("--model", required=True, help="the model's exact id; for opencode, provider/model; for agy, a slug such as gemini-4-carbon-high")
    p.add_argument("--effort", help="the model's reasoning effort, if the harness takes one")
    p.add_argument("--name", help="the bot's name in the tournament (default: from the model id)")
    p.add_argument("--hours", type=float, default=4, help="working time for the model (default: 4)")
    p.add_argument("--at-a-time", type=float, metavar="HOURS",
                   help="pause the entry after this much of the working time; `resume` goes on with it "
                        "(default: use all of it now)")
    p.add_argument("--max-sessions", type=int, default=50,
                   help="most sessions the time may be split over, as a backstop (default: 50)")
    p.add_argument("--fix-minutes", type=float, default=15,
                   help="length of the one extra session a failed check gets, or 0 for none (default: 15)")
    p.add_argument("--cpus", type=float, default=16, help="CPUs for the container (default: 16)")
    p.add_argument("--memory", default="8g", help="memory for the container (default: 8g)")
    image(p)
    p.add_argument("--auth-file", help="a Codex auth.json, when there is no OPENAI_API_KEY")
    p.set_defaults(run=cmd_run)

    p = commands.add_parser("resume", help="go on with a paused entry")
    p.add_argument("entry", metavar="ID", help="the entry's id: its directory name under entries/")
    p.add_argument("--at-a-time", type=float, metavar="HOURS",
                   help="pause again after this much of the working time (default: use all that is left)")
    p.add_argument("--seal", action="store_true",
                   help="give up the time that is left: check the bot as it stands and seal it")
    p.add_argument("--image", help="the benchmark image (default: the image of the entry's track)")
    p.add_argument("--auth-file", help="a Codex auth.json, when there is no OPENAI_API_KEY")
    p.set_defaults(run=cmd_resume)

    p = commands.add_parser("list", help="list the entries: sealed, paused, and how much time each has left")
    p.add_argument("--all", action="store_true", help="also list the entries that failed")
    p.set_defaults(run=cmd_list)

    p = commands.add_parser("arena", help="play a track's sealed entries and reference bots against each other")
    track(p)
    p.add_argument("--season",
                   help="the results directory under arena/; running again continues it (default: the track's name)")
    p.add_argument("--entry", action="append", default=[], metavar="ID",
                   help="play only this entry; repeatable (default: every sealed entry of the track)")
    p.add_argument("--no-references", action="store_true", help="leave the reference bots out")
    p.add_argument("--allow-other-kits", action="store_true",
                   help="also play entries that were built from another kit than the image's")
    p.add_argument("--jobs", type=int, help="games to play at once (default: half the CPUs)")
    p.add_argument("--hours", type=float, default=12, help="longest one map size may take (default: 12)")
    p.add_argument("--cpus", type=float, default=16, help="CPUs for the container (default: 16)")
    p.add_argument("--memory", default="8g", help="memory for the container (default: 8g)")
    image(p)
    p.set_defaults(run=cmd_arena)

    args = parser.parse_args()
    try:
        args.run(args)
    except BenchmarkError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted")
