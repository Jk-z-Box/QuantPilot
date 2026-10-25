"""Fail without printing any matched secret or its value."""
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
patterns = [re.compile(rb"gh[pousr]_" + rb"[A-Za-z0-9]{30,}"),
            re.compile(rb"github_pat_" + rb"[A-Za-z0-9_]{30,}"),
            re.compile(rb"sk-" + rb"(?:proj-)?[A-Za-z0-9_-]{32,}"),
            re.compile(rb"-----BEGIN " + rb"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]
try:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")  # noqa: S603,S607
except subprocess.CalledProcessError:
    paths = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
failures = []
for name in paths:
    path = root / name
    if not name or not path.is_file() or path.stat().st_size > 2_000_000:
        continue
    if any(pattern.search(path.read_bytes()) for pattern in patterns):
        failures.append(name)
if failures:
    print("Secret-like content detected in files: " + ", ".join(failures))
    raise SystemExit(1)
print("Secret scan passed; no credential values printed.")
