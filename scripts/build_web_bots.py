"""Bundle the reference players and their pinned browser Python runtime."""

import base64
import hashlib
import io
from pathlib import Path
import shutil
import tarfile
import urllib.request
import zipfile

VERSION = "0.28.3"
RUNTIME_DIR = f"pyodide-{VERSION}"
INTEGRITY = "rtCsyTU55oNGpLzSVuAd55ZvruJDEX8o6keSdWKN9jPeBVSNlynaKFG7eRqkiIgU7i2M6HEgYtm0atCEQX3u4A=="


def build(root):
    web = root / "web"
    cache = root / "target" / f"{RUNTIME_DIR}.tgz"
    cache.parent.mkdir(exist_ok=True)
    data = cache.read_bytes() if cache.exists() else b""
    expected = base64.b64decode(INTEGRITY)
    if hashlib.sha512(data).digest() != expected:
        url = f"https://registry.npmjs.org/pyodide/-/{RUNTIME_DIR}.tgz"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if hashlib.sha512(data).digest() != expected:
            raise ValueError("Unexpected Pyodide package checksum")
        cache.write_bytes(data)

    runtime = web / RUNTIME_DIR
    runtime.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as package:
        for name in ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "pyodide-lock.json", "python_stdlib.zip"):
            (runtime / name).write_bytes(package.extractfile(f"package/{name}").read())
    shutil.copyfile(root / "scripts" / "pyodide-LICENSE.txt", runtime / "LICENSE.txt")

    sources = [root / "kits/python/bitbath.py", root / "scripts/browser_bot.py"]
    sources.extend(root / "bots" / f"{name}_bot.py" for name in ("bastion", "smart", "vanguard"))
    with zipfile.ZipFile(web / "reference-bots.zip", "w") as bundle:
        for source in sources:
            bundle.writestr(zipfile.ZipInfo(source.name), source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1])
