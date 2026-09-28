"""correct_fact@1 — a person corrected a fact: write their value to the agency's knowledge.

The host asks this before it records the correction (Contract 4 §4, "truth verdicts
reach the fact"): a `human:<id>` source at tier `reviewer-verified` (or the link the
person gave), and a new row for the same fact — entity, key, market — with the
person's value, true as of today, under the host's decision id. The layer's own rule
decides what happens to the old row (a later as_of supersedes it). The answer is the
pin and the source's record; the host pins the new row, keeps the old pin marked
replaced, and records the decision. Nothing here writes the document.
"""

from __future__ import annotations

import re

from ..doc import ctx_facts
from ..layers import origin_uri
from ..rules.confidence import fact_confidence
from ..rules.tiering import registrable
from ..util import TaskError, bad, iso, today

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "correct_fact", "correct_fact", "1.0", "short", 1

_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "bn": 1e9, "b": 1e9, "billion": 1e9}


def parse_value(raw, unit: str):
    """The person's entry, in the fact's unit: "23%" of a proportion is 0.23,
    "€4.2m" is 4200000, a text fact keeps its text. Raises 400 when a number
    was needed and none was given."""
    if unit in ("text", "code", "date"):
        s = str(raw).strip()
        if not s:
            raise bad("the corrected value is empty")
        return s
    if unit == "boolean":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("yes", "true", "1"):
            return True
        if s in ("no", "false", "0"):
            return False
        raise bad("this fact is yes or no")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n, pct = float(raw), False
    else:
        s = str(raw).strip().lower().replace(",", "")
        m = re.fullmatch(r"[^\d\-.]*(-?\d+(?:\.\d+)?)\s*(%|per ?cent|percent|k|thousand|mn|m|million|bn|b|billion)?"
                         r"\s*[a-z€£$]*", s)
        if not m:
            raise bad(f"“{raw}” is not a number")
        n, suf = float(m.group(1)), m.group(2) or ""
        pct = suf in ("%", "per cent", "percent", "percent")
        n *= _MULT.get(suf, 1)
    if unit == "proportion":
        n = n / 100 if (pct or n > 1) else n
        if not 0 <= n <= 1:
            raise bad("a share is between 0% and 100%")
    return int(n) if n.is_integer() and unit in ("count", "units") else n


def run(req, caps):
    inp = req.inp if isinstance(req.inp, dict) else {}
    fid, who, did = inp.get("fact"), inp.get("by"), inp.get("decision_id")
    if not isinstance(who, str) or not re.fullmatch(r"human:\S+", who):
        raise bad("input.by must be the person correcting, human:<id>")
    if not isinstance(did, str) or not re.fullmatch(r"d_[0-9A-Z]{6,}", did):
        raise bad("input.decision_id must be the d_ id the host will record")
    old = next((f for f in ctx_facts(req.clan) if f.get("id") == fid), None)
    if old is None:
        raise TaskError(404, "unknown_fact", f"fact {fid} is not in this document")
    note = str(inp.get("note") or "").strip()
    if not note:
        raise bad("input.note is required: say why the value is wrong")
    value = parse_value(inp.get("value"), old.get("unit") or "text")
    if value == old.get("value"):
        raise TaskError(409, "no_change", "that is the value the fact already holds")

    link = str(inp.get("source_uri") or "").strip()
    uri = link if re.match(r"https?://", link) else who
    rec = {"uri": uri, "tier": "reviewer-verified", "domain": registrable(uri) if uri.startswith("http") else who,
           "licence": old.get("licence") or "open", "title": "corrected in the document",
           "publisher": who[len("human:"):], "retrieved_at": today()}
    src = caps.layers.add_source(rec)
    decision = {"id": did, "kind": "edit", "handler": req.handler, "action": "correct_fact",
                "rationale": f"{who} corrected {old.get('key')}: {note}", "cites": [fid]}
    learned = today()
    as_of = max(learned, str(old.get("as_of") or learned))
    row = caps.layers.append({"layer": old.get("layer") or "category", "entity": old["entity"], "key": old["key"],
                              "market": old.get("market"), "value": value, "unit": old.get("unit"),
                              "as_of": as_of, "retrieved_at": learned, "sources": [src],
                              "quotes": {src: note},
                              "licence": old.get("licence") or "open", "method": "report"}, decision)
    recs = row.get("source_records") or [dict(rec, id=src)]
    pin = {"id": row["id"], "entity": row["entity"], "key": row["key"], "value": row["value"], "unit": row["unit"],
           "as_of": row["as_of"], "retrieved_at": row["retrieved_at"], "sources": [src],
           "quotes": {src: note},
           "confidence": fact_confidence(recs), "licence": row.get("licence") or old.get("licence") or "open",
           "status": "contested" if row.get("status") == "contested" else "active",
           "version": row["version"], "supersedes": row.get("supersedes"),
           "origin": origin_uri(row["layer"], row["entity"], row["key"], row["version"]), "decision": did,
           "pinned_at": iso(), "pin_reason": f"corrected by {who}", "layer": row["layer"],
           "method": row.get("method") or "report"}
    if row.get("market"):
        pin["market"] = row["market"]
    source_record = {"id": src, **{k: v for k, v in rec.items() if v not in (None, "")}}
    result = {"summary": f"{fid} corrected to {value} by {who}; the old value stays on record.",
              "pin": pin, "source_record": source_record}
    return result, None, []
