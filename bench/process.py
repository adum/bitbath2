"""Runs a command with its output bounded, timed, and scrubbed of secrets."""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Result:
    returncode: int
    elapsed: float
    # Why the command was cut short: "timeout" or "output_limit". None if it wasn't.
    stop: str = None


def kill(process):
    """Kills the command and, where the platform allows, what it started."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, OSError):
        pass


def run_process(command, *, stdout, stderr, input_text=None, timeout=600, max_output=256 * 1024 * 1024,
                env=None, secrets=(), progress=None, progress_interval=60, echo=False):
    """Runs `command`, writing its output to the files `stdout` and `stderr`.

    Each secret is replaced in the output before it is written. `progress`,
    if given, is called every `progress_interval` seconds with the seconds
    elapsed and the bytes of output so far. `echo` also copies stdout to
    this process's own.
    """
    stdout, stderr = Path(stdout), Path(stderr)
    stdout.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    overflow = threading.Event()
    size = [0]
    lock = threading.Lock()
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            start_new_session=os.name == "posix",
        )

        def drain(source, target, show):
            # Hold back a tail, so a secret split across two reads is still caught.
            pending = b""
            encoded = [s.encode() for s in secrets if s]
            tail = max((len(s) for s in encoded), default=1) - 1
            while True:
                chunk = source.read1(8192)
                with lock:
                    size[0] += len(chunk)
                    if size[0] > max_output:
                        overflow.set()
                pending += chunk
                for secret in encoded:
                    pending = pending.replace(secret, b"[REDACTED]")
                keep = min(tail, len(pending)) if chunk else 0
                ready = pending[:-keep] if keep else pending
                if not overflow.is_set():
                    target.write(ready)
                    target.flush()
                    if show:
                        print(ready.decode(errors="replace"), end="", flush=True)
                pending = pending[-keep:] if keep else b""
                if not chunk:
                    break
            source.close()

        threads = [threading.Thread(target=drain, args=args, daemon=True)
                   for args in ((process.stdout, out, echo), (process.stderr, err, False))]
        for thread in threads:
            thread.start()

        def send_input():
            try:
                process.stdin.write(input_text.encode())
                process.stdin.flush()
            except OSError:
                pass
            finally:
                process.stdin.close()

        if input_text is not None:
            threading.Thread(target=send_input, daemon=True).start()
        stop = None
        next_progress = started + progress_interval
        try:
            while process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout:
                    stop = "timeout"
                elif overflow.is_set():
                    stop = "output_limit"
                if stop:
                    break
                if progress is not None and now >= next_progress:
                    progress(now - started, size[0])
                    next_progress = now + progress_interval
                time.sleep(0.05)
        finally:
            # This only stops the docker client. The caller removes the
            # container, which is what stops the programs inside it.
            kill(process)
            process.wait()
            for thread in threads:
                thread.join(timeout=5)
        return Result(process.returncode, time.monotonic() - started, stop)
