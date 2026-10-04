"""ask: a person's question about the research, answered where they asked it.

The question is answered first from what the document already holds (its pins
and findings). jev then says whether that answer answers the question; when it
does not, or nothing could be said, the lens's researchers search with the
question as their focus, and the question is answered again over everything,
the new facts included. Without jev, the answer's own `missing` list decides.

The answer is written to `data.asks[<id>]`: under the section it was asked in
(its lens), or as a section of its own when the person asked about something
the research did not cover. Every sentence is checked in code like a report
claim (it cites pins or findings the document holds, and states no figure they
do not). jev's scores go in the decisions' reasoning, never on the page.
"""

from __future__ import annotations

import logging

from ..doc import LENS_TITLES, LENSES, build_materials, ctx_data, ctx_facts, ctx_findings, decision, field_value, \
    lens_of_key, read_of
from ..jev import JevError, noul
from ..rules.cite import clean_claim
from ..util import bad, iso, uid, ulid_like
from .. import reasoning as rsn
from .research import Researcher, validate_research

log = logging.getLogger("napkin.ask")

# jev's "it answers the question" at or above this: no search.
ANSWERS = 0.6
# at most this many web searches for one question
SEARCHES = 2
MAX_PINS = 160

SYSTEM = """You answer an advertising planner's question about their campaign research, from the pinned
facts and findings you are given and nothing else. Write one to four plain sentences, each citing the ids
it rests on. State no number that is not the value of a pin you cite (or written in a finding you cite).
When the facts do not answer the question, say what they do show, if anything, and list in `missing` what
would be needed to answer it; never fill a gap from outside knowledge. `lens` is the research lens the
question belongs to (keep the one given, when one is given). `topic` is a short heading for the question,
two to five words."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def schema(cite_ids: list[str]) -> dict:
    claim = _obj({"text": {"type": "string"},
                  "cites": {"type": "array", "items": {"type": "string", "enum": cite_ids or ["(none)"]}}})
    return _obj({"sentences": {"type": "array", "items": claim},
                 "missing": {"type": "array", "items": {"type": "string"}},
                 "lens": {"type": "string", "enum": LENSES},
                 "topic": {"type": "string"}})


def validate(clan: dict, inp: dict) -> tuple[str, str | None]:
    q = inp.get("question")
    if not isinstance(q, str) or not q.strip():
        raise bad("input.question is required: what the person asked")
    if len(q) > 1000:
        raise bad("input.question is longer than 1000 characters")
    lens = inp.get("lens")
    if lens is not None and lens not in LENSES:
        raise bad(f"input.lens, when given, is one of {LENSES}")
    validate_research(clan, {"lenses": [lens or LENSES[0]]})  # markets and categories, as research needs
    return q.strip(), lens


class Asker:
    def __init__(self, doc, base, clan, handler, caps, question, lens, settings, attachments=()):
        self.doc, self.base, self.clan, self.handler, self.caps = doc, base, clan, handler, caps
        self.question, self.lens, self.settings = question, lens, settings
        self.data = ctx_data(clan)
        # a search reads the documents attached to the campaign too (the host adds their text)
        self.materials, _ = build_materials({"attachments": list(attachments or [])}, self.data)
        self.aid = ulid_like("ask_", 1, doc, question)

    # -- the evidence ------------------------------------------------------------
    def evidence(self, lens, extra=()):
        pins = [f for f in list(ctx_facts(self.clan)) + list(extra)
                if str(f.get("id", "")).startswith("f_") and f.get("status") != "superseded"]
        excluded = {e.get("fact_id") for e in (self.data.get("selection") or {}).get("excluded") or []}
        pins = [p for p in pins if p["id"] not in excluded]
        if lens:
            mine = [p for p in pins if lens_of_key(p.get("key")) == lens]
            pins = mine + [p for p in pins if p not in mine]  # its own first; the rest may answer it too
        pins = list({p["id"]: p for p in pins}.values())[:MAX_PINS]
        ids = {p["id"] for p in pins}
        fis = [f for f in ctx_findings(self.clan) if f.get("status") in ("proposed", "verified")
               and str(f.get("id", "")).startswith("fi_") and set(f.get("cites") or []) & ids]
        return {p["id"]: p for p in pins}, {f["id"]: f for f in fis}

    def names(self):
        brand = field_value(self.data, "brand") or {}
        return [brand.get("name", "")] + [c.get("name", "") for c in field_value(self.data, "competitor_set") or []
                                          if isinstance(c, dict)]

    # -- one answer ----------------------------------------------------------------
    def answer(self, lens, pin_by, fi_by):
        """-> (sentences, missing, lens, topic, dropped)."""
        payload = {"question": self.question, "lens": lens,
                   "lens_title": LENS_TITLES.get(lens) if lens else None,
                   "brand": (field_value(self.data, "brand") or {}).get("name"),
                   "markets": field_value(self.data, "markets") or [],
                   "pins": [{"id": p["id"], "entity": p.get("entity"), "key": p.get("key"), "value": p.get("value"),
                             "unit": p.get("unit"), "market": p.get("market"), "as_of": p.get("as_of"),
                             "quote": next(iter((p.get("quotes") or {}).values()), None)} for p in pin_by.values()],
                   "findings": [{"id": f["id"], "statement": f.get("statement")} for f in fi_by.values()]}
        raw = self.caps.model.structured("ask_answer", SYSTEM, payload, schema(sorted(pin_by) + sorted(fi_by)),
                                         max_tokens=2000)
        out, dropped = [], []
        for s in raw.get("sentences") or []:
            c, why = clean_claim(s.get("text"), s.get("cites"), pin_by, fi_by, self.names())
            out.append(c) if c else dropped.append(why)
        missing = [str(m).strip() for m in raw.get("missing") or [] if str(m).strip()][:4]
        got = lens or (raw.get("lens") if raw.get("lens") in LENSES else None) or LENSES[0]
        topic = str(raw.get("topic") or "").strip()[:80] or self.question[:80]
        return out[:4], missing, got, topic, dropped

    def check(self, sentences, pin_by, fi_by):
        """jev on the answer -> ({answers, grounded} probabilities, None) or (None, why it was not checked)."""
        if not sentences:
            return None, "nothing could be said from the facts"
        if not self.caps.jev.configured:
            return None, "jev is not configured"
        used = {c for s in sentences for c in s["cites"]}
        facts = [f"{p.get('key')} = {p.get('value')}{' (' + p['market'] + ')' if p.get('market') else ''}"
                 for i, p in pin_by.items() if i in used] + [f["statement"] for i, f in fi_by.items() if i in used]
        state = {"question": self.question, "answer": " ".join(s["text"] for s in sentences), "facts": facts}
        qs = {"answers": noul("Does the answer answer the question that was asked?",
                              "It answers the question asked, directly",
                              "It does not answer it, answers only part of it, or answers a different question"),
              "grounded": noul("Is every statement in the answer supported by the facts listed?",
                               "Every statement rests on the facts listed",
                               "Some statement goes beyond the facts listed")}
        try:
            return self.caps.jev.ask(state, qs, "ask_research"), None
        except JevError as e:
            return None, str(e)

    # -- the job ---------------------------------------------------------------------
    def run(self, progress=None):
        pin_by, fi_by = self.evidence(self.lens)
        s1, missing1, lens, topic, dropped1 = self.answer(self.lens, pin_by, fi_by)
        j1, why1 = self.check(s1, pin_by, fi_by)
        if j1 is not None:
            search = j1["answers"] < ANSWERS
        else:
            search = not s1 or bool(missing1)
        if progress:
            progress(1)
        did_check = uid("d_", self.doc, self.base, "check_answer", self.aid)
        decisions = [self.check_decision(did_check, s1, missing1, j1, why1, search, lens, dropped1)]
        facts_append, sources_append, sel_patch, hits, new_ids = [], [], None, [], []
        sentences, missing, j, why, dropped = s1, missing1, j1, why1, dropped1
        searches = []
        if search:
            # Up to SEARCHES web searches: the question first; then, when the
            # answer is still partial, what it said is missing. The client's
            # documents are not read again here: the research already read
            # them, and what they hold is in the facts the answer starts from.
            markets = field_value(self.data, "markets") or []
            cats = (field_value(self.data, "categories") or [])[:2]
            here = f"{self.doc}#asks[{self.aid}]"
            sel_contested = []
            focus = self.question
            for n in range(SEARCHES):
                clan_now = dict(self.clan, facts=list(ctx_facts(self.clan)) + facts_append)
                r = Researcher(self.doc, self.base, clan_now, self.handler, self.caps, [lens], markets, cats,
                               reuse_days=self.settings.reuse_days, concurrency=self.settings.research_concurrency,
                               seed=f"{self.aid}/{n}", question_first=True)
                res, rch, h = r.run(focus=focus)
                hits += h
                found = rch["facts_append"]
                facts_append += [f for f in found if f["id"] not in {x["id"] for x in facts_append}]
                sources_append += [s for s in rch.get("sources_append") or [] if s["id"] not in {x["id"] for x in sources_append}]
                urls = [v.get("uri") for v in (res.get("sources") or {}).values() if v.get("uri")]
                searches.append({"focus": focus[:300], "sources": len(urls), "urls": urls[:8], "facts": len(found)})
                # A search on one question is not the lens's run: the lens's
                # runs, coverage and gaps stay as they were (what it did not
                # find is the answer's `missing`). What it found is pinned, and
                # a value it disagrees on is a contest, as any research's.
                sel_contested += (rch["data_patch"].get("selection") or {}).get("contested") or []
                for d in rch["decisions"]:
                    if d.get("action") == "research_run":
                        d["targets"], d["fields_changed"] = [here], ["asks"]
                    elif d.get("action") == "research_merge":
                        d["targets"] = [t for t in d["targets"] if "#selection." not in t] + [here]
                        d["fields_changed"] = ["asks"]
                decisions += rch["decisions"]
                pin_by, fi_by = self.evidence(lens, facts_append)
                sentences, missing, _, _, dropped = self.answer(lens, pin_by, fi_by)
                j, why = self.check(sentences, pin_by, fi_by)
                answered = j["answers"] >= ANSWERS if j is not None else bool(sentences) and not missing
                if answered or not missing or n + 1 >= SEARCHES:
                    break
                focus = f"{self.question} Specifically: {'; '.join(missing[:2])}"
            new_ids = [f["id"] for f in facts_append]
            if sel_contested:
                old = [c for c in (self.data.get("selection") or {}).get("contested") or []
                       if c.get("id") not in {x["id"] for x in sel_contested}]
                sel_patch = {"contested": old + list({c["id"]: c for c in sel_contested}.values())}
        if progress:
            progress(2)
        if j is not None:
            status = "answered" if j["answers"] >= ANSWERS else ("partial" if sentences else "not_found")
        else:
            status = "answered" if sentences and not missing else ("partial" if sentences else "not_found")
        rec = {"id": self.aid, "question": self.question, "lens": lens, "in_section": self.lens is not None,
               "topic": topic, "answer": sentences, "missing": missing, "status": status, "searched": search,
               "new_facts": new_ids, "searches": searches,
               "web_sources": len({u for x in searches for u in x["urls"]}), "asked_at": iso(), "answered_at": iso()}
        did = uid("d_", self.doc, self.base, "ask_research", self.aid)
        rec["decision"] = did
        decisions.append(self.answer_decision(did, rec, j, why, dropped, pin_by, fi_by))
        patch = {"asks": {self.aid: rec}}
        if sel_patch:
            patch["selection"] = sel_patch
        change = {"doc": self.doc, "base_version": self.base, "read": read_of(self.data, patch), "data_patch": patch,
                  "facts_append": facts_append, "findings_append": [], "sources_append": sources_append,
                  "decisions": decisions}
        said = {"answered": "Answered", "partial": "Partly answered", "not_found": "Not answered"}[status]
        result = {"summary": f"{said}: {self.question[:120]}"
                             + (f" (searched; {len(new_ids)} new fact(s))" if search else " (from the facts already here)"),
                  "ask": self.aid, "status": status, "searched": search}
        return result, change, hits

    # -- the record ------------------------------------------------------------------------
    def check_decision(self, did, sentences, missing, j, why, search, lens, dropped):
        cites = list(dict.fromkeys(c for s in sentences for c in s["cites"]))
        where = LENS_TITLES.get(lens, lens)
        if j is not None:
            scored = f"jev: answers the question {j['answers']:.2f}, rests on what it cites {j['grounded']:.2f}"
            because = [rsn.point(scored, cites)]
            basis = f"jev judged whether the answer from the facts already here answers the question ({scored})"
        else:
            because = [rsn.point(f"Not checked by jev: {why}", cites)]
            basis = "without jev, the answer's own list of what is missing decides"
        if missing:
            because.append(rsn.point(f"What the facts here do not show: {'; '.join(missing)}", cites))
        rejected = [rsn.rej("answer from the facts already here", "they do not answer it")] if search else \
            [rsn.rej("search the web", "the facts already here answer it; a search would spend for nothing")]
        r = rsn.make(f"{'Search' if search else 'No search'} for “{self.question[:100]}” ({where}).", because,
                     rsn.certainty("medium" if j is not None else "low", basis),
                     "the person asks for a search, or the question changes", rejected=rejected,
                     attention=f"The cite rule dropped {len(dropped)} sentence(s)." if dropped else None)
        return decision(self.doc, did, "edit", self.handler, "check_answer", "", [f"asks[{self.aid}]"], cites,
                        reasoning=r, fields_changed=["asks"])

    def answer_decision(self, did, rec, j, why, dropped, pin_by, fi_by):
        cites = list(dict.fromkeys([c for s in rec["answer"] for c in s["cites"]] + rec["new_facts"]))
        conf = [(pin_by.get(c) or fi_by.get(c) or {}).get("confidence") for c in cites]
        level = rsn.lowest(conf, default="low") if rec["answer"] else "low"
        because = [rsn.point(s["text"], s["cites"]) for s in rec["answer"]] or \
            [rsn.point("Nothing in the facts answers it", rec["new_facts"] or [f"{self.doc}#asks[{self.aid}]"])]
        if j is not None:
            because.append(rsn.point(f"jev: answers the question {j['answers']:.2f}, rests on what it cites "
                                     f"{j['grounded']:.2f}", cites))
        elif rec["answer"]:
            because.append(rsn.point(f"Not checked by jev: {why}", cites))
        rejected = [rsn.rej("state what the facts do not hold",
                            "every sentence cites what it rests on and states no figure it does not hold")]
        if dropped:
            rejected.append(rsn.rej(f"keep the {len(dropped)} sentence(s) that failed the cite check",
                                    "; ".join(d for d in dropped if d)[:300]))
        where = "its own section" if not rec["in_section"] else LENS_TITLES.get(rec["lens"], rec["lens"])
        how = f"searched, {len(rec['new_facts'])} new fact(s)" if rec["searched"] else "from the facts already here"
        r = rsn.make(f"{ {'answered': 'Answered', 'partial': 'Partly answered', 'not_found': 'Could not answer'}[rec['status']] } "
                     f"“{self.question[:100]}” in {where} ({how}).", because,
                     rsn.certainty(level, "the lowest derived confidence of what the answer cites"),
                     "new facts land on the question, or a cited fact is corrected or excluded",
                     rejected=rejected,
                     attention={"partial": "It only partly answers the question.",
                                "not_found": "Nothing found answers the question."}.get(rec["status"]))
        return decision(self.doc, did, "edit", self.handler, "ask_research", "", [f"asks[{self.aid}]"], cites,
                        reasoning=r, fields_changed=["asks"])
