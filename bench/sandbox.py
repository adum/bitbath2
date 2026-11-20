"""Containers for the benchmark. Everything a model or its bot runs, runs in
one of these; nothing falls back to the host."""

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .process import run_process

DEFAULT_IMAGE = "bitbath-bench:local"
ROOT = Path(__file__).resolve().parents[1]
CLAUDE_SECCOMP = ROOT / "containers" / "claude-seccomp.json"
# The user a model and its bot run as in a build container.
AGENT_USER = "1000:1000"
# The most a submission's archive may hold. The runner's own check holds the
# bot directory to less; this bounds what is read off an untrusted container.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

# Stops every process in a container but its supervisor, then writes one
# directory to stdout as a tar archive. Run before copying files out, so that
# nothing a model left running changes them halfway.
QUIESCE_AND_TAR = """import os, signal, sys, time
keep = {1, int(sys.argv[1]), os.getpid()}
deadline = time.monotonic() + 10
while True:
    live = []
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) in keep:
            continue
        try:
            if open('/proc/' + name + '/stat').read().split(') ', 1)[1][0] != 'Z':
                live.append(int(name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    if not live:
        break
    if time.monotonic() >= deadline:
        raise SystemExit('could not stop the processes in the container')
    for pid in live:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.02)
os.execv('/bin/tar', ['tar', '-cf', '-', '-C', sys.argv[2], '.'])
"""


class BenchmarkError(RuntimeError):
    pass


def checked(command, *, input_bytes=None, timeout=60, env=None):
    """Runs a command and returns its stdout as text. Raises if it fails."""
    try:
        done = subprocess.run(command, input=input_bytes, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise BenchmarkError(f"{' '.join(command[:3])} took over {timeout} seconds") from None
    if done.returncode:
        detail = done.stderr.decode(errors="replace").strip()[:1000]
        raise BenchmarkError(f"{' '.join(command[:3])} failed: {detail}")
    return done.stdout.decode(errors="replace").strip()


def check_archive(data, *, limit=MAX_ARCHIVE_BYTES):
    """Makes sure a tar archive from a container holds only plain files and
    directories, under relative paths, within the size limit."""
    total, seen = 0, set()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                raise BenchmarkError(f"the archive has an unsafe path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise BenchmarkError(f"the archive has a link or special file: {member.name}")
            if name.as_posix() in seen:
                raise BenchmarkError(f"the archive has {member.name} twice")
            seen.add(name.as_posix())
            total += member.size
            if total > limit or len(seen) > 20000:
                raise BenchmarkError("the archive is over the size or file-count limit")


def extract_archive(data, destination, *, limit=MAX_ARCHIVE_BYTES):
    """Unpacks a checked archive, for people to read. File modes aren't kept
    on every host, which is why the archive itself is what gets submitted."""
    check_archive(data, limit=limit)
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open("wb") as target:
                    shutil.copyfileobj(source, target)


def archive_of(files):
    """A tar archive of {path: bytes}, with every file readable and runnable."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


class Docker:
    """Starts, fills, and removes the benchmark's containers."""

    def __init__(self, image=DEFAULT_IMAGE, *, cpus=4, memory="8g"):
        if not shutil.which("docker"):
            raise BenchmarkError("Docker is required: install and start Docker Desktop or Docker Engine")
        if not re.fullmatch(r"[1-9][0-9]*[mgMG]", memory):
            raise BenchmarkError("memory must be a size such as 4096m or 8g")
        self.image = image
        self.cpus = cpus
        self.memory = memory
        self.prefix = "bb-" + uuid.uuid4().hex[:12]
        self.containers = []
        self.networks = []
        self.supervisors = {}

    def call(self, *args, **kwargs):
        return checked(["docker", *args], **kwargs)

    def preflight(self):
        """The image's id and the kit it was built with."""
        try:
            self.call("info", "--format", "{{.ServerVersion}}", timeout=30)
        except BenchmarkError:
            raise BenchmarkError("Docker isn't running: start Docker Desktop or the Docker service") from None
        try:
            data = json.loads(self.call("image", "inspect", self.image, "--format", "{{json .}}", timeout=30))
        except BenchmarkError:
            raise BenchmarkError(f"no image {self.image}: run python benchmark.py build") from None
        labels = data.get("Config", {}).get("Labels") or {}
        if "bitbath.kit.commit" not in labels:
            raise BenchmarkError(f"{self.image} isn't a benchmark image: run python benchmark.py build")
        return {"id": data["Id"], "kit_commit": labels["bitbath.kit.commit"], "kit_sha256": labels["bitbath.kit.sha256"]}

    def base(self, name, *, network="none", user=AGENT_USER, caps=(), pids=512):
        """The `docker run` arguments every benchmark container shares."""
        scratch = "rw,exec,nosuid,nodev,size=1024m,mode=1777"
        args = [
            "run", "--detach", "--name", name, "--init", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", f"--pids-limit={pids}",
            "--cpus", str(self.cpus), "--memory", self.memory, "--memory-swap", self.memory,
            "--ulimit", "nofile=4096:4096", "--user", user, "--network", network,
            "--tmpfs", f"/tmp:{scratch}", "--tmpfs", f"/home/bench:{scratch}",
            # A new volume, which Docker fills with the image's kit.
            "--mount", "type=volume,destination=/workspace", "--workdir", "/workspace",
            "--env", "HOME=/home/bench", "--env", "TMPDIR=/tmp",
        ]
        for cap in caps:
            args.append(f"--cap-add={cap}")
        return args

    def start(self, name, args, *, env=None):
        # Registered before it exists, so a start that half fails is still removed.
        self.containers.append(name)
        self.call(*args, env=env, timeout=120)
        children = self.call("exec", name, "cat", "/proc/1/task/1/children").split()
        if not children:
            raise BenchmarkError("the container has no supervisor process")
        self.supervisors[name] = children[0]
        return name

    def agent(self, *, hosts, environment, seccomp=None):
        """A build container: the kit in /workspace, and a network that
        reaches only `hosts`, through a proxy in a container of its own."""
        internal, external = self.prefix + "-internal", self.prefix + "-egress"
        for network, extra in ((internal, ["--internal"]), (external, [])):
            self.networks.append(network)
            self.call("network", "create", *extra, network)
        proxy = self.prefix + "-proxy"
        args = self.base(proxy, network=external)
        args += ["--env", "INFERENCE_HOSTS=" + ",".join(hosts), self.image, "python3", "/opt/bench/proxy.py"]
        self.start(proxy, args)
        self.call("network", "connect", "--alias", "inference-proxy", internal, proxy)

        name = self.prefix + "-agent"
        args = self.base(name, network=internal)
        if seccomp is not None:
            args += ["--security-opt", f"seccomp={seccomp}"]
        args += ["--dns", "127.0.0.1"]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            args += ["--env", f"{key}=http://inference-proxy:8080"]
        args += ["--env", "NO_PROXY=localhost,127.0.0.1", "--env", "no_proxy=localhost,127.0.0.1"]
        # Named only: docker takes the values from this call's environment,
        # which keeps them out of its arguments and of `docker inspect` logs.
        for key in environment:
            args += ["--env", key]
        return self.start(name, args + [self.image], env={**os.environ, **environment})

    def offline(self, label, *, user=AGENT_USER, caps=(), pids=512):
        """A container with the kit in /workspace and no network at all."""
        name = f"{self.prefix}-{label}"
        return self.start(name, self.base(name, user=user, caps=caps, pids=pids) + [self.image])

    def run(self, container, *command, user=None, workdir=None, input_bytes=None, timeout=120):
        """Runs a short command in a container and returns its output."""
        args = ["exec"]
        if input_bytes is not None:
            args.append("-i")
        if user:
            args += ["--user", user]
        if workdir:
            args += ["--workdir", workdir]
        return self.call(*args, container, *command, input_bytes=input_bytes, timeout=timeout)

    def put(self, container, archive, destination, *, user=None):
        """Unpacks a tar archive into a new directory in the container."""
        script = 'rm -rf "$1" && mkdir -p "$1" && tar --no-same-owner -xf - -C "$1"'
        self.run(container, "sh", "-c", script, "sh", destination, user=user, input_bytes=archive)

    def get(self, container, directory):
        """Stops everything running in the container, then returns one of its
        directories as a checked tar archive."""
        with tempfile.TemporaryDirectory(prefix="bb-export-") as temporary:
            path = Path(temporary) / "export.tar"
            done = run_process(
                ["docker", "exec", container, "python3", "-c", QUIESCE_AND_TAR,
                 self.supervisors[container], directory],
                stdout=path, stderr=Path(temporary) / "stderr", timeout=180, max_output=MAX_ARCHIVE_BYTES,
            )
            if done.stop == "output_limit":
                raise BenchmarkError(f"{directory} holds over {MAX_ARCHIVE_BYTES // 2**20} MB")
            if done.returncode or done.stop:
                detail = (Path(temporary) / "stderr").read_text(errors="replace").strip()[:500]
                raise BenchmarkError(f"couldn't copy {directory} out of the container: {detail}")
            data = path.read_bytes()
        check_archive(data)
        return data

    def remove(self, name):
        self.call("rm", "--force", "--volumes", name, timeout=60)
        self.containers.remove(name)

    def close(self):
        for name in reversed(self.containers):
            try:
                self.call("rm", "--force", "--volumes", name, timeout=60)
            except BenchmarkError:
                pass
        for name in reversed(self.networks):
            try:
                self.call("network", "rm", name, timeout=60)
            except BenchmarkError:
                pass
        self.containers.clear()
        self.networks.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
