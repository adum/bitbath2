"""Rebuild the shipped Micro Astra module with Emscripten 4.0.21 and Git."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "20261121-062844-gpt-6-astra"
SHA256 = "df9b5896bf6f09de0d33ff3161a1371b4280d4494b71a0967709277ef23c6182"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=ROOT / "entries" / ENTRY / "submission.tar")
    parser.add_argument("--compiler", default=os.environ.get("EMXX", "em++"))
    args = parser.parse_args()
    if hashlib.sha256(args.archive.read_bytes()).hexdigest() != SHA256:
        parser.error("Expected the GPT-6 Astra submission used in Micro season 5")
    compiler = [args.compiler]
    if args.compiler.endswith(".py"):
        compiler = [sys.executable, args.compiler]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        with tarfile.open(args.archive) as archive:
            for name in ("bot.cpp", "learn/features.h", "learn/value_v1.h"):
                path = work / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(archive.extractfile("./" + name).read())
        adapter = ROOT / "bots/astra"
        subprocess.run(["git", "apply", str(adapter / "browser.patch")], cwd=work, check=True)
        subprocess.run(compiler + [
            "bot.cpp", "-I", str(adapter), "-std=c++17", "-O3", "-DNDEBUG", "--no-entry",
            "-sMODULARIZE", "-sEXPORT_NAME=createAstraModule", "-sENVIRONMENT=worker",
            "-sEXPORTED_RUNTIME_METHODS=ccall", "-sEXPORTED_FUNCTIONS=_malloc",
            "-sALLOW_MEMORY_GROWTH", "-sSTACK_SIZE=2097152",
            "-sFILESYSTEM=0", "-o", "astra.js",
        ], cwd=work, check=True)
        for name in ("astra.js", "astra.wasm"):
            shutil.copyfile(work / name, ROOT / "web" / name)


if __name__ == "__main__":
    main()
