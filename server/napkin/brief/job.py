"""Brief Maker's jobs (middleware-api.md §10): `draft_brief` (extract -> draft
-> judge) and `regenerate_field` (draft -> judge), one background worker each.

Each stage that writes produces a "chunk" (patch, read-set, decisions),
computed ONCE and kept, so a repeat is byte-identical and carries the same
decision ids; every reply carries the chunks whose decisions the request's
`clan.decision_chain` does not hold yet (§10.9). `draft` sends no change: a
draft lands only once judged.

Rules every write here follows (§10.3, §10.5): omitted, not blank — no patch
sets a field to "", [], {} or null, and none removes one; a locked field is
not drafted, written or proposed (`kept`); a field a person holds is proposed
(`result.proposals` + an `edit` decision with action `propose`) instead of
written; every passage a decision cites is written into `data.passages` by
the same change; every decision carries reasoning whose cites resolve.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import traceback

from .. import reasoning as rsn
from ..doc import apply_patch, ctx_data, ctx_decisions, decision, deep_merge
from ..model import ModelError
from ..util import TaskError, iso, uid
from . import capture as cap_stage
from .drafters import DraftContext, Drafter, _query, run_drafters
from .fields import (KEYS, LABELS, RUBRIC_FIELDS, address, clean, drafter_of, field_map, field_paths, filled, get,
                     group_keys, holder, last_writer, locked, put, read_of, rubric_groups)
from .judge import FIX_MAX, clip, coherence, definition_of_done, failures, judge_field, sentence
from .rubric import DEPENDENCIES, FAIL, PASS, REVIEW, health, reason_code

log = logging.getLogger("napkin.brief")
TAXONOMY = "reason-codes/1"


class Chunk:
    def __init__(self, stage, base, patch, read, decisions):
        self.stage, self.base, self.patch, self.read, self.decisions = stage, base, patch, read, decisions
        self.ids = {d["id"] for d in decisions}


def combine(doc, chunks):
    if not chunks:
        return None
    patch, read, decs = {}, {}, []
    for c in chunks:
        patch = deep_merge(patch, c.patch)
        for k, v in c.read.items():
            read.setdefault(k, v)  # what the EARLIEST writer of the path read
        decs += [d for d in c.decisions if d["id"] not in {x["id"] for x in decs}]
    return {"doc": doc, "base_version": chunks[0].base, "read": read if patch else {}, "data_patch": patch,
            "facts_append": [], "findings_append": [], "decisions": decs}


def _known_ids(clan) -> set:
    ids = set()
    for d in ctx_decisions(clan):
        ids.add(d.get("id"))
        for h in d.get("withheld") or []:
            ids.add(h.get("id") if isinstance(h, dict) else h)
    return ids


def _figure_needs_pin(raw):
    """§10.7: a point stating a figure cites the pin holding it. Drop the rest."""
    if not isinstance(raw, dict):
        return raw
    keep = [p for p in raw.get("because") or [] if isinstance(p, dict)
            and not (rsn.states_figure(p.get("point") or "") and not any(str(c).startswith("f_") for c in p.get("cites") or []))]
    return {**raw, "because": keep}


def _quote(s, n=160) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


class BriefJob:
    kind = "brief"

    def __init__(self, jid, task, handler, req, caps, *, mats=None, field=None, guidance=None):
        self.id, self.task, self.handler = jid, task, handler
        self.doc, self.base, self.clan = req.doc, req.base, req.clan
        self.caps, self.scope = caps, caps.scope
        self.mats, self.field, self.guidance = mats or [], field, guidance
        self.stages = ["extract", "draft", "judge"] if task == "draft_brief" else ["draft", "judge"]
        self.data0 = copy.deepcopy(ctx_data(req.clan))
        self.W = copy.deepcopy(self.data0)
        self.chain = ctx_decisions(req.clan)
        self.fm = field_map(req.clan)
        self.lock = threading.RLock()
        self.state, self.stage_idx = "queued", 0
        self.started_at, self.finished_at, self.error = iso(), None, None
        self.chunks: list[Chunk] = []
        self.last_change = None
        self.hits, self.proposals, self.notes = [], [], []
        self.locked = {k for k in KEYS if locked(self.data0, k)}
        self.held = {k: holder(self.doc, self.data0, self.chain, k) for k in KEYS if k not in self.locked}
        self.writer = {}  # app key -> the decision id that wrote/proposed it in this job
        self.fields = {}
        for k in KEYS:
            if k in self.locked:
                self.fields[k] = {"state": "kept", "by": None}
            elif task == "draft_brief" or k == field:
                self.fields[k] = {"state": "waiting", "by": None}
            else:
                self.fields[k] = {"state": "done" if filled(get(self.data0, k)) else "absent", "by": None}
        self.capture = None
        self.drafts = {}
        self.thread = threading.Thread(target=self._run, name=f"{task}-{jid}", daemon=True)

    # -- small helpers -----------------------------------------------------------
    def did(self, *parts):
        return uid("d_", self.doc, self.id, *parts)

    def dec(self, parts, kind, action, targets, cites, reasoning, worker, **extra):
        d = decision(self.doc, self.did(*parts), kind, self.handler, action, "", targets, cites,
                     reasoning=reasoning, **extra)
        d["agent"] = f"{self.handler}/{worker}"
        return d

    def set_state(self, keys, state, by):
        with self.lock:
            for k in keys:
                if k in self.fields and self.fields[k]["state"] != "kept":
                    self.fields[k] = {"state": state, "by": by}

    def add_chunk(self, stage, patch, decisions):
        paths = field_paths(patch)
        for p in paths:  # every field the patch writes is named by a decision's targets (§3)
            a = f"{self.doc}#{address(p)}"
            if any(a in d["targets"] for d in decisions):
                continue
            # a proposal targets its one field only (§10.5): a passage it cites is
            # targeted by a writing decision that cites it, else by the review
            # (and a verdict targets the fields it judges, nothing else)
            writers = [d for d in decisions if d["kind"] == "edit" and d.get("action") != "propose"] or decisions
            owner = None
            if p.startswith("passages."):
                pid = p.split(".", 1)[1]
                owner = next((d for d in writers if pid in (d.get("cites") or [])), None) or \
                    next((d for d in writers if d.get("action") == "review"), None)
            (owner or writers[0])["targets"].append(a)
        c = Chunk(stage, self.base, copy.deepcopy(patch), read_of(self.W, patch), decisions)
        with self.lock:
            self.chunks.append(c)
            self.W = apply_patch(self.W, patch)
        return c

    # -- the worker ----------------------------------------------------------------
    def start(self):
        self.thread.start()

    def _run(self):
        with self.lock:
            self.state = "running"
        for i, stage in enumerate(self.stages):
            with self.lock:
                self.stage_idx = i
            try:
                getattr(self, "stage_" + stage)()
            except (TaskError, ModelError) as e:
                etype = getattr(e, "etype", None) or "model_failed"
                msg = getattr(e, "message", None) or f"the model call failed ({getattr(e, 'kind', 'error')})"
                self._fail(stage, etype, msg)
                return
            except Exception as e:  # never a silent fallback
                log.error("%s %s stage %s failed: %s\n%s", self.task, self.id, stage, e, traceback.format_exc())
                self._fail(stage, "internal", f"the {stage} stage failed ({type(e).__name__})")
                return
        with self.lock:
            self.stage_idx = len(self.stages)
            self.state, self.finished_at = "done", iso()

    def _fail(self, stage, etype, message):
        with self.lock:
            for k, f in self.fields.items():
                if f["state"] in ("waiting", "extracting", "drafting", "judging", "revising"):
                    self.fields[k] = {"state": "failed", "by": f["by"]}
            self.state, self.error, self.finished_at = "failed", {"type": etype, "message": message}, iso()

    # -- replies -------------------------------------------------------------------
    def reply_change(self, clan):
        with self.lock:
            known = _known_ids(clan)
            pending = [c for c in self.chunks if not c.ids <= known]
            change = combine(self.doc, pending)
            if change is None and self.state in ("done", "failed"):
                change = self.last_change
            if change is not None:
                self.last_change = change
            return change

    def view(self):
        with self.lock:
            n = len(self.stages)
            return {"id": self.id, "state": self.state,
                    "progress": {"done": n if self.state == "done" else min(self.stage_idx, n - 1) if self.state != "failed"
                                 else self.stage_idx, "total": n},
                    "stage": self.stages[min(self.stage_idx, n - 1)], "question": None,
                    "started_at": self.started_at, "finished_at": self.finished_at, "error": self.error}

    def result(self):
        with self.lock:
            fields = copy.deepcopy(self.fields)
            props = copy.deepcopy(self.proposals)
            v = self.view()
        drafting = [k for k, f in fields.items() if f["state"] == "drafting"]
        if v["state"] == "done":
            done = sum(1 for f in fields.values() if f["state"] == "done" and f["by"])
            s = (f"Brief drafted: {done} field(s) written, {len(props)} proposed, "
                 f"{sum(1 for f in fields.values() if f['state'] == 'failed')} failed.") if self.task == "draft_brief" \
                else self._regen_summary(fields)
        elif v["state"] == "failed":
            s = f"Failed at {v['stage']}: {self.error['message']}"
        elif v["stage"] == "draft" and drafting:
            s = f"Drafting {len(drafting)} field(s) in parallel."
        else:
            s = {"extract": "Reading the material.", "draft": "Drafting.", "judge": "The Judge is reviewing."}[v["stage"]]
        return {"summary": s, "fields": fields, "proposals": props}

    def _regen_summary(self, fields):
        f = fields.get(self.field, {})
        st = f.get("state")
        if st == "done":
            return f"Redrafted the {LABELS[self.field]}."
        if st == "proposed":
            return f"Proposed a new {LABELS[self.field]}: a person holds it, so it was not written."
        if st == "failed":
            return f"The {LABELS[self.field]} failed the Judge twice and was not written: {'; '.join(self.notes) or 'see the verdict'}."
        return f"Nothing supports the {LABELS[self.field]}: {'; '.join(self.notes) or 'the capture holds nothing for it'}."

    # =============================================================================
    # extract
    # =============================================================================
    def stage_extract(self):
        captured = [k for k in KEYS if self.fm[k]["class"] == "captured"]
        self.set_state([k for k in captured if k not in self.locked], "extracting", "extract")
        view = self.caps.capture_view()  # model + job only: no retrieval, research or layers (RAG-free)
        tnotes = cap_stage.transcribe(view, self.mats)
        readable = [m for m in self.mats if m.readable]
        if not readable:
            raise TaskError(400, "invalid_input", "nothing readable: " + "; ".join(tnotes or ["no text"]))
        cap = cap_stage.run_capture(view, self.mats)
        score = cap_stage.run_scorecard(view, self.mats)
        self.capture = cap
        mats_by = {m.id: m for m in self.mats}
        wb = cap_stage.working_brief(cap["items"], cap["named"])
        t = iso()
        patch, decisions, written, abstained = {}, [], [], []
        new = [m for m in self.mats if not m.known]
        if new:
            patch["materials"] = {m.id: m.entry(t) for m in new}
        patch["capture"] = {"built_at": t, "handler": self.handler, "items": cap["items"], "gaps": cap["gaps"],
                            "how_to_win": cap["how_to_win"], "ledger": cap["ledger"]}
        patch["review"] = {"built_at": t, "handler": self.handler, "based_on": {"version": self.base},
                           "scorecard": score}
        mat_ids = [m.id for m in readable]
        unread = [m.id for m in self.mats if not m.readable]
        led = cap["ledger"]
        # the capture decision
        because = [rsn.point(f"Read {m.name} ({m.kind}{', a vision transcription' if m.transcribed else ''}): "
                             f"{sum(1 for i in cap['items'].values() if i['material_id'] == m.id)} item(s) captured",
                             m.id) for m in readable]
        attn = list(tnotes)
        if cap["dropped"]:
            because.append(rsn.point(f"{cap['dropped']} item(s) offered as the client's words were dropped: their "
                                     f"quotes are not in the material", mat_ids))
            attn.append(f"{cap['dropped']} captured item(s) failed the verbatim check and were dropped.")
        if unread:
            because.append(rsn.point(f"{len(unread)} attachment(s) had nothing readable and ground nothing", unread))
            attn.append(f"{len(unread)} attachment(s) could not be read.")
        if led["coverage_pct"] < 85:
            attn.append(f"The no-loss ledger maps {led['coverage_pct']}% of the material's sentences; "
                        f"{len(led['unmapped'])} have no home in the capture.")
        cov = led["coverage_pct"]
        lvl = "high" if cov >= 85 and not cap["dropped"] else "low" if cov < 50 else "medium"
        # per captured field
        for k in captured:
            if k in self.locked:
                continue
            got = wb.get(k)
            value = clean(k, got["value"]) if got else None
            if value is None:
                abstained.append(k)
                self.set_state([k], "absent", "extract")
                continue
            cites = list(got["caps"]) + list(got["mats"])
            why_c = cap_stage.capture_certainty(cap["items"], got["caps"], mats_by)
            pts = [rsn.point(f"The client wrote: “{_quote(cap['items'][c].get('quote') or cap['items'][c]['value'])}”"
                             + (" (inferred)" if cap["items"][c]["status"] == "assumption" else ""), c)
                   for c in got["caps"]]
            if not pts:
                pts = [rsn.point(f"The material names it: “{_quote(got.get('quote') or value)}”", got["mats"])]
            r = rsn.make(f"Wrote the {LABELS[k]} from the client's own words.", pts,
                         rsn.certainty(*why_c), "the client's material says otherwise, or a person edits it",
                         only_option="the value is what the client wrote")
            if self.held.get(k):
                d = self.dec(("propose", k), "edit", "propose", [k], cites, r, "extract", proposed_value=value)
                decisions.append(d)
                self.proposals.append({"field": k, "value": value, "decision": d["id"]})
                self.writer[k] = d["id"]
                self.set_state([k], "proposed", "extract")
                abstained.append(k)
                continue
            if value == get(self.W, k):
                self.set_state([k], "done", "extract")  # already what the client wrote: nothing to write
                abstained.append(k)
                continue
            d = self.dec(("extract", k), "edit", "extract", [k], cites, r, "extract")
            decisions.append(d)
            put(patch, k, value)
            self.writer[k] = d["id"]
            written.append(k)
            self.set_state([k], "done", "extract")
        cr = rsn.make(f"Captured the client's words: {len(cap['items'])} item(s) from {len(readable)} material(s); the "
                      f"no-loss ledger maps {cov}% of their sentences.", because,
                      rsn.certainty(lvl, "ledger coverage and verified quotes: "
                                    f"{cov}% mapped, {cap['dropped']} unverified item(s) dropped"),
                      "more material arrives, or a person edits the capture",
                      only_option="the capture records what the material says and nothing else",
                      attention=" ".join(attn) or None)
        cdec = self.dec(("capture",), "edit", "capture", ["capture"] + [f"materials[{m.id}]" for m in new], mat_ids, cr,
                        "extract", material_read=mat_ids, unread=unread, abstained=abstained)
        sm = score["single_mindedness"]
        not_pass = [d for d in score["dimensions"] if d["verdict"] != "pass"]
        sp = [rsn.point(f"{d['dimension'].replace('_', ' ')}: {d['verdict']}"
                        + (f" — “{_quote(d['evidence'], 100)}”" if d.get("evidence") else ""), mat_ids) for d in not_pass]
        sp = sp or [rsn.point("All seven dimensions pass", mat_ids)]
        sp.append(rsn.point(f"Overall: {score['summary']}", mat_ids))
        sr = rsn.make(f"Scored the client's brief on the seven BetterBriefs dimensions: "
                      f"{sum(1 for d in score['dimensions'] if d['verdict'] == 'pass')} pass, "
                      f"{sum(1 for d in score['dimensions'] if d['verdict'] == 'vague')} vague, "
                      f"{sum(1 for d in score['dimensions'] if d['verdict'] == 'missing')} missing.", sp,
                      rsn.certainty("medium", "a model judged the brief; every quoted evidence was checked verbatim"),
                      "the client's brief is revised", only_option="the scorecard judges the material as it stands",
                      attention=(f"The brief bundles {len(sm['split_into']) or 'several'} strategies; consider "
                                 f"splitting it." if sm["verdict"] == "multiple" else None))
        sdec = self.dec(("score",), "edit", "score", ["review"], mat_ids, sr, "judge")  # the Judge's review
        self.add_chunk("extract", patch, [cdec, sdec] + decisions)

    # =============================================================================
    # draft
    # =============================================================================
    def _groups(self):
        """(group, [keys not locked], rubric id, loop) for the drafted fields in play."""
        out, seen = [], set()
        for k in KEYS:
            if self.fm[k]["class"] != "drafted":
                continue
            g = drafter_of(self.fm, k)
            if g in seen:
                continue
            seen.add(g)
            keys = [x for x in group_keys(self.fm, g) if x not in self.locked]
            if self.task == "regenerate_field":
                if self.field not in keys:
                    continue
                keys = [self.field]
            if keys and self.fm[keys[0]].get("rubric") in RUBRIC_FIELDS:
                out.append((g, keys, self.fm[keys[0]]["rubric"], self.fm[keys[0]].get("loop") or "loop5_proposition"))
        return out

    def _values(self):
        return {k: get(self.W, k) for k in KEYS}

    def _context(self):
        items = (self.capture or {}).get("items") or ((self.W.get("capture") or {}).get("items") or {})
        working = {k: v for k, v in self._values().items() if self.fm[k]["class"] == "captured" and filled(v)}
        return DraftContext(self.caps, working, items, self.clan, guidance=self.guidance, current=self._values())

    def stage_draft(self):
        if self.task == "regenerate_field" and self.fm[self.field]["class"] != "drafted":
            return  # a captured field is re-derived from the capture; the composed one is recomposed (judge stage)
        plan_ = self._groups()
        self.ctx = self._context()
        plan = [Drafter(self.ctx, g, keys, rid, loop,
                        only_leaf=self.field if self.task == "regenerate_field" and "." in self.field
                        and g != self.field else None) for g, keys, rid, loop in plan_]
        self.plan = {d.group: d for d in plan}
        keys_of = {g: keys for g, keys, _, _ in plan_}

        def on_state(group, state):
            st = {"drafting": "drafting", "drafted": "waiting", "failed": "failed"}[state]
            self.set_state(keys_of[group], st, "drafter")

        self.drafts = run_drafters(self.ctx, plan, on_state)
        self.hits += self.ctx.hits

    # =============================================================================
    # judge
    # =============================================================================
    def stage_judge(self):
        values = self._values()
        drafts = getattr(self, "drafts", {}) or {}
        skip_regen = False
        if self.task == "regenerate_field":
            cls = self.fm[self.field]["class"]
            if cls == "captured":
                items = (self.W.get("capture") or {}).get("items") or {}
                got = cap_stage.working_brief(items, {}).get(self.field)
                self.rederived = got
                if got and clean(self.field, got["value"]) is not None:
                    values[self.field] = clean(self.field, got["value"])
                else:
                    skip_regen = True
                    self.notes.append("the capture holds nothing for it" if items else "the document has no capture")
                    self.set_state([self.field], "absent", "extract")
            elif cls == "drafted":
                d = next((x for x in drafts.values() if self.field in x.keys), None)
                if d is None or d.error:
                    skip_regen = True
                    self.notes.append(f"the drafter failed ({d.error if d else 'no drafter'})")
                    self.set_state([self.field], "failed", "drafter")
        for g, d in drafts.items():
            if d.error:
                continue
            for k in d.keys:
                values[k] = clean(k, d.leaf(k))
        judge_only = self.fm[self.field].get("rubric") if self.task == "regenerate_field" else None
        # regenerate_field of a field with no rubric (open questions, project name, client): nothing to judge
        judging = not (self.task == "regenerate_field" and judge_only is None)
        ctx = getattr(self, "ctx", None) or self._context()
        g = ctx.gist()
        q7, _ = _query("loop7_qa", g, values.get("single_minded_proposition") or values.get("insight"), "judge")
        loop7, l7notes = ctx.retrieve("loop7_qa", q7, None) if judging and ctx.caps.retrieval.configured else ({}, [])
        self.hits += [h for h in ctx.hits if h not in self.hits]
        rules = [{"id": pid, "citation": p["citation"], "text": p["text"]} for pid, p in list(loop7.items())[:4]]
        rule_ids = [r["id"] for r in rules]
        passages_out = {}
        patch, decisions, verdicts = {}, [], []
        field_results, judged_out = [], {}
        failed_keys, bad_captured = [], []
        model = self.caps.model

        def edit_ref(k):
            return self.writer.get(k) or last_writer(self.doc, self.chain, k) or f"{self.doc}#{k}"

        for rid, keys in (rubric_groups(self.fm) if judging else []):
            if judge_only is not None and rid != judge_only:
                continue
            live = [k for k in keys if k not in self.locked and filled(values.get(k))]
            if self.task == "regenerate_field":
                live = [k for k in live if k == self.field and not skip_regen]
            if not live:
                for k in keys:
                    if k not in self.locked:
                        judged_out[k] = {"outcome": "absent", "checks": []}
                    else:
                        judged_out[k] = {"outcome": "kept", "checks": []}
                continue
            prev = {k: dict(self.fields[k]) for k in live}
            self.set_state(live, "judging", "judge")
            res = judge_field(model, rid, values, rules)
            fails = failures(res)
            drafted = next((d for d in drafts.values() if d.rid == rid and not d.error), None)
            outcome = "passed" if not fails else "failed"
            first_clean = not fails and all(c["status"] == PASS for c in res["checks"])
            if fails and drafted is not None:
                self.set_state(live, "revising", "drafter")
                dr = self.plan[drafted.group]
                drafted = dr.revise(drafted, fails, insight=values.get("insight"))
                if drafted.revised:
                    for k in drafted.keys:
                        values[k] = clean(k, drafted.leaf(k))
                    self.set_state(live, "judging", "judge")
                    res = judge_field(model, rid, values, rules)
                    fails = failures(res)
                outcome = "revised" if not fails else "failed"
            field_results.append(res)
            for k in keys:
                judged_out[k] = {"outcome": outcome if k in live else ("kept" if k in self.locked else "absent"),
                                 "checks": [{x: c[x] for x in ("check", "method", "status", "note", "fix") if x in c}
                                            for c in res["checks"]]}
            # place the value: drafted and passed -> write / propose; failed twice -> not written
            refs = {}
            if drafted is not None:
                if fails:
                    for k in live:
                        values[k] = get(self.W, k)  # the brief keeps what it had
                        failed_keys.append(k)
                    self.set_state(live, "failed", "judge")
                    self.notes.append(clip("; ".join(f"{f['check']}: {f['reason']}" for f in fails), FIX_MAX))
                else:
                    refs = self._place_draft(drafted, live, res, first_clean, patch, decisions, passages_out)
            elif self.task == "regenerate_field" and self.fm[self.field]["class"] == "captured" and self.field in live:
                refs = self._place_rederived(values[self.field], patch, decisions)
                if fails:
                    bad_captured.append((self.field, fails))
            else:
                if fails:
                    bad_captured += [(k, fails) for k in live if self.fm[k]["class"] == "captured"]
                with self.lock:
                    for k in live:  # judged, not rewritten: back to what the field's state was
                        self.fields[k] = prev[k] if prev[k]["state"] not in ("waiting", "judging") else \
                            {"state": "done", "by": "judge"}
            cites = [refs.get(k) or edit_ref(k) for k in live]
            verdicts.append(self._verdict(rid, live, res, fails, outcome, cites, rule_ids if res["model_ran"] else []))
        # coherence, over the brief as it would stand
        only = {judge_only} if judge_only else None
        coh, coh_ran, coh_err = coherence(model, {k: v for k, v in values.items() if k not in failed_keys}, rules,
                                          only) if judging else ([], False, None)
        for c in coh:
            if c["status"] != FAIL:
                continue
            keys = [k for rid in c["fields"] for k in dict(rubric_groups(self.fm)).get(rid, [])
                    if filled(values.get(k)) and k not in self.locked and k not in failed_keys]
            if not keys:
                continue
            cites = [self.writer.get(k) or edit_ref(k) for k in keys]
            verdicts.append(self._coherence_verdict(c, keys, cites, rule_ids if c["method"] == "llm" else []))
        decisions += verdicts
        for r in rule_ids:
            if any(r in v["cites"] for v in verdicts):
                passages_out[r] = loop7[r]
        # open questions (written whole)
        if self.task == "regenerate_field" and self.field == "open_questions":
            prev = ((self.W.get("review") or {}).get("judge") or {}).get("fields") or {}
            for fk, fr in prev.items():
                if fk in self.fm and fr.get("outcome") == "failed":
                    bad = [c for c in fr.get("checks") or [] if c.get("status") == FAIL]
                    if self.fm[fk]["class"] == "drafted" and not filled(values.get(fk)):
                        failed_keys.append(fk)
                    elif self.fm[fk]["class"] == "captured" and bad:
                        bad_captured.append((fk, [{"check": bad[0]["check"], "reason": bad[0].get("note") or ""}]))
        if self.task == "draft_brief" or self.field == "open_questions":
            self._questions(values, failed_keys, bad_captured, verdicts, patch, decisions)
        # review, again: a deliberate rewrite with a new decision id (draft_brief only)
        evaluation = any(i.get("key") == "evaluation_criteria"
                         for i in ((self.W.get("capture") or {}).get("items") or {}).values())
        high = len(failed_keys) + sum(1 for q in ((self.capture or {}).get("open_questions") or [])
                                      if q.get("priority") == "blocker")
        dod = definition_of_done(field_results, coh, values, evaluation, high)
        hp = health(field_results, dod, high)
        if self.task == "draft_brief" and isinstance(self.W.get("review"), dict) and self.W["review"].get("scorecard"):
            review = copy.deepcopy(self.W["review"])
            review.update(built_at=iso(), handler=self.handler, based_on={"version": self.base},
                          judge={"reason_codes_version": "1", "fields": judged_out,
                                 "dependencies": [{"id": c["id"], "status": c["status"], "note": c["note"]}
                                                  for c in coh if c["id"] in DEPENDENCIES],
                                 "definition_of_done": dod, "health": hp})
            patch["review"] = review
            vids = [v["id"] for v in verdicts]
            pts = [rsn.point(f"The Judge's health score is {hp}/100, computed from the rubric checks", vids or
                             [f"{self.doc}#review"])]
            bad = [v for v in verdicts if v.get("polarity") == "bad"]
            if bad:
                pts.append(rsn.point(f"{len(bad)} bad verdict(s) stand and wait for a person", [v["id"] for v in bad]))
            rr = rsn.make("Recorded the Judge's review of the brief.", pts,
                          rsn.certainty("high", "computed by code from the checks and verdicts"),
                          "a field changes and the Judge runs again", only_option="the review records what the Judge found",
                          attention=coh_err or (f"{len(bad)} bad verdict(s) to answer before the brief locks." if bad else None))
            decisions.append(self.dec(("review",), "edit", "review", ["review"], vids, rr, "judge"))
        for pid, p in passages_out.items():
            patch.setdefault("passages", {})[pid] = p
        if decisions:
            self.add_chunk("judge", patch, decisions)
        with self.lock:
            for k, f in self.fields.items():
                if f["state"] in ("waiting", "judging", "revising", "drafting"):
                    self.fields[k] = {"state": "absent" if not filled(get(self.W, k)) else "done", "by": "judge"}

    # -- placing a judged value ------------------------------------------------------
    def _place_draft(self, d, live, res, first_clean, patch, decisions, passages_out) -> dict:
        """Write (or propose) a judged draft. -> {key: decision id}."""
        raw = _figure_needs_pin(d.grounds)
        fallback = [rsn.point(f"Precedent: {p['citation']}", pid) for pid, p in list(d.passages.items())[:2]] or \
                   [rsn.point("Drafted from the working brief's captured fields")]
        action = "regenerate" if self.task == "regenerate_field" else "draft"
        attn = list(d.attention)
        r, notes = rsn.from_model(raw, decided=f"Drafted the {LABELS.get(d.group, d.group)}.", known=d.known,
                                  certainty_=rsn.certainty("low", "placeholder"), fallback=fallback,
                                  would_change_if="the working brief changes, or a person rewrites it",
                                  rejected=d.rejected, only_option="one draft was written",
                                  attention=" ".join(attn) or None)
        cites = [c for p in r["because"] for c in p.get("cites") or []]
        psg = [c for c in cites if c.startswith("psg_") and c in d.passages]
        packs = {d.passages[c]["pack"] for c in psg}
        pins = [c for c in cites if c.startswith("f_")]
        if not psg:
            lvl, why = "low", "grounded in no passage"
            if not attn:
                r["attention"] = ((r.get("attention") or "") + " Drafted without a cited precedent passage.").strip()
        elif d.revised:
            lvl, why = "low", "passed the Judge only after one revision"
        elif first_clean and (len(packs) >= 2 or pins):
            lvl, why = "high", "every rubric check passed first time; grounded in two packs, or a pin and a passage"
        else:
            lvl, why = "medium", "passed with checks left for a person, or grounded in one pack"
        r["certainty"] = rsn.certainty(lvl, why)
        for pid in psg:
            passages_out[pid] = d.passages[pid]
        for c in pins:
            pin = next((p for p in self.ctx.pins if p["id"] == c), None)
            if pin:
                self.hits.append({"id": c, "scope": pin.get("layer") or "brand", "source": pin.get("origin") or c})
        refs = {}
        write = [k for k in live if not self.held.get(k)]
        propose = [k for k in live if self.held.get(k)]
        if write:
            dd = self.dec((action, d.group, self.task), "edit", action, write, cites, copy.deepcopy(r), "drafter")
            decisions.append(dd)
            for k in write:
                put(patch, k, clean(k, d.leaf(k)))
                refs[k] = self.writer[k] = dd["id"]
            self.set_state(write, "done", "judge")
        for k in propose:
            v = clean(k, d.leaf(k))
            dp = self.dec(("propose", k, self.task), "edit", "propose", [k], cites, copy.deepcopy(r), "drafter",
                          proposed_value=v)
            decisions.append(dp)
            self.proposals.append({"field": k, "value": v, "decision": dp["id"]})
            refs[k] = self.writer[k] = dp["id"]
            self.set_state([k], "proposed", "judge")
        return refs

    def _place_rederived(self, value, patch, decisions) -> dict:
        k, got = self.field, self.rederived
        items = (self.W.get("capture") or {}).get("items") or {}
        pts = [rsn.point(f"The client wrote: “{_quote(items[c].get('quote') or items[c]['value'])}”", c)
               for c in got["caps"] if c in items] or [rsn.point("Re-derived from the capture", got["mats"])]
        r = rsn.make(f"Re-derived the {LABELS[k]} from the capture.", pts,
                     rsn.certainty(*cap_stage.capture_certainty(items, got["caps"], {})),
                     "the client's material says otherwise, or a person edits it",
                     only_option="a captured field is the client's words, re-derived from the capture, never retrieved",
                     attention="The planner's guidance does not steer a captured field." if self.guidance else None)
        cites = list(got["caps"]) + list(got["mats"])
        if self.held.get(k):
            d = self.dec(("propose", k, self.task), "edit", "propose", [k], cites, r, "extract", proposed_value=value)
            self.proposals.append({"field": k, "value": value, "decision": d["id"]})
            self.set_state([k], "proposed", "judge")
        else:
            d = self.dec(("regenerate", k), "edit", "regenerate", [k], cites, r, "extract")
            put(patch, k, value)
            self.set_state([k], "done", "judge")
        decisions.append(d)
        self.writer[k] = d["id"]
        return {k: d["id"]}

    # -- verdicts ----------------------------------------------------------------------
    def _verdict(self, rid, keys, res, fails, outcome, cites, rule_ids):
        label = RUBRIC_FIELDS[rid]["label"]
        bad = bool(fails)
        deciding = [c for c in res["checks"] if c["status"] == FAIL] if bad else \
            [c for c in res["checks"] if c["status"] == PASS]
        review = [c for c in res["checks"] if c["status"] == REVIEW]
        pts = [rsn.point(f"{c['check']} ({c['method']}): {sentence(c['note'])}"
                         + (f" Fix: {sentence(c['fix'])}" if c.get("fix") else ""),
                         cites + (rule_ids if c["method"] == "llm" else []))
               for c in deciding[:8]]
        if not pts:
            pts = [rsn.point(f"No check failed; {len(review)} left for a person", cites)]
        auto_only = all(c["method"] == "auto" for c in deciding) and bool(deciding)
        attn = []
        if bad:
            drafted = any(self.fm[k]["class"] == "drafted" for k in keys)
            tail = " It failed twice and was not written." if drafted and outcome == "failed" else \
                " It is the client's words: take it back to the client." if not drafted else ""
            attn.append(f"{label} fails {', '.join(f['check'] for f in fails)}.{tail}")
        if review:
            attn.append(f"Left for a person: {', '.join(c['check'] for c in review)}"
                        + (f" ({res['error']})" if res.get("error") else "") + ".")
        decided = (f"The {label} passes the rubric" + (" after one revision." if outcome == "revised" else ".")) if not bad \
            else f"The {label} fails the rubric ({fails[0]['check']})."
        r = rsn.make(decided, pts,
                     rsn.certainty("high" if auto_only else "medium",
                                   "auto checks alone decided" if auto_only else "a model check decided"),
                     "the field is rewritten, or a person answers the verdict",
                     only_option="the rubric decides", attention=" ".join(attn) or None)
        extra = {"polarity": "bad" if bad else "good", "taxonomy_version": TAXONOMY}
        if bad:
            extra["reason_code"] = reason_code(fails[0]["check"])
        return self.dec(("verdict", rid, self.task), "verdict", "judge", keys, list(dict.fromkeys(cites + rule_ids)), r,
                        "judge", **extra)

    def _coherence_verdict(self, c, keys, cites, rule_ids):
        """One check across several fields. The reason is the first point, whole;
        the fix is the attention, after it — never joined into one clipped line."""
        name = c["id"].replace("_", " ")
        top = lambda k: "objectives" if k.startswith("objectives.") else LABELS.get(k.split(".")[0], LABELS[k])
        across = ", ".join(dict.fromkeys(top(k) for k in keys))
        cites_ = cites + rule_ids
        r = rsn.make(f"The brief does not hold together: {name}, across {across}.",
                     [rsn.point(sentence(c["note"]), cites_), rsn.point(f"The rule: {sentence(c['rule'])}", cites_)],
                     rsn.certainty("high" if c["method"] == "auto" else "medium",
                                   "an auto check decided" if c["method"] == "auto" else "a model check decided"),
                     "the fields it names are brought into line", only_option="the coherence rule decides",
                     attention=(f"Fix: {sentence(c['fix'])} " if c.get("fix") else "")
                               + "A person answers this before the brief locks.")
        return self.dec(("coherence", c["id"], self.task), "verdict", "judge", keys, list(dict.fromkeys(cites + rule_ids)),
                        r, "judge", polarity="bad", reason_code=reason_code(c["id"]), taxonomy_version=TAXONOMY)

    # -- open questions ------------------------------------------------------------------
    def _questions(self, values, failed_keys, bad_captured, verdicts, patch, decisions):
        k = "open_questions"
        if k in self.locked:
            return
        self.set_state([k], "judging", "judge")
        qs, pts = [], []
        capq = list((self.capture or {}).get("open_questions") or [])
        items = (self.capture or {}).get("items") or ((self.W.get("capture") or {}).get("items") or {})
        have = {q["question"] for q in capq}
        capq += [q for q in cap_stage.core_gap_questions(items) if q["question"] not in have]
        if capq:
            qs += [cap_stage.format_question(q) for q in capq]
            pts.append(rsn.point(f"{len(capq)} question(s) the client's material leaves open", f"{self.doc}#capture"))
        vby = {tuple(sorted(t.split("#", 1)[1] for t in v["targets"])): v for v in verdicts}
        for fk in dict.fromkeys(failed_keys):
            v = next((x for key, x in vby.items() if fk in key and x.get("polarity") == "bad"), None)
            qs.append(f"Agree the {LABELS[fk]}.")
            pts.append(rsn.point(f"The {LABELS[fk]} failed the Judge twice", v["id"] if v else f"{self.doc}#{fk}"))
        for fk, fails in bad_captured:
            v = next((x for key, x in vby.items() if fk in key and x.get("polarity") == "bad"), None)
            qs.append(f"{LABELS[fk][0].upper() + LABELS[fk][1:]}: {fails[0]['reason']} — take this back to the client.")
            pts.append(rsn.point(f"The client's {LABELS[fk]} fails {fails[0]['check']}", v["id"] if v else f"{self.doc}#{fk}"))
        qs = clean(k, list(dict.fromkeys(qs)))
        if qs is None:
            self.set_state([k], "absent", "judge")
            return
        r = rsn.make(f"Composed {len(qs)} open question(s) for the brief.", pts,
                     rsn.certainty("high", "composed by rule from the capture and the verdicts"),
                     "the client answers, or a verdict is resolved",
                     only_option="the questions follow from what is missing and what failed")
        cites = [c for p in pts for c in p.get("cites") or []]
        if self.held.get(k):
            d = self.dec(("propose", k, self.task), "edit", "propose", [k], cites, r, "judge", proposed_value=qs)
            self.proposals.append({"field": k, "value": qs, "decision": d["id"]})
            self.set_state([k], "proposed", "judge")
        elif qs == get(self.W, k):
            self.set_state([k], "done", "judge")
            return
        else:
            d = self.dec(("questions", self.task), "edit", "questions", [k], cites, r, "judge")
            put(patch, k, qs)
            self.set_state([k], "done", "judge")
        decisions.append(d)

