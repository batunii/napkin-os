"""The draft stage (middleware-api.md §10.3): one drafter per strategy field.

Order (the owner, 2026-09-24): insight -> single-minded proposition run as a
CHAIN — the SMP drafts from the chosen insight — and every other drafted
field runs in parallel with that chain.

A drafter builds its loop's retrieval query from the working brief (the
captured fields' values — never raw material, never the capture itself), asks
the retrieval port for the packs whose `loops` include its loop (and, for
insight and substantiation, each case pack at its own k), and drafts: insight
and SMP by tournament (N candidates in one call, ranked, auto-gated, one
sharpen pass), the rest once. It may cite passages (`psg_…`), capture items
(`cap_…`) and pins in `clan.facts` (`f_…`) — nothing else; every cite is
checked against the ids it was given. Drafters never see each other's drafts
(the SMP sees the chosen insight's VALUE only, by the owner's chain).

Ported from engine/parse_brief.py `_gen_field_system`, `_judge_hero_candidates`,
`_refine_field`, `SMP_ANGLE_SEEDS`, `LOOP37_SPECS`, `loops_3_7`. The engine's
self-reported `confidence` is not asked for (R1).
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import reasoning as rsn
from ..doc import ctx_facts
from ..retrieval import RetrievalError
from ..util import iso
from .fields import LABELS, RUBRIC_FIELDS
from .rubric import auto_gate

log = logging.getLogger("napkin.brief.draft")

N_CANDIDATES = 4
SMP_ANGLE_SEEDS = (
    "lead with the contrast between what this audience settles for and what they could deliberately choose",
    "lead with how this audience is judged or perceived by others — the product as a verdict on their standards",
    "lead with the competitive edge or advantage the product gives them over rivals",
    "lead with the brand's own craft / design / engineering equity as the reason to choose it",
)
CASE_LOOPS = ("loop4_insight", "loop6_substantiation")

GROUNDS_GUIDE = """
Also return `grounds` for the value: the evidence in the input that supports it.
- because: one point per item of evidence. Each point cites the ids it rests on — passage ids (psg_…),
  capture item ids (cap_…) and pin ids (f_…) exactly as they appear in the input. Cite nothing else. A point
  that states a figure must cite the pin (f_…) that holds it.
- rejected: the other directions the input allowed and why each lost (empty only when there was one; then
  say why in only_option, otherwise only_option is null).
- would_change_if: what new evidence would change the value.
- attention: a short note when a person should check this, or null.
Do not rate your own confidence: it is derived from the evidence elsewhere."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def value_schema(rid: str) -> dict:
    t = RUBRIC_FIELDS[rid].get("type")
    if t == "list":
        return {"type": "array", "items": {"type": "string"}, "minItems": 1}
    if t == "tfd":
        return _obj({"think": {"type": "string"}, "feel": {"type": "string"}, "do": {"type": "string"}})
    return {"type": "string", "minLength": 1}


def field_system(rid: str, n: int = 1) -> str:
    """engine `_gen_field_system`, schema-driven, without the confidence."""
    f = RUBRIC_FIELDS[rid]
    lim = f"Hard limit: {f['max_words']} words.\n" if f.get("max_words") else ""
    if f.get("max_items"):
        lim += f"At most {f['max_items']} items, strongest first.\n"
    own = ""
    if rid in ("insight", "smp"):
        own = ("\nOWNABLE TENSION: claim territory the named competitor does NOT own. If every rival in the category "
               "would nod at your line, it is a category truth and a FAIL — use the competitor context to find the "
               "white space. Reconcile the WHOLE stated audience. Never restate the brand's own existing vision / "
               "mission / tagline; that is not a campaign proposition.\n")
    if f.get("type") == "tfd":
        own += "\n'do' is a concrete, observable behaviour — never 'engage with', 'explore', 'interact with'.\n"
    head = (f"You are a senior strategy planner writing the '{f['label']}' field of a brief for THIS specific brand. "
            f"Write it now — do not extract it, derive it.\n\n"
            f"WHAT THIS FIELD IS: {f.get('prompt', '')}\n{lim}\n"
            f"STYLE REFERENCE (a DIFFERENT brand — copy the depth/shape ONLY, never its words, brand or topic):\n"
            f"  {f.get('good_example', '')}\n\n"
            f"BAD — never produce anything like this:\n  {f.get('bad_example', '')}  ({f.get('bad_reason', '')})\n"
            f"{own}\n"
            "Reason from the working brief, the client's captured words and the precedent passages given. The "
            "passages are for SHAPE and DEPTH only — do not borrow their words, brands or themes. Be specific to "
            "this brand: a line that could be pasted onto a different brief is a failure. You are synthesising "
            "strategy, never inventing client facts. When the planner gave guidance, follow it.\n")
    if n > 1:
        head += (f"\nWrite {n} GENUINELY DISTINCT drafts — different strategic ideas, not rewordings of one idea. "
                 f"Make them compete. Each draft carries its own grounds.\n")
    return head + GROUNDS_GUIDE


RANK_SYSTEM = ("You are a strategy director ranking candidate '{label}' lines for a creative brief. Reward PURITY "
               "and single-mindedness (one idea, never a list or an 'and'), ownable territory (a direct rival could "
               "not say the same line), and a real human tension specific to THIS brand. Penalise category truths "
               "everyone would nod at, restated brand taglines, and anything trying to say two things. Judge each "
               "on: {crit}. Return the candidate indexes best first, one line on why the winner wins, and one line "
               "on why each other candidate lost.")
SHARPEN_NOTE = ("REFINE MODE: you are given a strong draft. Make it PURER and more single-minded — one idea only, "
                "sharper, more ownable. Keep what already works; never add a second idea. If it is already optimal, "
                "return it unchanged.")
REVISE_NOTE = ("REVISION: an independent judge failed the current draft on the checks listed. Rewrite it so every "
               "failed check passes, following each fix. Keep what already works; one idea only.")


class Draft:
    def __init__(self, group, keys, rid):
        self.group, self.keys, self.rid = group, keys, rid
        self.value = None          # the group's value (tfd: a dict of the three leaves)
        self.grounds = None        # the model's grounds for the chosen value
        self.rejected = []         # [rej] losing candidates, with why
        self.passages = {}         # psg id -> the passage record written into data.passages
        self.known = set()         # every id the drafter was given
        self.attention = []
        self.error = None
        self.revised = False
        self.packs_cited = set()

    def leaf(self, key):
        if isinstance(self.value, dict):
            return self.value.get(key.split(".", 1)[1]) if "." in key else None
        return self.value


class DraftContext:
    """What every drafter reads: the working brief, the capture items, the
    pins, the retrieval and model capabilities. Never raw material."""

    def __init__(self, caps, working: dict, capture_items: dict, clan: dict, guidance: str | None = None,
                 current: dict | None = None):
        self.caps, self.working, self.guidance = caps, working, guidance
        self.current = current or {}  # every field's value as the document holds it (the SMP's insight, the ladder)
        self.capture = [{"id": cid, "key": it["key"], "value": it["value"], "status": it["status"]}
                        for cid, it in list((capture_items or {}).items())[:40]]
        self.pins = [{"id": f["id"], "entity": f.get("entity"), "key": f.get("key"), "value": f.get("value"),
                      "unit": f.get("unit"), "as_of": f.get("as_of"), "layer": f.get("layer"),
                      "origin": f.get("origin")} for f in ctx_facts(clan) if isinstance(f.get("id"), str)][:40]
        self.hits = []
        self._lock = threading.Lock()
        self.org = caps.scope["org"]

    def gist(self) -> dict:
        w = self.working
        objs = "; ".join(v for k, v in w.items() if k.startswith("objectives.") and isinstance(v, str))
        return {"problem": w.get("background") or "", "objective": objs, "audience": w.get("audience") or "",
                "competitors": w.get("competitor_context") or ""}

    def retrieve(self, loop: str, query: str, case_query: str | None) -> tuple[dict, list[str]]:
        """Passages for a loop: {psg id: record} and notes on what failed."""
        ret = self.caps.retrieval
        if not ret.configured:
            return {}, ["no retrieval service is configured: drafted without precedent passages"]
        out, notes = {}, []
        try:
            packs = ret.packs()
            general = [p["tag"] for p in packs if p.get("kind") != "case" and (not p.get("loops") or loop in p["loops"])]
            asks = []
            if general:
                asks.append((query, 5, general))
            if loop in CASE_LOOPS:
                for p in packs:
                    if p.get("kind") == "case" and (not p.get("loops") or loop in p["loops"]):
                        asks.append((case_query or query, int(p.get("k") or 2), [p["tag"]]))
            for q, k, tags in asks:
                r = ret.retrieve(q, k, packs=tags, purpose=loop)
                t = iso()
                for p in r["passages"]:
                    ver = r["trace"]["packs"].get(p["pack"])
                    if not ver or p["id"] in out:
                        continue  # a passage whose corpus version is unknown cannot be cited honestly
                    out[p["id"]] = {"uri": p["uri"], "pack": p["pack"], "scope": p["scope"], "licence": p["licence"],
                                    "source": p["source"], "section": p["section"], "citation": p["citation"],
                                    "text": p["text"], "text_sha256": p["text_sha256"], "pack_version": ver,
                                    "retrieved_at": t}
            if not out:
                notes.append(f"retrieval returned no passages for {loop}")
        except RetrievalError as e:
            notes.append(f"retrieval failed ({str(e)[:120]}): drafted without precedent passages")
        with self._lock:
            for pid, p in out.items():
                self.hits.append({"id": pid, "scope": "house" if p["scope"] == "house" else f"agency:{self.org}",
                                  "source": p["uri"]})
        return out, notes

    def payload(self, passages: dict, **extra) -> dict:
        body = {"working_brief": self.working, "capture": self.capture, "pins": self.pins,
                "passages": [{"id": k, "citation": v["citation"], "text": v["text"]} for k, v in passages.items()]}
        if self.guidance:
            body["planner_guidance"] = self.guidance
        body.update({k: v for k, v in extra.items() if v not in (None, "", {}, [])})
        return body

    def known(self, passages: dict) -> set:
        return set(passages) | {c["id"] for c in self.capture} | {p["id"] for p in self.pins}


def _query(loop: str, g: dict, insight: str | None, for_group: str) -> tuple[str, str | None]:
    """engine LOOP37_SPECS, from the working brief only."""
    if loop == "loop4_insight":
        return (f"find the human insight and cultural tension for {g['audience']} given {g['problem']}",
                f"award-winning precedent insight {g['audience']} {g['problem']}")
    if loop == "loop5_proposition":
        if for_group == "single_minded_proposition":
            return f"single-minded proposition to achieve {g['objective']}; insight: {insight or ''}", None
        return f"desired response think feel do that ladders to {g['objective']}; {insight or ''}", None
    if loop == "loop6_substantiation":
        return (f"effectiveness evidence and proof a strategy delivers {g['objective']}; how brands grow",
                f"award-winning effectiveness results proof {g['objective']}")
    if loop == "loop7_qa":
        return f"common mistakes and decision rules to pressure-test {insight or ''} for {g['objective']}", None
    return f"{loop.replace('_', ' ')} {g['problem']} {g['objective']}", None


class Drafter:
    def __init__(self, ctx: DraftContext, group: str, keys: list[str], rid: str, loop: str, only_leaf: str | None = None):
        self.ctx, self.group, self.keys, self.rid, self.loop, self.only_leaf = ctx, group, keys, rid, loop, only_leaf
        self.hero = rid in ("insight", "smp")

    def _call(self, purpose, system, payload, schema):
        return self.ctx.caps.model.structured(purpose, system, payload, schema, max_tokens=6000)

    def draft(self, insight: str | None = None) -> Draft:
        d = Draft(self.group, self.keys, self.rid)
        g = self.ctx.gist()
        q, cq = _query(self.loop, g, insight, self.group)
        d.passages, notes = self.ctx.retrieve(self.loop, q, cq)
        d.attention += notes
        d.known = self.ctx.known(d.passages)
        extra = {"insight": insight if self.group != "insight" else None, "field": LABELS.get(self.group, self.group)}
        if self.group.startswith("desired_response") and self.only_leaf:
            extra["redraft_only"] = self.only_leaf.split(".", 1)[1]
            extra["current_desired_response"] = {k.split(".")[1]: v for k, v in self.ctx.current.items()
                                                 if k.startswith("desired_response.") and v}
        vs = value_schema(self.rid)
        grounds = rsn.MODEL_SCHEMA
        purpose = f"draft_{self.rid}"
        try:
            if self.hero:
                n = N_CANDIDATES
                if self.rid == "smp":
                    extra["angles"] = [SMP_ANGLE_SEEDS[i % len(SMP_ANGLE_SEEDS)] for i in range(n)]
                raw = self._call(purpose, field_system(self.rid, n), self.ctx.payload(d.passages, n=n, **extra),
                                 _obj({"candidates": {"type": "array", "minItems": 2, "maxItems": n,
                                                      "items": _obj({"value": vs, "grounds": grounds})}}))
                cands = raw["candidates"]
                order, why_win, losers = self._rank(cands)
                cands = [cands[i] for i in order]
                chosen, chosen_fails = None, None
                for c in cands:  # the best-ranked candidate that clears the auto checks
                    ok, fails = auto_gate(self.rid, c["value"])
                    if ok:
                        chosen, chosen_fails = c, []
                        break
                    if chosen is None:
                        chosen, chosen_fails = c, fails
                for i, c in zip(order, cands):
                    if c is chosen:
                        continue
                    ok, fails = auto_gate(self.rid, c["value"])
                    why = losers.get(i) or ("failed an auto check: " + "; ".join(fails) if not ok else "ranked lower")
                    d.rejected.append(rsn.rej(_short(c["value"]), why))
                d.value, d.grounds = chosen["value"], chosen["grounds"]
                if chosen_fails:
                    d.attention.append("no candidate cleared the auto checks: " + "; ".join(chosen_fails))
                # one sharpen pass; kept only if it still clears the auto checks
                sharp = self._call(f"sharpen_{self.rid}", field_system(self.rid) + "\n" + SHARPEN_NOTE,
                                   self.ctx.payload(d.passages, draft=d.value, director_note=why_win, **extra),
                                   _obj({"value": vs, "grounds": grounds}))
                if auto_gate(self.rid, sharp["value"])[0] and sharp["value"] != d.value:
                    d.rejected.insert(0, rsn.rej(_short(d.value), "the sharpened version is purer"))
                    d.value, d.grounds = sharp["value"], sharp["grounds"]
            else:
                raw = self._call(purpose, field_system(self.rid), self.ctx.payload(d.passages, **extra),
                                 _obj({"value": vs, "grounds": grounds}))
                d.value, d.grounds = raw["value"], raw["grounds"]
        except Exception as e:  # a model failure makes this field failed; the others continue (§10.11)
            d.error = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("drafter %s failed: %s", self.group, d.error)
        d.value = _clean_value(self.rid, d.value)
        if d.error is None and d.value is None:
            d.error = "the drafter returned an empty value"
        return d

    def _rank(self, cands):
        f = RUBRIC_FIELDS[self.rid]
        crit = "; ".join(f"{r['id']}: {r['test']}" for r in f.get("rubric", []) if r["method"] == "llm")
        raw = self._call(f"rank_{self.rid}", RANK_SYSTEM.format(label=f["label"], crit=crit),
                         {"field": f["label"], "good_example": f.get("good_example"), "bad_example": f.get("bad_example"),
                          "candidates": [{"index": i, "value": c["value"]} for i, c in enumerate(cands)]},
                         _obj({"ranking": {"type": "array", "items": {"type": "integer"}}, "why_winner": {"type": "string"},
                               "losers": {"type": "array", "items": _obj({"index": {"type": "integer"},
                                                                          "why": {"type": "string"}})}}))
        order = [i for i in dict.fromkeys(raw["ranking"]) if isinstance(i, int) and 0 <= i < len(cands)]
        order += [i for i in range(len(cands)) if i not in order]
        losers = {x["index"]: x["why"].strip() for x in raw.get("losers") or [] if (x.get("why") or "").strip()}
        return order, raw.get("why_winner") or "", losers

    def revise(self, d: Draft, failures: list[dict], insight: str | None = None) -> Draft:
        """One revision with the Judge's fixes. The drafter keeps its passages;
        the Judge's reasoning never reaches it — only the failed checks."""
        extra = {"insight": insight if self.group != "insight" else None, "field": LABELS.get(self.group, self.group)}
        try:
            raw = self._call(f"revise_{self.rid}", field_system(self.rid) + "\n" + REVISE_NOTE,
                             self.ctx.payload(d.passages, current_draft=d.value, failed_checks=failures, **extra),
                             _obj({"value": value_schema(self.rid), "grounds": rsn.MODEL_SCHEMA}))
        except Exception as e:
            d.attention.append(f"the revision failed ({type(e).__name__})")
            return d
        v = _clean_value(self.rid, raw["value"])
        if v is None:
            return d
        d.rejected.insert(0, rsn.rej(_short(d.value), "failed the judge: " +
                                     "; ".join(f"{x['check']}" for x in failures)))
        d.value, d.grounds, d.revised = v, raw["grounds"], True
        return d


def _clean_value(rid, v):
    t = RUBRIC_FIELDS[rid].get("type")
    if t == "list":
        items = [x.strip() for x in v or [] if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
        return items or None
    if t == "tfd":
        if not isinstance(v, dict):
            return None
        out = {k: v[k].strip() for k in ("think", "feel", "do") if isinstance(v.get(k), str) and v[k].strip()}
        return out or None
    return v.strip() if isinstance(v, str) and v.strip() else None


def _short(v) -> str:
    if isinstance(v, dict):
        v = " / ".join(f"{k}: {x}" for k, x in v.items())
    elif isinstance(v, list):
        v = "; ".join(v)
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v if len(v) <= 240 else v[:237] + "…"


def run_drafters(ctx: DraftContext, plan: list[Drafter], on_state) -> dict:
    """Run the drafters: insight -> SMP as a chain, the rest in parallel with
    it. `on_state(group, state)` reports progress. Returns {group: Draft}."""
    by = {d.group: d for d in plan}
    out: dict = {}

    def one(dr: Drafter, insight=None):
        on_state(dr.group, "drafting")
        res = dr.draft(insight=insight)
        out[dr.group] = res
        on_state(dr.group, "failed" if res.error else "drafted")
        return res

    def chain():
        ins = None
        if "insight" in by:
            r = one(by["insight"])
            ins = r.value if not r.error else None
        if ins is None:
            ins = ctx.current.get("insight")
        if "single_minded_proposition" in by:
            one(by["single_minded_proposition"], insight=ins)

    rest = [d for g, d in by.items() if g not in ("insight", "single_minded_proposition")]
    with ThreadPoolExecutor(max_workers=1 + max(1, len(rest))) as ex:
        futs = [ex.submit(chain)]
        insight_now = ctx.current.get("insight")
        futs += [ex.submit(one, d, insight_now) for d in rest]
        for f in futs:
            f.result()
    return out
