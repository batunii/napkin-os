#!/usr/bin/env python3
"""Checks a finished research job against its own rules, from what run_sample.py saved.

    python3 server/tools/invariants.py <run dir>

Reads `clan.json` (the document at the end), or `facts.json` + `outcome.json` for older runs, and the
run's layers database (`mock/layers.sqlite`). A violation means the job's output broke a rule the
middleware promises; an empty list means none of these broke. Not exhaustive: it covers the rules that
can be checked from saved output (the review's I2, I5, I7, I8, I9, I12, plus cite resolution).

The verbatim-quote rule (I1) needs the source excerpts, which are not saved with a finished job; it is
checked when a run is recorded, by the recorded extract_facts payloads (see extraction_eval.py).
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
from napkin.rules.confidence import fact_confidence  # noqa: E402
from napkin.rules.figures import quote_supports  # noqa: E402

LENSES = ["market_structure", "brands_positioning", "consumer_culture", "category_codes", "rhythm_moments",
          "media_spend", "regulation_clearance", "effectiveness_evidence"]
REASONED = {"pin", "contest", "finding", "verdict"}


def load(run: Path):
    if (run / "clan.json").is_file():
        c = json.loads((run / "clan.json").read_text())
        return c.get("facts") or [], c.get("findings") or [], c.get("data") or {}, \
            (c.get("decision_chain") or {}).get("decisions") or []
    facts = json.loads((run / "facts.json").read_text())
    out = json.loads((run / "outcome.json").read_text()) if (run / "outcome.json").is_file() else {}
    return facts, out.get("findings") or [], {"selection": out.get("selection") or {}, "report": out.get("report") or {}}, []


def check(run: Path) -> list[dict]:
    facts, findings, data, chain = load(run)
    v = []
    db = None
    for cand in (run / "mock" / "layers.sqlite",):
        if cand.is_file():
            db = sqlite3.connect(cand)
            db.row_factory = sqlite3.Row
    fact_ids = {f["id"] for f in facts}
    finding_ids = {f["id"] for f in findings}

    for f in facts:
        quotes = list((f.get("quotes") or {}).values())
        if not quotes or not any(quote_supports(f["value"], f.get("unit"), q) for q in quotes):
            v.append({"id": "I2", "fact": f["id"], "msg": f"no quote supports {f['key']} = {f['value']!r} ({f.get('unit')})"})
        if set(f.get("quotes") or {}) - set(f.get("sources") or []):
            v.append({"id": "I5", "fact": f["id"], "msg": "a quote names a source the fact does not list"})
    if db is not None:
        have = {r["id"] for r in db.execute("select id from sources")}
        for f in facts:
            for s in f.get("sources") or []:
                if s not in have:
                    v.append({"id": "I5", "fact": f["id"], "msg": f"source {s} is not in the layers database"})
            rows = db.execute("select s.tier, s.domain, s.id from fact_sources fs join sources s on s.id = fs.source_id "
                              "where fs.fact_id = ?", (f["id"],)).fetchall() if "fact_id" in \
                [c[1] for c in db.execute("pragma table_info(fact_sources)")] else []
            if rows:
                got = fact_confidence([{"tier": r["tier"], "domain": r["domain"], "id": r["id"]} for r in rows])
                if got != f.get("confidence"):
                    v.append({"id": "I8", "fact": f["id"], "msg": f"stored confidence {f.get('confidence')} but the sources give {got}"})
            row = db.execute("select 1 from facts where id = ?", (f["id"],)).fetchone()
            if row is None:
                v.append({"id": "I12", "fact": f["id"], "msg": "the pin has no row in the layers database"})
    for d in chain:
        if d.get("kind") in REASONED:
            r = d.get("reasoning")
            if not isinstance(r, dict) or not str(r.get("decided") or "").strip():
                v.append({"id": "I7", "decision": d.get("id"), "msg": f"{d.get('kind')} decision without reasoning"})
    sel = data.get("selection") or {}
    run_pairs = {(x.get("lens"), x.get("market")) for x in sel.get("lenses_run") or []}
    skipped = sel.get("lenses_skipped") or []
    markets = sorted({m for _, m in run_pairs if m})
    camp = (data.get("campaign") or {}).get("markets") or {}
    if isinstance(camp, dict) and camp.get("value"):
        markets = list(camp["value"])
    for lens in LENSES:
        for m in markets:
            in_skip = any(s.get("lens") == lens and s.get("market") in (None, m) for s in skipped)
            if (lens, m) not in run_pairs and not in_skip:
                v.append({"id": "I9", "msg": f"{lens}/{m} is neither run nor skipped"})
    rep = data.get("report") or {}
    for sec in rep.get("sections") or []:
        for b in sec.get("blocks") or []:
            for c in b.get("cites") or []:
                if c not in fact_ids and c not in finding_ids:
                    v.append({"id": "CITE", "msg": f"report cites {c}, which the document does not hold"})
    return v


def summarise(v: list[dict]) -> dict:
    out: dict = {}
    for x in v:
        out[x["id"]] = out.get(x["id"], 0) + 1
    return out


if __name__ == "__main__":
    vs = check(Path(sys.argv[1]))
    print(json.dumps({"violations": len(vs), "by_rule": summarise(vs), "first": vs[:10]}, indent=1))
