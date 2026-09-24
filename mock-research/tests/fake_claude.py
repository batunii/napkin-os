#!/usr/bin/env python3
"""A stand-in `claude` binary for the offline tests.

Reads the prompt on stdin and picks its behaviour from a MODE:<name> marker in
the research question, so each test request drives one failure or one shape.
"""
import json
import os
import sys
import time

prompt = sys.stdin.read()
args = sys.argv[1:]
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write("call\n")
mode = prompt.split("MODE:", 1)[1].split()[0] if "MODE:" in prompt else "ok"

if "--json-schema" not in args or "WebSearch" not in args or "WebFetch" not in args:
    print("fake claude: missing expected flags", args, file=sys.stderr)
    sys.exit(3)


def envelope(structured, is_error=False):
    return json.dumps({"type": "result", "subtype": "error" if is_error else "success",
                       "is_error": is_error, "result": "x", "total_cost_usd": 0,
                       "duration_ms": 1, "structured_output": structured})


GOOD = [
    {"url": "https://www.cso.ie/en/stats/", "publisher": "Central Statistics Office",
     "title": "Vehicles Licensed", "published_at": "2026-08-01",
     "excerpts": [{"quote": "  1,234 new electric cars were licensed.  "}, {"quote": ""}]},
    {"url": "https://WWW.CSO.IE/en/stats/#frag", "publisher": "CSO", "title": "dup",
     "excerpts": [{"quote": "A second quote from the same page."}]},
    {"url": "ftp://example.com/x", "publisher": "x", "title": "x", "excerpts": [{"quote": "q"}]},
    {"url": "not a url", "publisher": "x", "title": "x", "excerpts": [{"quote": "q"}]},
    {"url": "https://example.com/noquotes", "publisher": "x", "title": "x", "excerpts": []},
    {"url": "https://example.com/notitle", "publisher": "x", "title": "", "excerpts": [{"quote": "q"}]},
    {"url": "https://www.simi.ie/en/news", "publisher": "", "title": "SIMI registrations",
     "published_at": "2099-01-01", "excerpts": [{"quote": "Registrations rose 5%."}]},
    "garbage",
] + [{"url": f"https://example.org/p{i}", "publisher": "Ex", "title": f"P{i}",
      "published_at": "May 2026", "excerpts": [{"quote": f"quote {i}"}]} for i in range(10)]

if mode == "ok":
    print(envelope({"sources": GOOD, "queries": ["ireland ev registrations", 5, ""]}))
elif mode == "empty":
    print(envelope({"sources": [], "queries": ["nothing here"]}))
elif mode == "exit":
    print("boom", file=sys.stderr)
    sys.exit(1)
elif mode == "is_error":
    print(envelope(None, is_error=True))
elif mode == "garbage":
    print("this is not json")
elif mode == "nostructured":
    print(envelope(None))
elif mode == "sleep":
    time.sleep(30)
