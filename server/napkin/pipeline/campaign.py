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
that document (§8.4). A stage that raises fails the job with a message naming
the stage; what already landed stays.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import traceback

from ..doc import (CAMPAIGN_FIELDS, GATES, LENS_TITLES, LENSES, STAGES, address, apply_patch, build_materials,
                   ctx_data, ctx_decisions, ctx_facts, ctx_findings, decision, deep_merge, field_paths, get_dotted,
                   human_owned, known_ids, market_list, read_of)
from ..layers import origin_uri
from ..rules import identify as id_rules
from ..rules import markets as market_rules
from ..rules.confidence import fact_confidence
from ..rules.quotes import find_quote
from ..util import TaskError, iso, slug, uid, ulid_like
from . import extract as extract_stage
from . import report as report_stage
from . import synthesise as synth_stage
from .research import Researcher

log = logging.getLogger("napkin.campaign")

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
Never guess what the material does not say."""

SELECT_SYSTEM = """You plan the research for an advertising campaign ask. There are eight research lenses.
Decide, for each lens, whether this ask needs it. Research everything by default: skip a lens (or skip it
in some markets) ONLY when the prompt says to leave it out, limits it to certain markets, or asks only
for other lenses, or when the lens plainly has nothing to read for these categories. Give the reason for
every skip, quoting the prompt where it is the prompt's instruction."""

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
    })


SELECT_SCHEMA = _obj({"lenses": {"type": "array", "items": _obj({
    "lens": {"type": "string", "enum": LENSES}, "run": {"type": "boolean"},
    "skip_markets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}})}})


def classify_schema(leaf_codes):
    return _obj({"leaves": {"type": "array", "items": {"type": "string", "enum": leaf_codes}}})


class Chunk:
    def __init__(self, stage, base, patch, read, facts=(), findings=(), decisions=(), message=None):
        self.stage, self.base = stage, base
        self.patch, self.read = patch, read
        self.facts, self.findings, self.decisions = list(facts), list(findings), list(decisions)
        self.message = message
        self.ids = {d["id"] for d in self.decisions}


def combine(doc, chunks) -> dict | None:
    if not chunks:
        return None
    patch, read, facts, findings, decs = {}, {}, [], [], []
    for c in chunks:
        patch = deep_merge(patch, c.patch)
        for k, v in c.read.items():
            read.setdefault(k, v)  # what the EARLIEST writer of the path read
        facts += [f for f in c.facts if f["id"] not in {x["id"] for x in facts}]
        findings += [f for f in c.findings if f["id"] not in {x["id"] for x in findings}]
        decs += [d for d in c.decisions if d["id"] not in {x["id"] for x in decs}]
    return {"doc": doc, "base_version": chunks[0].base, "read": read if patch else {}, "data_patch": patch,
            "facts_append": facts, "findings_append": findings, "decisions": decs}


class CampaignJob:
    def __init__(self, jid, doc, handler, clan, inp, caps, settings):
        self.id, self.doc, self.handler = jid, doc, handler
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
            if c.ids <= known:
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
                  msg_decision=None, base=None, read_from=None):
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
                                 f"The {stage} stage's chat message.", [])
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
        c = Chunk(stage, base if base is not None else self.W_version, patch,
                  read_of(read_from if read_from is not None else self.W, patch), facts, findings, decisions, message)
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

    def _run(self):
        while True:
            with self.lock:
                if self.state in ("done", "failed"):
                    return
                if self.state == "needs_input":
                    self.cond.wait()
                    continue
                self.state = "running"
                stage = STAGES[self.stage_idx]
                if stage == "report":
                    earlier = set().union(*(c.ids for c in self.chunks)) if self.chunks else set()
                    if not earlier <= known_ids(self.latest_clan):
                        self.cond.wait(timeout=30)
                        continue  # composes once the earlier stages have landed (§8.4)
            try:
                finished = getattr(self, "stage_" + stage)()
            except TaskError as e:
                self.fail(stage, e.etype, e.message)
                return
            except Exception as e:  # never a silent fallback
                log.error("start_campaign %s stage %s failed: %s\n%s", self.id, stage, e, traceback.format_exc())
                self.fail(stage, "internal", f"the {stage} stage failed ({type(e).__name__}: {str(e)[:200]})")
                return
            with self.lock:
                if not finished:
                    self.state = "needs_input"
                    continue
                self.stage_idx += 1
                if self.stage_idx >= len(STAGES):
                    self.state = "done"
                    self.finished_at = iso()
                    self.cond.notify_all()
                    return

    def fail(self, stage, etype, message):
        d = decision(self.doc, self.did(stage, "failed"), "edit", self.handler, "stage_failed",
                     f"The {stage} stage failed: {message}. What landed before it stays.", [])
        self.add_chunk(stage, {}, [d], text=f"The {stage} stage failed: {message}. What already landed stays.")
        with self.lock:
            self.state = "failed"
            self.error = {"type": etype, "message": message}
            self.finished_at = iso()
            self.cond.notify_all()

    # -- replies ----------------------------------------------------------------
    def reply_change(self, clan):
        """The change for this reply: every chunk not landed in `clan`."""
        with self.lock:
            self.latest_clan = clan
            self.cond.notify_all()
            known = known_ids(clan)
            pending = [c for c in self.chunks if not c.ids <= known]
            change = combine(self.doc, pending)
            if change is None and self.state == "done":
                change = self.last_change
            if change is not None:
                self.last_change = change
            return change

    def view(self):
        with self.lock:
            return {"id": self.id, "state": self.state,
                    "progress": {"done": min(self.stage_idx, len(STAGES)), "total": len(STAGES)},
                    "stage": STAGES[min(self.stage_idx, len(STAGES) - 1)],
                    "question": self.question if self.state == "needs_input" else None,
                    "started_at": self.started_at, "finished_at": self.finished_at, "error": self.error}

    def summary(self):
        v = self.view()
        if v["state"] == "needs_input":
            return f"Waiting for you: {self.question['text']}"
        if v["state"] == "done":
            return "Campaign ready: the report is in the document."
        if v["state"] == "failed":
            return f"Failed at {v['stage']}: {self.error['message']}"
        return f"{v['stage']}: {self.stage_idx} of {len(STAGES)} stage(s) done"

    def answer(self, clan, inp):
        """answer_question: validate, record, continue. Raises TaskError."""
        from ..util import bad
        with self.lock:
            if self.state != "needs_input":
                raise TaskError(409, "job_state", f"job {self.id} is {self.state}, not waiting for an answer")
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
            raw = self.caps.model.structured(
                "identify", IDENTIFY_SYSTEM,
                {"materials": extract_stage.material_payload(self.materials()),
                 "category_tree": [{"code": l["code"], "name": l["name"], "vertical": l["vertical_name"]}
                                   for l in leaves]},
                identify_schema([l["code"] for l in leaves]), max_tokens=4000)
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
            self._identify_raw = {"brands": brands, "client": client, "categories": cats[:2]}
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
        d = decision(self.doc, self.did("ask", qid), "edit", self.handler, "identify",
                     f"Asked the person ({field}): {text} Research waits; nothing is guessed.", [],
                     [o["source"]["material_id"] for o in options if o.get("source")]
                     + [f for o in options for f in o.get("fact_ids", [])])
        decs = list(decisions or []) + [d]
        self.add_chunk("identify", patch or {}, decs, facts=facts, text=f"{intro} {text}".strip(), question=q,
                       msg_decision=d)
        with self.lock:
            self.question = q
        return False

    def stage_identify(self):
        camp = self.W.get("campaign") or {}
        layers = self.caps.layers
        labels = {l["code"]: l["name"] for l in layers.leaves()}
        patch, decs, facts, notes = {"campaign": {}}, [], [], []
        did = self.did("identify", len(self.chunks))
        idec = decision(self.doc, did, "edit", self.handler, "identify", "", [], [])

        def write(field, env):
            patch["campaign"][field] = dict(env, gate=GATES[field], decision=did)
            idec["targets"].append(f"{self.doc}#campaign.{field}")
            idec.setdefault("fields_changed", []).append(f"campaign.{field}")

        def flush():
            idec["rationale"] = " ".join(notes) or "Nothing settled from the material yet."
            p = patch if patch["campaign"] else {}
            return p, ([idec] if idec["targets"] else []) + decs

        # 1. the subject brand -----------------------------------------------------
        brand = (camp.get("brand") or {}).get("value")
        if not brand:
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
            else:
                p, ds = flush()
                if kind == "ask":
                    names = [o["label"] for o in what if "value" in o]
                    intro = (f"The material names {' and '.join(names) if len(names) < 3 else ', '.join(names)}"
                             f"{'' if len(names) > 1 else ', as a comparator'}, and does not say which is the "
                             f"client's. Research waits for your answer.")
                    return self.ask("brand", "Which brand is the client's?", what, True, intro, ds, p, facts)
                return self.ask("brand", "Which brand is this campaign for? Type its name.", [], True,
                                "I could not find the client's brand in the prompt or the material.", ds, p, facts)
        subject_ref = brand.get("ref")
        layers.note_brand(subject_ref, brand.get("name"))

        # 2. the roster row: pinned already, or looked up in the brand layer ---------
        pinned = [f for f in self.W_facts if f.get("entity") == subject_ref
                  and str(f.get("key", "")).startswith("roster.") and f.get("status", "active") == "active"]
        if not any(f["key"].startswith("roster.categories") for f in pinned):
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
                decs.append(decision(self.doc, pin_did, "pin", self.handler, "lookup",
                                     f"Deterministic lookup of {brand['name']}'s roster row in the brand layer: "
                                     f"{len(facts)} row(s) pinned.", [f"facts[{f['id']}]" for f in facts],
                                     [s for f in facts for s in f["sources"]]))
                self.hits += [{"id": f["id"], "scope": "brand", "source": f["origin"]} for f in facts]
                pinned = pinned + facts
        cat_pins = sorted([f for f in pinned if f["key"] in ("roster.categories.primary", "roster.categories.secondary")
                           and re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", str(f.get("value")))],
                          key=lambda f: f["key"] != "roster.categories.primary")
        org_pin = next((f for f in pinned if f["key"] == "roster.client_org"
                        and re.fullmatch(r"org/[a-z0-9][a-z0-9-]*", str(f.get("value")))), None)

        # 3. categories ------------------------------------------------------------------
        cats_env = camp.get("categories")
        if not cats_env:
            if cat_pins:
                if not human_owned(self.W, ctx_decisions(self.clan), self.doc, "categories"):
                    vals = list(dict.fromkeys(f["value"] for f in cat_pins))[:2]
                    write("categories", {"value": vals, "origin": "proposed", "fact_ids": [f["id"] for f in cat_pins]})
                    idec["cites"] += [f["id"] for f in cat_pins]
                    notes.append(f"Categories from the roster row: {', '.join(vals)} (proposed, to confirm).")
            else:
                pt = self.pending_text if (self.pending_text or {}).get("field") == "categories" else None
                self.pending_text = None
                p, ds = flush()
                if pt:
                    leaves = layers.find(pt["text"])
                    how = "the category tree"
                    if not leaves:
                        codes = [l for l in labels]
                        leaves = [l for l in self.caps.model.structured(
                            "classify_category", CLASSIFY_SYSTEM,
                            {"typed": pt["text"], "category_tree": [{"code": c, "name": n} for c, n in labels.items()]},
                            classify_schema(codes), max_tokens=500).get("leaves", []) if l in labels][:2]
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
                                    f"{brand['name']} has no roster row, so its category is not known. The material "
                                    f"points at these; pick one or say what it is.", ds, p, facts)
                return self.ask("categories", "Which category is it? Type it.", [], True,
                                f"{brand['name']} has no roster row and the material does not say its category.",
                                ds, p, facts)
        elif cats_env.get("origin") in ("confirmed", "stated") and not cat_pins:
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
        if not camp.get("markets"):
            pt = self.pending_text if (self.pending_text or {}).get("field") == "markets" else None
            self.pending_text = None
            p, ds = flush()
            if pt:
                codes = market_rules.from_text(pt["text"])
                if codes:
                    opts = [{"id": "markets", "label": market_list(codes), "value": codes, "origin": "stated"},
                            {"id": "other", "label": "Something else"}]
                    return self.ask("markets", "Which markets is it for?", opts, True, f"From \"{pt['text']}\":",
                                    ds, p, facts)
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
            elif ext:
                write("client_org", {"value": ext["value"], "origin": "extracted", "source": ext["span"]})
                idec["cites"].append(ext["span"]["material_id"])
                notes.append(f"Client: {ext['value']['name']} (read from the material).")

        # 6. the subject is never its own comparator (a later stage rewriting an
        #    earlier stage's field: new decision, read = what extract wrote, §3)
        cs = camp.get("competitor_set")
        if cs and cs.get("origin") in ("extracted", "proposed") and \
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

        idec["rationale"] = " ".join(notes) or "Subject brand, categories and markets already settled."
        p, ds = flush()
        if not ds:
            ds = [decision(self.doc, did, "edit", self.handler, "identify", idec["rationale"], [])]
        text = " ".join(notes) or f"{brand['name']}: brand, categories and markets are settled."
        self.add_chunk("identify", p, ds, facts=facts, text=text)
        with self.lock:
            self.question = None
        return True

    def stage_select(self):
        camp = self.W.get("campaign") or {}
        markets = list((camp.get("markets") or {}).get("value") or [])
        cats = list((camp.get("categories") or {}).get("value") or [])
        prompt = next((m for m in self.materials() if m.kind == "prompt"), None)
        leaves = {l["code"]: l for l in self.caps.layers.leaves()}
        from .research import LENS_QUESTIONS
        raw = self.caps.model.structured(
            "select", SELECT_SYSTEM,
            {"prompt": prompt.text if prompt else "", "markets": markets,
             "categories": [{"code": c, "name": (leaves.get(c) or {}).get("name", c),
                             "regulated": (leaves.get(c) or {}).get("regulated")} for c in cats],
             "lenses": [{"lens": l, "title": LENS_TITLES[l], "question": LENS_QUESTIONS[l][0]} for l in LENSES]},
            SELECT_SCHEMA, max_tokens=2000)
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
        self.selected = (pairs, skipped)
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
                     [prompt.id] if prompt else [], fields_changed=["selection.lenses_skipped"])
        by_lens = {}
        for l, m in pairs:
            by_lens.setdefault(l, []).append(m)
        text = (f"Researching {len(by_lens)} lens(es) across {market_list(markets)}."
                + "".join(f" {LENS_TITLES[s['lens']]} is skipped"
                          f"{' in ' + market_list([s['market']]) if 'market' in s else ''}: {s['reason']}"
                          for s in skipped))
        self.add_chunk("select", {"selection": {"lenses_skipped": prior + skipped}}, [d], text=text)
        return True

    def stage_research(self):
        pairs = self.selected[0] if self.selected else []
        if not pairs:
            d = decision(self.doc, self.did("research"), "edit", self.handler, "research",
                         "Nothing selected: no lens x market to research.", [])
            self.add_chunk("research", {}, [d], text="Nothing to research: every lens was skipped.")
            return True
        camp = self.W.get("campaign") or {}
        lenses = [l for l in LENSES if any(p[0] == l for p in pairs)]
        markets = list(dict.fromkeys(m for _, m in pairs))
        cats = list((camp.get("categories") or {}).get("value") or [])[:2]
        r = Researcher(self.doc, self.W_version, self.wclan(), self.handler, self.caps, lenses, markets, cats,
                       pairs=set(pairs), reuse_days=self.settings.reuse_days,
                       concurrency=self.settings.research_concurrency, seed=self.id)
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
                       msg_decision=merge)
        return True

    def stage_synthesise(self):
        try:
            result, change, hits = synth_stage.run_synthesis(self.doc, self.W_version, self.wclan(), {},
                                                             self.handler, self.caps, seed=self.id,
                                                             with_audience=True)
        except TaskError:
            d = decision(self.doc, self.did("synthesise"), "edit", self.handler, "synthesise",
                         "No pins to synthesise from; no finding is invented.", [])
            self.add_chunk("synthesise", {}, [d], text="Nothing to synthesise: research pinned no facts.")
            return True
        self.hits += hits
        n = len(change["findings_append"])
        text = (f"{n} finding(s), each derived by the agent and waiting for a person to verify or reject."
                if n else "No new findings: what the pins say is already written up.")
        decs = change["decisions"] or [decision(self.doc, self.did("synthesise"), "edit", self.handler, "synthesise",
                                                result["summary"], [])]
        self.add_chunk("synthesise", change["data_patch"], decs, findings=change["findings_append"], text=text)
        return True

    def stage_report(self):
        clan = self.latest_clan  # the request's document, now holding every earlier stage
        report, cites, hits = report_stage.compose(self.doc, clan, self.handler, self.caps)
        d = decision(self.doc, self.did("report"), "edit", self.handler, "report",
                     "The report stage: structured blocks over the pins and findings the document held once the "
                     "earlier stages had landed. Every claim was checked against the document: it cites a pin or a "
                     "finding and states no figure they do not hold.", ["report"], cites, fields_changed=["report"])
        self.hits += hits
        self.add_chunk("report", {"report": report}, [d], base=clan.get("version"), read_from=ctx_data(clan),
                       text="Report ready. The short list under it is what to confirm before the brief.")
        return True


def _sub(patch, path):
    node = patch
    for p in path.split("."):
        node = node[p]
    for p in reversed(path.split(".")):
        node = {p: node}
    return node
