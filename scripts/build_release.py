"""Package the exact Git commit; never include local videos, credentials or models."""
import hashlib
import os
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit("Release version must be major.minor.patch")
if f'__version__ = "{version}"' not in (root / "clipforge/__init__.py").read_text(encoding="utf-8"):
    raise SystemExit("Package and application versions differ")
notes = Path(f"docs/releases/v{version}.md")
if not (root / notes).is_file():
    raise SystemExit(f"Missing release notes: {notes}")
archive = Path(f"dist/clipforge-v{version}.zip")
(root / archive).parent.mkdir(exist_ok=True)
subprocess.run(["git", "archive", "--format=zip", "--prefix=clipforge/", "-o", str(archive), "HEAD"], cwd=root, check=True)
private = {".venv", "__pycache__", ".pytest_cache", ".git", "secrets", "data", "inbox", "prepared", "models"}
with zipfile.ZipFile(root / archive) as package:
    for name in package.namelist():
        path = Path(name)
        relative = Path(*path.parts[1:]).as_posix()
        if private.intersection(path.parts) or relative == "config.json" or path.name.startswith(".env"):
            raise SystemExit(f"Private runtime path in release: {name}")
    if package.testzip() is not None:
        raise SystemExit("Release ZIP failed its integrity check")
digest = hashlib.sha256((root / archive).read_bytes()).hexdigest()
(root / (str(archive) + ".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="ascii")
outputs = f"version={version}\ntag=v{version}\narchive={archive.as_posix()}\nnotes={notes.as_posix()}\n"
if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(outputs)
print(outputs, end="")
