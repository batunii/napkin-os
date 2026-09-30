"""Seed the category tree: research every leaf x lens for a market, into
napkin_category, as facts and dossier cells (Planner Research Taxonomy).

    python -m napkin.seed.run --dsn "$NAPKIN_CATEGORY_DSN" --run-dir runs/ie-pilot --market IE \\
        --verticals alcohol soft_drinks retail [--leaves alcohol.cider ...] [--concurrency 4]

Pause with `touch <run-dir>/PAUSE` (remove it to go on); stop with Ctrl-C or
`touch <run-dir>/STOP`; resume by running the same command again. Limits and
timeouts are waited out, never failed (control.py).

The research and model ports come from the same environment the middleware
reads (NAPKIN_RESEARCH_URL, NAPKIN_MODEL_*), so any napkin.research/1 service or a
production service is a configuration swap. Per unit (leaf, lens):

  1. research: one targeted search per group of the lens's measures (the
     measure list), naming the sources to try first for the market;
  2. check each page ourselves (pagecheck): a passage the page does not hold
     is dropped; the publication date comes from the page's metadata;
  3. extract facts (one model call) against the measure list: value, unit,
     qualifier, period range, the market it describes and how we know, each
     with its exact quote; a measure the list lacks is proposed, not invented;
  4. the rules check each fact (quote verbatim in its passage, number in its
     quote, a known market code) and layers.append_fact applies the dating rule;
  4b. corroborate: up to three single-source figures are searched for again,
     asking for another publisher; what comes back is appended like any fact;
  5. write the cell: a summary in our words citing only facts and passages it
     was given, every figure traceable to one of them, stored unverified
     (author agent). The database derives its confidence from the sources.

A cell that is already fresh is skipped and every call is cached in the run
directory, so a stopped run resumes where it stopped without paying twice.
One JSON line per unit goes to <run-dir>/units.jsonl; progress to
<run-dir>/status.json; a summary prints at the end.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from ..config import Settings
from ..doc import LENS_NAMESPACE, LENSES, market_list
from ..model import ModelPort, Usage, build_wire
from ..pipeline.research import LENS_QUESTIONS, UNITS
from ..research import ResearchPort
from ..rules.figures import NUM, quote_supports
from ..rules.quotes import find_quote
from ..rules.tiering import registrable, tier_for
from . import pagecheck
from .control import Control, GaveUp, Stopped
from .store import Store

HANDLER = "seed_category@1"
ORG = "org/napkin"
REPO = Path(__file__).resolve().parents[3]
GEO = json.loads((REPO / "layers-service" / "data" / "geographies.json").read_text())
MARKETS = set(GEO["countries"]) | set(GEO["regions"]) | {GEO["global"], GEO["unknown"]}
MEASURE_LIST = json.loads((REPO / "layers-service" / "data" / "measures.json").read_text())
PREFERRED = json.loads((Path(__file__).parent / "data" / "preferred_sources.json").read_text())
CORRECTIONS = json.loads((Path(__file__).parent / "data" / "corrections.json").read_text())
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_IN_TEXT = re.compile(r"\b(?:f|exc|src|cap)_[0-9a-z]{6,}\b")
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
          r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
# Dates are not figures: "13 May 2026", "May 13", "Q3 2025", "2025/26".
DATE_IN_TEXT = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\b|\b{_MONTH}\s+\d{{1,2}}\b|\bQ[1-4]\b|"
                          r"\b(?:19|20)\d\d(?:/\d\d)?\b")

EXTRACT_SYSTEM = """You extract facts for an advertising agency's shared CATEGORY research layer.
You are given ONE category leaf, ONE research lens with its standing question, the target market,
and sources whose passages were checked against the live page. Extract only facts a passage states.

Each fact is one of the listed `measures`:
- key_suffix: the measure's key_suffix, exactly. Use "other" only for something important that no listed
  measure covers, and give it a snake_case name in proposed_key (it is sent to a planner, not stored
  as a fact). Otherwise proposed_key is null.
- qualifier: when the measure's qualifier is not "none", what this row is about, as a short name: the player
  or brand ("Aldi"), channel ("digital", "on-trade"), segment ("18-34"), moment ("Christmas"), the statement
  agreed with, the rule, the body ("ASAI"), the wider market ("alcohol market"), the metric ("top 5") or the
  campaign. null when the measure's qualifier is "none".
- A measure whose cardinality is "many" is a list: give each item as its own fact.
- value: a number (value_number), a short text (value_text, <= 200 chars), or a yes/no (value_boolean).
  A percentage is a proportion between 0 and 1. Money is a plain number in the currency stated.
- unit: one of the measure's units.
- period_start / period_end (YYYY-MM-DD): the time the figure DESCRIBES, as a range.
  "2025" -> 2025-01-01..2025-12-31; "Q3 2025" -> 2025-07-01..2025-09-30; "March 2026" -> 2026-03-01..2026-03-31;
  "the year to June 2025" -> 2024-07-01..2025-06-30. Both null when the passage does not say what time it describes.
  The page's publication date is NOT the period.
- period_basis: "stated" when the passage itself names the period ("in 2025", "Q3 2025", "2024/25"),
  "inferred" when you worked it out from context (a comparison such as "compared to 2024", the publication
  date, the page around the passage). null when the period is null.
- market: the place the fact describes: an ISO 3166-1 alpha-2 country code (the UK is GB, Ireland is IE),
  EU for the European Union as a whole, GLOBAL for worldwide, UNKNOWN when the passage does not establish it.
  A figure for another country is that country's fact, not the target market's.
- market_basis: "stated" (the passage or its page names the place), "inferred" (e.g. from an Irish statistics
  office publishing it), or "unknown" (only with market UNKNOWN).
- evidence: the source_id, the excerpt_index, and the quote copied character for character from that passage.
Never compute, convert, or use outside knowledge. Qualitative lenses (codes, culture, positioning) produce
short text facts, one idea each. When `focus` is given, extract only what it names."""

CELL_SYSTEM = """You write ONE cell of a planner's category dossier: one leaf, one research lens, one market.
Answer the lens's standing question for this leaf and market in 2-5 plain sentences (at most 900 characters),
in the research team's own words, for a strategist. Use ONLY the facts and passages given, and list every one
you use in cited_ids. Never write an id in the summary itself: it is read by people. Every number you write
must appear in a fact or passage you cite. Never calculate a number yourself: no sums, shares, ratios or
percentages of your own. Say plainly what is thin
or missing. Name the period and the place of any figure (e.g. "in Ireland in 2025", "across the EU"); when a
fact's period_basis is "inferred", say the period is implied, not stated.
change_note: what changed compared with previous_summary, in one or two sentences; "First version." when
previous_summary is null. If nothing usable was given, set summary to null."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
CELL_SCHEMA = _obj({"summary": NULLABLE_STR, "change_note": {"type": "string"},
                    "cited_ids": {"type": "array", "items": {"type": "string"}}})


def _h(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


class Seeder:
    def __init__(self, store: Store, model: ModelPort, research: ResearchPort, market: str, leaf_names: dict,
                 regulated: dict, log_path: Path | None, control: Control, max_sources: int = 8,
                 measures: dict | None = None, groups: dict | None = None, preferred: dict | None = None):
        self.store, self.model, self.research, self.market = store, model, research, market
        self.control = control
        self.measures = measures or {}          # lens -> [measure rows], from layers.measures
        self.groups = groups or {}              # lens -> {group: what that search looks for}
        self.preferred = preferred or {}        # lens -> source names to try first, for this market
        self.leaf_names, self.regulated, self.max_sources = leaf_names, regulated, max_sources
        self.usage = Usage()
        self.log_path = log_path
        self._log_lock = threading.Lock()
        self.http = httpx.Client(timeout=30.0)

    def log(self, row: dict):
        line = json.dumps(row, default=str)
        print(line, flush=True)
        if self.log_path:
            with self._log_lock, self.log_path.open("a") as fh:
                fh.write(line + "\n")

    def _model(self, purpose, system, payload, schema, max_tokens):
        return self.control.call(purpose, lambda: self.model.call(
            purpose, system, payload, schema, usage=self.usage, attribution=f"{HANDLER} {ORG}",
            max_tokens=max_tokens, headers={"X-Napkin-Handler": HANDLER, "X-Napkin-Job": "seed"}),
            cache_key=[purpose, system, payload, schema])

    def _page(self, url, quotes, published):
        """(capture or None, drop reasons), cached: a resumed run re-uses the same reading (same capture id)."""
        def fetch():
            cap, reasons = pagecheck.check(url, quotes, published, self.http)
            return {"capture": cap.__dict__ if cap else None, "reasons": reasons}
        d = self.control.call("page", fetch, cache_key=["v2", url, quotes, published])
        return (pagecheck.Capture(**d["capture"]) if d["capture"] else None), d["reasons"]

    # -- one leaf x lens ---------------------------------------------------------
    def _new_row(self, leaf, lens, phase):
        return {"leaf": leaf, "lens": lens, "market": self.market, "phase": phase, "searches": 0, "sources": 0,
                "verified_sources": 0, "passages_kept": 0, "passages_dropped": 0, "dropped_by_reason": {},
                "facts_proposed": 0, "facts_rejected": {}, "outcomes": {}, "measures_proposed": 0,
                "corroboration": {"attempted": 0, "corroborated": 0, "other": {}}, "cell": None, "error": None}

    def _guarded(self, row, work):
        """Run one unit's phase; a stop, a give-up or an error is recorded, never raised."""
        t0 = time.monotonic()
        try:
            self.control.wait_turn()
        except Stopped:
            row.update(cell="stopped before it started", left_for_next_run=True)
            self.log(row)
            return row
        conn = self.store.connect()
        try:
            work(conn)
        except Stopped:
            conn.rollback()
            row.update(cell="stopped mid-unit (resumes from the cache)", left_for_next_run=True)
        except GaveUp as e:
            conn.rollback()
            row.update(cell="waiting (a temporary failure outlasted every retry)", left_for_next_run=True,
                       gave_up=True, error=str(e)[:300])
        except Exception as e:
            conn.rollback()
            row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            conn.close()
            row["seconds"] = round(time.monotonic() - t0, 1)
            self.log(row)
        return row

    # -- phase A: facts ------------------------------------------------------------
    def unit_facts(self, leaf: str, lens: str) -> dict:
        row = self._new_row(leaf, lens, "facts")

        def work(conn):
            if self.store.cell_state(conn, leaf, lens, self.market)[0] == "fresh":
                row["cell"] = "skipped (fresh)"
                return
            question, name = LENS_QUESTIONS[lens][0], self.leaf_names[leaf]
            # 1 + 2. targeted searches, one per group of measures; every page checked
            srcs = self._discover(conn, leaf, lens, name, row)
            conn.commit()
            self.store.scope(conn)
            # 3 + 4. extract against the measure list; check; append
            self._save_srcs(leaf, lens, srcs)
            written = self._extract_write(conn, leaf, lens, name, question, srcs, self.measures[lens], row) \
                if srcs else []
            conn.commit()
            self.store.scope(conn)
            # 4b. corroborate single-source figures from a different publisher
            self._corroborate(conn, leaf, lens, name, question, written, row)
            conn.commit()
            row["cell"] = "facts written"
        return self._guarded(row, work)

    # -- phase C: the cell ------------------------------------------------------------
    def unit_cell(self, leaf: str, lens: str) -> dict:
        row = self._new_row(leaf, lens, "cell")

        def work(conn):
            state, prev_summary = self.store.cell_state(conn, leaf, lens, self.market)
            if state == "fresh":
                row["cell"] = "skipped (fresh)"
                return
            question, name = LENS_QUESTIONS[lens][0], self.leaf_names[leaf]
            srcs = self._load_srcs(leaf, lens)
            row["cell"] = self._cell(conn, leaf, lens, LENS_NAMESPACE[lens], name, question, prev_summary, srcs)
            conn.commit()
        return self._guarded(row, work)

    def _srcs_path(self, leaf, lens):
        return self.control.dir / "units" / f"{leaf}__{lens}__{self.market}.json"

    def _save_srcs(self, leaf, lens, srcs):
        path = self._srcs_path(leaf, lens)
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(srcs))
        tmp.replace(path)

    def _load_srcs(self, leaf, lens):
        try:
            return json.loads(self._srcs_path(leaf, lens).read_text())
        except FileNotFoundError:
            return []

    # kept for callers of the one-phase API (tests, a single unit by hand)
    def unit(self, leaf: str, lens: str) -> dict:
        self.unit_facts(leaf, lens)
        return self.unit_cell(leaf, lens)

    # -- research ----------------------------------------------------------------
    def _search(self, query: str, lens: str, leaf: str, max_sources: int) -> list[dict]:
        query = query[:500]
        res = self.control.call("research", lambda: self.research.search(
            query, lens, self.market, category=leaf, max_sources=max_sources,
            attribution={"handler": HANDLER, "job": "seed"}),
            cache_key=[query, lens, self.market, leaf, max_sources])
        return res["sources"]

    def _checked(self, conn, found: list[dict], row: dict, prefix: str) -> list[dict]:
        """Every page fetched again; only passages it really holds are kept."""
        srcs = []
        for s in found:
            cap, reasons = self._page(s["url"], list(s["excerpts"]), s.get("published_at"))
            for k, v in reasons.items():
                row["dropped_by_reason"][k] = row["dropped_by_reason"].get(k, 0) + v
                row["passages_dropped"] += v
            if cap is None:
                continue
            tier, domain = tier_for(cap.url), registrable(cap.url)
            ex_ids = self.store.write_capture(conn, cap.url, s.get("publisher"), tier, domain, cap)
            row["passages_kept"] += len(cap.excerpts)
            srcs.append({"sid": f"{prefix}{len(srcs)}", "url": cap.url, "publisher": s.get("publisher"),
                         "tier": tier, "published_at": cap.published_at, "excerpts": cap.excerpts,
                         "excerpt_ids": ex_ids})
        return srcs

    def _discover(self, conn, leaf, lens, name, row) -> list[dict]:
        """One targeted search per group of the lens's measures, naming the
        sources to try first; the results merged by URL."""
        prefer = self.preferred.get(lens) or []
        found: dict[str, dict] = {}
        for g, text in sorted(self.groups[lens].items()):
            q = (f"{name} in {market_list([self.market])} ({self.market}): {text}. Latest figures (2025 or 2026), "
                 f"with the year they describe." + (f" Try first: {', '.join(prefer[:6])}." if prefer else ""))
            row["searches"] += 1
            for s in self._search(q, lens, leaf, 6):
                if s["url"] in found:
                    have = found[s["url"]]["excerpts"]
                    have += [e for e in s["excerpts"] if e not in have]
                else:
                    found[s["url"]] = dict(s, excerpts=list(s["excerpts"]))
        row["sources"] = len(found)
        srcs = self._checked(conn, list(found.values()), row, "s")
        row["verified_sources"] = len(srcs)
        return srcs

    # -- facts -------------------------------------------------------------------
    def _extract_write(self, conn, leaf, lens, name, question, srcs, measures, row, focus=None) -> list[dict]:
        """Extract against the given measures, check, append. The written facts."""
        suffixes = [m["key"].split(".", 1)[1] for m in measures]
        schema = _obj({"facts": {"type": "array", "items": _obj({
            "key_suffix": {"type": "string", "enum": suffixes + ["other"]},
            "proposed_key": NULLABLE_STR,
            "qualifier": NULLABLE_STR,
            "value_number": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "value_text": NULLABLE_STR,
            "value_boolean": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            "unit": {"type": "string", "enum": UNITS},
            "period_start": NULLABLE_STR, "period_end": NULLABLE_STR,
            "period_basis": {"anyOf": [{"type": "string", "enum": ["stated", "inferred"]}, {"type": "null"}]},
            "market": {"type": "string"},
            "market_basis": {"type": "string", "enum": ["stated", "inferred", "unknown"]},
            "evidence": {"type": "array", "items": _obj({"source_id": {"type": "string"},
                                                         "excerpt_index": {"type": "integer"},
                                                         "quote": {"type": "string"}})},
        })}})
        payload = {"leaf": {"code": leaf, "name": name}, "lens": lens, "lens_question": question,
                   "target_market": self.market,
                   "measures": [{"key_suffix": m["key"].split(".", 1)[1], "units": m["units"],
                                 "cardinality": m["cardinality"], "qualifier": m["qualifier"],
                                 "definition": m["definition"]} for m in measures],
                   "sources": [{"source_id": s["sid"], "publisher": s["publisher"], "url": s["url"],
                                "published_at": s["published_at"],
                                "excerpts": [{"excerpt_index": i, "text": t} for i, t in enumerate(s["excerpts"])]}
                               for s in srcs]}
        if focus:
            payload["focus"] = focus
        raw = self._model("extract_category_facts", EXTRACT_SYSTEM, payload, schema, 8000)
        facts = raw.get("facts") or []
        row["facts_proposed"] += len(facts)
        by_sid = {s["sid"]: s for s in srcs}
        by_key = {m["key"]: m for m in measures}
        written = []
        for f in facts:
            if f.get("key_suffix") == "other":
                row["measures_proposed"] += self._propose(conn, leaf, lens, f, by_sid)
                continue
            checked = self._check(f, leaf, LENS_NAMESPACE[lens], by_sid, by_key)
            if isinstance(checked, str):
                row["facts_rejected"][checked] = row["facts_rejected"].get(checked, 0) + 1
                continue
            fact, evidence, publishers = checked
            decision = {"id": "d_" + _h(HANDLER, leaf, fact["key"], fact.get("qualifier"), fact["market"],
                                         fact["value"], fact.get("period_end"))[:24],
                        "kind": "pin", "handler": HANDLER, "action": "category_seed",
                        "rationale": f"{lens} for {name} ({self.market}): stated by "
                                     f"{len(evidence)} checked passage(s).",
                        "cites": [e["excerpt_id"] for e in evidence]}
            try:
                with conn.transaction():
                    out = self.store.append(conn, fact, evidence, decision)
            except Exception as e:  # the database refused it: counted, never fatal
                reason = "db:" + str(e).split("\n")[0][:80]
                row["facts_rejected"][reason] = row["facts_rejected"].get(reason, 0) + 1
                continue
            row["outcomes"][out["outcome"]] = row["outcomes"].get(out["outcome"], 0) + 1
            written.append({"fact": out["fact"], "outcome": out["outcome"], "measure": by_key[fact["key"]],
                            "qualifier": fact.get("qualifier"), "value": fact["value"], "unit": fact["unit"],
                            "market": fact["market"], "period_start": fact.get("period_start"),
                            "period_end": fact.get("period_end"), "publishers": publishers})
        return written

    def _propose(self, conn, leaf, lens, f, by_sid) -> int:
        """A measure the list lacks, with the passage that states it, for a planner to add or refuse."""
        name = re.sub(r"[^a-z0-9_]+", "_", str(f.get("proposed_key") or "").lower()).strip("_")
        market = str(f.get("market") or "").upper().replace("UK", "GB")
        value = f.get("value_number") if f.get("value_number") is not None else \
            f.get("value_text") if f.get("value_text") else f.get("value_boolean")
        if not name or market not in MARKETS or value is None:
            return 0
        for ev in f.get("evidence") or []:
            s = by_sid.get(ev.get("source_id"))
            i = ev.get("excerpt_index")
            if not s or not isinstance(i, int) or not 0 <= i < len(s["excerpts"]):
                continue
            span = find_quote(s["excerpts"][i], ev.get("quote", ""))
            if not span:
                continue
            key = f"{LENS_NAMESPACE[lens]}.{name}"
            with conn.transaction():
                self.store.propose(conn, "mp_" + _h(key, leaf, market, value, s["excerpt_ids"][i])[:24], lens, key,
                                   "category/" + leaf, market, str(value), str(f.get("unit") or "text"),
                                   s["excerpt_ids"][i], s["excerpts"][i][span[0]:span[1]])
            return 1
        return 0

    def _corroborate(self, conn, leaf, lens, name, question, written, row):
        """For up to three single-source figures, look for the same figure from
        another publisher. What comes back is appended like any fact: the same
        value corroborates, another value contests."""
        todo = [w for w in written if w["outcome"] in ("created", "superseded") and len(w["publishers"]) == 1
                and w["unit"] not in ("text", "boolean")][:3]
        for w in todo:
            m, v = w["measure"], w["value"]
            shown = f"{float(v) * 100:g}%" if w["unit"] == "proportion" else f"{v:,} {w['unit']}"
            when = f" for {w['period_start'][:4]}" if w["period_end"] else ""
            about = f" ({w['qualifier']})" if w["qualifier"] else ""
            q = (f"{name} in {market_list([w['market']] if w['market'] in GEO['countries'] else [self.market])}: "
                 f"{m['definition']}{about}{when}. One source states {shown}. Find a second, independent source, "
                 f"not {next(iter(w['publishers']))}, that states this figure.")
            row["searches"] += 1
            row["corroboration"]["attempted"] += 1
            srcs = self._checked(conn, self._search(q, lens, leaf, 4), row, f"c{row['corroboration']['attempted']}_")
            if not srcs:
                continue
            focus = (f"Extract only the measure {m['key'].split('.', 1)[1]}"
                     + (f" for qualifier '{w['qualifier']}'" if w["qualifier"] else "") + ".")
            before = dict(row["outcomes"])
            self._extract_write(conn, leaf, lens, name, question, srcs, [m], row, focus=focus)
            for k in row["outcomes"]:
                gained = row["outcomes"][k] - before.get(k, 0)
                if not gained:
                    continue
                if k == "corroborated":
                    row["corroboration"]["corroborated"] += gained
                else:
                    row["corroboration"]["other"][k] = row["corroboration"]["other"].get(k, 0) + gained

    def _check(self, f: dict, leaf: str, ns: str, by_sid: dict, by_key: dict):
        """(fact, evidence, publishers) or the reason it was rejected."""
        suffix = str(f.get("key_suffix") or "")
        key = f"{ns}.{suffix}"
        measure = by_key.get(key)
        if not measure:
            return "not a listed measure"
        unit = f.get("unit")
        if f.get("value_number") is not None:
            value = f["value_number"]
            value = int(value) if isinstance(value, float) and value.is_integer() else value
            # A share lies between 0 and 1; a growth rate can fall (-0.002) or more than double (1.5).
            if unit == "proportion" and "growth" not in key and not 0 <= value <= 1:
                return "proportion out of range"
        elif f.get("value_boolean") is not None:
            value, unit = bool(f["value_boolean"]), "boolean"
        elif isinstance(f.get("value_text"), str) and f["value_text"].strip():
            value, unit = f["value_text"].strip()[:200], "text" if "text" in measure["units"] else unit
            if unit in ("proportion", "eur", "gbp", "usd", "count", "units", "km", "kwh"):
                return "numeric unit without a number"
        else:
            return "no value"
        if unit not in measure["units"]:
            return "unit not allowed for the measure"
        qualifier = (f.get("qualifier") or "").strip()[:120] or None
        if (measure["qualifier"] != "none") != bool(qualifier):
            return "qualifier missing" if measure["qualifier"] != "none" else "qualifier not allowed"
        market, basis = str(f.get("market") or "").upper(), f.get("market_basis")
        if market == "UK":
            market = "GB"
        if market not in MARKETS:
            return "unknown market code"
        if (market == "UNKNOWN") != (basis == "unknown"):
            return "market and basis disagree"
        ps, pe = f.get("period_start"), f.get("period_end")
        if not (ps and pe and DAY.match(ps) and DAY.match(pe) and ps <= pe):
            ps = pe = None
        evidence, passages, publishers = [], [], set()
        for ev in f.get("evidence") or []:
            s = by_sid.get(ev.get("source_id"))
            i = ev.get("excerpt_index")
            if not s or not isinstance(i, int) or not 0 <= i < len(s["excerpts"]):
                continue
            text = s["excerpts"][i]
            span = find_quote(text, ev.get("quote", ""))
            if not span:
                continue
            quote = text[span[0]:span[1]]
            if not quote_supports(value, unit, quote):
                continue
            evidence.append({"excerpt_id": s["excerpt_ids"][i], "quote": quote, "quote_start": span[0]})
            passages.append(text)
            publishers.add(s["publisher"])
        if not evidence:
            return "quote not verbatim or does not state the value"
        # "stated" only when the period's year is in one of the fact's passages
        # (the database refuses it otherwise; here it is labelled honestly instead)
        period_basis = None
        if ps:
            year = re.compile(rf"(?<!\d){pe[:4]}(?!\d)")
            stated = f.get("period_basis") == "stated" and any(year.search(t) for t in passages)
            period_basis = "stated" if stated else "inferred"
        fact = {"id": "f_" + _h(HANDLER, leaf, key, qualifier, market, value, ps, pe, evidence[0]["excerpt_id"])[:24],
                "layer": "category", "entity": "category/" + leaf, "key": key, "market": market,
                "market_basis": basis, "value": value, "unit": unit, "licence": "open", "method": "report"}
        if qualifier:
            fact["qualifier"] = qualifier
        if ps:
            fact["period_start"], fact["period_end"], fact["period_basis"] = ps, pe, period_basis
        return fact, evidence, publishers

    def _numbers_in(self, item) -> set[str]:
        if "text" in item:
            return set(NUM.findall(item["text"]))
        got = set(NUM.findall(f"{item['value']} {item['quote'] or ''}"))
        if item["unit"] == "proportion":
            try:
                v = float(item["value"])
                got |= {f"{v * 100:g}", f"{round(v * 100)}"}
            except ValueError:
                pass
        return got

    def _judge_cell(self, out, known_facts, known_passages):
        """(None, summary, cited) when the summary may be stored, else (reason, …, …).
        Every figure must trace to a cited item; a figure found in an item the
        model was given but forgot to cite gets that item cited."""
        summary = ID_IN_TEXT.sub("", (out.get("summary") or ""))
        summary = re.sub(r"\(\s*[,;\s]*\)", "", summary)
        summary = re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", summary)).strip()
        if not summary:
            return "the model found nothing usable", None, []
        items = {**known_facts, **known_passages}
        cited = [c for c in dict.fromkeys(out.get("cited_ids") or []) if c in items]
        if not cited:
            return "it cited nothing it was given", None, []
        allowed = set().union(*(self._numbers_in(items[c]) for c in cited))
        stray = []
        for n in NUM.findall(DATE_IN_TEXT.sub(" ", summary)):
            if n in allowed:
                continue
            owner = next((i for i, it in items.items() if n in self._numbers_in(it)), None)
            if owner:
                cited.append(owner)
                allowed |= self._numbers_in(items[owner])
            else:
                stray.append(n)
        if stray:
            return f"figures in no fact or passage it was given: {', '.join(dict.fromkeys(stray))}", None, []
        return None, summary, cited

    def _cell(self, conn, leaf, lens, ns, name, question, prev_summary, srcs) -> str:
        facts = self.store.current_facts(conn, leaf, ns, self.market)
        passages = [{"id": eid, "publisher": s["publisher"], "published_at": s["published_at"], "text": t}
                    for s in srcs for eid, t in zip(s["excerpt_ids"], s["excerpts"])]
        if not facts and not passages:
            return "empty (nothing verified)"
        payload = {"leaf": {"code": leaf, "name": name}, "lens": lens, "lens_question": question,
                   "market": self.market, "previous_summary": prev_summary,
                   "facts": [{k: f[k] for k in ("id", "key", "qualifier", "value", "unit", "market", "period_start",
                                                "period_end", "period_basis", "status", "quote")} for f in facts],
                   "passages": passages}
        known_facts = {f["id"]: f for f in facts}
        known_passages = {p["id"]: p for p in passages}
        verdict = None
        for attempt in range(2):
            if attempt:
                payload = dict(payload, rejected_because=verdict)
            out = self._model("write_dossier_cell", CELL_SYSTEM, payload, CELL_SCHEMA, 2000)
            verdict, summary, cited = self._judge_cell(out, known_facts, known_passages)
            if verdict is None:
                break
        if verdict is not None:
            return f"rejected ({verdict})"
        r = self.store.confirm(conn, {
            "leaf": leaf, "lens": lens, "market": self.market, "summary": summary[:4000],
            "change_note": (out.get("change_note") or "First version.")[:2000],
            "author": f"agent:{HANDLER}",
            "citations": [{"fact_id": c} if c in known_facts else {"excerpt_id": c} for c in cited],
            "decision": {"id": "d_" + _h(HANDLER, "cell", leaf, lens, self.market, summary)[:24],
                         "kind": "synthesis", "handler": HANDLER, "action": "confirm_cell",
                         "rationale": f"{lens} for {name} ({self.market}) from {len(cited)} citation(s).",
                         "cites": cited}})
        # confidence is derived by the database from the sources (0005), never sent
        return f"v{r['version']} ({r['sources']} sources, {r['publishers']} publishers, {r['confidence']})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed category research into napkin_category.")
    ap.add_argument("--dsn", required=True, help="napkin_category as its app role")
    ap.add_argument("--market", default="IE")
    ap.add_argument("--verticals", nargs="*", default=[])
    ap.add_argument("--leaves", nargs="*", default=[])
    ap.add_argument("--lenses", nargs="*", default=LENSES)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-sources", type=int, default=8)
    ap.add_argument("--run-dir", type=Path, required=True,
                    help="holds PAUSE / STOP, status.json, units.jsonl and the call cache; reuse it to resume")
    a = ap.parse_args(argv)
    control = Control(a.run_dir)
    control.install_signals()
    a.log = a.run_dir / "units.jsonl"
    if a.market not in GEO["countries"]:
        ap.error("--market must be a country code (IE, GB, …)")

    settings = Settings.from_env()
    if not settings.research_url:
        ap.error("NAPKIN_RESEARCH_URL is not set")
    model = ModelPort(build_wire(settings), settings.model, settings.model_timeout)
    research = ResearchPort(settings.research_url, settings.research_timeout, token=settings.research_token)
    store = Store(a.dsn, ORG)

    with store.connect() as conn:
        tree = conn.execute("SELECT code, vertical, name, regulated FROM layers.categories ORDER BY ordinal").fetchall()
        leaves = [c for c, v, _, _ in tree if v in a.verticals or c in a.leaves]
        if not leaves:
            ap.error("no leaves selected")
        for leaf in leaves:
            store.open_cells(conn, leaf, a.market)
        conn.commit()
        measures = {}
        for m in store.measures(conn):
            measures.setdefault(m["lens"], []).append(m)
    names = {c: n for c, _, n, _ in tree}
    regulated = {c: r for c, _, _, r in tree}
    groups = {lens: spec["groups"] for lens, spec in MEASURE_LIST["lenses"].items()}
    preferred = PREFERRED.get(a.market, {})
    missing = [lens for lens in a.lenses if not measures.get(lens)]
    if missing:
        ap.error(f"no measures loaded for {missing}: migrate the database (seeds data/measures.json)")
    seeder = Seeder(store, model, research, a.market, names, regulated, a.log, control, a.max_sources,
                    measures=measures, groups=groups, preferred=preferred)
    units = [(leaf, lens) for leaf in leaves for lens in a.lenses]
    control.progress["total"] = 2 * len(units)       # phase A (facts) and phase C (cells)
    control.write_status()
    print(json.dumps({"start": len(units), "leaves": leaves, "market": a.market, "run_dir": str(a.run_dir),
                      "pause": f"touch {a.run_dir / 'PAUSE'}", "stop": f"touch {a.run_dir / 'STOP'}"}), flush=True)
    rows = []
    # Phase A: facts for every unit
    with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
        for fu in as_completed([ex.submit(seeder.unit_facts, leaf, lens) for leaf, lens in units]):
            row = fu.result()
            rows.append(row)
            control.unit_done(row)
    # Phase B: facts that belong to a vertical or every category move up; then the
    # reviewed corrections. Only when phase A finished everything, so a figure seen
    # under two leaves is promoted once, with both leaves' evidence.
    phase_a_left = [r for r in rows if r.get("left_for_next_run")]
    if not phase_a_left and not control.stopping():
        with store.connect() as conn:
            promoted = store.promote(conn, leaves, f"{a.market}-{time.strftime('%Y%m%d')}")
            applied = store.apply_corrections(conn, CORRECTIONS)
            conn.commit()
        print(json.dumps({"promoted": promoted, "corrections": applied}), flush=True)
        # Phase C: the cells, citing leaf, vertical and all-category facts
        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            for fu in as_completed([ex.submit(seeder.unit_cell, leaf, lens) for leaf, lens in units]):
                row = fu.result()
                rows.append(row)
                control.unit_done(row)
    facts_rows = [r for r in rows if r.get("phase") == "facts"]
    cell_rows = [r for r in rows if r.get("phase") == "cell"]
    summary = {
        "units": len(units),
        "facts_phase": {"done": sum(1 for r in facts_rows if r["cell"] == "facts written"),
                        "skipped_fresh": sum(1 for r in facts_rows if r["cell"] == "skipped (fresh)")},
        "left_for_next_run": sum(1 for r in rows if r.get("left_for_next_run")),
        "failed": sum(1 for r in rows if r["error"] and not r.get("left_for_next_run")),
        "cells_written": sum(1 for r in rows if str(r["cell"]).startswith("v")),
        "skipped_fresh": sum(1 for r in cell_rows if r["cell"] == "skipped (fresh)"),
        "sources": sum(r["sources"] for r in rows), "verified_sources": sum(r["verified_sources"] for r in rows),
        "passages_kept": sum(r["passages_kept"] for r in rows),
        "passages_dropped": sum(r["passages_dropped"] for r in rows),
        "facts_proposed": sum(r["facts_proposed"] for r in rows),
        "searches": sum(r.get("searches", 0) for r in rows),
        "measures_proposed": sum(r.get("measures_proposed", 0) for r in rows),
        "corroboration_attempted": sum(r.get("corroboration", {}).get("attempted", 0) for r in rows),
        "corroborated": sum(r.get("corroboration", {}).get("corroborated", 0) for r in rows),
        "dropped_by_reason": {k: sum(r.get("dropped_by_reason", {}).get(k, 0) for r in rows)
                              for k in {k for r in rows for k in r.get("dropped_by_reason", {})}},
        "outcomes": {k: sum(r["outcomes"].get(k, 0) for r in rows)
                     for k in {k for r in rows for k in r["outcomes"]}},
        "model_calls": seeder.usage.calls, "tokens_in": seeder.usage.input_tokens,
        "tokens_out": seeder.usage.output_tokens}
    print(json.dumps({"summary": summary}), flush=True)
    with a.log.open("a") as fh:
        fh.write(json.dumps({"summary": summary}) + "\n")
    control.write_status()
    if summary["left_for_next_run"] or summary["failed"]:
        print(json.dumps({"resume": "run the same command again; finished cells are skipped, calls come from the cache"}),
              flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
