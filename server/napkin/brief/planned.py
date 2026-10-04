"""draft_brief, planned: the brief is written as a whole, not one field at a time.

  extract   Ellis captures the client's own words (unchanged: the capture and its scorecard).
  context   Sam plans the whole brief in one call: the strategy in a line; for every part what it
            must say and what it rests on (the client, the research, the library, or an assumption);
            what research it needs and what only the client can answer. Then the research the plan
            asks for runs (at most `research_max` lens x market units, the Research Tool's own units:
            searched, quote-checked, pinned into the brief with their sources).
  draft     Dara writes every part in one call, from the plan, the client's words, the facts (pins,
            findings, the research upstream) and the library's passages. Each part says what it
            rests on and cites it.
  judge     Jude reviews the brief as a whole, once: the BetterBriefs dimensions, the chain from
            objective to insight to proposition to proof, and every researched claim against the
            fact it cites. What he says to fix goes back to Dara once; then he scores again.
            One note per issue, naming the parts it touches, never the same note on every box.

Why (the owner, 2026-10-01): field writers that each see their own slice wrote a sharp insight
next to empty objectives; a single agent that reads everything and picks one strategy scored
8/14 on BetterBriefs against the field-by-field engine's 4.7. The per-field drafters stay for
regenerate_field (redo one part).

Every call, search, decision and stage is in the job's run log (napkin.runlog).
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

from .. import reasoning as rsn
from .. import runlog
from ..doc import ISO_3166, LENSES, ctx_data, ctx_facts, ctx_findings, ctx_sources, lens_of_key
from ..jev import JevError, noul
from ..rules import merge as merge_rules
from ..util import iso
from . import capture as cap_stage
from . import fill as fill_stage
from .drafters import _query
from .fields import ARRAY_KEYS, KEYS, LABELS, clean, filled, get, put
from .job import BriefJob, log

# The facts the plan, the draft and the judge read (every fact the brief holds, up to this many).
FACTS_MAX = 400
# jev's "the facts held already answer the need" at or above this: the planned search is skipped.
HELD = 0.6

# Who handles a part, from what the plan says it rests on (the owner, 2026-10-01: mix and match,
# decided part by part on what the brief receives, steered by one plan).
WHO = {"client": "Ellis keeps the client's words; Dara writes them out",
       "research": "Sam researches it; Dara writes it citing the facts",
       "library": "Dara writes it from the library's precedents",
       "assumption": "Dara proposes it, marked to confirm with the client",
       "ask_client": "a question for the client; Dara proposes a starting point, marked to confirm"}

DIMS = ["objectives_quality", "audience_vividness", "single_minded_message", "evaluation_criteria",
        "budget_interlock", "strategic_clarity", "language"]
BASES = ["client", "research", "library", "assumption"]
WHAT = {
    "project_name": "a short working name", "client": "the client and brand",
    "background": "the business situation and why now",
    "objectives.commercial": "the business result, measurable and time-bound where the input allows",
    "objectives.behavioural": "what the audience should do differently",
    "objectives.attitudinal": "what the audience should think or feel differently",
    "audience": "a vivid picture: who they are, habits, occasions, needs, and who it is NOT for",
    "competitor_context": "named competitors, what each claims, and the space left open",
    "insight": "one human truth about the audience that opens the problem",
    "single_minded_proposition": "one sentence, one thought",
    "reasons_to_believe": "proof points for the proposition (list)",
    "desired_response.think": "what they should think", "desired_response.feel": "what they should feel",
    "desired_response.do": "what they should do",
    "tone_and_world": "how the work should feel (list of short phrases)",
    "budget_and_scope": "deliverables, channels, timing; never a budget figure that was not given",
    "mandatories": "what must appear or be avoided (list)",
    "open_questions": "what only the client can answer (list)",
}


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(required or props),
            "properties": props}


_S = {"type": "string"}
_STRS = {"type": "array", "items": _S}
_NS = {"anyOf": [_S, {"type": "null"}]}

# -- plan ------------------------------------------------------------------------------------
PLAN_SYSTEM = """You are the lead strategic planner at an advertising agency. Before anyone writes, you plan
the whole creative brief so every part serves ONE strategy.

You get what the client said, the parts already captured from their words, the facts and findings the
agency already holds for this brief, the planning library's passages, and the research the agency can run:
lenses (research angles) and category codes.

Plan:
- strategy: the strategic idea of the brief in one or two sentences: the problem, the audience, the angle.
- parts: for EVERY part listed, what it must say to serve the strategy (must_say) and what it rests on:
  "client" (the client said it), "research" (a fact we hold or will research), "library" (a planning
  passage), "assumption" (your professional proposal, for the agency to confirm), or "ask_client" (only
  the client can say it: budget figures, success measures, mandatories they hold). Read the request like a
  senior planner: "under-30s", "global", "a coffee chain" are things to interpret and build on, not gaps.
- brand and competitors: the client's brand and the competitors that matter most (named brands, up to five),
  from the client's words or what is generally known about the category.
- research: the research runs that would most improve the brief, best first, each with a lens from the
  list, an ISO 3166-1 alpha-2 market (the UK is GB; infer the market from the brand when the client does
  not say it, e.g. an Irish chain is IE), `focus`: the specific thing to find, naming the brands, the
  audience or the moment (e.g. "what Insomnia, Costa and Starbucks claim about iced coffee"), why it is
  needed and the parts it serves. Work through the brief checklist and research what the brief lacks:
  competitors and what each claims (brands_positioning), the audience's habits, occasions and tensions
  (consumer_culture), the category's worn-out codes (category_codes), key moments and timing
  (rhythm_moments), media spend benchmarks (media_spend), what the rules require (regulation_clearance),
  proof that has worked before (effectiveness_evidence), the market's size and players (market_structure).
  Facts already held need no research: read the facts given first, and list a run only for what they
  do not answer (say in `why` what is missing from them). A brief made from research usually needs little
  or none.
- category: the category code from the list that fits the brief best, or null.
- ask_client: the questions only the client can answer, each with the part it serves.
Never restate the person's request ("make me a brief") as content. Plain British English."""


def plan_schema(keys, lens_codes, cat_codes):
    part = _obj({"must_say": _S, "rests_on": {"type": "string", "enum": BASES + ["ask_client"]}})
    return _obj({
        "strategy": _S,
        "parts": _obj({k: part for k in keys}),
        "brand": _NS,
        "competitors": _STRS,
        "research": {"type": "array", "items": _obj({
            "lens": {"type": "string", "enum": lens_codes}, "market": _S, "focus": _S, "why": _S,
            "for_parts": {"type": "array", "items": {"type": "string", "enum": keys}}})},
        "category": {"anyOf": [{"type": "string", "enum": cat_codes}, {"type": "null"}]} if cat_codes else
        {"type": "null"},
        "ask_client": {"type": "array", "items": _obj({"question": _S, "part": {"type": "string", "enum": keys}})},
    })


# -- draft -----------------------------------------------------------------------------------
DRAFT_SYSTEM = """You are a senior strategic planner writing a complete creative brief, every part at once, so
the parts make one argument: the objectives, the audience, the insight, the proposition and its proof all
serve the plan's strategy.

You get what the client said, the parts captured from their words, the plan, the facts (each with an id,
its value, the quote it was read from and its source), the findings, the research the brief came from,
and planning library passages (ids psg_...).

For each part give value, basis, cites, builds_on_client and why:
- basis "client": the client said it. Keep their meaning; you may write it out more fully. Cite nothing.
- basis "research": it rests on facts or findings given to you. Cite every id it rests on. Use their
  numbers exactly as given; never a number that is not in a cited fact.
- basis "library": it rests on a planning passage; cite the psg_ id.
- basis "assumption": your professional proposal from what is generally known; it is marked for the agency
  to confirm. Name real competitors and a concrete audience where you can. No figures at all.
- builds_on_client: true when the client stated this part and you wrote it out more fully.
- why: one sentence on what the part rests on.
Lists (reasons to believe, tone, mandatories, open questions) are arrays of short items. A reason to
believe you cannot prove from a fact starts with "To confirm:". Budget and scope: deliverables, channels and
timing only, no budget figure the client did not give. Open questions: what only the client can answer.
Return null for a part only when nothing sensible can be said. Never restate the person's request as content.
Write in plain British English, specific and vivid, no category jargon."""


def draft_schema(keys):
    def val(k):
        return _STRS if k in ARRAY_KEYS else _S
    return _obj({k: {"anyOf": [{"type": "null"}, _obj({
        "value": val(k), "basis": {"type": "string", "enum": BASES}, "cites": _STRS,
        "builds_on_client": {"type": "boolean"}, "why": _S})]} for k in keys})


# -- review ----------------------------------------------------------------------------------
REVIEW_SYSTEM = """You are the agency's brief judge. Review the WHOLE creative brief once, as a creative team would
receive it, against the BetterBriefs rubric (the global study on briefing) and against its own evidence.

1. dimensions: exactly these seven, each {dimension, verdict pass|vague|missing, evidence (a short quote from
   the brief), fix (one sentence)}:
   objectives_quality (a handful at most, benchmarked and time-stamped, the commercial/behavioural/attitudinal
   chain linked), audience_vividness (a vivid picture, not a bare age range; who it is NOT for),
   single_minded_message (ONE message with relevant proof), evaluation_criteria (how the work will be judged),
   budget_interlock (budget, objectives and audience feasible together), strategic_clarity (a clear choice,
   including what not to do), language (simple, succinct, no jargon).
2. chain: does objective -> audience -> insight -> proposition -> reasons to believe hold as one argument?
   {verdict pass|fail, note}.
3. claims: for every part whose basis is research, each claim against the fact it cites: supported (the
   fact says it), partly, or unsupported. {part, claim, cite, verdict, note}.
4. notes: what should change, ONE note per issue, naming every part it touches (never the same note per part).
   severity "fix" (the brief is wrong or weaker without it) or "consider". Something only the client can
   supply (a budget, success measures) is a question, not a fix.
5. revise: the parts Dara should rewrite, each with one clear instruction. Only parts a rewrite can improve
   from what the brief already holds; never ask her to invent client facts.
6. questions: what only the client can answer.
7. single_mindedness {verdict single|multiple, split_into}, and a one-sentence summary."""


def review_schema(keys):
    kenum = {"type": "string", "enum": keys}
    return _obj({
        "dimensions": {"type": "array", "items": _obj({
            "dimension": {"type": "string", "enum": DIMS}, "verdict": {"type": "string", "enum": ["pass", "vague", "missing"]},
            "evidence": _S, "fix": _S})},
        "chain": _obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "note": _S}),
        "claims": {"type": "array", "items": _obj({
            "part": kenum, "claim": _S, "cite": _S,
            "verdict": {"type": "string", "enum": ["supported", "partly", "unsupported"]}, "note": _S})},
        "notes": {"type": "array", "items": _obj({
            "parts": {"type": "array", "items": kenum}, "note": _S,
            "severity": {"type": "string", "enum": ["fix", "consider"]}})},
        "revise": {"type": "array", "items": _obj({"part": kenum, "instruction": _S})},
        "questions": _STRS,
        "single_mindedness": _obj({"verdict": {"type": "string", "enum": ["single", "multiple"]}, "split_into": _STRS}),
        "summary": _S,
    })


PTS = {"pass": 2, "vague": 1, "missing": 0}


def score_of(review) -> int:
    return sum(PTS.get(d.get("verdict"), 0) for d in review.get("dimensions") or [])


def _short(s, n=220):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


class PlannedBriefJob(BriefJob):
    """draft_brief, planned (module docstring)."""

    def __init__(self, *a, research_max: int = 3, judge: str = "review", **kw):
        super().__init__(*a, **kw)
        self.research_max = max(0, int(research_max))
        self.judge_mode = judge  # "review": Jude reviews the whole brief; "jev": the check loop (checkloop.py)
        self.plan_out, self.plan_dec = None, None
        self.lib = {}           # psg id -> passage record (the library's)
        self.new_pins, self.new_sources = [], []
        self.whole = {}         # key -> {value, basis, cites, builds_on_client, why}
        self.review_out = None
        self.scores = []

    # -- what every call sees ----------------------------------------------------------------
    def _open_keys(self):
        return [k for k in KEYS if k not in self.locked]

    def _said(self):
        return "\n\n".join(m["text"] for m in cap_stage.material_payload(self.mats))[:12000]

    def _captured(self):
        return {k: v for k, v in self._values().items() if self.fm[k]["class"] == "captured" and filled(v)}

    def _held(self):
        """Every fact the brief holds and may use: its pins (those carried from the research
        included) and those pinned in this job; never one a person marked wrong."""
        excluded = {e.get("fact_id") for e in ((ctx_data(self.clan).get("selection") or {}).get("excluded") or [])
                    if isinstance(e, dict)}
        seen, out = set(), []
        for f in list(ctx_facts(self.clan)) + list(self.new_pins):
            fid = f.get("id")
            if isinstance(fid, str) and fid not in seen and fid not in excluded and f.get("status") != "superseded":
                seen.add(fid)
                out.append(f)
        return out

    def _evidence(self):
        """Every fact the brief holds, each with the quote and the source it was read from (the
        document's own source records, and those of this job's research), the findings, and the
        research the brief was made from. All of them: what the plan cannot see, it researches
        again, and what the draft cannot see, it cannot cite (2026-10-03: 80 of 331 were sent,
        and the 89 facts this job researched were never seen)."""
        view = fill_stage.research_view(self.clan, self.data0)
        srcs = {**ctx_sources(self.clan), **{s["id"]: s for s in self.new_sources}}
        facts = []
        for f in self._held()[:FACTS_MAX]:
            sid = next((s for s in f.get("sources") or [] if s in srcs), next(iter(f.get("sources") or []), None))
            s = srcs.get(sid) or {}
            q = next(iter((f.get("quotes") or {}).values()), None)
            facts.append({"id": f["id"], "entity": f.get("entity"), "key": f.get("key"), "value": f.get("value"),
                          "unit": f.get("unit"), "market": f.get("market"), "as_of": f.get("as_of"),
                          "quote": q[:240] if isinstance(q, str) else None,
                          "source": {k: s.get(k) for k in ("publisher", "title", "uri", "published_at") if s.get(k)}
                          or None})
        findings = [{"id": x["id"], "statement": x.get("statement"), "status": x.get("status")}
                    for x in ctx_findings(self.clan) if isinstance(x.get("id"), str) and x.get("status") != "rejected"]
        return {"facts": facts, "findings": findings[:60], "research": view["research"]}

    def _already_held(self, asks):
        """The planned research the brief's facts already answer: jev reads the facts held for each
        ask's lens and market against its focus. -> (to run, skipped with jev's score). Without jev,
        every ask runs."""
        if not asks or not self.caps.jev.configured:
            return asks, []
        held = self._held()
        run, skipped = [], []
        for a in asks:
            mine = [f for f in held if lens_of_key(f.get("key")) == a["lens"] and f.get("market") in (a["market"], None)]
            if not mine:
                run.append(a)
                continue
            lines = [f"{f.get('key')} = {f.get('value')}{' (' + f['market'] + ')' if f.get('market') else ''}"
                     for f in mine[:120]]
            try:
                p = self.caps.jev.ask({"need": a.get("focus") or a["lens"], "facts_held": lines},
                                      {"held": noul("Do the facts held already answer the need?",
                                                    "They answer it: no new research is needed",
                                                    "They leave the need unanswered, or only partly answered")},
                                      "brief_research_held")["held"]
            except JevError:
                run.append(a)
                continue
            (skipped if p >= HELD else run).append({**a, "held_p": p, "held_facts": len(mine)} if p >= HELD else a)
        return run, skipped

    def _ids(self, ev):
        return {f["id"] for f in ev["facts"]} | {x["id"] for x in ev["findings"]} | set(self.lib)

    # =========================================================================================
    # context: Sam plans, then the research the plan asks for
    # =========================================================================================
    def stage_context(self):
        rl = self.caps.runlog
        keys = self._open_keys()
        self.set_state(keys, "waiting", None)
        # the library's passages, once, for the plan and the draft
        try:
            ctx = self._context()
            self.ctx = ctx
            g = ctx.gist()
            loops = ("loop4_insight", "loop5_proposition", "loop6_substantiation")

            def look(loop):
                q, cq = _query(loop, g, None, "plan")
                return ctx.retrieve(loop, q, cq)
            with ThreadPoolExecutor(max_workers=len(loops)) as ex:  # the three lookups at once, not in turn
                for got, notes in ex.map(look, loops):
                    self.lib.update(got)
                    for n in notes:
                        rl.note("library", detail=n)
        except Exception as e:  # noqa: BLE001 - the plan goes on without the library
            rl.note("library", error=f"{type(e).__name__}: {str(e)[:200]}")
        leaves = []
        try:
            leaves = [{"code": x["code"], "name": x.get("name")} for x in self.caps.layers.leaves()]
        except Exception as e:  # noqa: BLE001 - no research to run, the plan still stands
            rl.note("leaves", error=f"{type(e).__name__}: {str(e)[:200]}")
        ev = self._evidence()
        payload = {"client_said": self._said(), "captured": self._captured(),
                   "parts": {k: WHAT.get(k, LABELS.get(k, k)) for k in keys},
                   "facts": ev["facts"], "findings": ev["findings"], "research_upstream": ev["research"],
                   "library": [{"id": pid, "citation": p["citation"], "text": p["text"][:700]}
                               for pid, p in list(self.lib.items())[:8]],
                   "lenses": LENSES if leaves else [], "categories": leaves,
                   "research_budget": self.research_max if leaves else 0}
        plan = self.caps.model.structured("plan_brief", PLAN_SYSTEM, payload,
                                          plan_schema(keys, LENSES, [x["code"] for x in leaves]), max_tokens=8000)
        self.plan_out = plan
        rests = {}
        for k, p in (plan.get("parts") or {}).items():
            rests.setdefault(p.get("rests_on"), []).append(k)
        self.routing = {k: (p.get("rests_on"), WHO.get(p.get("rests_on"), "Dara")) for k, p in
                        (plan.get("parts") or {}).items()}
        rl.note("routing", parts={k: who for k, (_, who) in self.routing.items()})
        rl.note("plan", rests_on=rests, research=plan.get("research"), category=plan.get("category"),
                brand=plan.get("brand"), competitors=plan.get("competitors"),
                ask_client=len(plan.get("ask_client") or []),
                strategy=plan.get("strategy") if runlog.bodies() else None)
        mat_ids = [m.id for m in self.mats if m.readable]
        pts = [rsn.point(f"The strategy: {_short(plan.get('strategy'), 400)}", mat_ids)]
        for basis, ks in rests.items():
            pts.append(rsn.point(f"Rests on {basis.replace('_', ' ')}: {', '.join(LABELS.get(k, k) for k in ks)}",
                                 mat_ids))
        r = rsn.make("Planned the brief as a whole before anyone writes.", pts,
                     rsn.certainty("medium", "a planner's reading of the client's words and what the agency holds"),
                     "the client's words, the research or a person's edit change the strategy",
                     only_option="one plan steers every part")
        self.plan_dec = self.dec(("plan",), "edit", "plan", [], mat_ids, r, "synthesise")
        decisions = [self.plan_dec]
        # the research the plan asks for
        asks, seen = [], set()
        for a in plan.get("research") or []:
            m = str(a.get("market") or "").upper()
            pair = (a.get("lens"), m)
            if a.get("lens") in LENSES and m in ISO_3166 and pair not in seen:
                seen.add(pair)
                asks.append({**a, "market": m})
        cat = plan.get("category")
        # What the brief already holds is not researched again: jev reads each planned search's
        # need against the facts held for its lens and market.
        asks, held = self._already_held(asks)
        if held:
            rl.note("research_held", skipped=[{"lens": a["lens"], "market": a["market"], "focus": _short(a.get("focus"), 160),
                                               "p": round(a["held_p"], 2), "facts": a["held_facts"]} for a in held])
            decisions.append(self.dec(("research-held",), "edit", "research_held", [], [self.plan_dec["id"]], rsn.make(
                f"Did not research again what the brief already holds: {len(held)} planned search(es) skipped.",
                [rsn.point(f"{a['lens'].replace('_', ' ')} in {a['market']}: {_short(a.get('focus'), 160)} "
                           f"(jev: the {a['held_facts']} fact(s) held answer it, {a['held_p']:.2f})", [self.plan_dec["id"]])
                 for a in held],
                rsn.certainty("medium", "jev judged the facts held against each need"),
                "the plan asks for something the facts do not cover",
                rejected=[rsn.rej("search again for what is held", "it spends a search for facts the brief has")]),
                "synthesise"))
        dropped = asks[self.research_max:]
        asks = asks[:self.research_max] if cat else []
        if dropped:
            rl.note("research_capped", dropped=[f"{a['lens']}/{a['market']}" for a in dropped], cap=self.research_max)
        if asks:
            decisions += self._research(asks, cat)
        self.add_chunk("context", {}, decisions, facts=self.new_pins, sources=self.new_sources)

    def _research(self, asks, cat):
        """The Research Tool's own units for the pairs the plan asked for: searched (or reused from the
        layers), quote-checked, merged and pinned into the brief with their sources."""
        from ..pipeline.research import Researcher
        from ..util import slug
        rl = self.caps.runlog
        pairs = [(a["lens"], a["market"]) for a in asks]
        plan = self.plan_out or {}
        brand = str(plan.get("brand") or "").strip()
        comps = [{"ref": "brand/" + slug(n), "name": n.strip()} for n in (plan.get("competitors") or [])[:5]
                 if isinstance(n, str) and n.strip() and slug(n) and n.strip().lower() != brand.lower()]
        r = Researcher(self.doc, self.base, self.clan, self.handler, self.caps, sorted({p[0] for p in pairs}),
                       sorted({p[1] for p in pairs}), [cat], pairs=pairs, competitors=comps,
                       subject={"ref": "brand/" + slug(brand), "name": brand} if brand and slug(brand) else None)
        focus = {(a["lens"], a["market"]): a.get("focus") for a in asks}
        with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as ex:
            units = list(ex.map(lambda p: r.unit(p[0], p[1], focus=focus.get(p)), pairs))
        for u in units:
            rl.note("research_unit", lens=u["lens"], market=u["market"], sources=len(u["sources"]),
                    facts=len(u["cands"]), reused=u["reused"], gaps=len(u["gaps"]), error=u["error"])
        cands = [c for u in units for c in u["cands"]]
        existing = ctx_facts(self.clan)
        pinned = {(f.get("entity"), f.get("key"), f.get("market")): f for f in existing}
        merged = merge_rules.merge(cands, pinned, set())
        t = iso()
        # the layer row records the decision it was written with; its full reasoning is given below
        dec = self.dec(("research",), "pin", "research_merge", [], [],
                       rsn.make("Pinned what the research found.", [rsn.point("Research for the brief's plan")],
                                rsn.certainty("medium", "quote-checked facts"), "the research is run again",
                                only_option="the plan asked for it"), "synthesise")
        pins = []
        for c in merged["pins"]:
            try:
                row = r._write(c, dec, status="active")
            except Exception as e:  # noqa: BLE001 - one fact that will not write is left out, on record
                rl.note("pin_failed", key=c.get("key"), error=f"{type(e).__name__}: {str(e)[:160]}")
                continue
            if (row["entity"], row["key"], row["market"]) in pinned:
                continue
            pins.append(r._pin(row, c, dec["id"], t))
        if merged["contests"]:
            rl.note("research_contests", left_out=[ct["key"] for ct in merged["contests"]])
        srcs = {s["sid"]: s for u in units for s in u["sources"]}
        self.new_pins = pins
        self.new_sources = r._source_records(pins, [], srcs, cands)
        self.hits += r.hits
        unit_pts = [rsn.point(f"{u['lens'].replace('_', ' ')} in {u['market']}: "
                              + (f"{len(u['cands'])} fact(s) from {len(u['sources'])} source(s)" if not u["error"]
                                 else f"failed ({_short(u['error'], 120)})"),
                              [s["sid"] for s in u["sources"]] or [self.plan_dec["id"]]) for u in units]
        if pins:
            unit_pts.append(rsn.point(f"{len(pins)} fact(s) passed the quote check and are pinned", [p["id"] for p in pins]))
        dec["targets"] = [f"{self.doc}#facts[{p['id']}]" for p in pins]
        dec["cites"] = [p["id"] for p in pins] + [s for p in pins for s in p["sources"]]
        rsn.give(dec, rsn.make(f"Researched what the plan asked for: {len(units)} run(s), {len(pins)} fact(s) pinned.",
                                    unit_pts, rsn.certainty("medium" if pins else "low",
                                                            "each fact's words match its source" if pins else
                                                            "nothing passed the quote check"),
                                    "the research is run again, or a person rejects a fact",
                                    only_option="the plan named what to research",
                                    attention=None if pins else "The research found nothing the brief could pin."))
        return [dec]

    # =========================================================================================
    # draft: Dara writes the whole brief
    # =========================================================================================
    def stage_draft(self):
        keys = [k for k in self._open_keys()]
        self.set_state(keys, "drafting", "drafter")
        ev = self._evidence()
        payload = {"client_said": self._said(), "captured": self._captured(),
                   "plan": self.plan_out or {"strategy": None},
                   "facts": ev["facts"], "findings": ev["findings"], "research_upstream": ev["research"],
                   "library": [{"id": pid, "citation": p["citation"], "text": p["text"][:900]}
                               for pid, p in list(self.lib.items())[:12]],
                   "parts": {k: WHAT.get(k, LABELS.get(k, k)) for k in keys},
                   "who_handles": {k: who for k, (_, who) in getattr(self, "routing", {}).items()}}
        out = self.caps.model.structured("draft_brief_whole", DRAFT_SYSTEM, payload, draft_schema(keys),
                                         max_tokens=16000)
        self.whole = self._clean_whole(out, ev)
        self.set_state([k for k in keys if k in self.whole], "waiting", "drafter")
        self.set_state([k for k in keys if k not in self.whole], "absent", "drafter")
        self.caps.runlog.note("draft", written=sorted(self.whole), empty=[k for k in keys if k not in self.whole],
                              basis={k: v["basis"] for k, v in self.whole.items()},
                              uncited_research=[k for k, v in self.whole.items() if v.get("downgraded")])

    def _clean_whole(self, out, ev):
        ids = self._ids(ev)
        res = {}
        for k, r in (out or {}).items():
            if k not in KEYS or not isinstance(r, dict):
                continue
            v = clean(k, r.get("value"))
            if v is None:
                continue
            cites = [c for c in dict.fromkeys(r.get("cites") or []) if c in ids]
            basis = r.get("basis") if r.get("basis") in BASES else "assumption"
            if basis == "client" and not filled(self._captured().get(k)):
                basis = "assumption"  # the client did not say it: Ellis captured nothing for this part
                self.caps.runlog.note("basis_corrected", part=k, said="client", now="assumption")
            down = basis in ("research", "library") and not cites
            if down:
                basis = "assumption"  # a research claim that cites nothing it was given is an assumption
            res[k] = {"value": v, "basis": basis, "cites": cites, "builds_on_client": bool(r.get("builds_on_client")),
                      "why": _short(r.get("why"), 300), "downgraded": down}
        return res

    # =========================================================================================
    # judge: Jude reviews the whole brief, Dara revises once, Jude scores again
    # =========================================================================================
    def _review(self, ev, purpose):
        brief = {k: {"value": v["value"], "basis": v["basis"], "cites": v["cites"]} for k, v in self.whole.items()}
        for k in KEYS:
            if k not in brief and filled(get(self.W, k)):
                brief[k] = {"value": get(self.W, k), "basis": "client", "cites": []}
        payload = {"brief": brief, "strategy": (self.plan_out or {}).get("strategy"), "facts": ev["facts"],
                   "findings": ev["findings"], "client_said": self._said()}
        rv = self.caps.model.structured(purpose, REVIEW_SYSTEM, payload, review_schema(list(KEYS)), max_tokens=8000)
        dims = {d["dimension"]: d for d in rv.get("dimensions") or []}
        rv["dimensions"] = [dims.get(d) or {"dimension": d, "verdict": "missing", "evidence": "-", "fix": "-"}
                            for d in DIMS]
        self.scores.append(score_of(rv))
        self.caps.runlog.note(purpose, score=self.scores[-1], of=14,
                              dims={d["dimension"]: d["verdict"] for d in rv["dimensions"]},
                              chain=rv["chain"]["verdict"],
                              claims={c["verdict"]: sum(1 for x in rv["claims"] if x["verdict"] == c["verdict"])
                                      for c in rv["claims"]},
                              notes=len(rv["notes"]), fixes=sum(1 for n in rv["notes"] if n["severity"] == "fix"),
                              revise=[x["part"] for x in rv["revise"]])
        return rv

    def stage_judge(self):
        if not self.whole:
            return super().stage_judge()  # nothing drafted as a whole: the field-by-field judge reviews what there is
        rl = self.caps.runlog
        keys = list(self.whole)
        self.set_state(keys, "judging", "judge")
        ev = self._evidence()
        if self.judge_mode == "jev" and self.caps.jev.configured:
            return self._judge_by_jev(ev)
        rv = self._review(ev, "review_brief")
        revise = [x for x in rv.get("revise") or [] if x["part"] in self.whole]
        if revise:
            parts = [x["part"] for x in revise]
            self.set_state(parts, "revising", "drafter")
            payload = {"client_said": self._said(), "captured": self._captured(), "plan": self.plan_out,
                       "brief": {k: v["value"] for k, v in self.whole.items()},
                       "rewrite": {x["part"]: x["instruction"] for x in revise},
                       "facts": ev["facts"], "findings": ev["findings"],
                       "library": [{"id": pid, "citation": p["citation"], "text": p["text"][:900]}
                                   for pid, p in list(self.lib.items())[:8]],
                       "parts": {k: WHAT.get(k, LABELS.get(k, k)) for k in parts}}
            try:
                out = self.caps.model.structured("rewrite_brief_parts", DRAFT_SYSTEM + "\n\nRewrite ONLY the parts in "
                                                 "`rewrite`, following each instruction, keeping the rest of the brief "
                                                 "in mind.", payload, draft_schema(parts), max_tokens=8000)
                got = self._clean_whole(out, ev)
                for k, v in got.items():
                    v["revised"] = True
                    self.whole[k] = v
                rl.note("revised", parts=sorted(got))
                self.set_state(parts, "judging", "judge")
                rv = self._review(ev, "review_brief_again")
            except Exception as e:  # noqa: BLE001 - the first review stands
                rl.note("revise_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        self.review_out = rv
        self._place(rv, ev)

    def _judge_by_jev(self, ev):
        """The check loop on the whole draft: jev flags, Jude says why, Dara rewrites; then the
        brief is placed with jev's reading as the review."""
        from . import checkloop
        values = {k: v["value"] for k, v in self.whole.items()}
        for k in KEYS:
            if k not in values and filled(get(self.W, k)):
                values[k] = get(self.W, k)
        lib = [{"id": pid, "citation": p["citation"], "text": p["text"][:900]} for pid, p in list(self.lib.items())[:8]]
        res = checkloop.run(self, values, self._said(), self._open_keys(), ev, lib, self.plan_out)
        if res["rewritten"]:
            self.set_state(list(res["rewritten"]), "revising", "drafter")
        for k, v in res["rewritten"].items():
            self.whole[k] = v
        self.set_state(list(self.whole), "judging", "judge")
        read = res["after"] or res["before"]
        if read is None:  # jev did not answer: Jude reviews the whole brief as before
            return self._place(self._review(ev, "review_brief"), ev)
        chain = (res["jude"] or {}).get("chain") or {}
        notes = [{"parts": [p for p in chain.get("parts") or [] if p in KEYS], "severity": "fix",
                  "note": chain["issue"]}] if chain.get("issue") and chain.get("parts") else []
        rv = {"dimensions": [{"dimension": d, "verdict": read["dims"][d], "evidence": "", "fix": ""} for d in DIMS],
              "chain": {"verdict": "pass" if read["chain"] >= 0.5 else "fail",
                        "note": chain.get("issue") or ("jev reads it as one argument" if read["chain"] >= 0.5
                                                       else "jev doubts the argument holds")},
              "claims": [], "notes": notes, "revise": [], "questions": res["questions"],
              "single_mindedness": {"verdict": "single", "split_into": []},
              "summary": (f"jev checked every part and flagged {sum(1 for x in res['before']['p'].values() if x >= 0.5)}"
                          f"; Jude sent {len(res['todo'])} back to Dara, who rewrote {len(res['rewritten'])}.")
              if res["before"] else "Checked."}
        self.scores = [x["score"] for x in (res["before"], res["after"]) if x]
        self._place(rv, ev)

    # -- placing ---------------------------------------------------------------------------------
    def _place(self, rv, ev):
        patch, decisions = {}, []
        mat_ids = [m.id for m in self.mats if m.readable]
        plan_id = self.plan_dec["id"] if self.plan_dec else None
        passages_out = {}
        claim_bad = {}
        for c in rv.get("claims") or []:
            if c["verdict"] != "supported":
                claim_bad.setdefault(c["part"], []).append(c)
        assumed = []
        for k, v in self.whole.items():
            if k == "open_questions":
                continue
            cites = list(v["cites"])
            if v["basis"] == "client":
                pts = [rsn.point(f"The client's words, written out: {_short(v['why'], 240)}", mat_ids)]
                lvl, why = "high", "the client said it"
            elif v["basis"] in ("research", "library"):
                pts = [rsn.point(_short(v["why"], 260) or "Rests on what it cites", cites)]
                bad = claim_bad.get(k)
                lvl = "low" if bad else "medium"
                why = ("Jude found a claim its fact does not fully support" if bad
                       else "rests on the facts and passages it cites")
            else:
                assumed.append(k)
                pts = [rsn.point(_short(v["why"], 260) or "A planner's proposal", [plan_id] if plan_id else mat_ids)]
                lvl, why = "low", "a planner's assumption, for the agency to confirm with the client"
            if v.get("builds_on_client"):
                pts.append(rsn.point("It builds on what the client wrote", mat_ids))
            pts = [p if p.get("cites") or not rsn.states_figure(p["point"]) else
                   {**p, "cites": [plan_id] if plan_id else mat_ids} for p in pts]
            attn = None
            if claim_bad.get(k):
                attn = "Check: " + " ".join(_short(f"{c['claim']} — {c['note']}", 200) for c in claim_bad[k][:2])
            r = rsn.make(f"Wrote the {LABELS.get(k, k)}" + (" after Jude's note." if v.get("revised") else "."), pts,
                         rsn.certainty(lvl, why), "the client says otherwise, the research changes, or a person edits it",
                         only_option="one brief, written as a whole", attention=attn)
            psg = [c for c in cites if c in self.lib]
            for c in psg:
                passages_out[c] = self.lib[c]
            if self.held.get(k):
                d = self.dec(("propose", k, "whole"), "edit", "propose", [k], cites, r, "drafter",
                             proposed_value=v["value"], basis=v["basis"])
                self.proposals.append({"field": k, "value": v["value"], "decision": d["id"]})
                self.set_state([k], "proposed", "judge")
            else:
                d = self.dec(("draft", k, "whole"), "edit", "draft", [k], cites, r, "drafter", basis=v["basis"])
                put(patch, k, v["value"])
                self.set_state([k], "done", "judge")
            self.writer[k] = d["id"]
            self.basis[k] = v["basis"]
            decisions.append(d)
        # Jude: one note per issue, naming the parts it touches
        for i, n in enumerate(rv.get("notes") or []):
            parts = [p for p in n["parts"] if p in KEYS and p not in self.locked]
            if not parts:
                continue
            cites = [self.writer[p] for p in parts if p in self.writer] or [f"{self.doc}#{parts[0]}"]
            fix = n["severity"] == "fix"
            r = rsn.make(_short(n["note"], 300), [rsn.point(_short(n["note"], 400), cites)],
                         rsn.certainty("medium", "a model judged the brief as a whole"),
                         "the parts it names change", only_option="one note for one issue",
                         attention=("Jude suggests a change." if fix else None))
            decisions.append(self.dec(("note", i), "verdict", "judge", parts, cites, r, "judge",
                                      polarity="bad" if fix else "good", taxonomy_version="reason-codes/1",
                                      **({"reason_code": "brief_review"} if fix else {})))
        # the open questions: the plan's, the draft's and Jude's, once each
        qs = [q["question"] for q in (self.plan_out or {}).get("ask_client") or []]
        qs += list((self.whole.get("open_questions") or {}).get("value") or [])
        qs += list(rv.get("questions") or [])
        seen, oq = set(), []
        for q in qs:
            key = " ".join(q.lower().split())[:80]
            if key not in seen:
                seen.add(key)
                oq.append(q.strip())
        oq = clean("open_questions", oq[:12])
        if oq and "open_questions" not in self.locked:
            r = rsn.make(f"Gathered {len(oq)} question(s) only the client can answer.",
                         [rsn.point("From the plan, the draft and Jude's review", [plan_id] if plan_id else mat_ids)],
                         rsn.certainty("high", "each is something the brief cannot settle without the client"),
                         "the client answers", only_option="the questions follow from what is missing")
            if self.held.get("open_questions"):
                d = self.dec(("propose", "open_questions", "whole"), "edit", "propose", ["open_questions"],
                             [plan_id] if plan_id else [], r, "judge", proposed_value=oq)
                self.proposals.append({"field": "open_questions", "value": oq, "decision": d["id"]})
            else:
                d = self.dec(("questions", "whole"), "edit", "questions", ["open_questions"],
                             [plan_id] if plan_id else [], r, "judge")
                put(patch, "open_questions", oq)
            decisions.append(d)
            self.set_state(["open_questions"], "done", "judge")
        # the review itself
        t = iso()
        sc = {"dimensions": [{"dimension": d["dimension"], "verdict": d["verdict"],
                              **({"evidence": d["evidence"]} if str(d.get("evidence") or "").strip() else {}),
                              **({"fix": d["fix"]} if str(d.get("fix") or "").strip() else {})}
                             for d in rv["dimensions"]],
              "single_mindedness": rv["single_mindedness"], "summary": rv["summary"] or "-"}
        health = round(100 * score_of(rv) / 14)
        notes_by = {}
        for n in rv.get("notes") or []:
            for p in n["parts"]:
                notes_by.setdefault(p, []).append(n)
        fields = {}
        for k in KEYS:
            if k in self.locked:
                fields[k] = {"outcome": "kept", "checks": []}
            elif k not in self.whole:
                fields[k] = {"outcome": "absent", "checks": []}
            else:
                ns = notes_by.get(k, [])
                fields[k] = {"outcome": "revised" if self.whole[k].get("revised") else
                             ("failed" if any(n["severity"] == "fix" for n in ns) else "passed"),
                             "checks": [{"check": "whole_brief_review", "method": "llm",
                                         "status": "review" if n["severity"] == "fix" else "pass",
                                         "note": _short(n["note"], 300)} for n in ns[:3]]
                             + [{"check": "claim_supported", "method": "llm",
                                 "status": "pass" if c["verdict"] == "supported" else "review",
                                 "note": _short(f"{c['claim']}: {c['note']}", 300)}
                                for c in (rv.get("claims") or []) if c["part"] == k][:3]}
        dim = {d["dimension"]: d["verdict"] for d in rv["dimensions"]}
        st = lambda v: "pass" if v == "pass" else "review" if v == "vague" else "fail"  # noqa: E731
        dod = [{"id": "objectives_linked", "status": st(dim["objectives_quality"])},
               {"id": "smp_single", "status": st(dim["single_minded_message"])},
               {"id": "rtb_supports_smp", "status": "pass" if rv["chain"]["verdict"] == "pass" else "fail"},
               {"id": "evaluation_present", "status": st(dim["evaluation_criteria"])},
               {"id": "all_required_filled", "status": "pass" if all(
                   k in self.whole or filled(get(self.W, k)) for k in KEYS if k != "open_questions") else "fail"}]
        patch["review"] = {"built_at": t, "handler": self.handler, "based_on": {"version": self.base},
                           "scorecard": sc,
                           "judge": {"reason_codes_version": "1", "fields": fields,
                                     "dependencies": [{"id": "chain", "status": "pass" if rv["chain"]["verdict"] == "pass"
                                                       else "fail", "note": _short(rv["chain"]["note"], 300) or "-"}],
                                     "definition_of_done": dod, "health": health}}
        for pid, p in passages_out.items():
            patch.setdefault("passages", {})[pid] = p
        vids = [d["id"] for d in decisions if d["kind"] == "verdict"]
        pts = [rsn.point(f"BetterBriefs: {sum(1 for d in rv['dimensions'] if d['verdict'] == 'pass')} pass, "
                         f"{sum(1 for d in rv['dimensions'] if d['verdict'] == 'vague')} vague, "
                         f"{sum(1 for d in rv['dimensions'] if d['verdict'] == 'missing')} missing",
                         vids or [f"{self.doc}#review"]),
               rsn.point(f"The argument from objective to proof: {rv['chain']['verdict']} — "
                         f"{_short(rv['chain']['note'], 200)}", vids or [f"{self.doc}#review"])]
        if assumed:
            pts.append(rsn.point("Assumed, to confirm with the client: " + ", ".join(LABELS.get(k, k) for k in assumed),
                                 [self.writer[k] for k in assumed if k in self.writer]))
        if len(self.scores) > 1:
            pts.append(rsn.point(f"Score before Dara's revision {self.scores[0]}, after {self.scores[-1]}, of 14",
                                 vids or [f"{self.doc}#review"]))
        r = rsn.make(f"Reviewed the brief as a whole: {_short(rv['summary'], 200)}", pts,
                     rsn.certainty("medium", "a model judged the whole brief against the rubric and its sources"),
                     "a part changes and the brief is reviewed again", only_option="one review of the whole brief",
                     attention=(f"{len(assumed)} part(s) are assumptions to confirm with the client." if assumed else None))
        decisions.append(self.dec(("review", "whole"), "edit", "review", ["review"], vids, r, "judge"))
        self.add_chunk("judge", patch, decisions)
        with self.lock:
            for k, f in self.fields.items():
                if f["state"] in ("waiting", "judging", "revising", "drafting"):
                    self.fields[k] = {"state": "absent" if not filled(get(self.W, k)) else "done", "by": "judge"}
