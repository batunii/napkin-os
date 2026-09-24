#!/usr/bin/env python3
"""Run every port's contract suite, each against its own base URL.

    python3 run_all.py --base-url http://127.0.0.1:8797 --no-live        # the mock, spends nothing
    python3 run_all.py --base-url http://127.0.0.1:8797                  # the mock, live (Claude spend)
    python3 run_all.py --layers-url https://layers.internal --layers-token T \\
        --model-url https://api.anthropic.com --model-key "$ANTHROPIC_API_KEY" --only layers,model

Each --<port>-url defaults to --base-url, so a mix (a real layers service and
the mock for the rest) is one flag. Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORTS = ("layers", "retrieval", "research", "model")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url")
    for p in PORTS:
        ap.add_argument(f"--{p}-url")
        ap.add_argument(f"--{p}-token")
    ap.add_argument("--model-key", help="the model port's API key (default: dummy)")
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--model-api", default="anthropic,openai", help="wires to check")
    ap.add_argument("--org", default="org/dev-agency")
    ap.add_argument("--only", default=",".join(PORTS))
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--expect-refusals", action="store_true", help="the model target is the mock")
    ap.add_argument("--no-image", action="store_true")
    a = ap.parse_args()
    failed = []
    for port in a.only.split(","):
        url = getattr(a, f"{port}_url") or a.base_url
        if not url:
            sys.exit(f"--{port}-url or --base-url is required")
        token = getattr(a, f"{port}_token")
        runs = []
        if port == "layers":
            runs.append(["layers_contract.py", "--base-url", url] + (["--token", token] if token else []))
        elif port == "retrieval":
            runs.append(["retrieval_contract.py", "--base-url", url, "--org", a.org]
                        + (["--token", token] if token else []) + (["--no-live"] if a.no_live else []))
        elif port == "research":
            runs.append(["research_contract.py", "--base-url", url, "--no-cap-check"]
                        + (["--token", token] if token else []) + (["--no-live"] if a.no_live else []))
        elif port == "model":
            for api in a.model_api.split(","):
                runs.append(["model_contract.py", "--base-url", url, "--api", api, "--model", a.model,
                             "--api-key", a.model_key or token or "dummy"]
                            + (["--no-live"] if a.no_live else []) + (["--no-image"] if a.no_image else [])
                            + (["--expect-refusals"] if a.expect_refusals else []))
        for cmd in runs:
            print(f"\n=== {' '.join(cmd[:1] + cmd[1:3])} ===", flush=True)
            rc = subprocess.run([sys.executable, str(HERE / cmd[0]), *cmd[1:]]).returncode
            if rc:
                failed.append(cmd[0] + (f" ({cmd[cmd.index('--api') + 1]})" if "--api" in cmd else ""))
    print("\nall suites passed" if not failed else f"\nFAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
