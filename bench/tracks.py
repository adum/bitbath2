"""The benchmark's tracks. A track is one game to build a bot for: a starter
kit, the bots to practise against, and the tournament the entries play.
Each has a directory in bench/tracks/:

  track.json   what is played: the check, the practice games between a
               model's sessions, the reference bots, the maps and seeds, the
               rating anchor, and what the prompt says about the opposition
  kit.txt      the allowlist of repository paths in the starter kit
  kit/         the kit's own files: its README, which is the task a model
               reads, and the placeholder submission

A track has an image of its own, since the image holds the kit, and an entry
plays only the tournaments of the track it was built on.
"""

import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TRACKS = "bench/tracks"
# The track of the images and entries made before there were tracks.
FIRST = "micro"


def names():
    return sorted(path.parent.name for path in (ROOT / TRACKS).glob("*/track.json"))


def load(name):
    """A track's settings, with its name and the paths of its kit's files."""
    path = ROOT / TRACKS / name / "track.json"
    if not path.is_file():
        raise SystemExit(f"error: there's no track '{name}': the tracks are {', '.join(names())}")
    track = json.loads(path.read_text(encoding="utf-8"))
    track.update(name=name, allowlist=f"{TRACKS}/{name}/kit.txt", overlay=f"{TRACKS}/{name}/kit")
    return track


def image(name):
    """The name a track's image is built under, unless another is given."""
    return f"bitbath-bench:{name}"


def reference(spec):
    """One reference bot of a track, as (its files, the command that runs it,
    the command that builds it or None). In track.json a reference is the
    path of a Python bot in one file, or {"files", "run", "build"} for a bot
    of several files or in another language. The files go into one bot
    directory side by side, and the commands run there."""
    if isinstance(spec, str):
        return [spec], f"python {PurePosixPath(spec).name}", None
    return list(spec["files"]), spec["run"], spec.get("build")
