"""research: one research call per lens x market, facts extracted from the
sources by the model, checked, tiered, merged, written to the layers, pinned.

Per unit (lens, market):
  0. reuse — the layers already hold fresh active facts for this lens and
     market (the category layer is shared): pin those, no research call;
  1. the research port returns sources with verbatim excerpts;
  2. each source is tiered by the domain policy and recorded in the layer;
  3. ONE structured-output call extracts facts, each with the exact quote and
     the source it came from. A fact whose quote is not in that source's
     excerpts, or whose number is not in its quote, is rejected;
  4. a unit that fails (timeout, upstream error, invalid output) becomes a gap
     carrying its error — never a silent success.
Then, across units: the merge by entity + key + market (contests for
disagreements, nothing picked), D1 — every fact is appended to the layers with
the merge decision FIRST, then pinned from the row the layer returns —, and
coverage per market and merged.
"""

from __future__ import annotations

import datetime as _dt
import re
from concurrent.futures import ThreadPoolExecutor

from ..doc import (CONF, ISO_3166, LENS_NAMESPACE, LENSES, LICENCE_RANK, ctx_data, ctx_facts, decision,
                   field_value, lens_of_key, market_list, read_of)
from ..layers import origin_uri
from ..rules import merge as merge_rules
from ..rules.confidence import coverage_of, fact_confidence, merge_coverage
from ..rules.figures import quote_supports
from ..rules.quotes import verbatim
from ..rules.tiering import registrable, tier_for
from ..util import bad, iso, lid, slug, today, uid
from .. import reasoning as rsn

# The standing question each lens asks (Planner Research Taxonomy, panel 3),
# and the facts it wants — the wanted keys become gaps when not found.
LENS_QUESTIONS = {
    "market_structure": ("Who is in the market and how big is it: players, shares, size, growth, consolidation.",
                         ["size_eur", "units_annual", "value_growth_yoy", "share"]),
    "brands_positioning": ("What each brand claims and stands for: lines, assets, launches, agency moves.",
                           ["claim", "launch_date", "model_range", "awareness_prompted"]),
    "consumer_culture": ("Who buys and what is moving: segments, attitudes, cultural tensions.",
                         ["purchase_intent_share", "barrier_top", "segment_share"]),
    "category_codes": ("The conventions of advertising in the category: what every ad does, what is worn out.",
                       ["dominant_code", "worn_out_code"]),
    "rhythm_moments": ("When it happens: seasonal peaks, cycles, launch windows, events the category plans around.",
                       ["peak_months", "key_moment"]),
    "media_spend": ("Where the money goes: channels, share of spend, attention costs.",
                    ["adspend_eur", "tv_share_of_spend", "digital_share_of_spend"]),
    "regulation_clearance": ("What you cannot say: codes, substantiation, mandatory copy, incentives and rules.",
                             ["code_applies", "mandatory_copy", "grant_eur"]),
    "effectiveness_evidence": ("What has worked before: award cases, evidence on long vs short, share of voice.",
                               ["published_cases", "case_example"]),
}
UNITS = ["proportion", "eur", "gbp", "usd", "count", "units", "date", "text", "years", "months", "km", "kwh",
         "code", "boolean"]

SYSTEM = """You extract facts for an advertising agency's research layer from web sources.
You are given ONE research lens and ONE market, the campaign's subject brand, comparators and
category leaves, and sources with verbatim excerpts. Extract only facts an excerpt states.
Each fact: what it is about (the category, the subject brand, or a named comparator), a short
snake_case key_suffix naming the measure, not the period (the lens namespace is added for you;
the period goes in as_of, so "bev_share", never "bev_share_2025"), the value (a number, a short
text, or a boolean), its unit, as_of (the date the figure describes or was published,
YYYY-MM-DD, never in the future, or null), whether it is specific to this market, and the
evidence: the source_id and the exact quote from that source's excerpts (character for
character). Numbers: a percentage is a proportion between 0 and 1; money in the currency
stated (eur / gbp / usd), as a plain number. Never compute, convert beyond that, or use
outside knowledge. List the wanted facts you could not find in not_found."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def facts_schema(categories: list[str], competitors: list[str]) -> dict:
    return _obj({
        "facts": {"type": "array", "items": _obj({
            "about": {"type": "string", "enum": ["category", "subject", "competitor"]},
            "category": {"anyOf": [{"type": "string", "enum": categories}, {"type": "null"}]},
            "competitor": {"anyOf": [{"type": "string", "enum": competitors or ["(none)"]}, {"type": "null"}]},
            "key_suffix": {"type": "string"},
            "value_number": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "value_text": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "value_boolean": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            "unit": {"type": "string", "enum": UNITS},
            "as_of": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "market_specific": {"type": "boolean"},
            "evidence": {"type": "array", "items": _obj({"source_id": {"type": "string"},
                                                         "quote": {"type": "string"}})},
        })},
        "not_found": {"type": "array", "items": {"type": "string"}},
    })


def validate_research(clan: dict, inp: dict):
    """The synchronous checks (400s) before any job is started."""
    data = ctx_data(clan)
    lenses = inp.get("lenses", LENSES)
    if not isinstance(lenses, list) or not lenses or any(l not in LENSES for l in lenses):
        raise bad(f"input.lenses must be a non-empty list of lens ids from {LENSES}")
    lenses = [l for l in LENSES if l in lenses]
    markets = inp.get("markets")
    if markets is None:
        markets = field_value(data, "markets")
        if not markets:
            raise bad("campaign.markets is absent and input.markets not given: research cannot run (gate research)")
    if not isinstance(markets, list) or not markets or any(not isinstance(m, str) or m not in ISO_3166
                                                           for m in markets):
        raise bad("markets must be ISO 3166-1 alpha-2 codes (the UK is GB)")
    markets = list(dict.fromkeys(markets))
    cats = field_value(data, "categories")
    if not cats or not isinstance(cats, list):
        raise bad("campaign.categories is absent: research cannot run (gate research)")
    return lenses, markets, cats[:2]


def _date_ok(s) -> bool:
    return isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) is not None


class Researcher:
    """One research_lens run (or start_campaign's research stage)."""

    def __init__(self, doc, base, clan, handler, caps, lenses, markets, cats, pairs=None, reuse_days=30,
                 concurrency=4, seed=None):
        self.doc, self.base, self.clan, self.handler, self.caps = doc, base, clan, handler, caps
        self.data = ctx_data(clan)
        self.lenses, self.markets, self.cats = lenses, markets, cats
        self.pairs = [(l, m) for l in lenses for m in markets if pairs is None or (l, m) in pairs]
        self.reuse_days, self.concurrency = reuse_days, concurrency
        self.seed = seed if seed is not None else base
        subject = field_value(self.data, "brand")
        self.subject = subject if isinstance(subject, dict) and subject.get("ref") else None
        self.comps = [c for c in (field_value(self.data, "competitor_set") or [])
                      if isinstance(c, dict) and c.get("ref")]
        self.leaf_names = {l["code"]: l["name"] for l in caps.layers.leaves()}
        self.done = 0
        self.hits = []

    # -- entities --------------------------------------------------------------
    def entities(self):
        """(entity ref, layer) this run may write about."""
        out = [("category/" + c, "category") for c in self.cats]
        if self.subject:
            out.append((self.subject["ref"], "brand"))
        out += [(c["ref"], "category") for c in self.comps]
        return out

    # -- one unit ----------------------------------------------------------------
    def unit(self, lens: str, market: str) -> dict:
        ns = LENS_NAMESPACE[lens]
        u = {"lens": lens, "market": market, "cands": [], "gaps": [], "reused": 0, "sources": [], "error": None,
             "queries": []}
        # 0. reuse what the layers already hold, fresh
        cutoff = (_dt.date.today() - _dt.timedelta(days=self.reuse_days)).isoformat()
        for entity, layer in self.entities():
            for row in self.caps.layers.facts(layer, entity, key_prefix=ns, market=market):
                if row["status"] != "active" or row["retrieved_at"] < cutoff:
                    continue
                u["cands"].append(self._cand_from_row(row, lens, market))
                u["reused"] += 1
        if u["reused"]:
            self.hits += [{"id": c["row"]["id"], "scope": c["layer"], "source": c["row"]["origin"]} for c in u["cands"]]
            return u
        # 1. research
        brand = self.subject["name"] if self.subject else None
        cat_names = [self.leaf_names.get(c, c) for c in self.cats]
        question, wanted = LENS_QUESTIONS[lens]
        query = (f"{question} Category: {', '.join(cat_names)}. Market: {market_list([market])} ({market})."
                 + (f" Brand: {brand}." if brand else "")
                 + (f" Comparators: {', '.join(c['name'] for c in self.comps)}." if self.comps else ""))
        try:
            res = self.caps.research.search(query, lens, market, entity=self.subject["ref"] if self.subject else None,
                                            category=self.cats[0])
        except Exception as e:
            u["error"] = str(e)
            u["gaps"].append(self._gap(lens, market, wanted[0], note=f"research failed: {e}"))
            return u
        u["queries"] = res["trace"]["queries"] or [query]
        srcs = []
        for s in res["sources"]:
            if not _date_ok(s["retrieved_at"]) or s["retrieved_at"] > today():
                s = dict(s, retrieved_at=today())
            if s["published_at"] and not _date_ok(s["published_at"]):
                s = dict(s, published_at=None)
            tier = tier_for(s["url"])
            sid = self.caps.layers.add_source({"uri": s["url"], "publisher": s["publisher"], "title": s["title"],
                                               "tier": tier, "domain": registrable(s["url"]), "licence": "open",
                                               "retrieved_at": s["retrieved_at"],
                                               "published_at": s["published_at"]})
            srcs.append(dict(s, sid=sid, tier=tier, domain=registrable(s["url"])))
            self.hits.append({"id": sid, "scope": "category", "source": s["url"]})
        u["sources"] = srcs
        if not srcs:
            for w in wanted[:2]:
                u["gaps"].append(self._gap(lens, market, w, tried=u["queries"]))
            return u
        # 3. the model extracts; the rules check
        comp_names = [c["name"] for c in self.comps]
        payload = {"lens": lens, "lens_question": question, "key_namespace": ns, "wanted": wanted,
                   "market": market, "subject_brand": brand, "comparators": comp_names,
                   "categories": [{"code": c, "name": self.leaf_names.get(c, c)} for c in self.cats],
                   "sources": [{"source_id": s["sid"], "url": s["url"], "publisher": s["publisher"],
                                "title": s["title"], "published_at": s["published_at"], "excerpts": s["excerpts"]}
                               for s in srcs]}
        try:
            raw = self.caps.model.structured("extract_facts", SYSTEM, payload, facts_schema(self.cats, comp_names),
                                             max_tokens=8000)
        except Exception as e:
            u["error"] = str(e)
            u["gaps"].append(self._gap(lens, market, wanted[0], tried=[s["url"] for s in srcs],
                                       note=f"fact extraction failed: {e}"))
            return u
        by_sid = {s["sid"]: s for s in srcs}
        for f in raw.get("facts") or []:
            c = self._check(f, lens, market, by_sid)
            if c:
                u["cands"].append(c)
        found = {c["key"].split(".", 1)[1] for c in u["cands"]}
        for w in (raw.get("not_found") or []):
            w = re.sub(r"[^a-z0-9_]+", "_", str(w).lower()).strip("_")
            if w and w in wanted and w not in found:
                u["gaps"].append(self._gap(lens, market, w, tried=[s["url"] for s in srcs]))
        if not u["cands"] and not u["gaps"]:
            u["gaps"].append(self._gap(lens, market, wanted[0], tried=[s["url"] for s in srcs],
                                       note="the sources held nothing that passed the quote check"))
        return u

    def _cand_from_row(self, row, lens, market) -> dict:
        return {"entity": row["entity"], "key": row["key"], "market": row["market"], "value": row["value"],
                "unit": row["unit"], "as_of": row["as_of"], "retrieved_at": row["retrieved_at"],
                "layer": row["layer"], "lens": lens, "run": f"{lens}/{market}", "sources": list(row["sources"]),
                "records": row["source_records"], "method": row.get("method") or "report", "row": row,
                "quotes": {}}

    def _check(self, f: dict, lens: str, market: str, by_sid: dict) -> dict | None:
        """The rules a model-extracted fact must pass."""
        about = f.get("about")
        if about == "category":
            leaf = f.get("category") or self.cats[0]
            if leaf not in self.cats:
                return None
            entity, layer = "category/" + leaf, "category"
        elif about == "subject" and self.subject:
            entity, layer = self.subject["ref"], "brand"
        elif about == "competitor":
            comp = next((c for c in self.comps if c["name"] == f.get("competitor")), None)
            if not comp:
                return None
            entity, layer = comp["ref"], "category"
        else:
            return None
        suffix = re.sub(r"[^a-z0-9_.]+", "_", str(f.get("key_suffix") or "").lower()).strip("._")
        if not suffix:
            return None
        key = f"{LENS_NAMESPACE[lens]}.{suffix}"
        if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)*", key):
            return None
        unit = f.get("unit")
        if f.get("value_number") is not None:
            value = f["value_number"]
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if unit == "proportion" and not (0 <= value <= 1):
                return None
        elif f.get("value_boolean") is not None:
            value, unit = bool(f["value_boolean"]), "boolean"
        elif isinstance(f.get("value_text"), str) and f["value_text"].strip():
            value = f["value_text"].strip()[:300]
            if unit in ("proportion", "eur", "gbp", "usd", "count", "units", "km", "kwh"):
                return None  # a numeric unit needs a number
        else:
            return None
        if unit == "date" and not (isinstance(value, str) and re.match(r"\d{4}(-\d{2}(-\d{2})?)?$", value)):
            return None
        # evidence: verbatim in the named source's excerpts, and the number in the quote
        sources, quotes, retrieved = [], {}, []
        for ev in f.get("evidence") or []:
            s = by_sid.get(ev.get("source_id"))
            if not s:
                continue
            q = next((verbatim(x, ev.get("quote", "")) for x in s["excerpts"] if verbatim(x, ev.get("quote", ""))),
                     None)
            if not q or not quote_supports(value, unit, q):
                continue
            if s["sid"] not in sources:
                sources.append(s["sid"])
                quotes[s["sid"]] = q
                retrieved.append(s["retrieved_at"] or today())
        if not sources:
            return None
        retrieved_at = max(retrieved)
        as_of = f.get("as_of")
        if not _date_ok(as_of):
            pub = [by_sid[s]["published_at"] for s in sources if _date_ok(by_sid[s]["published_at"])]
            as_of = min(pub) if pub else retrieved_at
        if as_of > retrieved_at:
            return None  # a fact cannot be true after it was learned
        mk = market if f.get("market_specific", True) else None
        return {"entity": entity, "key": key, "market": mk, "value": value, "unit": unit, "as_of": as_of,
                "retrieved_at": retrieved_at, "layer": layer, "lens": lens, "run": f"{lens}/{market}",
                "sources": sources, "quotes": quotes, "method": "report",
                "records": [{"id": s, "tier": by_sid[s]["tier"], "domain": by_sid[s]["domain"],
                             "licence": "open", "uri": by_sid[s]["url"]} for s in sources]}

    def _gap(self, lens, market, wanted, tried=None, note=None) -> dict:
        entity = self.subject["ref"] if lens == "brands_positioning" and self.subject else "category/" + self.cats[0]
        key = f"{LENS_NAMESPACE[lens]}.{wanted}"
        g = {"id": lid("gap_", self.doc, entity, key, market), "key": f"{entity}:{key}", "lens": lens,
             "market": market, "searched": f"{wanted.replace('_', ' ')} for {entity} in {market_list([market])}",
             "sources_tried": tried or [f"research:{lens}/{market}"]}
        if note:
            g["note"] = note[:300]
        return g

    # -- the run ------------------------------------------------------------------
    def run(self, progress=None):
        units = []
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as ex:
            futs = [ex.submit(self.unit, l, m) for l, m in self.pairs]
            for fu in futs:
                units.append(fu.result())
                self.done += 1
                if progress:
                    progress(self.done)
        return self.merge(units)

    def merge(self, units):
        doc, handler = self.doc, self.handler
        t_now = iso()
        existing = ctx_facts(self.clan)
        pinned = {(f.get("entity"), f.get("key"), f.get("market")): f for f in existing}
        pinned_ids = {f.get("id") for f in existing}
        sel = self.data.get("selection") or {}
        excluded = {e.get("fact_id") for e in (sel.get("excluded") or [])}
        open_keys = {c.get("key") for c in (sel.get("contested") or []) if c.get("status") == "open"}
        cands = [c for u in units for c in u["cands"]]
        merged = merge_rules.merge(cands, pinned, open_keys)
        merge_did = uid("d_", doc, self.seed, "research-merge", self.lenses, self.markets, len(self.pairs))
        merge_dec = decision(doc, merge_did, "pin", handler, "research_merge", "", [], [], timestamp=t_now)

        # D1: write to the layers FIRST (with the decision), then pin from the row.
        facts_append = []
        for c in merged["pins"]:
            row = self._write(c, merge_dec, status="active")
            if row["id"] in pinned_ids or row["id"] in excluded or row["status"] == "superseded":
                continue
            ident = (row["entity"], row["key"], row["market"])
            if ident in pinned:
                continue
            facts_append.append(self._pin(row, c, merge_did, t_now))
            pinned_ids.add(row["id"])
        contests, contest_decs = [], []
        for ct in merged["contests"]:
            cid = lid("ct_", doc, ct["key"])
            cdid = uid("d_", doc, self.seed, "contest", ct["key"])
            cdec = decision(doc, cdid, "contest", handler, "open_contest", "", [f"selection.contested[{cid}]"], [],
                            timestamp=t_now)
            vals = []
            if ct["pinned"] is not None:
                p = ct["pinned"]
                vals.append({"value": p["value"], "unit": p.get("unit"), "fact_id": p["id"], "from": "pinned",
                             "sources": list(p.get("sources", []))})
            for v in ct["values"]:
                row = self._write(v, cdec, status="contested")
                vals.append({"value": row["value"], "unit": row["unit"], "fact_id": row["id"],
                             "from": ", ".join(v["runs"]), "sources": list(v["sources"])})
            vals = [{k: x for k, x in v.items() if x is not None} for v in vals]
            if len({repr(v["value"]) for v in vals}) < 2:
                continue
            contests.append({"id": cid, "key": ct["key"], "status": "open", "opened_by": cdid, "values": vals})
            cdec["rationale"] = (f"{len(vals)} values for {ct['key']} disagree"
                                 f"{' with the pinned value' if ct['pinned'] is not None else ''}; the layer rows are "
                                 f"contested and nothing is picked.")
            cdec["cites"] = [v["fact_id"] for v in vals] + [s for v in vals for s in v["sources"]]
            rsn.give(cdec, contest_reasoning(ct["key"], cid, vals))
            contest_decs.append(cdec)

        # selection
        by_market, runs, run_decs, gaps = {}, [], [], []
        for u in units:
            counts = [len(c["sources"]) if not c.get("row") else len({r["domain"] for r in c["records"]})
                      for c in u["cands"]]
            cov = coverage_of(counts)
            by_market.setdefault(u["market"], {})[u["lens"]] = cov
            rdid = uid("d_", doc, self.seed, "run", u["lens"], u["market"])
            runs.append({"lens": u["lens"], "market": u["market"], "ran_at": t_now, "handler": handler,
                         "decision": rdid})
            gaps += u["gaps"]
            how = (f"reused {u['reused']} fact(s) from the layers" if u["reused"] else
                   f"{len(u['sources'])} source(s), {len(u['cands'])} fact(s) passed the quote check")
            run_decs.append(decision(
                doc, rdid, "edit", handler, "research_run",
                f"{u['lens']}/{u['market']}: {how}; coverage {cov}; {len(u['gaps'])} gap(s)"
                + (f"; failed: {u['error']}" if u["error"] else "") + ".",
                [f"selection.lenses_run[{u['lens']}/{u['market']}]"] + [f"selection.gaps[{g['id']}]" for g in u["gaps"]],
                [s["sid"] for s in u["sources"]], timestamp=t_now,
                fields_changed=["selection.lenses_run", "selection.coverage_by_market"]
                + (["selection.gaps"] if u["gaps"] else []),
                reasoning=run_reasoning(u, cov, self.reuse_days)))
        old_cbm = sel.get("coverage_by_market") or {}
        cbm_all = {m: dict(old_cbm.get(m) or {}) for m in old_cbm}
        for m, v in by_market.items():
            cbm_all.setdefault(m, {}).update(v)
        coverage = {}
        for lens in self.lenses:
            vals = [cbm_all[m][lens] for m in cbm_all if lens in cbm_all[m]]
            if vals:
                coverage[lens] = merge_coverage(vals)
        run_keys = {(r["lens"], r["market"]) for r in runs}
        all_runs = [x for x in (sel.get("lenses_run") or []) if (x.get("lens"), x.get("market")) not in run_keys] + runs
        gap_ids = {g["id"] for g in gaps}
        old_gaps = [g for g in (sel.get("gaps") or []) if g.get("id") not in gap_ids]
        ct_ids = {c["id"] for c in contests}
        old_ct = [c for c in (sel.get("contested") or []) if c.get("id") not in ct_ids]
        sel_patch = {"lenses_run": all_runs, "coverage": coverage, "coverage_by_market": by_market,
                     "gaps": old_gaps + gaps}
        if contests:
            sel_patch["contested"] = old_ct + contests
        skipped = sel.get("lenses_skipped") or []
        keep = [s for s in skipped if not any(s.get("lens") == l and s.get("market") in (None, m) for l, m in run_keys)]
        if len(keep) != len(skipped):
            sel_patch["lenses_skipped"] = keep  # a later run of a skipped pair removes its entry
        merge_dec["targets"] = [f"{doc}#facts[{f['id']}]" for f in facts_append] + \
            [f"{doc}#selection.coverage", f"{doc}#selection.coverage_by_market"] + \
            ([f"{doc}#selection.lenses_skipped"] if "lenses_skipped" in sel_patch else [])
        merge_dec["cites"] = list(dict.fromkeys([f["id"] for f in facts_append] +
                                                [s for f in facts_append for s in f["sources"]]))
        n_reused = sum(u["reused"] for u in units)
        merge_dec["rationale"] = (
            f"Merged {len(units)} lens x market run(s) by entity + key + market: {len(facts_append)} pin(s), "
            f"{len(contests)} contest(s), {len(gaps)} gap(s)"
            + (f", {n_reused} fact(s) reused from the layers" if n_reused else "")
            + ". Each fact was written to the layer with this decision before it was pinned; confidence is "
              "derived from source tier and corroboration.")
        merge_dec["fields_changed"] = ["selection.coverage", "selection.coverage_by_market"]
        rsn.give(merge_dec, merge_reasoning(facts_append, contests, gaps, len(units), n_reused))
        change = {"doc": doc, "base_version": self.base, "data_patch": {"selection": sel_patch},
                  "read": read_of(self.data, {"selection": sel_patch}),
                  "facts_append": facts_append, "findings_append": [],
                  "decisions": run_decs + [merge_dec] + contest_decs}
        srcs = {s["sid"]: s for u in units for s in u["sources"]}
        result = {"summary": f"{len(units)} lens x market run(s): {len(facts_append)} fact(s) pinned, "
                             f"{len(contests)} contest(s) open, {len(gaps)} gap(s)"
                             + (f", {n_reused} reused from the layers" if n_reused else "") + ".",
                  "coverage": coverage, "reused": n_reused,
                  "sources": {sid: {"uri": s["url"], "tier": s["tier"], "licence": "open", "title": s["title"],
                                    "publisher": s["publisher"]} for sid, s in srcs.items()}}
        return result, change, self.hits

    def _write(self, c: dict, dec: dict, status: str) -> dict:
        if c.get("row") and status == "active":
            return c["row"]
        lic = max((r.get("licence", "open") for r in c.get("records") or []), key=LICENCE_RANK.index,
                  default="open")
        return self.caps.layers.append({"layer": c["layer"], "entity": c["entity"], "key": c["key"],
                                        "market": c.get("market"), "value": c["value"], "unit": c["unit"],
                                        "as_of": c["as_of"], "retrieved_at": c["retrieved_at"],
                                        "sources": c["sources"], "quotes": c.get("quotes") or {},
                                        "licence": lic, "method": c.get("method", "report"),
                                        **({"status": "contested"} if status == "contested" else {})}, dec)

    def _pin(self, row: dict, c: dict, did: str, t_now: str) -> dict:
        recs = row.get("source_records") or c.get("records") or []
        lic = max((r.get("licence", "open") for r in recs), key=LICENCE_RANK.index, default="open")
        f = {"id": row["id"], "entity": row["entity"], "key": row["key"], "value": row["value"], "unit": row["unit"],
             "as_of": row["as_of"], "retrieved_at": row["retrieved_at"], "sources": list(row["sources"]),
             "confidence": fact_confidence(recs), "licence": lic,
             "status": "contested" if row["status"] == "contested" else "active",
             "version": row["version"], "supersedes": row.get("supersedes"),
             "origin": origin_uri(row["layer"], row["entity"], row["key"], row["version"]), "decision": did,
             "pinned_at": t_now,
             "pin_reason": (f"research_lens {', '.join(c.get('runs') or [c['run']])}: "
                            f"{len(row['sources'])} source(s), tiers "
                            f"{'/'.join(sorted({r.get('tier', '?') for r in recs}))}"
                            + (" (reused from the layer)" if c.get("row") else "")),
             "layer": row["layer"], "method": row.get("method") or "report"}
        if row.get("market"):
            f["market"] = row["market"]
        assert f["confidence"] in CONF
        return f


# ---------------------------------------------------------------------------
# Reasoning: every research decision is deterministic, so the code writes it
# whole from the evidence it holds. Certainty is the derived confidence.
# ---------------------------------------------------------------------------

def _where(market):
    return f" in {market_list([market])}" if market else ""


def _fact_line(f) -> str:
    unit = f" {f['unit']}" if f.get("unit") and f["unit"] not in ("text", "code", "date", "boolean") else ""
    return f"{f['entity']} {f['key']}{_where(f.get('market'))} is {f['value']}{unit}"


def run_reasoning(u, cov, reuse_days) -> dict:
    """One lens x market run: what came back, what passed the quote check,
    what was not found. Certainty from coverage: every fact corroborated
    (filled) is high, some single-source (thin) medium, nothing (empty) low."""
    lens, market = u["lens"], u["market"]
    sids = [s["sid"] for s in u["sources"]]
    gids = [g["id"] for g in u["gaps"]]
    because, rejected = [], []
    if u["reused"]:
        rows = [c["row"]["id"] for c in u["cands"]]
        because.append(rsn.point(f"The layers already held {u['reused']} fresh fact(s) for this lens and market",
                                 rows))
        rejected.append(rsn.rej("research it again", f"the layers' facts are within the {reuse_days}-day reuse "
                                                     f"window, so a new call would spend for nothing"))
    elif u["error"]:
        because.append(rsn.point(f"The research call failed: {u['error'][:160]}", gids or sids))
        rejected.append(rsn.rej("report the lens as covered", "a failed run is a gap, never a silent success"))
    else:
        because.append(rsn.point(f"{len(sids)} source(s) came back for {lens.replace('_', ' ')}{_where(market)}",
                                 sids) if sids else rsn.point("No source came back for the lens here", gids))
        passed = [c for c in u["cands"]]
        if passed:
            because.append(rsn.point(f"{len(passed)} fact(s) passed the quote check: the quote is verbatim in "
                                     f"the source and the figure is in the quote",
                                     [s for c in passed for s in c["sources"]]))
        rejected.append(rsn.rej("keep what the model read but the source does not say",
                                "a fact is kept only when its quote is in the source's excerpts"))
    if u["gaps"]:
        because.append(rsn.point(f"Not found: {', '.join(g['searched'] for g in u['gaps'][:4])}", gids))
    level = {"filled": "high", "thin": "medium"}.get(cov, "low")
    basis = {"filled": "coverage filled: every fact rests on two or more independent sources",
             "thin": "coverage thin: some facts rest on a single source",
             "empty": "coverage empty: nothing passed the checks"}[cov]
    attention = None
    if u["error"]:
        attention = "The research call failed; this lens and market is a gap until it is rerun."
    elif cov == "empty":
        attention = "Nothing was found for this lens here."
    elif cov == "thin":
        attention = "Some facts here rest on one source."
    return rsn.make(f"Ran {lens.replace('_', ' ')}{_where(market)}: coverage {cov}.", because,
                    rsn.certainty(level, basis),
                    "a new source states what was not found, or a corroborating source appears",
                    rejected=rejected, attention=attention)


def merge_reasoning(pins, contests, gaps, n_units, n_reused) -> dict:
    """The merge: which facts were pinned and on what evidence. Certainty is
    the lowest derived confidence of the pins (source tier + independent
    corroboration), never an average and never the model's."""
    because = [rsn.point(_fact_line(f) + f": {len(f['sources'])} source(s), confidence {f['confidence']}",
                         f["id"], f["sources"]) for f in pins[:10]]
    if len(pins) > 10:
        rest = pins[10:]
        because.append(rsn.point(f"and {len(rest)} more pin(s)", [f["id"] for f in rest]))
    if not because:
        because = [rsn.point("Nothing new passed the checks to pin", [g["id"] for g in gaps])]
    rejected = [rsn.rej(f"pin one of the values for {c['key']}",
                        "the runs disagree; the contest holds every value and nothing is picked") for c in contests]
    rejected.append(rsn.rej("pin a fact the layer has not recorded",
                            "every fact is written to its layer with this decision first, then pinned from the row"))
    counts = {l: sum(1 for f in pins if f["confidence"] == l) for l in rsn.LEVELS}
    level = rsn.lowest(f["confidence"] for f in pins) if pins else "low"
    basis = ("lowest derived confidence of the pins (source tier + independent corroboration): "
             + ", ".join(f"{counts[l]} {l}" for l in reversed(rsn.LEVELS) if counts[l])) if pins else \
        "nothing was pinned"
    attention = []
    if counts["low"]:
        attention.append(f"{counts['low']} pin(s) rest on thin evidence (low confidence).")
    if contests:
        attention.append(f"{len(contests)} value(s) are contested and wait for a person.")
    return rsn.make(f"Pinned {len(pins)} fact(s) from {n_units} run(s)"
                    + (f", {n_reused} reused from the layers" if n_reused else "") + ".",
                    because, rsn.certainty(level, basis),
                    "a higher-tier or corroborating source revises a value, or a person excludes a pin",
                    rejected=rejected, attention=" ".join(attention) or None)


def contest_reasoning(key, cid, vals) -> dict:
    """A contest: every value with the facts and sources it rests on;
    nothing is picked."""
    because = [rsn.point(f"The pinned value is {v['value']}" if v["from"] == "pinned"
                         else f"The {v['from']} run says {v['value']}", v["fact_id"], v.get("sources") or [])
               for v in vals]
    return rsn.make(
        f"Opened a contest on {key}; nothing is picked.", because,
        rsn.certainty("high", "the values differ as their sources state them; which is right is what is open"),
        "a primary source settles the value, or a person resolves the contest",
        rejected=[rsn.rej("pick either value silently", "nothing in the evidence says which run is right"),
                  rsn.rej("drop the contested values", "the evidence for each would be lost")],
        attention=f"The runs disagree on {key}; a person should resolve it ({cid}).")
