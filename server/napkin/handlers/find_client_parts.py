"""find_client_parts@1 — Ellis suggests which parts a client's answer was about.

The host asks this inside `POST /client-review` (Contract 4 §7.5, §8.2) when the
client answered the whole document `accepted_with_changes` or `rejected`, no part
was marked, and there is proof text. One model call over the input alone — the
answer, the proof, the app's parts — on the capture view (model and job only: no
retrieval, research or layers). The handler reads nothing of the document the host
sends for transport.

What the model says is checked here and never trusted (middleware-api.md §11
item 4): a suggestion is dropped when its address is not an input part, its answer
is not one of the three, its quote is empty, over 500 characters or not in the
proof, or an earlier suggestion took its address. "In the proof" is exact after
whitespace collapsing only; the quote kept is the proof's own characters. The host
checks again before it records anything, and its check is the one that counts.

It writes nothing: the reply's change is null, and the client's words enter no
layer, source or index.
"""

from __future__ import annotations

import json

from .. import client_review as prompt
from ..rules.quotes import verbatim_ws
from ..util import bad

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "find_client_parts", "find_client_parts", "1.0", "short", 1

MAX_PROOF = 24000  # the host's extraction limit
MAX_PARTS = 100
MAX_VALUE = 2000


def _value(v):
    """A part's value as plain text: a string as it is, anything else compact JSON."""
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return s[:MAX_VALUE]


def read_input(inp) -> tuple[str, str, list[dict]]:
    """-> (answer, proof, parts), or 400 invalid_input (§11 item 8)."""
    inp = inp if isinstance(inp, dict) else {}
    answer, proof, parts = inp.get("answer"), inp.get("proof"), inp.get("parts")
    if answer not in ("accepted_with_changes", "rejected"):
        raise bad("input.answer must be accepted_with_changes or rejected: an accepted document needs no parts")
    if not isinstance(proof, str) or not proof.strip():
        raise bad("input.proof is empty: there are no words to read")
    if len(proof) > MAX_PROOF:
        raise bad(f"input.proof is over {MAX_PROOF} characters")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise bad(f"input.parts must list 1 to {MAX_PARTS} parts")
    out, seen = [], set()
    for p in parts:
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k] for k in ("address", "label")):
            raise bad("every part needs an address and a label")
        if p["address"] in seen:
            raise bad("two parts share one address")
        seen.add(p["address"])
        out.append({"address": p["address"], "label": p["label"], "value": _value(p.get("value"))})
    return answer, proof, out


def check(raw, proof: str, addresses: set[str]) -> tuple[list[dict], int]:
    """The model's suggestions -> (the ones that hold, how many were dropped).
    Each kept one is exactly {address, answer, quote}."""
    kept, taken, dropped = [], set(), 0
    for s in raw if isinstance(raw, list) else []:
        s = s if isinstance(s, dict) else {}
        addr, ans = s.get("address"), s.get("answer")
        quote = verbatim_ws(proof, s.get("quote"))
        if addr not in addresses or addr in taken or ans not in prompt.ANSWERS \
                or not quote or len(quote) > prompt.MAX_QUOTE:
            dropped += 1
            continue
        taken.add(addr)
        kept.append({"address": addr, "answer": ans, "quote": quote})
    return kept, dropped


def run(req, caps):
    """-> (result, change, hits). The change is always None."""
    answer, proof, parts = read_input(req.inp)
    view = caps.capture_view()
    raw = view.model.structured(prompt.PURPOSE, prompt.SYSTEM, prompt.payload(answer, proof, parts), prompt.SCHEMA,
                                max_tokens=prompt.MAX_TOKENS)
    kept, dropped = check(raw.get("suggestions"), proof, {p["address"] for p in parts})
    n = len(kept)
    summary = (f"{n} part{'s' if n != 1 else ''} suggested" if n else "no part suggested") + \
              (f"; {dropped} dropped: not in the proof or not a part" if dropped else "")
    return {"summary": summary, "suggestions": kept, "dropped": dropped}, None, []
