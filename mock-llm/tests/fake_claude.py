#!/usr/bin/env python3
"""A stand-in `claude` binary for the offline tests (MOCK_LLM_CLAUDE_BIN).

It speaks just enough of `claude -p --output-format json` to exercise the
server: the envelope shape, errors, slowness and concurrency. Behaviour is set
by env vars the test passes to the mock server:

  FAKE_CLAUDE_MODE   ok (default) | unauth | garbage | struct_missing
  FAKE_CLAUDE_SLEEP  seconds to sleep before answering (default 0)
  FAKE_CLAUDE_LOG    directory; each run writes <pid>.json with start/end
                     times and argv, so a test can measure overlap
"""

import json
import os
import sys
import time

argv = sys.argv[1:]
start = time.time()
prompt = sys.stdin.read()
system = None
if "--system-prompt-file" in argv:
    with open(argv[argv.index("--system-prompt-file") + 1], encoding="utf-8") as f:
        system = f.read()
elif "--system-prompt" in argv:
    system = argv[argv.index("--system-prompt") + 1]
schema = json.loads(argv[argv.index("--json-schema") + 1]) if "--json-schema" in argv else None

time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

log_dir = os.environ.get("FAKE_CLAUDE_LOG")
if log_dir:
    with open(os.path.join(log_dir, f"{os.getpid()}.json"), "w") as f:
        json.dump({"start": start, "end": time.time(), "argv": argv, "cwd": os.getcwd(),
                   "cwd_entries": os.listdir(os.getcwd())}, f)

base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "api_error_status": None,
        "usage": {"input_tokens": 11, "cache_creation_input_tokens": 5,
                  "cache_read_input_tokens": 7, "output_tokens": 3}}

if mode == "garbage":
    print("this is not json")
    sys.exit(2)
if mode == "unauth":
    base.update(is_error=True, result="Not logged in · Please run /login",
                usage={"input_tokens": 0, "output_tokens": 0})
    print(json.dumps(base))
    sys.exit(1)

base["result"] = json.dumps({"system": system, "prompt": prompt, "model": argv[argv.index("--model") + 1]})
if schema is not None and mode != "struct_missing":
    base["structured_output"] = {k: "x" for k in schema.get("required", [])}
print(json.dumps(base))
