"""The start_campaign job: six stages, one job (middleware-api.md §8, M1).

extract -> identify -> select -> research -> synthesise -> report

A background worker runs the stages in order. Each stage's output is a
"chunk" (patch, read-set, pins, findings, decisions, and the agent message
that narrates it), computed ONCE and cached, so a repeat is byte-identical and
carries the same decision ids. Every reply carries the chunks whose decisions
the request's `clan.decision_chain` does not hold yet (§8.4).

identify may stop the job at `needs_input` with a question; the worker waits
until `answer_question` is accepted. The report stage waits until a poll
brings a document holding every earlier stage's decisions, and composes from
that document (§8.4).

No stage halts the job (the owner, 2026-09-30: "no stage should halt a brief
or Research being produced"). A stage that raises, that the model refuses or
answers malformed, or that outlives its time is a recorded gap — a decision
and a chat message naming the stage and why, with what the job does without
it — and the next stage runs. A question nobody answers within QUESTION_WAIT
goes on with a stated default (never a guessed brand). A change the host keeps
refusing is dropped after REFUSED_AFTER polls, so what follows it can land,
and its stage becomes a gap. The report stage waits at most REPORT_WAIT for a
poll bringing the earlier stages, then composes from the job's own copy. The
job always ends `done`, with a report, or with the reason there is none.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
import traceback

from ..doc import (CAMPAIGN_FIELDS, GATES, LENS_NAMESPACE, LENS_TITLES, LENSES, STAGES, address, apply_patch,
                   build_materials, ctx_data, ctx_decisions, ctx_facts, ctx_findings, decision, deep_merge, field_paths,
                   get_dotted, human_owned, known_ids, market_list, read_of)
from ..jobs import Abandoned, Cancelled, abandon, abandoned, bounded
from ..layers import origin_uri
from ..model import ModelError
from ..rules import identify as id_rules
from ..rules import markets as market_rules
from ..rules.confidence import fact_confidence
from ..rules.quotes import find_quote
from ..util import TaskError, iso, lid, slug, uid, ulid_like
from .. import reasoning as rsn
from . import extract as extract_stage
from . import report as report_stage
from . import synthesise as synth_stage
from .research import Researcher

log = logging.getLogger("napkin.campaign")

# Nothing the job waits on is waited on for ever (§8.3, §8.4). Seconds; a
# setting of the same name (lower case) overrides each.
QUESTION_WAIT = 30 * 60      # a question waits this long for the person, then the stated default
REPORT_WAIT = 10 * 60        # the report stage waits this long for a poll bringing the earlier stages
STAGE_TIMEOUT = 2 * 60 * 60  # a stage still running after this is left behind
REFUSED_AFTER = 3            # polls a change is missing from, sent with others and then alone: refused

# The decision a gap records carries the stage's own action, so the person's
# view names the agent who does that work and offers its "Ask to redo".
STAGE_ACTION = {"extract": "extract", "identify": "identify", "select": "select", "research": "research",
                "synthesise": "synthesise", "report": "report"}
# Why a stage did not finish, in words (no figures: a reasons point states none uncited).
WHAT = {"refusal": "the model declined the call", "timeout": "it ran out of time",
        "invalid_output": "the model's answer did not validate", "model": "the model call failed",
        "task": "it could not run", "raised": "it stopped with an error",
        "not_applied": "the host did not apply its change",
        "gated": "no category is settled, and research runs per category"}
# What the job does without the stage.
WITHOUT = {"extract": "the fields the material states stay open, and identify reads the material itself",
           "identify": "what it did not settle stays open, and research runs only on what is settled",
           "select": "every lens runs in every market, the default",
           "research": "nothing new is researched, and the gaps say what was not looked into",
           "synthesise": "no finding is proposed, and the report stands on the pins",
           "report": "no report is written, and Refresh report composes one from what landed"}
# The same, in the words the person's view uses for the crew's work: the chat
# message and the attention item say these; the stage and the kind stay in the
# decision's reasoning and in the logs.
DOING = {"extract": "reading what you sent", "identify": "working out the brand and its category",
         "select": "picking what to look into", "research": "looking it up",
         "synthesise": "working out the points", "report": "writing the report"}
WHY = {"refusal": "the model would not answer", "timeout": "it took too long",
       "invalid_output": "the answer came back in a form we could not use",
       "model": "we could not reach the model", "task": "it could not start",
       "raised": "something went wrong on our side",
       "not_applied": "the document did not take what it wrote",
       "gated": "no category is settled yet, and we look things up per category"}
INSTEAD = {"extract": "what your material says stays open, and we read it again to find the brand",
           "identify": "what we could not settle stays open, and we only look up what is settled",
           "select": "every researcher looks into every market",
           "research": "nothing new was looked up; what is missing is under Selection",
           "synthesise": "the report uses the facts as they are, with no points worked out",
           "report": "there is no report yet; Refresh report writes one from what is here"}


def gap_words(stage, kind) -> str:
    """A gap in the crew's words: what we could not do, why, and what we did instead."""
    return (f"We couldn't finish {DOING.get(stage, stage)}: {WHY.get(kind, WHY['raised'])}. "
            f"We went on without it: {INSTEAD.get(stage, 'the rest goes on without it')}.")


# What an unanswered question leaves open, when it has no default to go on with.
UNANSWERED = {"brand": "no roster row is looked up, and research covers the category and the markets",
              "categories": "research needs a category, so it does not run",
              "markets": "research runs once per market, so it does not run"}

IDENTIFY_SYSTEM = """You read a campaign ask for an advertising agency. From the prompt and the attached
material (each with a material_id) list:
- every brand named, in order, each with the exact quote that names it (copied character for character)
  and its basis: "brand_label" when the material labels it the brand (e.g. "Brand: X"), "named_as_ours"
  when the material calls it ours / the client's / the one we work for, otherwise "none"; and whether the
  material presents it as a competitor or comparator. A car maker, a drinks brand and a bank are all
  brands; a market, a country or a product category is not.
- the client organisation, only if the material names it (e.g. a signature), with its quote;
- at most two category leaves from the given tree that the ask is about, most likely first, each with
  the quote that points at it. Choose from the tree only.
Never guess what the material does not say.
The grounds are for the client's brand: the quotes (by material_id) that make it the client's, and
for each other brand named, what in the material makes it not the client's.""" + rsn.GUIDE

SELECT_SYSTEM = """You plan the research for an advertising campaign ask. There are eight research lenses.
Decide, for each lens, whether this ask needs it. Research everything by default: skip a lens (or skip it
in some markets) ONLY when the prompt says to leave it out, limits it to certain markets, or asks only
for other lenses, or when the lens plainly has nothing to read for these categories. Give the reason for
every skip, quoting the prompt where it is the prompt's instruction.
The grounds are for the plan as a whole: what in the prompt (cite its material_id) supports each skip,
and the other plans the prompt would allow (research everything, skip more) and what rules each out.""" + rsn.GUIDE

CLASSIFY_SYSTEM = """Map a person's typed description of a product category to at most two leaves of the
given category tree, most likely first. Return an empty list when nothing in the tree fits."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def identify_schema(leaf_codes: list[str]) -> dict:
    q = {"quote": {"type": "string"}, "material_id": {"type": "string"}}
    return _obj({
        "brands": {"type": "array", "items": _obj({
            "name": {"type": "string"}, **q,
            "basis": {"type": "string", "enum": ["brand_label", "named_as_ours", "none"]},
            "comparator": {"type": "boolean"}})},
        "client_org": {"anyOf": [_obj({"name": {"type": "string"}, **q}), {"type": "null"}]},
        "categories": {"type": "array", "items": _obj({"leaf": {"type": "string", "enum": leaf_codes}, **q})},
        "grounds": rsn.MODEL_SCHEMA,
    })


SELECT_SCHEMA = _obj({"lenses": {"type": "array", "items": _obj({
    "lens": {"type": "string", "enum": LENSES}, "run": {"type": "boolean"},
    "skip_markets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}})},
    "grounds": rsn.MODEL_SCHEMA})


def classify_schema(leaf_codes):
    return _obj({"leaves": {"type": "array", "items": {"type": "string", "enum": leaf_codes}}})


def _why(e) -> tuple[str, str]:
    """(kind, detail) for what a stage raised: a key of WHAT, and a short message."""
    if isinstance(e, ModelError):
        kind = {"refusal": "refusal", "timeout": "timeout", "invalid_output": "invalid_output",
                "truncated": "invalid_output"}.get(e.kind, "model")
        return kind, str(e)[:200] or f"the model call failed ({e.kind})"
    if isinstance(e, TaskError):
        return "task", (e.message or e.etype)[:200]
    return "raised", f"{type(e).__name__}: {str(e)[:200]}"


def _minutes(seconds) -> str:
    if seconds < 90:
        return f"{int(seconds)} second(s)"
    return f"{round(seconds / 60)} minute(s)"


class Chunk:
    def __init__(self, stage, base, patch, read, facts=(), findings=(), decisions=(), message=None, sources=(),
                 gap=False):
        self.stage, self.base = stage, base
        self.patch, self.read = patch, read
        self.facts, self.findings, self.decisions = list(facts), list(findings), list(decisions)
        self.sources = list(sources)
        self.message = message
        self.ids = {d["id"] for d in self.decisions}
        self.gap = gap  # a stage's gap: never itself made a gap when refused
        # Refusal detection (reply_change): polls this was missing from since
        # it was last sent, whether it is being sent on its own, and refused.
        self.misses, self.solo, self.refused = 0, False, False


def combine(doc, chunks) -> dict | None:
    if not chunks:
        return None
    patch, read, facts, findings, sources, decs = {}, {}, [], [], [], []
    for c in chunks:
        patch = deep_merge(patch, c.patch)
        for k, v in c.read.items():
            read.setdefault(k, v)  # what the EARLIEST writer of the path read
        facts += [f for f in c.facts if f["id"] not in {x["id"] for x in facts}]
        findings += [f for f in c.findings if f["id"] not in {x["id"] for x in findings}]
        sources += [s for s in c.sources if s["id"] not in {x["id"] for x in sources}]
        decs += [d for d in c.decisions if d["id"] not in {x["id"] for x in decs}]
    return {"doc": doc, "base_version": chunks[0].base, "read": read if patch else {}, "data_patch": patch,
            "facts_append": facts, "findings_append": findings, "sources_append": sources, "decisions": decs}


class CampaignJob:
    def __init__(self, jid, doc, handler, clan, inp, caps, settings):
        self.id, self.doc, self.handler = jid, doc, handler
        self.task = "start_campaign"
        self.caps, self.settings = caps, settings
        self.scope = caps.scope
        self.inp = inp
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.started_at = iso()
        self.finished_at = None
        self.state = "queued"
        self.stage_idx = 0
        self.question = None
        self.error = None
        self.chunks: list[Chunk] = []
        self.last_change = None
        self.seq = 0
        self.answers = {}
        self.pending_text = None
        self.answer_by = None
        self.selected = None
        self.hits = []
        self._identify_raw = None
        self._identify_failed = None   # why identify's model call gave nothing, when it did not answer
        self.gaps = []                 # [{stage, reason}]: every step the job went on without
        self.skipped_fields = set()    # campaign fields a question asked and nobody answered
        self.unattended = False        # a question went unanswered: later ones go on at once
        self.reported = False
        self._asked_at = None
        self._report_wait_from = None
        self._report_from = "landed"   # landed | working: which document the report composes from
        self._last_sent = set()        # the chunks the previous reply carried
        self.latest_clan = clan
        self.W, self.W_facts, self.W_version, self.clan = {}, [], None, clan
        self.sync(clan)
        self.thread = threading.Thread(target=self._run, name=f"campaign-{jid}", daemon=True)

    # -- the working copy ----------------------------------------------------
    def sync(self, clan):
        """The request's document, plus any chunk of ours that has not landed there."""
        known = known_ids(clan)
        W = copy.deepcopy(ctx_data(clan))
        facts = copy.deepcopy(ctx_facts(clan))
        for c in self.chunks:
            if c.ids <= known or c.refused:
                continue
            for p in field_paths(c.patch):
                if get_dotted(W, p) is None:
                    W = apply_patch(W, _sub(c.patch, p))
            facts += [f for f in c.facts if f["id"] not in {x["id"] for x in facts}]
        camp = W.setdefault("campaign", {})
        for f, v in self.answers.items():
            if f not in camp:  # the person answered; the view's write has not reached us
                camp[f] = {"value": v, "origin": "confirmed", "gate": GATES[f], "decision": "d_PENDINGANSWER"}
        self.W, self.W_facts, self.W_version, self.clan = W, facts, clan.get("version"), clan

    def wclan(self):
        return {"id": self.doc, "version": self.W_version, "data": self.W, "facts": self.W_facts,
                "findings": ctx_findings(self.clan), "decision_chain": self.clan.get("decision_chain") or {}}

    def did(self, *parts):
        return uid("d_", self.doc, self.id, *parts)

    def add_chunk(self, stage, patch, decisions, facts=(), findings=(), text=None, question=None,
                  msg_decision=None, base=None, read_from=None, sources=(), gap=False, cancelling=False):
        if abandoned() or (self.caps.jobs.cancelled and not cancelling):
            raise Abandoned(stage)  # the job went on without this stage, or was cancelled: what it would write is dropped
        patch = copy.deepcopy(patch)
        decisions = list(decisions)
        message = None
        if text:
            self.seq += 1
            mid = ulid_like("msg_", self.seq, self.doc, self.id, stage)
            m = {"role": "agent", "text": text, "at": iso(), "job_id": self.id, "stage": stage}
            if question:
                m["question"] = question
            patch.setdefault("intake", {}).setdefault("messages", {})[mid] = m
            message = {"id": mid, "text": text, "stage": stage}
            owner = msg_decision or (decisions[0] if decisions else None)
            if owner is None:
                owner = decision(self.doc, self.did(stage, "narrate", self.seq), "edit", self.handler, "narrate",
                                 f"The {stage} stage's chat message.", [], reasoning=rsn.make(
                                     f"Posted the {stage} stage's message in the chat.",
                                     [rsn.point(f"The {stage} stage finished and says what it did")],
                                     rsn.certainty("high", "it reports what the stage recorded"),
                                     "the stage is rerun", only_option="a stage always says what it did"))
            if owner not in decisions:
                decisions.append(owner)
            owner["targets"].append(f"{self.doc}#intake.messages[{mid}]")
            owner.setdefault("fields_changed", [])
            if "intake.messages" not in owner["fields_changed"]:
                owner["fields_changed"].append("intake.messages")
        for p in field_paths(patch):  # every field the patch writes is named by a decision (§3)
            a = f"{self.doc}#{address(p)}"
            if not any(a == t or t.startswith(a + "[") for d in decisions for t in d["targets"]):
                decisions[0]["targets"].append(a)
        self.caps.runlog.decisions(decisions, stage)
        c = Chunk(stage, base if base is not None else self.W_version, patch,
                  read_of(read_from if read_from is not None else self.W, patch), facts, findings, decisions, message,
                  sources, gap=gap)
        with self.lock:
            self.chunks.append(c)
            self.W = apply_patch(self.W, patch)
            self.W_facts += [f for f in c.facts if f["id"] not in {x["id"] for x in self.W_facts}]
        return c

    def messages(self):
        with self.lock:
            return [c.message for c in self.chunks if c.message]

    # -- the worker ----------------------------------------------------------------
    def start(self):
        self.thread.start()

    def _limit(self, name):
        v = getattr(self.settings, name, None)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else globals()[name.upper()]

    def _run(self):
        self.caps.runlog.start("start_campaign", self.doc, STAGES, model=self.caps.model.model_id)
        try:
            self._loop()
        except Exception as e:  # the runner itself, not a stage: the job still ends, and says so
            log.error("start_campaign %s runner failed: %s\n%s", self.id, e, traceback.format_exc())
            self.fail(STAGES[min(self.stage_idx, len(STAGES) - 1)], "internal",
                      f"the job's runner failed ({type(e).__name__})")
        finally:
            with self.lock:
                state, err, gaps = self.state, self.error, list(self.gaps)
            self.caps.runlog.end(state, summary=(err or {}).get("message"), gaps=gaps)

    def cancel(self) -> bool:
        """A person stopped the job: it is `cancelled` at once and holds its
        document no longer. What it sent stays; the stage running now is left
        behind (it can write nothing more); one decision and a chat message
        say where it stopped."""
        with self.lock:
            if self.state not in ("queued", "running", "needs_input"):
                return False
            stage = STAGES[min(self.stage_idx, len(STAGES) - 1)]
            self.state, self.finished_at, self.question = "cancelled", iso(), None
            self.caps.jobs.stop()
            self.cond.notify_all()
        self.caps.runlog.gap(stage, "cancelled", "a person cancelled the job")
        d = decision(self.doc, self.did(stage, "cancelled"), "edit", self.handler, "cancel_job", "", [],
                     reasoning=rsn.make(
                         f"Stopped at the {stage} step: a person cancelled the job.",
                         [rsn.point("A person asked to stop the job"),
                          rsn.point(f"Without the rest, {WITHOUT[stage]}")],
                         rsn.certainty("high", "the person asked to stop it"),
                         "the step is asked for again, or the campaign is started again",
                         only_option="a person stopped it",
                         attention=f"Cancelled at the {stage} step; what finished before it stays."))
        try:
            self.add_chunk(stage, {}, [d], text=f"Stopped: you cancelled it at the {stage} step. "
                                                f"What finished before it stays.", gap=True, cancelling=True)
        except Exception:  # noqa: BLE001 - the job is cancelled either way
            log.error("start_campaign %s: the cancel could not be recorded", self.id)
        return True

    def _loop(self):
        while True:
            with self.lock:
                if self.state in ("done", "failed", "cancelled"):
                    return
                if self.state == "needs_input":
                    left = (self._asked_at or 0) + self._limit("question_wait") - time.monotonic()
                    if left > 0:
                        self.cond.wait(timeout=min(left, 30))
                        continue
                    self.went_on()  # nobody answered: the stated default, recorded (§8.3)
                self.state = "running"
                stage = STAGES[self.stage_idx]
                if stage == "report" and not self._report_ready():
                    continue
            finished = self._stage(stage)
            with self.lock:
                if self.state == "cancelled":
                    return
                if finished is None:
                    continue  # the stage runs again: identify, once a question went on without its answer
                if not finished:
                    self.state = "needs_input"
                    self._asked_at = time.monotonic()
                    continue
                self.stage_idx += 1
                if self.stage_idx >= len(STAGES):
                    self.state = "done"
                    self.finished_at = iso()
                    self.cond.notify_all()
                    return

    def _report_ready(self) -> bool:
        """(Under the lock.) The report composes from the first poll whose
        document holds every earlier stage (§8.4) — or, when no poll brings
        them within REPORT_WAIT, from the job's own copy. Never a wait for
        ever: a change the host refused is not waited on (reply_change)."""
        earlier = set().union(*(c.ids for c in self.chunks if not c.refused))
        if earlier <= known_ids(self.latest_clan):
            self._report_from = "landed"
            return True
        now = time.monotonic()
        if self._report_wait_from is None:
            self._report_wait_from = now
        left = self._report_wait_from + self._limit("report_wait") - now
        if left <= 0:
            self._report_from = "working"
            return True
        self.cond.wait(timeout=min(left, 30))
        return False

    def _stage(self, stage):
        """One stage, in its own thread for at most STAGE_TIMEOUT. -> True
        (finished, or went on without it), False (asked the person), None (run
        it again). What it raises, however it fails, is a gap, never the end."""
        limit = self._limit("stage_timeout")
        with self.caps.runlog.stage(stage):
            value, err, left = bounded(getattr(self, "stage_" + stage), limit, f"campaign-{self.id}-{stage}",
                                       stop=lambda: self.caps.jobs.cancelled)
        if self.caps.jobs.cancelled:
            if left is not None:
                abandon(left)
            return True  # cancelled: nothing to record but the cancel itself (cancel)
        if left is not None:
            with self.lock:
                abandon(left)
                self.question = None
            log.warning("start_campaign %s stage %s outlived %ss; going on without it", self.id, stage, limit)
            self.skip(stage, "timeout", f"it did not finish within {_minutes(limit)}")
            return True
        if err is not None:
            kind, detail = _why(err)
            if kind == "raised":
                log.error("start_campaign %s stage %s failed: %s\n%s", self.id, stage, err,
                          "".join(traceback.format_exception(type(err), err, err.__traceback__)))
            else:
                log.warning("start_campaign %s stage %s did not finish (%s): %s", self.id, stage, kind, detail)
            with self.lock:
                self.question = None
            self.skip(stage, kind, detail)
            return True
        return value

    def skip(self, stage, kind, detail, refused=None):
        self.caps.runlog.gap(stage, kind, detail)
        self._record_skip(stage, kind, detail, refused)

    def _record_skip(self, stage, kind, detail, refused=None):
        """Record a stage the job goes on without: a decision carrying the
        stage's own action (so "Ask to redo" can run it again), its reason and
        what the job does without it, and a chat message. `refused` is the
        chunk the host would not apply; the gap then writes nothing but the
        message, read from what the host holds. Recording it never stops the
        job either: a gap that cannot be written in full is written plainly."""
        with self.lock:
            self.gaps.append({"stage": stage, "reason": kind})
            n = len(self.gaps)
            if stage == "select" and refused is None:
                self.selected = self.default_plan()
        try:
            self._skip(stage, kind, detail, refused, n)
        except Abandoned:
            raise
        except Exception as e:  # noqa: BLE001 - the plain gap, then on
            log.error("start_campaign %s: the %s stage's gap could not be written in full (%s)", self.id, stage,
                      type(e).__name__)
            try:
                d = decision(self.doc, self.did(stage, "skipped", n, "plain"), "edit", self.handler,
                             STAGE_ACTION[stage], "", [], reasoning=self._gap_reasoning(stage, kind, detail))
                self.add_chunk(stage, {}, [d], text=gap_words(stage, kind), gap=True)
            except Exception:  # noqa: BLE001
                log.error("start_campaign %s: the %s stage's gap could not be written", self.id, stage)

    def _skip(self, stage, kind, detail, refused, n):
        if stage == "research" and refused is None:
            self._research_gap(self.did("research", "skipped", n), kind, detail)
            return
        d = decision(self.doc, self.did(stage, "skipped", n), "edit", self.handler, STAGE_ACTION[stage], "", [],
                     reasoning=self._gap_reasoning(stage, kind, detail))
        extra = {}
        if refused is not None:
            with self.lock:
                clan = self.latest_clan
            extra = {"base": clan.get("version"), "read_from": ctx_data(clan)}
        self.add_chunk(stage, {}, [d], text=gap_words(stage, kind), gap=True, **extra)

    def _gap_reasoning(self, stage, kind, detail) -> dict:
        return rsn.make(
            f"Went on without the {stage} stage: {WHAT[kind]}.",
            [rsn.point(f"The {stage} stage did not finish: {WHAT[kind]}"),
             rsn.point(f"Without it, {WITHOUT[stage]}")],
            rsn.certainty("high", "the stage did not finish, so nothing it would have written is in the document"),
            "the step is asked for again, or the campaign is started again",
            rejected=[rsn.rej(f"stop the campaign at the {stage} stage",
                              "a stopped job leaves the person with nothing; the later stages run on what there is")],
            attention=f"{gap_words(stage, kind)} (What went wrong: {detail[:200]})")

    def default_plan(self):
        """select's default: every lens in every market (§8.1)."""
        markets = list(((self.W.get("campaign") or {}).get("markets") or {}).get("value") or [])
        return [(l, m) for l in LENSES for m in markets], []

    def _research_gap(self, did, kind, detail, pairs=None):
        """Research did not run: each selected lens x market becomes a gap the
        report names, with a `research_run` decision the view offers to run
        again; one decision owns the chat message."""
        pairs = list(pairs if pairs is not None else (self.selected[0] if self.selected else []))
        camp = self.W.get("campaign") or {}
        cats = list((camp.get("categories") or {}).get("value") or [])
        entity = f"category/{cats[0]}" if cats else "category/unsettled"
        gaps, decs = [], []
        for l, m in pairs:
            gid = lid("gap_", self.doc, self.id, "not-run", l, m)
            where = f"{LENS_TITLES[l]} in {market_list([m])}"
            gaps.append({"id": gid, "key": f"{entity}:{LENS_NAMESPACE[l]}", "lens": l, "market": m,
                         "searched": where, "sources_tried": [f"research:{l}/{m}"],
                         "note": f"research did not run: {WHAT[kind]} ({detail})"[:300]})
            decs.append(decision(
                self.doc, self.did("research", "not-run", l, m), "edit", self.handler, "research_run", "",
                [f"selection.gaps[{gid}]", f"selection.lenses_run[{l}/{m}]"], fields_changed=["selection.gaps"],
                reasoning=rsn.make(
                    f"Did not research {where}: {WHAT[kind]}.",
                    [rsn.point(f"The research stage did not run: {WHAT[kind]}", gid)],
                    rsn.certainty("high", "nothing was researched for this lens and market"),
                    "the lens is looked into again",
                    rejected=[rsn.rej("report the lens as covered",
                                      "a lens that did not run is a gap, never a silent success")],
                    attention=f"{where} was not researched ({detail[:160]}); ask to look again.")))
        gids = [g["id"] for g in gaps]
        own = decision(self.doc, did, "edit", self.handler, "research", "", [], gids, reasoning=rsn.make(
            f"Researched nothing: {WHAT[kind]}.",
            [rsn.point(f"The research stage did not run: {WHAT[kind]}", gids)],
            rsn.certainty("high", "no lens and market was researched"),
            "the step is asked for again, or the campaign is started again",
            rejected=[rsn.rej("stop the campaign at the research stage",
                              "a stopped job leaves the person with nothing; the report names what is missing")],
            attention=f"{gap_words('research', kind)} (What went wrong: {detail[:200]})"))
        patch = {}
        if gaps:
            old = [g for g in ((self.W.get("selection") or {}).get("gaps") or []) if g.get("id") not in set(gids)]
            patch = {"selection": {"gaps": old + gaps}}
        text = gap_words("research", kind) + (" Ask to look again from Selection." if gaps else "")
        self.add_chunk("research", patch, [own] + decs, text=text, msg_decision=own, gap=True)

    def fail(self, stage, etype, message):
        """The runner itself failed (never a stage: a stage is a gap, see skip)."""
        try:
            d = decision(self.doc, self.did(stage, "failed"), "edit", self.handler, "stage_failed",
                         f"The job's runner failed at the {stage} stage: {message}. What landed before it stays.", [],
                         reasoning=rsn.make(
                             f"Stopped the campaign at the {stage} stage; what landed before it stays.",
                             [rsn.point("The job's runner raised an error (its message is in the attention note)")],
                             rsn.certainty("high", "the runner did not finish"),
                             "the cause is fixed and the campaign is started again",
                             only_option="the runner cannot go on",
                             attention=f"The job's runner failed at the {stage} stage: {message[:200]}"))
            self.add_chunk(stage, {}, [d], text=f"The job stopped at the {stage} stage: {message}. What already "
                                                f"landed stays.", gap=True)
        except Exception:  # noqa: BLE001 - the job still ends
            log.error("start_campaign %s: could not record the runner's failure", self.id)
        with self.lock:
            self.state = "failed"
            self.error = {"type": etype, "message": message}
            self.finished_at = iso()
            self.cond.notify_all()

    # -- replies ----------------------------------------------------------------
    def reply_change(self, clan):
        """The change for this reply: every chunk not landed in `clan` (§8.4).

        A change the host will not apply must not hold the job, nor everything
        after it (the host applies a change whole). A chunk missing from
        REFUSED_AFTER polls after it was sent is sent on its own; on its own
        and missing from REFUSED_AFTER more, the host refused it. It is dropped,
        what follows it goes on landing, and its stage becomes a gap."""
        refused = []
        with self.lock:
            self.latest_clan = clan
            known = known_ids(clan)
            pending = [c for c in self.chunks if not c.refused and not c.ids <= known]
            for c in pending:
                if id(c) in self._last_sent:
                    c.misses += 1
            for c in pending:
                if c.solo and c.misses >= REFUSED_AFTER:
                    c.refused = True
                    refused.append(c)
            if refused:
                pending = [c for c in pending if not c.refused]
                for c in pending:
                    c.misses = 0
            if pending and not pending[0].solo and pending[0].misses >= REFUSED_AFTER:
                pending[0].solo, pending[0].misses = True, 0  # find which one the host will not take
            send = pending[:1] if pending and pending[0].solo else pending
            self._last_sent = {id(c) for c in send}
            change = combine(self.doc, send)
            if change is None and self.state in ("done", "cancelled"):
                change = self.last_change
            if change is not None:
                self.last_change = change
            self.cond.notify_all()
        for c in refused:
            log.warning("start_campaign %s: the host did not apply the %s stage's change; going on without it",
                        self.id, c.stage)
            if not c.gap:
                self.skip(c.stage, "not_applied", "the host did not apply the change it was sent", refused=c)
        return change

    def view(self):
        with self.lock:
            return {"id": self.id, "state": self.state,
                    "progress": {"done": min(self.stage_idx, len(STAGES)), "total": len(STAGES)},
                    "stage": STAGES[min(self.stage_idx, len(STAGES) - 1)],
                    "question": self.question if self.state == "needs_input" else None,
                    "started_at": self.started_at, "finished_at": self.finished_at, "error": self.error}

    def gap_list(self):
        """`result.gaps`: every step the job went on without, `[{stage, reason}]` (display only)."""
        with self.lock:
            return [dict(g) for g in self.gaps]

    def summary(self):
        v = self.view()
        if v["state"] == "needs_input":
            return f"Waiting for you: {self.question['text']}"
        if v["state"] == "done":
            if not self.gaps:
                return "Campaign ready: the report is in the document."
            stages = ", ".join(dict.fromkeys(g["stage"] for g in self.gaps))
            head = "Campaign ready: the report is in the document" if self.reported else \
                "Campaign finished without a report"
            return f"{head}, with {len(self.gaps)} gap(s) ({stages}); each is named in the chat."
        if v["state"] == "failed":
            return f"Failed at {v['stage']}: {self.error['message']}"
        if v["state"] == "cancelled":
            return f"Cancelled at {v['stage']}: what finished before it is in the document."
        return f"{v['stage']}: {self.stage_idx} of {len(STAGES)} stage(s) done"

    def answer(self, clan, inp):
        self.caps.runlog.answer("identify", inp)
        """answer_question: validate, record, continue. Raises TaskError."""
        from ..util import bad
        with self.lock:
            if self.state != "needs_input":
                why = "; nobody answered in time, so it went on with a stated default" if self.unattended else ""
                raise TaskError(409, "job_state", f"job {self.id} is {self.state}, not waiting for an answer{why}")
            q = self.question
            if inp.get("question_id") != q["id"]:
                raise bad("question_id is not the job's open question")
            has_opt, has_text = "option_id" in inp, "text" in inp
            if has_opt == has_text:
                raise bad("answer with exactly one of option_id and text")
            fld = q["address"].partition("#campaign.")[2]
            if has_opt:
                opt = next((o for o in q["options"] if o["id"] == inp["option_id"]), None)
                if opt is None:
                    raise bad("option_id is not one of the question's options")
                if "value" not in opt:
                    raise bad("that option is the escape: answer it with text")
                self.answers[fld] = opt["value"]
            else:
                if not q["allow_text"]:
                    raise bad("this question does not take a free-text answer")
                if not isinstance(inp["text"], str) or not inp["text"].strip():
                    raise bad("text must be a non-empty string")
                self.pending_text = {"field": fld, "text": inp["text"].strip()}
            by = next((m.get("by") for m in ((ctx_data(clan).get("intake") or {}).get("messages") or {}).values()
                       if isinstance(m, dict) and (m.get("answer") or {}).get("question_id") == q["id"]), None)
            self.answer_by = by if isinstance(by, str) and by.startswith("human:") else self.answer_by
            self.question = None
            self.state = "running"
            self.sync(clan)  # the answer's document is the base from here
            self.latest_clan = clan
            self.cond.notify_all()

    # -- material ----------------------------------------------------------------
    def materials(self):
        mats, _ = build_materials(self.inp, self.W)
        return mats

    def _span(self, material_id, quote, name=None):
        mats = self.materials()
        by = {m.id: m for m in mats}
        for mat in ([by[material_id]] if material_id in by else []) + [m for m in mats if m.id != material_id]:
            pos = find_quote(mat.text, quote or "")
            if pos:
                return {"material_id": mat.id, "locator": mat.locator(pos[0]), "quote": mat.text[pos[0]:pos[1]]}
        if name:  # the quote did not verify: the name itself, where the material says it
            for mat in mats:
                m = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", mat.text, re.I)
                if m:
                    return {"material_id": mat.id, "locator": mat.locator(m.start()), "quote": m.group(0)}
        return None

    def identify_raw(self):
        """ONE model call over the material, cached for the job."""
        if self._identify_raw is None:
            leaves = self.caps.layers.leaves()
            try:
                raw = self.caps.model.structured(
                    "identify", IDENTIFY_SYSTEM,
                    {"materials": extract_stage.material_payload(self.materials()),
                     "category_tree": [{"code": l["code"], "name": l["name"], "vertical": l["vertical_name"]}
                                       for l in leaves]},
                    identify_schema([l["code"] for l in leaves]), max_tokens=4000)
                self._identify_failed = None
            except Exception as e:  # nothing read is nothing found: the rules ask the person instead
                kind, detail = _why(e)
                log.warning("start_campaign %s: identify's model call did not answer (%s): %s", self.id, kind, detail)
                with self.lock:
                    self.gaps.append({"stage": "identify", "reason": kind})
                self._identify_failed = WHAT[kind]
                raw = {}
            brands = []
            for b in raw.get("brands") or []:
                name = (b.get("name") or "").strip()
                if not slug(name):
                    continue
                sp = self._span(b.get("material_id"), b.get("quote"), name)
                if sp is None:
                    continue  # a brand the material does not name
                brands.append({"ref": id_rules.brand_ref(name), "name": name, "span": sp, "basis": b.get("basis"),
                               "comparator": bool(b.get("comparator"))})
            client = None
            co = raw.get("client_org")
            if co and slug(co.get("name", "")):
                sp = self._span(co.get("material_id"), co.get("quote"))
                if sp:
                    client = {"value": {"ref": "org/" + slug(co["name"]), "name": co["name"].strip()}, "span": sp}
            codes = {l["code"] for l in leaves}
            cats = []
            for c in raw.get("categories") or []:
                sp = self._span(c.get("material_id"), c.get("quote"))
                if c.get("leaf") in codes and sp and c["leaf"] not in [x[0] for x in cats]:
                    cats.append((c["leaf"], sp))
            self._identify_raw = {"brands": brands, "client": client, "categories": cats[:2],
                                  "grounds": raw.get("grounds"),
                                  "material_ids": [m.id for m in self.materials()]}
        return self._identify_raw

    # -- stages -----------------------------------------------------------------
    def stage_extract(self):
        did = self.did("extract")
        result, change, hits = extract_stage.run_extract(self.doc, self.W_version, self.wclan(), self.inp,
                                                         self.handler, self.caps,
                                                         skip={"brand", "client_org", "categories"}, did=did,
                                                         action="extract")
        self.hits += hits
        dec = change["decisions"][0]
        dec["targets"] = [t for t in dec["targets"] if t != f"{self.doc}#campaign"]
        for mid in result.get("materials_new", []):
            dec["targets"].append(f"{self.doc}#materials[{mid}]")
        filled = result["fields"]
        names = ", ".join(f.replace("_", " ") for f in filled) or "nothing"
        text = (f"Read {len(result['materials_read'])} material(s). Filled from what they say: {names}."
                + (f" Left open, because nothing supports them: "
                   f"{', '.join(a.replace('_', ' ') for a in result['abstained'])}." if result["abstained"] else ""))
        if result["materials_unread"]:
            text += f" {len(result['materials_unread'])} attachment(s) had no readable text and ground nothing."
        self.add_chunk("extract", change["data_patch"], [dec], text=text)
        return True

    def ask(self, field, text, options, allow_text, intro, decisions=None, patch=None, facts=()):
        qid = uid("q_", self.doc, self.id, field, self.seq + 1, n=12)
        q = {"id": qid, "text": text, "options": options, "allow_text": allow_text,
             "address": f"{self.doc}#campaign.{field}"}
        if self.unattended:  # nobody answered the last question: this one goes on with its default at once
            self.default_answer(q, decisions, patch, facts)
            return None
        opt_cites = ([o["source"]["material_id"] for o in options if o.get("source")]
                     + [f for o in options for f in o.get("fact_ids", [])])
        named = [o["label"] for o in options if "value" in o]
        label = field.replace("_", " ")
        because = [rsn.point(intro, opt_cites) if intro and (opt_cites or not rsn.states_figure(intro))
                   else rsn.point(f"Nothing settles the {label}")]
        if named:
            because.append(rsn.point(f"The candidates: {', '.join(named)}", opt_cites)
                           if opt_cites or not rsn.states_figure(", ".join(named))
                           else rsn.point("The candidates are the options offered"))
        d = decision(self.doc, self.did("ask", qid), "edit", self.handler, "identify",
                     f"Asked the person ({field}): {text} Research waits; nothing is guessed.", [], opt_cites,
                     reasoning=rsn.make(
                         f"Asked the person for the {label} instead of guessing.", because,
                         rsn.certainty("low", f"the material and the layers do not settle the {label}; that is "
                                              f"why it is asked"),
                         "the person answers, or the material names it plainly",
                         rejected=[rsn.rej(f"pick {named[0]}" if named else "guess one",
                                           "nothing says which is right, and a wrong guess would steer every "
                                           "later stage")],
                         attention="Waiting for your answer: research does not start until it is given."))
        decs = list(decisions or []) + [d]
        self.add_chunk("identify", patch or {}, decs, facts=facts, text=f"{intro} {text}".strip(), question=q,
                       msg_decision=d)
        with self.lock:
            self.question = q
        self.caps.runlog.question("identify", text, [o.get("label") for o in options])
        return False

    def went_on(self):
        """(Under the lock.) Nobody answered within QUESTION_WAIT: take the
        question's stated default, record it, and run identify again. Later
        questions in this job go on at once (nobody is there to answer)."""
        q = self.question
        self.question = None
        self.unattended = True
        self.state = "running"
        if q:
            self.default_answer(q)

    def default_answer(self, q, decisions=(), patch=None, facts=()):
        """A question's stated default, written with its decision: the
        material's most likely candidate (extracted, with its span, or proposed
        from pins) — never for the brand, which is never guessed — or, with no
        candidate, the field left open and named as a gap."""
        fld = q["address"].partition("#campaign.")[2]
        label = fld.replace("_", " ")
        pick = None if fld == "brand" else next(
            (o for o in q["options"] if "value" in o and (o.get("origin") == "extracted" and o.get("source")
                                                         or o.get("origin") == "proposed" and o.get("fact_ids"))),
            None)
        did = self.did("default", q["id"])
        patch = copy.deepcopy(patch) if patch else {}
        cites = []
        if pick:
            env = {"value": copy.deepcopy(pick["value"]), "origin": pick["origin"], "gate": GATES[fld],
                   "decision": did}
            if pick.get("source"):
                env["source"] = pick["source"]
                cites.append(pick["source"]["material_id"])
            if pick.get("fact_ids"):
                env["fact_ids"] = list(pick["fact_ids"])
                cites += list(pick["fact_ids"])
            patch.setdefault("campaign", {})[fld] = env
        with self.lock:
            if not pick:
                self.skipped_fields.add(fld)
            self.gaps.append({"stage": "identify", "reason": "unanswered"})
        waited = _minutes(self._limit("question_wait"))
        because = [rsn.point(f"Nobody answered the question about the {label}")]
        if pick:
            because.append(rsn.point(f"The material's most likely reading is {pick['label']}", cites))
        rejected = [rsn.rej("wait for the answer for ever", "a job that never finishes leaves the person with nothing")]
        named = [o["label"] for o in q["options"] if "value" in o]
        if fld == "brand" and named:
            rejected.append(rsn.rej(f"take {named[0]} as the client's brand", "the subject brand is never guessed"))
        r = rsn.make(
            f"Went on with {pick['label']} as the {label}: nobody answered." if pick else
            f"Went on without the {label}: nobody answered.", because,
            rsn.certainty("low", "nobody confirmed it; it is the material's most likely reading") if pick else
            rsn.certainty("high", "nothing was chosen, so nothing was written"),
            "the person confirms or changes it, or starts the campaign again with it",
            rejected=rejected,
            attention=(f"Nobody answered, so the job went on with {pick['label']} from the material: confirm the "
                       f"{label}." if pick else
                       f"Nobody answered, so the job went on without the {label}: {UNANSWERED[fld]}."))
        d = decision(self.doc, did, "edit", self.handler, "identify", "", [f"campaign.{fld}"] if pick else [], cites,
                     reasoning=r, **({"fields_changed": [f"campaign.{fld}"]} if pick else {}))
        text = (f"Nobody answered in {waited}, so I went on with {pick['label']} for the {label}, as the material "
                f"reads: confirm it." if pick else
                f"Nobody answered in {waited}, so I went on without the {label}: {UNANSWERED[fld]}.")
        self.add_chunk("identify", patch, list(decisions or []) + [d], facts=facts, text=text, msg_decision=d)

    def stage_identify(self):
        camp = self.W.get("campaign") or {}
        layers = self.caps.layers
        labels = {l["code"]: l["name"] for l in layers.leaves()}
        patch, decs, facts, notes = {"campaign": {}}, [], [], []
        did = self.did("identify", len(self.chunks))
        idec = decision(self.doc, did, "edit", self.handler, "identify", "", [], [])
        # What each written field rests on, the derived level of each, what lost.
        ev_material, ev_pins, levels, rejected = [], [], [], []

        def write(field, env):
            patch["campaign"][field] = dict(env, gate=GATES[field], decision=did)
            idec["targets"].append(f"{self.doc}#campaign.{field}")
            idec.setdefault("fields_changed", []).append(f"campaign.{field}")

        def flush():
            idec["rationale"] = " ".join(notes) or "Nothing settled from the material yet."
            p = patch if patch["campaign"] else {}
            if idec["targets"]:
                self.identify_reasoning(idec, ev_material, ev_pins, levels, rejected)
            return p, ([idec] if idec["targets"] else []) + decs

        # 1. the subject brand -----------------------------------------------------
        brand = (camp.get("brand") or {}).get("value")
        if not brand and "brand" not in self.skipped_fields:
            pt = self.pending_text if (self.pending_text or {}).get("field") == "brand" else None
            self.pending_text = None
            if pt:
                pool = layers.find_brands(pt["text"]) + [(b["ref"], b["name"]) for b in self.identify_raw()["brands"]]
                opts, found = id_rules.brand_options_from_text(pt["text"], pool)
                p, ds = flush()
                return self.ask("brand", "Which brand is the client's?" if found else
                                "Which brand is the client's? Type its name.", opts, True,
                                f"From \"{pt['text']}\":" if found else f"\"{pt['text']}\" does not name a brand I can use.",
                                ds, p, facts)
            brands = self.identify_raw()["brands"]
            kind, what = id_rules.decide_subject(brands)
            if kind == "subject":
                brand = {"ref": what["ref"], "name": what["name"]}
                write("brand", {"value": brand, "origin": "extracted", "source": what["span"]})
                idec["cites"].append(what["span"]["material_id"])
                notes.append(f"{what['name']} is the client's brand (read from the material).")
                basis = id_rules.checked_basis(what)
                mid = what["span"]["material_id"]
                if basis == "brand_label":
                    ev_material.append(rsn.point(f"The material labels {what['name']} as the brand", mid))
                    levels.append(("high", f"{what['name']} is labelled the brand in the material"))
                elif basis == "named_as_ours":
                    ev_material.append(rsn.point(f"The material calls {what['name']} the client's own", mid))
                    levels.append(("high", f"the material names {what['name']} as the client's"))
                else:
                    ev_material.append(rsn.point(f"{what['name']} is the only brand the material names, and not as "
                                                 f"a comparator", mid))
                    levels.append(("medium", f"{what['name']} is the only brand named; nothing labels it the "
                                             f"client's"))
                for b in brands:
                    opt = f"{b['name']} as the client's brand"
                    if b["ref"] != what["ref"] and opt not in [r["option"] for r in rejected]:
                        rejected.append(rsn.rej(opt, "the material presents it as a comparator" if b.get("comparator")
                                                else "the material does not name it as the client's"))
            else:
                p, ds = flush()
                if kind == "ask":
                    names = [o["label"] for o in what if "value" in o]
                    intro = (f"The material names {' and '.join(names) if len(names) < 3 else ', '.join(names)}"
                             f"{'' if len(names) > 1 else ', as a comparator'}, and does not say which is the "
                             f"client's. Research waits for your answer.")
                    return self.ask("brand", "Which brand is the client's?", what, True, intro, ds, p, facts)
                return self.ask("brand", "Which brand is this campaign for? Type its name.", [], True,
                                "I could not read the material for the brand, so I am asking." if self._identify_failed
                                else "I could not find the client's brand in the prompt or the material.", ds, p, facts)
        # With no brand (nobody answered which it is), identify goes on with the rest.
        bname = brand.get("name") if brand else None
        subject_ref = brand.get("ref") if brand else None
        if brand:
            layers.note_brand(subject_ref, bname)

        # 2. the roster row: pinned already, or looked up in the brand layer ---------
        pinned = [f for f in self.W_facts if brand and f.get("entity") == subject_ref
                  and str(f.get("key", "")).startswith("roster.") and f.get("status", "active") == "active"]
        if brand and not any(f["key"].startswith("roster.categories") for f in pinned):
            row = layers.roster(subject_ref)
            if row and row["categories"]:
                pin_did = self.did("roster", subject_ref)
                t = iso()
                for r in row["facts"]:
                    facts.append({"id": r["id"], "entity": r["entity"], "key": r["key"], "value": r["value"],
                                  "unit": r["unit"], "as_of": r["as_of"], "retrieved_at": r["retrieved_at"],
                                  "sources": r["sources"], "confidence": fact_confidence(r["source_records"]),
                                  "licence": r["licence"], "status": "active", "version": r["version"],
                                  "supersedes": r["supersedes"],
                                  "origin": origin_uri("brand", r["entity"], r["key"], r["version"]),
                                  "decision": pin_did, "pinned_at": t,
                                  "pin_reason": f"Roster row ({r['key']}) from the brand layer, for campaign."
                                                f"{'categories' if 'categories' in r['key'] else 'client_org'}",
                                  "layer": "brand", "method": r.get("method") or "report"})
                lv = rsn.lowest(f["confidence"] for f in facts)
                tiers = sorted({x.get("tier", "?") for r in row["facts"] for x in r["source_records"]})
                decs.append(decision(self.doc, pin_did, "pin", self.handler, "lookup",
                                     f"Deterministic lookup of {brand['name']}'s roster row in the brand layer: "
                                     f"{len(facts)} row(s) pinned.", [f"facts[{f['id']}]" for f in facts],
                                     [s for f in facts for s in f["sources"]], reasoning=rsn.make(
                                         f"Pinned {brand['name']}'s roster row from the brand layer.",
                                         [rsn.point(f"The brand layer holds {f['key']} = {f['value']}",
                                                    f["id"], f["sources"]) for f in facts],
                                         rsn.certainty(lv, f"lowest derived confidence of the rows ({lv}); sources "
                                                           f"tiered {', '.join(tiers)}"),
                                         "the roster row is revised in the brand layer, or a person corrects it",
                                         rejected=[rsn.rej("ask the person for the categories",
                                                           "the brand layer already holds a roster row for "
                                                           "this brand")])))
                self.hits += [{"id": f["id"], "scope": "brand", "source": f["origin"]} for f in facts]
                pinned = pinned + facts
        cat_pins = sorted([f for f in pinned if f["key"] in ("roster.categories.primary", "roster.categories.secondary")
                           and re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", str(f.get("value")))],
                          key=lambda f: f["key"] != "roster.categories.primary")
        org_pin = next((f for f in pinned if f["key"] == "roster.client_org"
                        and re.fullmatch(r"org/[a-z0-9][a-z0-9-]*", str(f.get("value")))), None)

        # 3. categories ------------------------------------------------------------------
        cats_env = camp.get("categories")
        no_row = f"{bname} has no roster row" if brand else "No brand is settled, so no roster row gives it"
        if not cats_env and "categories" not in self.skipped_fields:
            if cat_pins:
                if not human_owned(self.W, ctx_decisions(self.clan), self.doc, "categories"):
                    vals = list(dict.fromkeys(f["value"] for f in cat_pins))[:2]
                    write("categories", {"value": vals, "origin": "proposed", "fact_ids": [f["id"] for f in cat_pins]})
                    idec["cites"] += [f["id"] for f in cat_pins]
                    notes.append(f"Categories from the roster row: {', '.join(vals)} (proposed, to confirm).")
                    ev_pins.append(rsn.point(f"{brand['name']}'s roster row in the brand layer gives the categories "
                                             f"{', '.join(vals)}", [f["id"] for f in cat_pins]))
                    lv = rsn.lowest(f.get("confidence") for f in cat_pins)
                    levels.append((lv, f"the roster pins are {lv} (derived from source tier and corroboration)"))
                    for leaf, _sp in (self._identify_raw or {}).get("categories") or []:
                        if leaf not in vals:
                            rejected.append(rsn.rej(f"the material's reading, {leaf}",
                                                    "the brand layer's roster row is the record for this brand"))
            else:
                pt = self.pending_text if (self.pending_text or {}).get("field") == "categories" else None
                self.pending_text = None
                p, ds = flush()
                if pt:
                    leaves = layers.find(pt["text"])
                    how = "the category tree"
                    if not leaves:
                        codes = [l for l in labels]
                        try:
                            leaves = [l for l in self.caps.model.structured(
                                "classify_category", CLASSIFY_SYSTEM,
                                {"typed": pt["text"],
                                 "category_tree": [{"code": c, "name": n} for c, n in labels.items()]},
                                classify_schema(codes), max_tokens=500).get("leaves", []) if l in labels][:2]
                        except Exception as e:  # the typed words match no leaf: the question asks again
                            log.warning("start_campaign %s: classify_category did not answer (%s)", self.id,
                                        _why(e)[0])
                            leaves = []
                        how = "the closest leaves"
                    if leaves:
                        return self.ask("categories", "Which category is it?",
                                        id_rules.typed_leaf_options(leaves, labels), True,
                                        f"From \"{pt['text']}\" ({how}):", ds, p, facts)
                    return self.ask("categories", "Which category is it? Name it as the tree does, e.g. "
                                                  "\"EV and charging\".", [], True,
                                    f"\"{pt['text']}\" does not match a category leaf I know.", ds, p, facts)
                cands = self.identify_raw()["categories"]
                if cands:
                    return self.ask("categories", "Which category is it?",
                                    id_rules.category_options(cands, "extracted", labels), True,
                                    f"{no_row}, so its category is not known. The material points at these; pick one "
                                    f"or say what it is.", ds, p, facts)
                return self.ask("categories", "Which category is it? Type it.", [], True,
                                f"{no_row}, and the material does not say its category.", ds, p, facts)
        elif cats_env and brand and cats_env.get("origin") in ("confirmed", "stated") and not cat_pins:
            # A person settled the categories: the brand layer learns the roster row.
            rdec = {"id": self.did("roster-write", subject_ref), "kind": "edit", "handler": self.handler,
                    "action": "roster_confirmed", "rationale": f"{cats_env.get('by') or 'A person'} confirmed "
                                                              f"{brand['name']}'s categories in the chat.",
                    "cites": [self.doc]}
            who = cats_env.get("by") or self.answer_by or "human:unknown"
            src = layers.add_source({"uri": who, "tier": "reviewer-verified", "domain": who,
                                     "licence": "client-confidential", "title": "confirmed in the chat"})
            layers.set_roster(subject_ref, brand.get("name"), list(cats_env.get("value") or [])[:2], rdec, [src])
            notes.append(f"{brand['name']}'s categories are written to the brand layer's roster.")

        # 4. markets ------------------------------------------------------------------------
        if not camp.get("markets") and "markets" not in self.skipped_fields:
            pt = self.pending_text if (self.pending_text or {}).get("field") == "markets" else None
            self.pending_text = None
            p, ds = flush()
            if not pt:  # the request names a region ("global", "Europe"): read it, offer it to confirm
                said = " ".join(m.text for m in self.materials() if getattr(m, "text", None))
                reg = market_rules.region_in(said)
                if reg:
                    pt = {"field": "markets", "text": reg[0], "region": reg}
            if pt:
                codes = market_rules.from_text(pt["text"])
                if codes:
                    reg = pt.get("region") or market_rules.region_in(pt["text"])
                    opts = [{"id": "markets", "label": market_list(codes), "value": codes, "origin": "stated"}]
                    if reg and len(codes) > 3:  # research runs once per market: offer the biggest three too
                        opts.append({"id": "markets_top3", "label": market_list(codes[:3]), "value": codes[:3],
                                     "origin": "stated"})
                    opts.append({"id": "other", "label": "Something else"})
                    intro = (f"I read “{reg[0]}” as these {len(codes)} markets. Research runs once per market, so "
                             f"more markets take longer and cost more:" if reg else f"From \"{pt['text']}\":")
                    return self.ask("markets", "Which markets is it for?", opts, True, intro, ds, p, facts)
                return self.ask("markets", "Which markets is it for? Name the countries.", [], True,
                                f"\"{pt['text']}\" does not name a country I can research.", ds, p, facts)
            return self.ask("markets", "Which markets is it for? Name the countries.", [], True,
                            "Nothing names a market, and research runs once per market.", ds, p, facts)

        # 5. the client ---------------------------------------------------------------------
        if not camp.get("client_org") and not human_owned(self.W, ctx_decisions(self.clan), self.doc, "client_org"):
            ext = self.identify_raw()["client"]
            if org_pin:
                name = ext["value"]["name"] if ext and ext["value"]["ref"] == org_pin["value"] else \
                    org_pin["value"][4:].replace("-", " ").title()
                write("client_org", {"value": {"ref": org_pin["value"], "name": name}, "origin": "proposed",
                                     "fact_ids": [org_pin["id"]]})
                idec["cites"].append(org_pin["id"])
                notes.append(f"Client: {name} (from the roster row, proposed).")
                ev_pins.append(rsn.point(f"The roster row names the client, {name}", org_pin["id"]))
                lv = org_pin.get("confidence") if org_pin.get("confidence") in rsn.LEVELS else "low"
                levels.append((lv, f"the client pin is {lv} (derived)"))
            elif ext:
                write("client_org", {"value": ext["value"], "origin": "extracted", "source": ext["span"]})
                idec["cites"].append(ext["span"]["material_id"])
                notes.append(f"Client: {ext['value']['name']} (read from the material).")
                ev_material.append(rsn.point(f"The material names the client, {ext['value']['name']}",
                                             ext["span"]["material_id"]))
                levels.append(("high", "the client is quoted verbatim from the material"))

        # 6. the subject is never its own comparator (a later stage rewriting an
        #    earlier stage's field: new decision, read = what extract wrote, §3)
        cs = camp.get("competitor_set")
        if brand and cs and cs.get("origin") in ("extracted", "proposed") and \
                any(isinstance(c, dict) and c.get("ref") == subject_ref for c in cs.get("value") or []):
            keep = [c for c in cs["value"] if c.get("ref") != subject_ref]
            if keep:
                env = {k: v for k, v in cs.items() if k != "decision"}
                env["value"] = keep
                if "item_provenance" in env:
                    env["item_provenance"] = {k: v for k, v in env["item_provenance"].items() if k != subject_ref}
                    if not env["item_provenance"]:
                        env.pop("item_provenance")
                write("competitor_set", env)
            else:
                patch["campaign"]["competitor_set"] = None
                idec["targets"].append(f"{self.doc}#campaign.competitor_set")
            notes.append(f"{brand['name']} removed from the comparators: it is the client's brand.")
            ev_pins.append(rsn.point(f"{brand['name']} is the client's brand, so it is not its own comparator",
                                     f"{self.doc}#campaign.brand"))
            rejected.append(rsn.rej(f"keep {brand['name']} among the comparators",
                                    "a brand is never its own comparator"))

        idec["rationale"] = " ".join(notes) or "Subject brand, categories and markets already settled."
        p, ds = flush()
        if not ds:
            settled = [f"{self.doc}#campaign.{f}" for f in ("brand", "categories", "markets") if camp.get(f)]
            open_ = [f for f in ("brand", "categories", "markets") if f in self.skipped_fields]
            ds = [decision(self.doc, did, "edit", self.handler, "identify", idec["rationale"], [],
                           reasoning=rsn.make(
                               "Wrote nothing: the brand, categories and markets were already in the document."
                               if not open_ else
                               f"Wrote nothing more: nobody answered for the {' or the '.join(open_)}, so it stays "
                               f"open.",
                               [rsn.point("The document already holds them", settled) if settled else
                                rsn.point("Nothing more was settled")],
                               rsn.certainty("high", "the document holds each of them" if not open_ else
                                             "nothing more was written"),
                               "a person clears one of them" if not open_ else "the campaign is started again",
                               only_option="there was nothing left to identify"))]
        text = " ".join(notes) or (f"{bname}: brand, categories and markets are settled." if brand and
                                   not self.skipped_fields else "Went on with what is settled.")
        self.add_chunk("identify", p, ds, facts=facts, text=text)
        with self.lock:
            self.question = None
        return True

    def identify_reasoning(self, idec, ev_material, ev_pins, levels, rejected):
        """identify's reasoning: the model's points on the material (cite-checked
        against the material ids), the code's points on the pins it read, and
        the certainty from the rules — how the brand was settled, and the
        derived confidence of any pin a field was proposed from."""
        raw = self._identify_raw or {}
        fields = [t.partition("#campaign.")[2] for t in idec["targets"] if "#campaign." in t]
        level = rsn.lowest(l for l, _ in levels) if levels else "high"
        basis = "; ".join(b for l, b in levels if l == level) or "each field rests on a quote or a pin"
        r, _ = rsn.from_model(
            raw.get("grounds") if ev_material else None,
            decided=f"Settled {', '.join(f.replace('_', ' ') for f in fields) or 'nothing'} for the campaign.",
            known=raw.get("material_ids") or [m.id for m in self.materials()],
            certainty_=rsn.certainty(level, basis), fallback=ev_material, always=ev_pins,
            would_change_if="the client names a different brand, or the brand layer's roster row changes",
            rejected=rejected, only_option="the material and the layers point at one reading and nothing else",
            attention=("Only one brand is named and nothing labels it the client's; confirm it."
                       if level == "medium" and ev_material else None))
        rsn.give(idec, r)

    def stage_select(self):
        camp = self.W.get("campaign") or {}
        markets = list((camp.get("markets") or {}).get("value") or [])
        cats = list((camp.get("categories") or {}).get("value") or [])
        prompt = next((m for m in self.materials() if m.kind == "prompt"), None)
        leaves = {l["code"]: l for l in self.caps.layers.leaves()}
        from .research import LENS_QUESTIONS
        failed = None
        try:
            raw = self.caps.model.structured(
                "select", SELECT_SYSTEM,
                {"prompt": prompt.text if prompt else "", "prompt_material_id": prompt.id if prompt else None,
                 "markets": markets,
                 "categories": [{"code": c, "name": (leaves.get(c) or {}).get("name", c),
                                 "regulated": (leaves.get(c) or {}).get("regulated")} for c in cats],
                 "lenses": [{"lens": l, "title": LENS_TITLES[l], "question": LENS_QUESTIONS[l][0]} for l in LENSES]},
                SELECT_SCHEMA, max_tokens=2000)
        except Exception as e:  # no plan from the model: the default plan, every lens in every market
            kind, detail = _why(e)
            log.warning("start_campaign %s: select's model call did not answer (%s): %s", self.id, kind, detail)
            with self.lock:
                self.gaps.append({"stage": "select", "reason": kind})
            raw, failed = {}, f"{WHAT[kind]} ({detail[:160]})"
        skip = {}
        for item in raw.get("lenses") or []:
            l, reason = item.get("lens"), (item.get("reason") or "").strip()
            if l not in LENSES or not reason:
                continue  # a skip needs a reason; without one the lens runs
            if not item.get("run"):
                skip[(l, None)] = reason
            else:
                for m in item.get("skip_markets") or []:
                    m = market_rules.normalise(m)
                    if m in markets and len(markets) > 1:
                        skip[(l, m)] = reason
        pairs = [(l, m) for l in LENSES for m in markets if (l, None) not in skip and (l, m) not in skip]
        skipped = [{"lens": l, **({"market": m} if m else {}), "reason": r} for (l, m), r in
                   sorted(skip.items(), key=lambda kv: (LENSES.index(kv[0][0]),
                                                        markets.index(kv[0][1]) if kv[0][1] in markets else -1))]
        did = self.did("select")
        prior = [x for x in ((self.W.get("selection") or {}).get("lenses_skipped") or [])
                 if (x.get("lens"), x.get("market")) not in {(s["lens"], s.get("market")) for s in skipped}
                 and not any(x.get("lens") == l and x.get("market") in (None, m) for l, m in pairs)]
        for s in skipped:
            s["decision"] = did
        d = decision(self.doc, did, "edit", self.handler, "select",
                     f"Selected {len(pairs)} lens x market pair(s) for this prompt; skipped {len(skipped)}"
                     + (": " + "; ".join(f"{s['lens']}{'/' + s['market'] if 'market' in s else ''}" for s in skipped)
                        if skipped else "") + ".",
                     ["selection.lenses_skipped"] + [f"selection.lenses_skipped[{s['lens']}"
                                                     f"{'/' + s['market'] if 'market' in s else ''}]" for s in skipped],
                     [prompt.id] if prompt else [], fields_changed=["selection.lenses_skipped"],
                     reasoning=self.select_reasoning(raw.get("grounds"), prompt, pairs, skipped, markets, failed))
        by_lens = {}
        for l, m in pairs:
            by_lens.setdefault(l, []).append(m)
        text = (f"Researching {len(by_lens)} lens(es) across {market_list(markets)}."
                + "".join(f" {LENS_TITLES[s['lens']]} is skipped"
                          f"{' in ' + market_list([s['market']]) if 'market' in s else ''}: {s['reason']}"
                          for s in skipped))
        if failed:
            text += f" The model could not plan the research: {failed}. Every lens runs in every market, the default."
        self.add_chunk("select", {"selection": {"lenses_skipped": prior + skipped}}, [d], text=text)
        with self.lock:
            self.selected = (pairs, skipped)
        return True

    def select_reasoning(self, raw, prompt, pairs, skipped, markets, failed=None):
        """select's reasoning: the model's points, cite-checked against the
        prompt's material id; certainty from the rules — a skip whose reason
        is the prompt's own words is certain, one the model inferred is not."""
        pid = [prompt.id] if prompt else []

        def quotes_prompt(reason):
            """The reason is the prompt's words, or quotes them."""
            spans = [reason] + re.findall(r'["\u201c]([^"\u201d]{8,})["\u201d]', reason)
            return bool(prompt) and any(find_quote(prompt.text, x) for x in spans)
        inferred = [s for s in skipped if not quotes_prompt(s["reason"])]
        if failed:
            level, basis = "medium", "the model gave no plan, so the default plan runs"
        elif not skipped:
            level, basis = "high", "every lens runs in every market, the default"
        elif inferred:
            level, basis = "medium", "a skip rests on the model's reading rather than the prompt's words"
        else:
            level, basis = "high", "every skip quotes the prompt's own instruction"
        n = len({l for l, _ in pairs})
        where = lambda s: f" in {market_list([s['market']])}" if "market" in s else ""
        fallback = [rsn.point("Every lens runs by default unless the prompt says otherwise", pid)]
        fallback += [rsn.point(f"{LENS_TITLES[s['lens']]}{where(s)} is skipped: {s['reason']}", pid)
                     for s in skipped]
        rejected = [rsn.rej(f"research {LENS_TITLES[s['lens']]}{where(s)}", s["reason"]) for s in skipped]
        if not skipped:
            rejected.append(rsn.rej("skip lenses the prompt does not mention",
                                    "research covers every lens unless the prompt limits it"))
        attention = (f"Skipped for a reason the prompt does not state: "
                     f"{'; '.join(LENS_TITLES[s['lens']] + where(s) for s in inferred)}." if inferred else None)
        if failed:
            attention = f"The model could not plan the research: {failed}. Every lens runs in every market."
        r, _ = rsn.from_model(
            raw, decided=f"Research {n} lens(es) across {market_list(markets)}; skip {len(skipped)}.",
            known=pid, certainty_=rsn.certainty(level, basis), fallback=fallback,
            would_change_if="the person asks for a skipped lens, or limits the research further",
            rejected=rejected, attention=attention)
        return r

    def stage_research(self):
        pairs = self.selected[0] if self.selected else []
        camp = self.W.get("campaign") or {}
        if not pairs and not (camp.get("markets") or {}).get("value"):
            with self.lock:
                self.gaps.append({"stage": "research", "reason": "no_market"})
            d = decision(self.doc, self.did("research"), "edit", self.handler, "research",
                         "No market is settled: nothing to research.", [], reasoning=rsn.make(
                             "Researched nothing: no market is settled.",
                             [rsn.point("Research runs once per market, and the campaign names none")],
                             rsn.certainty("high", "the campaign holds no market"),
                             "the campaign is started again with its markets",
                             rejected=[rsn.rej("pick a market", "the markets are the client's to name, never guessed")],
                             attention="No research ran: no market is settled. Start the campaign again naming the "
                                       "markets."))
            self.add_chunk("research", {}, [d], text="Nothing to research: no market is settled.")
            return True
        if not pairs:
            d = decision(self.doc, self.did("research"), "edit", self.handler, "research",
                         "Nothing selected: no lens x market to research.", [], reasoning=rsn.make(
                             "Researched nothing: every lens was skipped.",
                             [rsn.point("The selection leaves no lens and market to research",
                                        f"{self.doc}#selection.lenses_skipped")],
                             rsn.certainty("high", "the selection is empty"),
                             "the person asks for a lens", only_option="there was nothing selected to research",
                             attention="No research ran; the report will have nothing researched to show."))
            self.add_chunk("research", {}, [d], text="Nothing to research: every lens was skipped.")
            return True
        lenses = [l for l in LENSES if any(p[0] == l for p in pairs)]
        markets = list(dict.fromkeys(m for _, m in pairs))
        cats = list((camp.get("categories") or {}).get("value") or [])[:2]
        if not cats:  # research runs per category (gate research): every pair is a gap, named
            with self.lock:
                self.gaps.append({"stage": "research", "reason": "gated"})
            self._research_gap(self.did("research", "gated"), "gated", "no category is settled", pairs)
            return True
        r = Researcher(self.doc, self.W_version, self.wclan(), self.handler, self.caps, lenses, markets, cats,
                       pairs=set(pairs), reuse_days=self.settings.reuse_days,
                       concurrency=self.settings.research_concurrency, seed=self.id, materials=self.materials())
        result, change, hits = r.run()
        self.hits += hits
        decs = change["decisions"]
        merge = next(d for d in decs if d["action"] == "research_merge")
        for k in change["data_patch"].get("selection", {}):
            a = f"{self.doc}#selection.{k}"
            if not any(t == a or t.startswith(a + "[") for d in decs for t in d["targets"]):
                merge["targets"].append(a)
        srcs = result.get("sources") or {}
        tiers = {}
        for s in srcs.values():
            tiers[s["tier"]] = tiers.get(s["tier"], 0) + 1
        text = (f"Research: {result['summary']}"
                + (f" Sources by tier: {', '.join(f'{v} {k}' for k, v in sorted(tiers.items()))}." if tiers else ""))
        self.add_chunk("research", change["data_patch"], decs, facts=change["facts_append"], text=text,
                       msg_decision=merge, sources=change.get("sources_append") or [])
        return True

    def stage_synthesise(self):
        try:
            result, change, hits = synth_stage.run_synthesis(self.doc, self.W_version, self.wclan(), {},
                                                             self.handler, self.caps, seed=self.id,
                                                             with_audience=True)
        except TaskError:
            d = decision(self.doc, self.did("synthesise"), "edit", self.handler, "synthesise",
                         "No pins to synthesise from; no finding is invented.", [], reasoning=rsn.make(
                             "Proposed no finding: research pinned no facts.",
                             [rsn.point("A finding must cite pins, and the document holds none")],
                             rsn.certainty("high", "the document holds no pin"),
                             "research pins facts", only_option="without a pin there is nothing to cite"))
            self.add_chunk("synthesise", {}, [d], text="No points to work out: we found no facts for them to rest on.")
            return True
        self.hits += hits
        n = len(change["findings_append"])
        text = (f"{n} finding(s), each derived by the agent and waiting for a person to verify or reject."
                if n else "No new findings: what the pins say is already written up.")
        decs = change["decisions"] or [decision(
            self.doc, self.did("synthesise"), "edit", self.handler, "synthesise", result["summary"], [],
            reasoning=rsn.make("Proposed no new finding.",
                               [rsn.point("Every set of pins worth a statement is already written up",
                                          [f["id"] for f in ctx_findings(self.wclan())])],
                               rsn.certainty("high", "the findings already cover the pins"),
                               "research pins new facts", only_option="nothing new was left to say"))]
        self.add_chunk("synthesise", change["data_patch"], decs, findings=change["findings_append"], text=text)
        return True

    def working_clan(self):
        """The document as it stands once every chunk of ours it lacks has
        landed: what the report composes from when no poll brought them."""
        with self.lock:
            clan = self.latest_clan
            known = known_ids(clan)
            data = copy.deepcopy(ctx_data(clan))
            facts, findings = copy.deepcopy(ctx_facts(clan)), copy.deepcopy(ctx_findings(clan))
            for c in self.chunks:
                if c.refused or c.ids <= known:
                    continue
                data = apply_patch(data, c.patch)
                facts += [f for f in c.facts if f["id"] not in {x["id"] for x in facts}]
                findings += [f for f in c.findings if f["id"] not in {x["id"] for x in findings}]
        if isinstance(data.get("projection"), dict):
            data["projection"].pop("built_from", None)  # the host's hashes describe the older members
        return {**clan, "data": data, "facts": facts, "findings": findings}

    def stage_report(self):
        working = self._report_from == "working"
        # the request's document, now holding every earlier stage — or, when no
        # poll brought them in time, the job's own copy of it
        clan = self.working_clan() if working else self.latest_clan
        try:
            report, cites, hits, why = report_stage.compose(self.doc, clan, self.handler, self.caps)
        except TaskError as e:  # nothing to cite: no report, and the reason, never a stalled job
            return self.no_report(e.message)
        if working:
            note = ("Composed from the job's own copy of the document: no poll brought the earlier stages in time. "
                    "Refresh report composes it from the document.")
            why["attention"] = f"{why['attention']} {note}" if why.get("attention") else note
        d = decision(self.doc, self.did("report"), "edit", self.handler, "report",
                     "The report stage: structured blocks over the pins and findings the document held once the "
                     "earlier stages had landed. Every claim was checked against the document: it cites a pin or a "
                     "finding and states no figure they do not hold.", ["report"], cites, fields_changed=["report"],
                     reasoning=why)
        self.hits += hits
        self.add_chunk("report", {"report": report}, [d], base=clan.get("version"), read_from=ctx_data(clan),
                       text="Report ready. The short list under it is what to confirm before the brief.")
        with self.lock:
            self.reported = True
        return True

    def no_report(self, reason):
        """A report must cite a pin or a finding (§8.5); with none there is no
        report, and the job ends saying why and what it went on without."""
        with self.lock:
            self.gaps.append({"stage": "report", "reason": "nothing_to_cite"})
            stages = list(dict.fromkeys(g["stage"] for g in self.gaps if g["stage"] != "report"))
        without = f" It went on without: {', '.join(stages)}." if stages else ""
        d = decision(self.doc, self.did("report", "none"), "edit", self.handler, "report", "", [], reasoning=rsn.make(
            "Wrote no report: the document holds nothing to cite.",
            [rsn.point("Every report claim cites a pin or a finding, and the document holds neither"),
             rsn.point("What the campaign went without is named in the chat"
                       + (f": {', '.join(stages)}" if stages else ""))],
            rsn.certainty("high", "the document holds no pin and no finding"),
            "research pins facts, then Refresh report composes one",
            rejected=[rsn.rej("write a report without sources", "a claim nothing cites is never shown")],
            attention=f"No report: {reason[:200]}.{without} Refresh report composes one once research lands."))
        skipped = [DOING.get(st, st) for st in stages]
        said = f" We couldn't finish {', '.join(skipped)}." if skipped else ""
        self.add_chunk("report", {}, [d], text=f"There's no report: nothing was looked up that it could cite.{said} "
                                               f"Refresh report writes one once there is.", gap=True)
        return True


def _sub(patch, path):
    node = patch
    for p in path.split("."):
        node = node[p]
    for p in reversed(path.split(".")):
        node = {p: node}
    return node
