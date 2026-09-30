"""Drafting through the brief engine (middleware-api.md §10.14).

Sai's engine is the brief generator (clan-extract.md §11.1, OD11): its agent
server (`engine/agent-server/server.py`) runs `parse_brief.run()` and
`mapping.map_brief()` and answers one POST with the brief as bare JSON. When
`NAPKIN_BRIEF_ENGINE_URL` is set, `draft_brief` sends it the brief's material
text, the planner's prompt and `clan.upstream_payload`, and turns its reply
into the middleware's own change. We adapt to the engine as it is and never
change it here.

  request   POST <url>  {request_kind: "agent",
                         payload: {task: "draft_brief", input, attachments: [{name, extracted_text}],
                                   loops37?: true,
                                   upstream?: {brand?, category?, competitors?, facts?, decisions?}},
                         clan: {data: {project_name?, client?}}}
            `input` is the client's brief: the prompt, then each material the client sent
            under a plain heading of ours. Only a picture's vision transcription goes as an
            attachment, which the engine reads as supporting context, never as fact.
  reply     2xx, the app's fields (omitted when empty) plus `rationale`, `context`,
            `fact_refs` {field: [{item, id, version, scope, source_ids}]}, `fact_conflicts`
            [{id, version, line, p}], and `meta` {research_facts, research_decisions} when sent

The change keeps every §10 rule: omitted, not blank; a locked field is not
written; a field a person holds is proposed; every write is named by a
decision. Each written field gets one `draft` decision by the drafter (Dara),
citing the materials and, for every `fact_ref`, the fact id (and the finding
it became, when that is how the document holds it), so the evidence drawer
resolves them. The open questions and every fact conflict are attention
items.

**Never a halt.** A slow, failing or unreadable engine is never the job's
failure: the job runs the middleware's own stages instead (extract -> draft ->
judge), and the capture decision's `attention` says why. A reply that is
readable but thin lands as it is, and what it left out is named in the run
decision's `abstained` and `attention`.
"""

from __future__ import annotations

import logging
import re

import httpx

from .. import reasoning as rsn
from ..doc import ctx_facts, ctx_findings
from ..util import iso
from . import capture as cap_stage
from .fields import KEYS, LABELS, clean, get, put
from .job import BriefJob

log = logging.getLogger("napkin.brief.engine")

# The engine drafts a whole brief in one answer (loops 3-7 and the strategy fill run
# inside it); its own server takes one draft at a time. The wait is the research port's.
TIMEOUT = 900.0
# An httpx transport for the engine port (a test double sets it); None in service.
TRANSPORT = None

UPSTREAM_KEYS = ("brand", "category", "competitors", "facts", "decisions")
LEAVES = {"desired_response": ("think", "feel", "do"),
          "objectives": ("commercial", "behavioural", "attitudinal")}
# The golden-brief ids `build_rationale` names under "DRAFTS TO REVIEW", as the app's keys.
GOLDEN_TO_KEYS = {"insight": ["insight"], "smp": ["single_minded_proposition"],
                  "reasons to believe": ["reasons_to_believe"],
                  "desired response": ["desired_response.think", "desired_response.feel", "desired_response.do"],
                  "background": ["background"], "audience": ["audience"],
                  "competitor context": ["competitor_context"], "budget scope": ["budget_and_scope"],
                  "mandatories": ["mandatories"], "tone world assets": ["tone_and_world"],
                  "objectives": ["objectives.commercial", "objectives.behavioural", "objectives.attitudinal"]}
ATTENTION_MAX = 600


class EngineError(Exception):
    """The engine gave no brief: unreachable, too slow, a non-2xx, or a body we cannot
    read. The message never carries request or reply content."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class EnginePort:
    """The engine's agent server, one draft per call."""

    def __init__(self, url: str, timeout: float = TIMEOUT, transport=None, loops37: bool = True):
        self.url, self.timeout, self.transport = url.rstrip("/") + "/", timeout, transport
        # The engine's own per-request switch for Loops 3-7 (insight, SMP, RTBs); off, the
        # request leaves it to the engine host's BRIEF_LOOPS37.
        self.loops37 = loops37

    def draft(self, body: dict, attribution: dict | None = None) -> dict:
        attribution = attribution or {}
        headers = {"X-Napkin-Handler": str(attribution.get("handler") or "-"),
                   "X-Napkin-Job": str(attribution.get("job") or "-")}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
                r = c.post(self.url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise EngineError(f"the brief engine did not answer within {self.timeout:.0f}s") from e
        except httpx.HTTPError as e:
            raise EngineError(f"the brief engine is unreachable ({type(e).__name__})") from e
        if not 200 <= r.status_code < 300:
            raise EngineError(f"the brief engine returned {r.status_code}")
        try:
            out = r.json()
        except ValueError as e:
            raise EngineError("the brief engine's reply is not JSON") from e
        if not isinstance(out, dict):
            raise EngineError("the brief engine's reply is not an object")
        if "error" in out:  # its contract: a 200 never carries one; treat it as a failure
            raise EngineError("the brief engine's reply carries an error")
        return out


def port_for(settings) -> EnginePort | None:
    """The engine port when NAPKIN_BRIEF_ENGINE_URL is set, else None (the middleware's
    own drafters, exactly as before)."""
    url = getattr(settings, "brief_engine_url", None)
    if not url:
        return None
    port = EnginePort(url, TIMEOUT, transport=TRANSPORT)
    port.loops37 = bool(getattr(settings, "brief_engine_loops37", True))
    return port


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def upstream_of(clan: dict) -> dict | None:
    """`clan.upstream_payload`, the extract's `upstream` printer output (clan-extract.md
    §1.1, §11.2), narrowed to the keys `parse_brief.run(upstream=...)` reads. The engine
    checks the types itself and drops a wrong one; None when there is nothing."""
    up = (clan or {}).get("upstream_payload")
    if not isinstance(up, dict):
        return None
    out = {k: up[k] for k in UPSTREAM_KEYS if up.get(k) not in (None, "", [], {})}
    return out or None


def model_false(doc: str, chain: list) -> set:
    """The app keys a `model: false` mark holds on (§10.13 item 6): for each key, the
    newest `classify` decision, not superseded, whose target is `<doc>#<key>` or a path
    above it, decides; a flag the mark does not set reads as false. Fields are the only
    thing a person can mark."""
    marks = []
    for d in chain or []:
        if not isinstance(d, dict) or d.get("kind") != "classify" or d.get("superseded_by"):
            continue
        lic = d.get("licence") if isinstance(d.get("licence"), dict) else {}
        for t in d.get("targets") or []:
            if isinstance(t, str) and t.startswith(f"{doc}#"):
                marks.append((t.split("#", 1)[1], bool(lic.get("model", False))))
    out = set()
    for k in KEYS:
        for path, model in marks:  # newest first: the first that covers the key decides
            if k == path or k.startswith(path + ".") or k.startswith(path + "["):
                if not model:
                    out.add(k)
                break
    return out


def _heading(m) -> str:
    return f"--- From the client: {m.name} ---"


def request_body(mats, data: dict, upstream: dict | None, withheld=frozenset(), loops37: bool = True) -> dict:
    """The engine's request (server.py `do_draft`, README "Contract"). Every material is
    the client's, so all of them are the brief the engine captures: `input` is the
    planner's prompt, then each readable material under a plain heading of ours (never
    the engine's ATTACHMENT header, whose text it takes as supporting context only). A
    picture's vision transcription is ours, not the client's words, and goes as an
    attachment. The names the document already has ride in `clan.data`, which
    `map_brief` keeps, less any a `model: false` mark holds. No other part of the
    document is sent."""
    prompt = next((m for m in mats if m.kind == "prompt" and m.readable), None)
    parts = [prompt.text.strip()] if prompt else []
    atts = []
    for m in mats:
        if not m.readable or m is prompt:
            continue
        if m.transcribed:
            atts.append({"name": m.name, "extracted_text": m.text})
        else:
            parts.append(f"{_heading(m)}\n{m.text.strip()}")
    payload = {"task": "draft_brief", "input": "\n\n".join(p for p in parts if p), "attachments": atts}
    if loops37:
        payload["loops37"] = True
    if upstream:
        payload["upstream"] = upstream
    names = {k: data[k].strip() for k in ("project_name", "client")
             if k not in withheld and isinstance(data.get(k), str) and data[k].strip()}
    return {"request_kind": "agent", "payload": payload, "clan": {"data": names}}


# ---------------------------------------------------------------------------
# The reply
# ---------------------------------------------------------------------------

def reply_values(reply: dict) -> tuple[dict, list[str]]:
    """({app key: clean value}, [keys the reply sent in a shape the field cannot hold])."""
    out, misshapen = {}, []
    for k in KEYS:
        top, _, leaf = k.partition(".")
        if k in reply:  # the regeneration shape: a literal dotted key
            v = reply[k]
        elif leaf:
            node = reply.get(top)
            v = node.get(leaf) if isinstance(node, dict) else None
        else:
            v = reply.get(k)
        if v in (None, "", [], {}):
            continue
        value = clean(k, v)
        if value is None:
            misshapen.append(k)
        else:
            out[k] = value
    return out, misshapen


def refs_by_key(fact_refs) -> dict:
    """`fact_refs` {app field: [{item, id, version, scope, source_ids}]} per app key: a
    desired-response ref goes to its think/feel/do leaf, a list ref stays on its field."""
    out = {}
    if not isinstance(fact_refs, dict):
        return out
    for field, refs in fact_refs.items():
        for r in refs if isinstance(refs, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"].strip():
                continue
            if field in LEAVES:
                key = f"{field}.{r.get('item')}" if r.get("item") in LEAVES[field] else None
            else:
                key = field if field in KEYS else None
            if key:
                out.setdefault(key, []).append(r)
    return out


def review_drafts(rationale: str) -> set:
    """The keys `build_rationale` names as drafts kept though they failed the engine's
    checks ("DRAFTS TO REVIEW (failed their checks): insight, smp")."""
    m = re.search(r"DRAFTS TO REVIEW \(failed their checks\): ([^;.]+)", rationale or "")
    out = set()
    for name in (m.group(1).split(",") if m else []):
        out.update(GOLDEN_TO_KEYS.get(name.strip().lower(), []))
    return out


def _clip(s: str, n: int = ATTENTION_MAX) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


class Resolver:
    """What a fact id the engine cites is in this document: a pin in `clan.facts`, or
    the finding whose `verification.fact_id` it is (clan-extract.md §11.4)."""

    def __init__(self, clan: dict):
        self.pins = {f["id"]: f for f in ctx_facts(clan) if isinstance(f.get("id"), str)}
        self.by_fact = {}
        for fi in ctx_findings(clan):
            v = fi.get("verification") if isinstance(fi.get("verification"), dict) else {}
            if isinstance(v.get("fact_id"), str) and isinstance(fi.get("id"), str) and fi.get("status") != "rejected":
                self.by_fact.setdefault(v["fact_id"], fi["id"])

    def cites(self, fid: str) -> list:
        """The ids a decision cites for fact `fid`: the pin, or the finding (with the fact
        id when a pin holds it too); [] when the document holds neither."""
        out = []
        if fid in self.pins:
            out.append(fid)
        if fid in self.by_fact:
            out.append(self.by_fact[fid])
        return out

    def address(self, fid: str) -> str | None:
        if fid in self.pins:
            return f"facts[{fid}]"
        if fid in self.by_fact:
            return f"findings[{self.by_fact[fid]}]"
        return None


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

class EngineBriefJob(BriefJob):
    """`draft_brief` through the engine; the middleware's own stages when it gives
    nothing. The stages stay extract -> draft -> judge (the view reads them): the
    engine is called from the extract stage, which reports `draft` while it writes;
    on its answer, draft and judge have nothing left to do."""

    def __init__(self, jid, task, handler, req, caps, *, mats, port: EnginePort):
        super().__init__(jid, task, handler, req, caps, mats=mats)
        self.port = port
        self.upstream = upstream_of(req.clan)
        self.withheld = model_false(self.doc, self.chain)  # never sent, never written
        self.engine_state = "waiting"  # waiting | running | used | skipped
        self.engine_note = None        # why the engine was not used
        self._noted = False

    # -- stages --------------------------------------------------------------------
    def stage_extract(self):
        reason = None
        live = [k for k in KEYS if k not in self.locked]
        tnotes = self._transcribe()
        if not any(m.readable for m in self.mats):
            reason = "nothing readable to send it"
        else:
            with self.lock:
                self.engine_state, self.stage_idx = "running", 1  # Dara writes: the view shows the draft stage
            self.set_state(live, "drafting", "drafter")
            kept = (list(self.proposals), list(self.hits), dict(self.writer))  # _land commits its chunk last
            try:
                body = request_body(self.mats, self.data0, self.upstream, self.withheld,
                                    getattr(self.port, "loops37", True))
                reply = self.port.draft(body, dict(self.caps.attribution))
                if self._land(reply, tnotes):
                    with self.lock:
                        self.engine_state = "used"
                    return
                reason = "its reply held no brief field"
            except EngineError as e:
                reason = e.message
            except Exception as e:  # a reply we could not map is the engine's gap, never the job's
                log.error("%s %s: the engine's reply could not be mapped: %s", self.task, self.id, type(e).__name__)
                reason = f"its reply could not be read ({type(e).__name__})"
            with self.lock:
                self.proposals, self.hits, self.writer = kept
            self.set_state(live, "waiting", None)
        log.info("%s %s: the brief engine was not used (%s); the middleware drafts", self.task, self.id, reason)
        with self.lock:
            self.engine_state, self.engine_note, self.stage_idx = "skipped", reason, 0
        super().stage_extract()

    def stage_draft(self):
        if self.engine_state != "used":
            super().stage_draft()

    def stage_judge(self):
        if self.engine_state != "used":
            super().stage_judge()

    def _transcribe(self) -> list:
        """Pictures become text first (§1.6), so the engine, which reads text only, reads
        them too. A picture that cannot be read is recorded unread."""
        if not any(m.image and not m.readable for m in self.mats):
            return []
        return cap_stage.transcribe(self.caps.capture_view(), self.mats)

    # -- the fallback says why -------------------------------------------------------
    def add_chunk(self, stage, patch, decisions):
        if self.engine_state == "skipped" and not self._noted:
            cap = next((d for d in decisions if d.get("action") == "capture"), None)
            if cap is not None:
                r = cap["reasoning"]
                note = f"We wrote this brief ourselves: the brief engine could not be used this time ({self.engine_note})."
                r["attention"] = (f"{r['attention']} {note}" if r.get("attention") else note)
                self._noted = True
        return super().add_chunk(stage, patch, decisions)

    def _fail(self, stage, etype, message):
        if self.engine_state == "skipped" and self.engine_note:
            message = f"{message}; the brief engine was not used either ({self.engine_note})"
        super()._fail(stage, etype, message)

    def result(self):
        out = super().result()
        with self.lock:
            state, note, jstate = self.engine_state, self.engine_note, self.state
        out["engine"] = {"state": state, **({"reason": note} if note else {})}
        if state == "running":
            out["summary"] = "The brief engine is writing the brief."
        elif state == "skipped" and jstate == "done":
            out["summary"] += f" The brief engine was not used: {note}."
        return out

    # -- the engine's answer as a change ----------------------------------------------
    def _land(self, reply: dict, tnotes: list) -> bool:
        """Map the reply into one change. False when it holds no brief field (the job
        then drafts itself)."""
        values, misshapen = reply_values(reply)
        if not values:
            return False
        rationale = reply.get("rationale") if isinstance(reply.get("rationale"), str) else ""
        meta = reply.get("meta") if isinstance(reply.get("meta"), dict) else {}
        rfacts = meta.get("research_facts") if isinstance(meta.get("research_facts"), dict) else \
            reply.get("research_facts") if isinstance(reply.get("research_facts"), dict) else None
        rdecs = meta.get("research_decisions") if isinstance(meta.get("research_decisions"), dict) else \
            reply.get("research_decisions") if isinstance(reply.get("research_decisions"), dict) else None
        conflicts = [c for c in (reply.get("fact_conflicts") or []) if isinstance(c, dict)
                     and isinstance(c.get("id"), str)] if isinstance(reply.get("fact_conflicts"), list) else []
        refs = refs_by_key(reply.get("fact_refs"))
        to_review = review_drafts(rationale)
        res = Resolver(self.clan)
        readable = [m for m in self.mats if m.readable]
        mat_ids = [m.id for m in readable]
        unread = [m.id for m in self.mats if not m.readable]
        t = iso()
        patch, decisions, written, abstained, unresolved = {}, [], [], [], []
        new = [m for m in self.mats if not m.known]
        if new:
            patch["materials"] = {m.id: m.entry(t) for m in new}
        engine_by = "the brief engine (parse_brief.run, map_brief)"

        for k in KEYS:
            if k in self.locked or k == "open_questions":
                continue
            if k in self.withheld:  # a person marked it not for models: the engine's value is not taken
                self.set_state([k], "kept", "drafter")
                continue
            value = values.get(k)
            if value is None:
                abstained.append(k)
                self.set_state([k], "absent", "drafter")
                continue
            krefs = refs.get(k, [])
            cites, pts = [], [rsn.point(f"Written by {engine_by} from the client's material", mat_ids)]
            for r in krefs:
                got = res.cites(r["id"])
                if not got:
                    unresolved.append(r["id"])
                    continue
                cites += got
                ver = f" v{r['version']}" if r.get("version") not in (None, "") else ""
                where = f" ({r['scope']} research)" if r.get("scope") else ""
                item = f", item {r['item']}" if isinstance(r.get("item"), int) else ""
                pts.append(rsn.point(f"Rests on the verified research fact {r['id']}{ver}{where}{item}", got))
            cites = list(dict.fromkeys(cites))
            kept_draft = k in to_review
            if kept_draft:
                lvl, why = "low", "the engine kept this draft though it failed its own checks"
            elif cites:
                lvl, why = "medium", "written by the engine and grounded in the research facts it cites"
            else:
                lvl, why = "medium", "written by the engine, which checks its own drafts; its precedent is not " \
                                     "recorded in the document"
            r_ = rsn.make(f"Drafted the {LABELS[k]}.", pts, rsn.certainty(lvl, why),
                          "the client's material or the research changes, or a person rewrites it",
                          only_option="the brief engine wrote one value for the field",
                          attention=("The brief engine kept this draft though it failed its checks: review it."
                                     if kept_draft else None))
            extra = {"drafted_by": "brief-engine"}
            if krefs:
                extra["fact_refs"] = krefs
            for c in cites:
                pin = res.pins.get(c)
                if pin is not None:
                    self.hits.append({"id": c, "scope": pin.get("layer") or "brand", "source": pin.get("origin") or c})
            if value == get(self.W, k):
                # already what the field holds (map_brief echoes the names we sent): nothing
                # to write, and nothing for the person who holds it to clear
                self.set_state([k], "done", "drafter")
                continue
            if self.held.get(k):
                d = self.dec(("propose", k, "engine"), "edit", "propose", [k], mat_ids + cites, r_, "drafter",
                             proposed_value=value, **extra)
                decisions.append(d)
                self.proposals.append({"field": k, "value": value, "decision": d["id"]})
                self.writer[k] = d["id"]
                self.set_state([k], "proposed", "drafter")
                continue
            d = self.dec(("engine", k), "edit", "draft", [k], mat_ids + cites, r_, "drafter", **extra)
            decisions.append(d)
            put(patch, k, value)
            self.writer[k] = d["id"]
            written.append(k)
            self.set_state([k], "done", "drafter")

        decisions += self._questions_from(values.get("open_questions"), conflicts, mat_ids, patch)
        decisions += [self._conflict(c, res, mat_ids) for c in conflicts]
        decisions.insert(0, self._run_decision(rationale, reply.get("context"), rfacts, rdecs, written, abstained,
                                               misshapen, unresolved, tnotes, readable, unread, new))
        self.add_chunk("draft", patch, decisions)
        return True

    def _questions_from(self, qs, conflicts, mat_ids, patch) -> list:
        """The engine's open questions, written whole, as an attention item."""
        k = "open_questions"
        if k in self.locked:
            return []
        if k in self.withheld:
            self.set_state([k], "kept", "drafter")
            return []
        if not qs:
            self.set_state([k], "absent", "drafter")
            return []
        if qs == get(self.W, k):
            self.set_state([k], "done", "drafter")
            return []
        shown = "; ".join(_clip(q, 160) for q in qs[:3]) + (f"; and {len(qs) - 3} more" if len(qs) > 3 else "")
        pts = [rsn.point(f"The brief engine left {len(qs)} question(s) open for a person", mat_ids)]
        if conflicts:
            pts.append(rsn.point(f"{len(conflicts)} of them ask which is current where the client brief and verified "
                                 f"research disagree", mat_ids))
        r = rsn.make(f"Listed {len(qs)} open question(s) for the brief.", pts,
                     rsn.certainty("high", "the engine's questions, recorded as it sent them"),
                     "the client answers, or the material changes",
                     only_option="the questions are what the engine found missing or in dispute",
                     attention=_clip(f"Open questions to answer: {shown}"))
        if self.held.get(k):
            d = self.dec(("propose", k, "engine"), "edit", "propose", [k], mat_ids, r, "drafter", proposed_value=qs,
                         drafted_by="brief-engine")
            self.proposals.append({"field": k, "value": qs, "decision": d["id"]})
            self.set_state([k], "proposed", "drafter")
            return [d]
        put(patch, k, qs)
        self.set_state([k], "done", "drafter")
        d = self.dec(("engine", k), "edit", "questions", [k], mat_ids, r, "drafter", drafted_by="brief-engine")
        self.writer[k] = d["id"]
        return [d]

    def _conflict(self, c, res: Resolver, mat_ids) -> dict:
        """One brief-vs-research conflict (the engine's C1d): an attention item on the
        fact, for a person to settle. The engine picks no winner, and neither do we."""
        fid = c["id"]
        got = res.cites(fid)
        addr = res.address(fid)
        line = _clip(c.get("line") or fid, 300)
        p = c.get("p")
        pts = [rsn.point(f"The engine's check found the client brief contradicts the research: {line}",
                         mat_ids + got)]
        r = rsn.make("Recorded a conflict between the client brief and verified research.", pts,
                     rsn.certainty("medium", "a model check in the engine found it"
                                   + (f" (p {p})" if isinstance(p, (int, float)) else "")),
                     "a person settles which is current",
                     only_option="the engine picks no winner; a person settles it",
                     attention=_clip(f"The client brief and verified research disagree ({line}). Settle which "
                                     f"is current: the brief states neither as fact until then."))
        return self.dec(("conflict", fid), "edit", "flag_conflict", [addr] if addr else ["open_questions"],
                        mat_ids + got, r, "drafter", fact_conflict={k: c[k] for k in ("id", "version", "p")
                                                                     if k in c},
                        drafted_by="brief-engine")

    def _run_decision(self, rationale, context, rfacts, rdecs, written, abstained, misshapen, unresolved, tnotes,
                      readable, unread, new) -> dict:
        """What the engine was given and what it gave: the materials it read, the
        research it used and skipped, the fields it left out, and anything to check."""
        mat_ids = [m.id for m in readable]
        pts = [rsn.point(f"Sent {len(readable)} readable material(s) to the brief engine", mat_ids)]
        if rationale:
            pts.append(rsn.point(f"The engine says: {_clip(rationale, 400)}", mat_ids))
        if self.upstream:
            n_f, n_d = len(self.upstream.get("facts") or []), len(self.upstream.get("decisions") or [])
            pts.append(rsn.point(f"Sent the research the brief carries: {n_f} fact row(s), {n_d} decision row(s)",
                                 mat_ids))
        attn = list(tnotes)
        if unread:
            attn.append(f"{len(unread)} attachment(s) had nothing readable and were not sent.")
        if rationale.startswith("Degraded run"):
            attn.append("The engine ran degraded: see its note.")
        if "NOT a Claude brief" in rationale:
            attn.append("The engine answered with another model: see its note.")
        if rationale.startswith("Heuristic extraction") or rationale.startswith("Capture fell back"):
            attn.append("The engine captured without its model: check the captured facts.")
        skipped_f = (rfacts or {}).get("skipped") or []
        skipped_d = (rdecs or {}).get("skipped") or []
        if skipped_f:
            attn.append(f"The engine left out {len(skipped_f)} research fact(s) it could not use.")
        if skipped_d:
            attn.append(f"The engine left out {len(skipped_d)} research decision(s) it could not use.")
        if misshapen:
            attn.append(f"Not written, in a shape the field cannot hold: {', '.join(LABELS[k] for k in misshapen)}.")
        if unresolved:
            attn.append(f"The engine cited {len(set(unresolved))} research fact(s) this document does not hold; "
                        "those cites were left out.")
        r = rsn.make(f"The brief engine drafted the brief: {len(written)} field(s) written.", pts,
                     rsn.certainty("medium", "the engine's own capture and checks; the middleware checked the "
                                   "shapes and the cites"),
                     "the material or the research changes, and the brief is drafted again",
                     only_option="the brief engine is the brief generator (OD11)",
                     attention=_clip(" ".join(attn)) if attn else None)
        extra = {"material_read": mat_ids, "unread": unread, "abstained": abstained, "drafted_by": "brief-engine"}
        if rfacts is not None:
            extra["research_facts"] = rfacts
        if rdecs is not None:
            extra["research_decisions"] = rdecs
        if isinstance(context, str) and context.strip():
            extra["engine_context"] = context.strip()
        return self.dec(("engine_run",), "edit", "draft_run", [f"materials[{m.id}]" for m in (new or readable)],
                        mat_ids, r, "drafter", **extra)
