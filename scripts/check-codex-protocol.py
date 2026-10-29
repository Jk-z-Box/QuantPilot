"""Exercise the pinned real app-server protocol in a disposable, unauthenticated home.

No real credentials, inference results, account changes or trading operations are
used. A turn must be accepted with the exact production sandbox and JSON schema.
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from quantpilot.codex_broker import Broker, Protocol
from quantpilot.v2.ai import Review


class TurnAccepted(Exception):
    pass


class ProbeProtocol(Protocol):
    def call(self, method, params, timeout=20):
        result = super().call(method, params, timeout)
        if method == "turn/start":
            turn_id = result["turn"]["id"]
            try:
                super().call("turn/interrupt", {"threadId": params["threadId"], "turnId": turn_id}, 5)
            finally:
                raise TurnAccepted from None
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="/opt/quantpilot/codex/0.165.0/codex")
    parser.add_argument("--cwd", type=Path)
    args = parser.parse_args()
    version = subprocess.check_output([args.binary, "--version"], text=True).strip()  # noqa: S603
    if version != "codex-cli 0.165.0":
        raise SystemExit("Protocol acceptance must use the pinned Codex 0.165.0 binary")
    with tempfile.TemporaryDirectory(prefix="qp-protocol-") as temporary:
        home = Path(temporary)
        cwd = args.cwd or home / "empty"
        cwd.mkdir(mode=0o700, exist_ok=True)
        broker = Broker(cwd, lambda notify: ProbeProtocol(args.binary, home, cwd, notify))
        try:
            broker.response(
                {
                    "model": "gpt-6-sol",
                    "instructions": "Return an empty revisions array.",
                    "input": '{"opportunities":[]}',
                    "text": {"format": {"name": "opportunity_review", "schema": Review.model_json_schema()}},
                }
            )
            raise SystemExit("Protocol probe did not observe an accepted turn")
        except TurnAccepted:
            print(
                json.dumps(
                    {
                        "pinned_protocol_accepted": True,
                        "profile": "quantpilot",
                        "real_credentials_used": False,
                    }
                )
            )
        finally:
            if broker.rpc:
                broker.rpc.close()


if __name__ == "__main__":
    main()
