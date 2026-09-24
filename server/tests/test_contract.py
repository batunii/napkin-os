"""The swap guarantee: mock-middleware/contract_test.py, unchanged, against
this middleware with fake components."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_contract_suite_passes_with_fake_components(server):
    r = subprocess.run([sys.executable, str(REPO / "mock-middleware" / "contract_test.py"), "--base-url", server.url,
                        "--schema-dir", str(REPO / "app" / "templates" / "campaign-research"), "--job-timeout", "60"],
                       capture_output=True, text=True, timeout=600)
    print(r.stdout[-6000:], r.stderr[-3000:])
    assert r.returncode == 0, r.stdout[-6000:]
