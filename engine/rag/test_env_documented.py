"""Every environment variable the engine's code reads is documented in engine/README.md,
engine/rag/README.md or engine/.env.example (audit C5: 77 variables, 13 documented
nowhere). A new os.environ.get("X") without a row fails here.
Run: cd engine/rag && python3 -m pytest -q test_env_documented.py
"""
from __future__ import annotations

import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
READ = re.compile(r"""(?:os\.environ\.get|os\.getenv|environ\.get|env\.get|os\.environ\[|os\.environ\.setdefault|_setting\([^,]+,\s*env,)\s*\(?\s*["']([A-Z][A-Z0-9_]{2,})["']""")
PATTERNS = {"BRIEF_ROUTE_": "BRIEF_ROUTE_<JOB>", "BRIEF_EFFORT_": "BRIEF_EFFORT_<JOB>"}    # documented as families


def code_vars() -> dict:
    """{variable: [files]} for every variable read outside tests and outputs."""
    out: dict = {}
    for f in ENGINE.rglob("*.py"):
        if {"outputs", "__pycache__"} & set(f.parts) or f.name.startswith("test_") or f.name == "conftest.py":
            continue
        for m in READ.finditer(f.read_text(errors="replace")):
            out.setdefault(m.group(1), []).append(str(f.relative_to(ENGINE)))
    return out


def test_every_variable_the_code_reads_is_documented():
    """No variable is read without a row in the READMEs or .env.example."""
    found = code_vars()
    assert len(found) > 50, f"the pattern found only {len(found)} variables: it is broken, not the docs"
    docs = " ".join((ENGINE / p).read_text() for p in ("README.md", "rag/README.md", ".env.example")
                    if (ENGINE / p).exists())
    missing = {v: f for v, f in found.items()
               if v not in docs and not any(v.startswith(k) and doc in docs for k, doc in PATTERNS.items())}
    assert not missing, f"undocumented environment variables: {missing}"
