"""The benchmark's command line. See the README's benchmark section."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import kit
from .harnesses import HARNESSES
from .sandbox import DEFAULT_IMAGE, ROOT, BenchmarkError


def cmd_export(args):
    manifest = kit.export(args.directory, args.rev)
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Starter kit: {args.directory}")
    print(f"  commit {manifest['commit']}")
    print(f"  sha256 {manifest['sha256']}")
    print(f"  {len(manifest['files'])} files")


def cmd_build(args):
    """Builds the benchmark image around a freshly exported kit."""
    if not shutil.which("docker"):
        raise BenchmarkError("Docker is required: install and start Docker Desktop or Docker Engine")
    with tempfile.TemporaryDirectory(prefix="bb-image-") as temporary:
        context = Path(temporary)
        manifest = kit.export(context / "kit", args.rev)
        (context / "kit.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        (context / "bench").mkdir()
        for name in ("proxy.py", "bwrap.py", "agy_policy.py"):
            # Written with Unix line endings: two of them are run by their first line.
            text = (ROOT / "bench" / name).read_text(encoding="utf-8")
            (context / "bench" / name).write_text(text, encoding="utf-8", newline="\n")
        command = ["docker", "build", "--file", str(ROOT / "containers" / "benchmark.Dockerfile"),
                   "--tag", args.image, "--label", f"bitbath.kit.commit={manifest['commit']}",
                   "--label", f"bitbath.kit.sha256={manifest['sha256']}"]
        for pin in args.build_arg:
            command += ["--build-arg", pin]
        if subprocess.run([*command, str(context)]).returncode:
            raise BenchmarkError("docker build failed")
    print(f"Image {args.image}: kit commit {manifest['commit'][:12]}, sha256 {manifest['sha256'][:12]}")


def cmd_run(args):
    from .run import run
    run(args)


def cmd_arena(args):
    from .arena import arena
    arena(args)


def main():
    parser = argparse.ArgumentParser(description="The Bitbath bot-building benchmark.")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("export", help="write the starter kit to a directory")
    p.add_argument("directory", help="where to write the kit; it must be empty or not exist yet")
    p.add_argument("--rev", default="HEAD", help="the commit to take the kit from (default: HEAD)")
    p.add_argument("--manifest", help="also write the kit's manifest, with every file's hash, to this file")
    p.set_defaults(run=cmd_export)

    p = commands.add_parser("build", help="build the benchmark image, with the starter kit inside")
    p.add_argument("--rev", default="HEAD", help="the commit to take the kit from (default: HEAD)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help=f"the image's name (default: {DEFAULT_IMAGE})")
    p.add_argument("--build-arg", action="append", default=[], metavar="NAME=VERSION",
                   help="pin a version in the Dockerfile, such as CLAUDE_VERSION=2.1.325; repeatable")
    p.set_defaults(run=cmd_build)

    p = commands.add_parser("run", help="have a model build a bot, and seal it as an entry")
    p.add_argument("--harness", required=True, choices=HARNESSES, help="the coding agent the model works through")
    p.add_argument("--model", required=True, help="the model's exact id; for opencode, provider/model; for agy, a slug such as gemini-4-carbon-high")
    p.add_argument("--effort", help="the model's reasoning effort, if the harness takes one")
    p.add_argument("--name", help="the bot's name in the tournament (default: from the model id)")
    p.add_argument("--hours", type=float, default=4, help="working time for the model (default: 4)")
    p.add_argument("--max-sessions", type=int, default=50,
                   help="most sessions the time may be split over, as a backstop (default: 50)")
    p.add_argument("--fix-minutes", type=float, default=15,
                   help="length of the one extra session a failed check gets, or 0 for none (default: 15)")
    p.add_argument("--cpus", type=float, default=16, help="CPUs for the container (default: 16)")
    p.add_argument("--memory", default="8g", help="memory for the container (default: 8g)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help=f"the benchmark image (default: {DEFAULT_IMAGE})")
    p.add_argument("--auth-file", help="a Codex auth.json, when there is no OPENAI_API_KEY")
    p.set_defaults(run=cmd_run)

    p = commands.add_parser("arena", help="play the sealed entries and the reference bots against each other")
    p.add_argument("--season", default="default",
                   help="the results directory under arena/; running again continues it (default: default)")
    p.add_argument("--entry", action="append", default=[], metavar="ID",
                   help="play only this entry; repeatable (default: every sealed entry)")
    p.add_argument("--no-references", action="store_true", help="leave the reference bots out")
    p.add_argument("--allow-other-kits", action="store_true",
                   help="also play entries that were built from another kit than the image's")
    p.add_argument("--jobs", type=int, help="games to play at once (default: half the CPUs)")
    p.add_argument("--hours", type=float, default=12, help="longest one map size may take (default: 12)")
    p.add_argument("--cpus", type=float, default=16, help="CPUs for the container (default: 16)")
    p.add_argument("--memory", default="8g", help="memory for the container (default: 8g)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help=f"the benchmark image (default: {DEFAULT_IMAGE})")
    p.set_defaults(run=cmd_arena)

    args = parser.parse_args()
    try:
        args.run(args)
    except BenchmarkError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted")
