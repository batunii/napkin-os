"""reconsider: a person marked a fact wrong; what the crew concluded from it is
checked without it, and what no longer stands is worked out again.

  1. The fact is excluded (selection.excluded, with the person's reason): no
     finding, answer or report cites it from here on.
  2. Every conclusion the crew wrote that rests on it is checked without it:
     each finding citing it, each answer to a question citing it, and the
     report. A finding left with nothing else to rest on falls; otherwise jev
     says whether the facts that remain still support it (without jev, it is
     reworked, to be safe).
  3. What falls is reworked: one call writes each fallen finding again from
     the facts that remain (cite-checked, a new proposal for a person to
     verify; the old one is a person's to reject), or says it no longer
     stands. Each answer that cited the fact is answered again from what
     remains, and the report is recomposed without it.

A finding is never rejected here: that is a person's (os-layer). The host
flags a finding that rests on an excluded fact until a person settles it.
"""

from __future__ import annotations

import copy
import json
import re

from ..doc import CONF, LENS_TITLES, LENSES, ctx_data, ctx_facts, ctx_findings, decision, field_value, lens_of_key, \
    read_of
from ..jev import JevError, noul
from ..rules.cite import clean_claim
from ..rules.confidence import finding_confidence
from ..util import bad, iso, uid
from .. import reasoning as rsn
from . import report as report_stage
from .ask import Asker

STANDS = 0.6

SYSTEM = """You rework research findings for an advertising planner after a person marked one of the facts
they rested on wrong. For each finding you are given, the removed fact, and the facts that remain. Write
the finding again so it rests only on the remaining facts and says only what they support, citing the ids
it rests on; or, when the remaining facts do not support any version of it, give null. Plain, specific
sentences. State no number that is not the value of a pin you cite."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def schema(ids: list[str], pins: list[str]) -> dict:
    return _obj({"findings": {"type": "array", "items": _obj({
        "id": {"type": "string", "enum": ids or ["(none)"]},
        "statement": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "cites": {"type": "array", "items": {"type": "string", "enum": pins or ["(none)"]}},
        "why": {"type": "string"}})}})


def _line(p) -> str:
    where = f" ({p['market']})" if p.get("market") else ""
    return f"{p.get('key')} = {p.get('value')}{where}"


def validate(clan: dict, inp: dict) -> tuple[str, str, str | None]:
    fid = inp.get("fact_id")
    if not isinstance(fid, str) or not re.fullmatch(r"f_[0-9A-Z]{6,}", fid):
        raise bad("input.fact_id is required: the pin a person marked wrong")
    if fid not in {f.get("id") for f in ctx_facts(clan)}:
        raise bad(f"{fid} is not a pin in this document")
    why = inp.get("why")
    if not isinstance(why, str) or not why.strip():
        raise bad("input.why is required: what the person said is wrong with it")
    verdict = inp.get("verdict_id")
    return fid, why.strip()[:500], verdict if isinstance(verdict, str) and verdict else None


class Reconsider:
    def __init__(self, doc, base, clan, handler, caps, fact_id, why, verdict_id, settings):
        self.doc, self.base, self.clan, self.handler, self.caps = doc, base, clan, handler, caps
        self.fid, self.why, self.verdict, self.settings = fact_id, why, verdict_id, settings
        self.data = ctx_data(clan)

    def _cited(self, obj) -> bool:
        return self.fid in json.dumps(obj, ensure_ascii=False, default=str)

    # -- 2. does it still stand? --------------------------------------------------
    def stands(self, f, remaining, pin_by) -> tuple[bool, float | None, str]:
        if not remaining:
            return False, None, "nothing else it cites remains"
        if not self.caps.jev.configured:
            return False, None, "not checked by jev (no key): reworked, to be safe"
        state = {"finding": f.get("statement"), "facts_that_remain": [_line(pin_by[c]) for c in remaining],
                 "fact_removed": _line(self.pin) + f" (marked wrong: {self.why})"}
        try:
            p = self.caps.jev.ask(state, {"stands": noul(
                "Do the facts that remain, on their own, still support the finding as it is written?",
                "They support it as written", "Without the removed fact it says more than they support")},
                "reconsider_fact")["stands"]
        except JevError as e:
            return False, None, f"not checked by jev ({str(e)[:120]}): reworked, to be safe"
        return p >= STANDS, p, f"jev: still supported {p:.2f}"

    # -- the job --------------------------------------------------------------------
    def run(self, progress=None):
        doc, handler = self.doc, self.handler
        self.pin = next(f for f in ctx_facts(self.clan) if f.get("id") == self.fid)
        did = uid("d_", doc, self.base, "reconsider", self.fid)
        sel = self.data.get("selection") or {}
        excluded = [e for e in sel.get("excluded") or [] if isinstance(e, dict) and e.get("fact_id") != self.fid]
        excluded.append({"fact_id": self.fid, "reason": self.why, "decision": did})
        # the document as it is without the fact
        data2 = copy.deepcopy(self.data)
        data2.setdefault("selection", {})["excluded"] = excluded
        clan2 = dict(self.clan, data=data2)
        pin_by = {f["id"]: f for f in ctx_facts(clan2) if str(f.get("id", "")).startswith("f_")
                  and f["id"] not in {e["fact_id"] for e in excluded} and f.get("confidence") in CONF}
        findings = [f for f in ctx_findings(self.clan) if f.get("status") in ("proposed", "verified")
                    and self.fid in (f.get("cites") or [])]
        checked, fallen = [], []
        for f in findings:
            remaining = [c for c in f.get("cites") or [] if c in pin_by]
            ok, p, how = self.stands(f, remaining, pin_by)
            checked.append({"id": f["id"], "statement": f.get("statement"), "stands": ok, "p": p, "how": how})
            if not ok:
                fallen.append(f)
        if progress:
            progress(1)

        # -- 3. rework what fell ------------------------------------------------------------
        new_findings, new_decs, reworked = [], [], {}
        if fallen:
            lenses = {f.get("lens") or next((lens_of_key(pin_by[c]["key"]) for c in f.get("cites") or [] if c in pin_by),
                                            None) for f in fallen}
            pool = [p for p in pin_by.values() if lens_of_key(p.get("key")) in lenses] or list(pin_by.values())
            pool = pool[:160]
            payload = {"removed_fact": {"id": self.fid, "fact": _line(self.pin), "why_wrong": self.why},
                       "findings": [{"id": f["id"], "statement": f.get("statement"), "lens": f.get("lens")} for f in fallen],
                       "facts_that_remain": [{"id": p["id"], "entity": p.get("entity"), "key": p.get("key"),
                                              "value": p.get("value"), "unit": p.get("unit"), "market": p.get("market")}
                                             for p in pool]}
            brand = field_value(self.data, "brand") or {}
            names = [brand.get("name", "")] + [c.get("name", "") for c in field_value(self.data, "competitor_set") or []
                                               if isinstance(c, dict)]
            try:
                raw = self.caps.model.structured("rework_findings", SYSTEM, payload,
                                                 schema([f["id"] for f in fallen], [p["id"] for p in pool]), max_tokens=4000)
            except Exception as e:  # noqa: BLE001 - the fallen findings stay flagged for a person; nothing halts
                raw = {"findings": []}
                for f in fallen:
                    reworked[f["id"]] = {"why": f"could not be reworked: {str(e)[:160]}"}
            existing = {tuple(sorted(x.get("cites") or [])) for x in ctx_findings(self.clan)}
            by_old = {f["id"]: f for f in fallen}
            t = iso()
            for item in raw.get("findings") or []:
                old = by_old.get(item.get("id"))
                if not old or item["id"] in reworked:
                    continue
                if not item.get("statement"):
                    reworked[old["id"]] = {"why": item.get("why") or "the facts that remain do not support it"}
                    continue
                c, why = clean_claim(item.get("statement"), item.get("cites"), pin_by, {}, names)
                if not c or tuple(sorted(c["cites"])) in existing:
                    reworked[old["id"]] = {"why": why or "a finding on those facts is already in the document"}
                    continue
                cites = sorted(c["cites"])
                cited = [pin_by[x] for x in cites]
                lens = old.get("lens") or lens_of_key(cited[0].get("key"))
                nid = uid("fi_", doc, lens, cites)
                ndid = uid("d_", doc, self.base, "rework", old["id"], nid)
                fi = {"id": nid, "statement": c["text"], "cites": cites, "method": "synthesis", "status": "proposed",
                      "derived_by": handler, "confidence": finding_confidence(cited), "derived_at": t, "decision": ndid}
                if lens in LENSES:
                    fi["lens"] = lens
                mk = sorted({p["market"] for p in cited if p.get("market")})
                if mk:
                    fi["markets"] = mk
                new_findings.append(fi)
                existing.add(tuple(cites))
                reworked[old["id"]] = {"new": nid, "statement": c["text"]}
                new_decs.append(decision(doc, ndid, "finding", handler, "synthesise_finding",
                                         "", [f"findings[{nid}]"], cites, timestamp=t, reasoning=rsn.make(
                                             f"Worked out again without {self.fid}, which a person marked wrong: in place "
                                             f"of {old['id']}.",
                                             [rsn.point(f"The old finding said: {old.get('statement')}", old["id"]),
                                              rsn.point(item.get("why") or "It now rests only on the facts that remain",
                                                        cites)],
                                             rsn.certainty(fi["confidence"], "the lowest derived confidence of what it cites"),
                                             "a cited fact is revised or excluded, or a person rejects it",
                                             rejected=[rsn.rej(f"keep citing {self.fid}", f"a person marked it wrong: {self.why}")],
                                             attention="A new finding in place of one that rested on a fact marked wrong: "
                                                       "verify this one, and reject the old one.")))
        if progress:
            progress(2)

        # -- answers that cited it ------------------------------------------------------------
        asks_patch, ask_decs, answers = {}, [], []
        for aid, a in ((self.data.get("asks") or {}).items()):
            if not isinstance(a, dict) or not self._cited(a.get("answer")):
                continue
            asker = Asker(doc, self.base, clan2, handler, self.caps, a.get("question") or "", a.get("lens"), self.settings)
            asker.aid = aid
            pb, fb = asker.evidence(a.get("lens"))
            try:
                sentences, missing, _, _, _ = asker.answer(a.get("lens"), pb, fb)
            except Exception as e:  # noqa: BLE001 - the old answer stays, flagged by the decision
                answers.append({"id": aid, "question": a.get("question"), "error": str(e)[:160]})
                continue
            j, _ = asker.check(sentences, pb, fb)
            status = ("answered" if j["answers"] >= 0.6 else ("partial" if sentences else "not_found")) if j else \
                ("answered" if sentences and not missing else ("partial" if sentences else "not_found"))
            rec = dict(a, answer=sentences, missing=missing, status=status, answered_at=iso())
            adid = uid("d_", doc, self.base, "reanswer", aid, self.fid)
            rec["decision"] = adid
            asks_patch[aid] = rec
            answers.append({"id": aid, "question": a.get("question"), "status": status})
            ask_decs.append(decision(doc, adid, "edit", handler, "ask_research", "", [f"asks[{aid}]"],
                                     [c for s in sentences for c in s["cites"]], fields_changed=["asks"], reasoning=rsn.make(
                                         f"Answered “{(a.get('question') or '')[:100]}” again without {self.fid}.",
                                         [rsn.point(s["text"], s["cites"]) for s in sentences]
                                         or [rsn.point("Nothing that remains answers it", [f"{doc}#asks[{aid}]"])],
                                         rsn.certainty("medium" if sentences else "low", "from the facts that remain"),
                                         "new facts land on the question",
                                         rejected=[rsn.rej(f"keep citing {self.fid}", f"a person marked it wrong: {self.why}")])))

        # -- the report ---------------------------------------------------------------------------
        report_patch, rep_decs, rep_hits = None, [], []
        rep = self.data.get("report")
        if isinstance(rep, dict) and (self._cited(rep) or fallen):
            clan3 = dict(clan2, findings=list(ctx_findings(self.clan)) + new_findings)
            try:
                report, cites, rep_hits, why = report_stage.compose(doc, clan3, handler, self.caps)
                report_patch = report
                rdid = uid("d_", doc, self.base, "report-reconsider", self.fid)
                rep_decs.append(decision(doc, rdid, "edit", handler, "compose_report",
                                         f"Recomposed without {self.fid}, which a person marked wrong.", ["report"],
                                         cites, fields_changed=["report"], reasoning=why))
            except Exception as e:  # noqa: BLE001 - the old report stays; Refresh report will say it is out of date
                report_patch = None
                answers.append({"id": "report", "error": str(e)[:160]})

        # -- the record ----------------------------------------------------------------------------
        held = [c for c in checked if c["stands"]]
        because = [rsn.point(f"A person marked {_line(self.pin)} wrong: {self.why}", self.fid)]
        for c in checked:
            r = reworked.get(c["id"], {})
            because.append(rsn.point(
                f"“{(c['statement'] or '')[:140]}” " + ("still stands" if c["stands"] else
                                                        ("worked out again as " + r["new"]) if r.get("new") else
                                                        ("no longer stands: " + r.get("why", ""))) + f" ({c['how']})",
                [c["id"]] + ([r["new"]] if r.get("new") else [])))
        if answers:
            because.append(rsn.point(f"{len(answers)} answer(s) to questions cited it and were answered again",
                                     [f"{doc}#asks[{a['id']}]" for a in answers if a["id"] != "report"] or [self.fid]))
        if report_patch is not None:
            because.append(rsn.point("The report cited it, or a finding it uses changed: it was recomposed", [f"{doc}#report"]))
        if not checked and not answers and report_patch is None:
            because.append(rsn.point("Nothing the crew wrote rests on it", self.fid))
        summary = (f"{len(checked)} finding(s) rested on it: {len(held)} still stand, "
                   f"{sum(1 for v in reworked.values() if v.get('new'))} worked out again, "
                   f"{sum(1 for v in reworked.values() if not v.get('new'))} no longer stand"
                   + (f"; {len([a for a in answers if a['id'] != 'report'])} answer(s) redone" if answers else "")
                   + ("; the report was recomposed" if report_patch is not None else "") + ".")
        r = rsn.make(f"Took {self.fid} out, and checked what rested on it: {summary}", because,
                     rsn.certainty("medium" if self.caps.jev.configured else "low",
                                   "jev judged whether each finding still stands without it" if self.caps.jev.configured
                                   else "without jev, every finding that cited it was reworked"),
                     "a person keeps the fact after all, or corrects its value",
                     rejected=[rsn.rej("leave the conclusions as they are", "they cite a fact a person marked wrong")],
                     attention=("Verify the new finding(s), and reject the old one(s) they replace." if new_findings
                                else None))
        rd = decision(doc, did, "edit", handler, "reconsider_fact", "",
                      [f"selection.excluded[{self.fid}]", f"facts[{self.fid}]"],
                      [self.fid], fields_changed=["selection.excluded"], reasoning=r)
        patch = {"selection": {"excluded": excluded}}
        if asks_patch:
            patch["asks"] = asks_patch
        if report_patch is not None:
            patch["report"] = report_patch
        change = {"doc": doc, "base_version": self.base, "read": read_of(self.data, patch), "data_patch": patch,
                  "facts_append": [], "findings_append": new_findings, "decisions": [rd] + new_decs + ask_decs + rep_decs}
        result = {"summary": summary, "checked": checked, "reworked": reworked, "answers": answers,
                  "report": report_patch is not None}
        return result, change, rep_hits
