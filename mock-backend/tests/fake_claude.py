#!/usr/bin/env python3
"""A stand-in `claude` binary for the offline tests (MOCK_CLAUDE_BIN). It speaks
just enough of `claude -p --output-format json|stream-json` to exercise every
family of the mock backend without spending anything.

Which family is calling is read from the flags, as the real CLI would see them:
  --tools WebSearch WebFetch     -> research: sources chosen by a MODE:<name> marker in the prompt
  --json-schema with `picks`     -> retrieval: picks built from the <section> blocks in the prompt
  otherwise                      -> model: echoes {system, prompt, model, images} as the result

Environment (the test passes it through the mock server):
  FAKE_CLAUDE_MODE    model: ok | unauth | garbage | struct_missing | overloaded
  FAKE_RETRIEVAL      retrieval: ok | fail | empty | invent
  FAKE_CLAUDE_SLEEP   seconds to sleep before answering
  FAKE_CLAUDE_LOG     a directory; each run writes <pid>.json (argv, times, cwd contents, images)
"""

import json
import os
import re
import sys
import time

argv = sys.argv[1:]
start = time.time()
raw = sys.stdin.read()


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def multi(name):
    if name not in argv:
        return []
    out = []
    for a in argv[argv.index(name) + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


stream_in = flag("--input-format") == "stream-json"
stream_out = flag("--output-format") == "stream-json"
images = 0
prompt = raw
if stream_in:
    msg = json.loads(raw.strip().splitlines()[0])
    content = msg["message"]["content"]
    images = sum(1 for b in content if b.get("type") == "image")
    prompt = "\n".join(b["text"] for b in content if b.get("type") == "text")
system = None
if "--system-prompt-file" in argv:
    with open(flag("--system-prompt-file"), encoding="utf-8") as f:
        system = f.read()
schema = json.loads(flag("--json-schema")) if "--json-schema" in argv else None
tools = multi("--tools")

time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))

log_dir = os.environ.get("FAKE_CLAUDE_LOG")
if log_dir:
    with open(os.path.join(log_dir, f"{os.getpid()}.json"), "w") as f:
        json.dump({"start": start, "end": time.time(), "argv": argv, "cwd": os.getcwd(),
                   "cwd_entries": os.listdir(os.getcwd()), "images": images,
                   "env_base_url": os.environ.get("ANTHROPIC_BASE_URL")}, f)

base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "api_error_status": None,
        "total_cost_usd": 0, "duration_ms": 1,
        "usage": {"input_tokens": 11, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7,
                  "output_tokens": 3}}


def emit(env, code=0):
    if stream_out:
        print(json.dumps({"type": "system", "subtype": "init"}))
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}))
    print(json.dumps(env))
    sys.exit(code)


# ------------------------------------------------------------------ research
if "WebSearch" in tools:
    if "WebFetch" not in tools or schema is None or "--allowedTools" not in argv:
        print("fake claude: research without its flags", argv, file=sys.stderr)
        sys.exit(3)
    mode = prompt.split("MODE:", 1)[1].split()[0] if "MODE:" in prompt else "ok"
    good = [
        {"url": "https://www.cso.ie/en/stats/", "publisher": "Central Statistics Office", "title": "Vehicles Licensed",
         "published_at": "2026-08-01",
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
    ] + [{"url": f"https://example.org/p{i}", "publisher": "Ex", "title": f"P{i}", "published_at": "May 2026",
          "excerpts": [{"quote": f"quote {i}"}]} for i in range(10)]
    if mode == "ok":
        emit({**base, "result": "x", "structured_output": {"sources": good, "queries": ["ireland ev registrations",
                                                                                        5, ""]}})
    if mode == "empty":
        emit({**base, "result": "x", "structured_output": {"sources": [], "queries": ["nothing here"]}})
    if mode == "exit":
        print("boom", file=sys.stderr)
        sys.exit(1)
    if mode == "is_error":
        emit({**base, "subtype": "error", "is_error": True, "result": "x", "structured_output": None}, 1)
    if mode == "garbage":
        print("this is not json")
        sys.exit(0)
    if mode == "nostructured":
        emit({**base, "result": "x"})
    if mode == "sleep":
        time.sleep(30)
    sys.exit(4)

# ------------------------------------------------------------------ retrieval
if schema is not None and "picks" in schema.get("properties", {}):
    mode = os.environ.get("FAKE_RETRIEVAL", "ok")
    if mode == "fail":
        emit({**base, "subtype": "error_during_execution", "is_error": True, "result": "failed"}, 1)
    sections = re.findall(r'<section index="(\d+)"[^>]*>\n(.*?)\n</section>', prompt, re.S)
    picks = []
    if mode == "invent":
        picks = [{"section": 0, "quote": "A sentence that is in no pack file at all."}]
    elif mode == "ok":
        for idx, text in sections:
            first = next((ln for ln in text.splitlines() if ln.strip()), "")
            picks.append({"section": int(idx), "quote": first})
        if sections:
            idx, text = sections[0]
            first = next((ln for ln in text.splitlines() if ln.strip()), "")
            picks.insert(1, {"section": int(idx), "quote": first})                 # a duplicate
            picks.insert(1, {"section": int(idx), "quote": "  ".join(first.split()) + "  "})  # whitespace mangled
            picks.append({"section": int(idx), "quote": "Not in the section, invented."})
            picks.append({"section": 9999, "quote": first})
    emit({**base, "result": json.dumps({"picks": picks}), "structured_output": {"picks": picks}})

# ------------------------------------------------------------------ model
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "garbage":
    print("this is not json")
    sys.exit(2)
if mode == "unauth":
    emit({**base, "is_error": True, "result": "Not logged in · Please run /login",
          "usage": {"input_tokens": 0, "output_tokens": 0}}, 1)
if mode == "overloaded":
    emit({**base, "is_error": True, "result": "Overloaded", "api_error_status": 529}, 1)
base["result"] = json.dumps({"system": system, "prompt": prompt, "model": flag("--model"), "images": images})
if schema is not None and mode != "struct_missing":
    base["structured_output"] = {k: "x" for k in schema.get("required", [])}
emit(base)
