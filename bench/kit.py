"""The starter kit: the files a model gets to build its bot from.

The kit holds the paths that bench/starter.txt lists, as they are in one
commit, and the files in bench/starter/.
"""

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = "bench/starter.txt"
OVERLAY = "bench/starter"


def git(*args):
    """Runs git in the repository and returns its output as bytes."""
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def unpack(archive, dest):
    """Unpacks a tar archive from git into `dest`."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        # The archive is git's own, of this repository. Pythons before 3.12
        # may not have the stricter "data" filter.
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)


def read_allowlist(text):
    """The allowlist's paths, and its copies as {destination: source}."""
    include, copies = [], {}
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if "=" in line:
            target, source = (part.strip() for part in line.split("=", 1))
            copies[target] = source
        elif line:
            include.append(line)
    for target, source in copies.items():
        if not any(source == path or source.startswith(path + "/") for path in include):
            raise SystemExit(f"{ALLOWLIST}: {target} copies {source}, which isn't listed")
    return include, copies


def workspace_manifest(commit, members):
    """A Cargo.toml for the kit: the repository's, with only the kit's crates."""
    cargo = git("show", f"{commit}:Cargo.toml").decode()
    # Profile sections name crates, and one that isn't in the kit would be
    # an error there. The kit builds with the default profiles.
    cargo = cargo.split("[profile")[0].rstrip() + "\n"
    listed = re.search(r"^members = \[(.*?)\]", cargo, re.MULTILINE | re.DOTALL)
    if not listed:
        raise SystemExit("Cargo.toml: no 'members = [...]' line")
    kept = [m for m in re.findall(r'"([^"]+)"', listed.group(1)) if m in members]
    return cargo[: listed.start()] + f"members = {json.dumps(kept)}" + cargo[listed.end() :]


def export(dest, rev):
    """Writes the starter kit to `dest` and returns its manifest."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} isn't empty")
    commit = git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
    include, copies = read_allowlist(git("show", f"{commit}:{ALLOWLIST}").decode())

    # The kit comes from the commit, not from the files on disk, so that
    # work in progress here never reaches it.
    changed = git("status", "--porcelain", "--", *include, ALLOWLIST, OVERLAY).decode().splitlines()
    if changed and rev == "HEAD":
        print("warning: these have changes that aren't committed, and so aren't in the kit:", file=sys.stderr)
        for line in changed:
            print(f"  {line}", file=sys.stderr)

    dest.mkdir(parents=True, exist_ok=True)
    unpack(git("archive", "--format=tar", commit, "--", *include), dest)
    unpack(git("archive", "--format=tar", f"{commit}:{OVERLAY}"), dest)
    for target, source in copies.items():
        (dest / target).parent.mkdir(parents=True, exist_ok=True)
        (dest / target).write_bytes((dest / source).read_bytes())
    (dest / "Cargo.toml").write_text(workspace_manifest(commit, include), encoding="utf-8", newline="\n")

    files = {}
    for path in sorted(p for p in dest.rglob("*") if p.is_file()):
        files[path.relative_to(dest).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    listing = "".join(f"{h}  {name}\n" for name, h in files.items())
    return {"commit": commit, "sha256": hashlib.sha256(listing.encode()).hexdigest(), "files": files}
