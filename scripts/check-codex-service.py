"""Run the no-auth protocol acceptance under the proposed service hardening."""

import os
import subprocess
import sys
from pathlib import Path

# No RuntimeDirectory: a transient check must never remove the live broker socket.
PROPERTIES = {
    "User",
    "Group",
    "WorkingDirectory",
    "NoNewPrivileges",
    "PrivateTmp",
    "PrivateDevices",
    "ProtectSystem",
    "ProtectHome",
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectControlGroups",
    "RestrictSUIDSGID",
    "RestrictAddressFamilies",
    "ReadWritePaths",
    "InaccessiblePaths",
    "CapabilityBoundingSet",
    "LockPersonality",
    "MemoryMax",
    "TasksMax",
    "UMask",
}


def command(root, python):
    args = ["systemd-run", "--wait", "--pipe", "--collect", f"--unit=quantpilot-protocol-check-{os.getpid()}"]
    for line in (root / "quantpilot-ai.service").read_text().splitlines():
        if "=" in line and line.split("=", 1)[0] in PROPERTIES:
            if line.startswith("ReadWritePaths="):
                # On a fresh install the broker runtime directory does not exist yet.
                line = line.replace(" /run/quantpilot-ai", " -/run/quantpilot-ai")
            args.extend(["-p", line])
    args.extend([python, str(root / "check-codex-protocol.py"), "--cwd", "/var/lib/quantpilot-ai/empty"])
    return args


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Service acceptance requires the root installer")
    result = subprocess.run(  # noqa: S603 - local installer-owned unit and script, no web input
        command(Path(__file__).resolve().parent, sys.executable), check=False
    )
    raise SystemExit(result.returncode)
