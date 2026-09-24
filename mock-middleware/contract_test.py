#!/usr/bin/env python3
"""
Contract suite for `napkin.middleware/1` — the swap guarantee.

Runs against ANY implementation of docs/contracts/middleware-api.md: the
stand-in in this directory today, the FastAPI middleware in server/ later. It
must pass unchanged against both, so it names no implementation, no port and
no backend, and asserts only what the contract says.

    python3 mock-middleware/contract_test.py --base-url http://localhost:8790 \\
        --schema-dir app/templates/campaign-research

    --token T          sent as `Authorization: Bearer T` (and 401 is checked without it)
    --job-timeout S    how long a long job may take to reach done (900)

Every `change` is validated against the campaign schemas (schema.json,
facts.schema.json, findings.schema.json in --schema-dir). Uses `jsonschema` if
it is importable; otherwise a built-in draft-07 validator covering every keyword
those schemas use. Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "napkin.middleware/1"
LENSES = ["market_structure", "brands_positioning", "consumer_culture", "category_codes",
          "rhythm_moments", "media_spend", "regulation_clearance", "effectiveness_evidence"]
DECISION_KINDS = {"edit", "contest", "resolve", "verdict", "classify", "pin", "finding",
                  "verify", "approve", "lease", "backref"}
CONF = ["low", "medium", "high"]
# Decisions that must say why (middleware-api.md §3): these kinds always, and
# an edit whenever it writes one of these fields.
REASONED_KINDS = {"pin", "contest", "finding"}
AGENT_FIELDS = {"campaign", "selection", "report"}


def reasoning_problems(r) -> list:
    """The shape of a decision's reasoning (OS-layer contract §3)."""
    if not isinstance(r, dict):
        return ["reasoning is not an object"]
    out, blank = [], (lambda v: not isinstance(v, str) or not v.strip())
    if blank(r.get("decided")):
        out.append("reasoning.decided is empty")
    because = r.get("because")
    if not isinstance(because, list) or not because:
        out.append("reasoning.because has no point")
        because = []
    for i, p in enumerate(because):
        if not isinstance(p, dict) or blank(p.get("point")):
            out.append(f"reasoning.because[{i}] is empty")
            continue
        cites = p.get("cites", [])
        if not isinstance(cites, list) or any(blank(c) for c in cites):
            out.append(f"reasoning.because[{i}] has an empty cite")
        elif not cites and re.search(r"\d", p["point"]):
            out.append(f"reasoning.because[{i}] states a figure and cites nothing")
    rejected = r.get("rejected", [])
    if not isinstance(rejected, list):
        out.append("reasoning.rejected is not a list")
        rejected = []
    for i, x in enumerate(rejected):
        if not isinstance(x, dict) or blank(x.get("option")) or blank(x.get("why")):
            out.append(f"reasoning.rejected[{i}] needs an option and a why")
    if not rejected and blank(r.get("only_option")):
        out.append("reasoning.rejected is empty and only_option does not say why there was one option")
    c = r.get("certainty")
    if not isinstance(c, dict) or c.get("level") not in CONF or blank(c.get("why")):
        out.append("reasoning.certainty needs a level (high, medium, low) and a why")
    if blank(r.get("would_change_if")):
        out.append("reasoning.would_change_if is empty")
    if "attention" in r and r["attention"] is not None and blank(r["attention"]):
        out.append("reasoning.attention is present but empty")
    return out


# ---------------------------------------------------------------------------
# JSON Schema (draft-07 subset: every keyword the campaign schemas use)
# ---------------------------------------------------------------------------

def _type_ok(v, t):
    return {
        "object": isinstance(v, dict), "array": isinstance(v, list), "string": isinstance(v, str),
        "boolean": isinstance(v, bool), "null": v is None,
        "integer": isinstance(v, int) and not isinstance(v, bool),
        "number": isinstance(v, (int, float)) and not isinstance(v, bool),
    }[t]


def _canon(v):
    return json.dumps(v, sort_keys=True)


def mini_validate(inst, schema, root, path="$"):
    errs = []
    if schema is True or schema == {}:
        return errs
    if schema is False:
        return [f"{path}: schema false"]
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/"), ref
        node = root
        for part in ref[2:].split("/"):
            node = node[part]
        errs += mini_validate(inst, node, root, path)
    if "type" in schema:
        ts = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(inst, t) for t in ts):
            return errs + [f"{path}: expected {ts}, got {type(inst).__name__}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in {schema['enum']}")
    if "const" in schema and inst != schema["const"]:
        errs.append(f"{path}: {inst!r} != const {schema['const']!r}")
    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errs.append(f"{path}: {inst!r} does not match {schema['pattern']}")
        if "minLength" in schema and len(inst) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
    if _type_ok(inst, "number") and "minimum" in schema and inst < schema["minimum"]:
        errs.append(f"{path}: below minimum {schema['minimum']}")
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if schema.get("uniqueItems") and len({_canon(x) for x in inst}) != len(inst):
            errs.append(f"{path}: items not unique")
        if isinstance(schema.get("items"), dict):
            for i, x in enumerate(inst):
                errs += mini_validate(x, schema["items"], root, f"{path}[{i}]")
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing required '{k}'")
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                errs += mini_validate(v, props[k], root, f"{path}.{k}")
            elif "additionalProperties" in schema:
                ap = schema["additionalProperties"]
                if ap is False:
                    errs.append(f"{path}: unexpected key '{k}'")
                elif isinstance(ap, dict):
                    errs += mini_validate(v, ap, root, f"{path}.{k}")
        if "propertyNames" in schema:
            for k in inst:
                errs += mini_validate(k, schema["propertyNames"], root, f"{path}<key {k}>")
    for sub in schema.get("allOf", []):
        errs += mini_validate(inst, sub, root, path)
    if "anyOf" in schema and not any(not mini_validate(inst, s, root, path) for s in schema["anyOf"]):
        errs.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema and sum(not mini_validate(inst, s, root, path) for s in schema["oneOf"]) != 1:
        errs.append(f"{path}: does not match exactly one of oneOf")
    if "not" in schema and not mini_validate(inst, schema["not"], root, path):
        errs.append(f"{path}: matches a 'not' schema")
    if "if" in schema:
        if not mini_validate(inst, schema["if"], root, path):
            if "then" in schema:
                errs += mini_validate(inst, schema["then"], root, path)
        elif "else" in schema:
            errs += mini_validate(inst, schema["else"], root, path)
    return errs


try:
    import jsonschema  # optional

    def validate(inst, schema):
        v = jsonschema.Draft7Validator(schema)
        return [f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in v.iter_errors(inst)]
    VALIDATOR = "jsonschema"
except ImportError:
    def validate(inst, schema):
        return mini_validate(inst, schema, schema)
    VALIDATOR = "built-in draft-07 subset"


def merge_patch(target, patch):
    """RFC 7396."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def patch_leaves(patch, at=""):
    """Every dotted path a merge patch sets: non-objects (null included) and empty objects."""
    out = []
    for k, v in patch.items():
        p = f"{at}.{k}" if at else k
        if isinstance(v, dict) and v:
            out += patch_leaves(v, p)
        else:
            out.append(p)
    return out


def get_path(data, dotted):
    """The value at a dotted path, None when absent."""
    for k in dotted.split("."):
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Fail(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Client:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method, path, body=None, raw=None, auth=True):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token and auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode(errors="replace")
        try:
            return status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return status, text

    def task(self, task, inp, clan, **kw):
        body = {"request_kind": "middleware", "payload": {"task": task, "input": inp}, "clan": clan}
        return self.request("POST", "/v1/tasks", body, **kw)


class Suite:
    def __init__(self, client, schema_dir, job_timeout):
        self.c = client
        self.job_timeout = job_timeout
        sd = Path(schema_dir)
        self.data_schema = json.loads((sd / "schema.json").read_text())
        self.facts_schema = json.loads((sd / "facts.schema.json").read_text())
        self.findings_schema = json.loads((sd / "findings.schema.json").read_text())
        self.pipeline = parse_pipeline((sd / "app" / "pipeline.yaml").read_text())
        self.results = []

    # -- envelope checks ----------------------------------------------------
    def envelope(self, status, body, task, states=None):
        check(status == 200, f"expected 200, got {status}: {body}")
        check(isinstance(body, dict), "body is not an object")
        check("error" not in body, "a 200 body carries an 'error' key")
        check(body.get("api") == API, f"api != {API}")
        check(isinstance(body.get("task"), str), "task missing")
        h = body.get("handler")
        check(isinstance(h, str) and re.fullmatch(r"[a-z_]+@\d+(\.\d+)?", h),
              f"handler {h!r} is not name@version in the form findings.derived_by and lenses_run.handler accept")
        if task != "job_status":
            check(body["task"] == task, f"task {body['task']!r} != {task!r}")
            declared = self.pipeline.get(task)
            if declared:
                name, major = declared.split("@")
                check(h.split("@")[0] == name and h.split("@")[1].split(".")[0] == major,
                      f"handler {h} does not implement the declared {declared}")
        job = body.get("job")
        check(isinstance(job, dict), "job missing")
        for k in ("id", "state", "progress", "started_at", "finished_at", "error"):
            check(k in job, f"job.{k} missing")
        check(isinstance(job["id"], str) and job["id"], "job.id empty")
        check(job["state"] in ("queued", "running", "needs_input", "done", "failed"), f"job.state {job['state']}")
        check(job["state"] != "needs_input" or body["task"] == "start_campaign",
              "only a start_campaign job waits for input")
        if states:
            check(job["state"] in states, f"job.state {job['state']} not in {states}")
        p = job["progress"]
        check(isinstance(p, dict) and all(isinstance(p.get(k), int) and not isinstance(p.get(k), bool)
                                          for k in ("done", "total")), "job.progress needs integer done/total")
        check(0 <= p["done"] <= p["total"], f"progress {p} out of range")
        if job["state"] == "done":
            check(job["finished_at"] is not None and job["error"] is None, "done job needs finished_at, no error")
        elif body["task"] != "start_campaign":  # start_campaign sends staged changes (§8.4)
            check(body.get("change") is None, f"a {job['state']} job must not carry a change")
        check(isinstance(body.get("result"), dict) and isinstance(body["result"].get("summary"), str),
              "result.summary missing")
        check("change" in body, "change key missing")
        tr = body.get("trace")
        check(isinstance(tr, dict), "trace missing")
        sc = tr.get("scope")
        check(isinstance(sc, dict) and isinstance(sc.get("org"), str) and sc["org"]
              and isinstance(sc.get("brand"), str) and sc["brand"], "trace.scope needs org and brand")
        check(isinstance(tr.get("backend"), str) and tr["backend"], "trace.backend missing")
        check("model" in tr and (tr["model"] is None or isinstance(tr["model"], str)), "trace.model missing")
        check(isinstance(tr.get("hits"), list), "trace.hits must be a list")
        for hit in tr["hits"]:
            check(isinstance(hit, dict) and {"id", "scope", "source"} <= set(hit), f"bad hit {hit}")
        u = tr.get("usage")
        check(isinstance(u, dict) and all(isinstance(u.get(k), int) and u[k] >= 0
                                          for k in ("input_tokens", "output_tokens")), "trace.usage needs ints")
        return body

    def error(self, status, body, want_status, want_types):
        check(status == want_status, f"expected {want_status}, got {status}: {body}")
        check(isinstance(body, dict) and isinstance(body.get("error"), dict), f"no error object: {body}")
        check(body["error"].get("type") in want_types, f"error.type {body['error'].get('type')} not in {want_types}")
        check(isinstance(body["error"].get("message", ""), str), "error.message must be a string")
        check("change" not in body and "job" not in body, "an error body must not carry a job or change")

    # -- change checks ------------------------------------------------------
    def change(self, body, clan, expect_base=None, staged=False):
        """staged: a start_campaign change, judged against the host's current
        document; base_version and the read-set are checked by the caller
        against every version the job could have read."""
        ch = body["change"]
        check(isinstance(ch, dict), "done job without a change")
        for k in ("doc", "base_version", "data_patch", "facts_append", "findings_append", "decisions"):
            check(k in ch, f"change.{k} missing")
        check(ch["doc"] == clan["id"], "change.doc is not the document it was computed for")
        if not staged:
            check(ch["base_version"] == (expect_base if expect_base is not None else clan["version"]),
                  f"change.base_version {ch['base_version']!r} is not the version read")
        dp = ch["data_patch"]
        check(isinstance(dp, dict), "data_patch must be an object")
        check("projection" not in dp, "data_patch touches projection (host-owned)")
        # The read-set: what the job read of every field it patches, so the host
        # can judge a stale base field by field (§3).
        rs = ch.get("read")
        leaves = patch_leaves(dp)
        if leaves:
            check(isinstance(rs, dict), "change.read missing: a data_patch carries the read-set it was computed from")
        if rs is not None:
            check(isinstance(rs, dict), "change.read must be an object")
            for leaf in leaves:
                check(any(leaf == k or leaf.startswith(k + ".") for k in rs),
                      f"change.read does not cover {leaf}")
            for k, v in rs.items():
                check(staged or get_path(clan.get("data") or {}, k) == v,
                      f"change.read[{k!r}] is not what the document held when the job read it")
        for lst in ("facts_append", "findings_append", "decisions"):
            check(isinstance(ch[lst], list), f"{lst} must be a list")

        data = merge_patch(clan.get("data") or {}, dp)
        errs = validate(data, self.data_schema)
        check(not errs, "data after data_patch fails schema.json: " + "; ".join(errs[:8]))

        existing_facts = facts_of(clan)
        new_facts = ch["facts_append"]
        if staged:  # a repeated stage re-sends its pins: the same id must be the same pin
            have = {f["id"]: f for f in existing_facts}
            for f in new_facts:
                check(f.get("id") not in have or _canon(have[f["id"]]) == _canon(f),
                      f"pin {f.get('id')} re-sent with different content (a pin is frozen)")
            new_facts = [f for f in new_facts if f.get("id") not in have]
        facts = existing_facts + new_facts
        errs = validate({"facts": ch["facts_append"]}, self.facts_schema) + \
            validate({"facts": facts}, self.facts_schema)
        check(not errs, "facts fail facts.schema.json: " + "; ".join(errs[:8]))
        ids = [f["id"] for f in facts]
        check(len(ids) == len(set(ids)), "duplicate fact ids after append")
        ident = {}
        for f in facts:
            key = (f["entity"], f["key"], f.get("market"))
            check(key not in ident or ident[key] == f["value"],
                  f"{key} pinned twice with different values: a silent pick instead of a contest")
            ident[key] = f["value"]
        dec_ids = [d.get("id") for d in ch["decisions"]]
        chain_ids = {d.get("id") for d in chain_of(clan)}
        for f in ch["facts_append"]:
            if "layer" in f:
                check(f["origin"].startswith(f"fact://{f['layer']}/"), f"{f['id']} origin/layer disagree")
            check(f["origin"].endswith(f"/{f['key']}@{f['version']}"), f"{f['id']} origin does not name key@version")
            check(f["as_of"] <= f["retrieved_at"], f"{f['id']} is as_of after it was retrieved")
            check(f["decision"] in dec_ids or f["decision"] in chain_ids, f"{f['id']} cites an unknown decision")

        existing_fi = findings_of(clan)
        new_fi = ch["findings_append"]
        if staged:
            have = {f["id"]: f for f in existing_fi}
            for f in new_fi:
                check(f.get("id") not in have or _canon(have[f["id"]]) == _canon(f),
                      f"finding {f.get('id')} re-sent with different content")
            new_fi = [f for f in new_fi if f.get("id") not in have]
        errs = validate({"findings": ch["findings_append"]}, self.findings_schema) + \
            validate({"findings": existing_fi + new_fi}, self.findings_schema)
        check(not errs, "findings fail findings.schema.json: " + "; ".join(errs[:8]))
        by_id = {f["id"]: f for f in facts}
        for fi in ch["findings_append"]:
            check(fi["status"] == "proposed", f"{fi['id']} is {fi['status']}: only a human verifies")
            check(fi["derived_by"] == body["handler"], f"{fi['id']} derived_by != the handler that ran")
            for c in fi["cites"]:
                check(c in by_id, f"{fi['id']} cites {c}, which is not a pin in the document")
            cited = [by_id[c] for c in fi["cites"]]
            check(fi["confidence"] == finding_confidence(cited),
                  f"{fi['id']} confidence {fi['confidence']} != derived {finding_confidence(cited)}")
            check(fi["decision"] in dec_ids, f"{fi['id']} decision not in change.decisions")

        check(len(dec_ids) == len(set(dec_ids)), "duplicate decision ids")
        for d in ch["decisions"]:
            check(isinstance(d.get("id"), str) and re.fullmatch(r"d_[0-9A-Z]{6,}", d["id"]), f"decision id {d.get('id')}")
            check(d.get("kind") in DECISION_KINDS, f"decision kind {d.get('kind')}")
            for k in ("agent", "action", "rationale"):
                check(isinstance(d.get(k), str) and d[k], f"decision {d['id']} needs {k}")
            check(isinstance(d.get("targets"), list) and d["targets"], f"decision {d['id']} has no targets")
            for t in d["targets"]:
                check(isinstance(t, str) and t.startswith(clan["id"] + "#") and len(t) > len(clan["id"]) + 1,
                      f"target {t!r} is not <doc-id>#<path>")
                check(not re.search(r"\[\d+\]", t), f"target {t!r} is positional")
            check(isinstance(d.get("cites", []), list), "decision.cites must be a list")
            check(d.get("handler") == body["handler"], f"decision {d['id']} handler != response handler")
            check(d.get("backend") == body["trace"]["backend"], f"decision {d['id']} backend != trace.backend")
        self.reasoning(body, clan, ch, data, facts, existing_fi + new_fi, dec_ids, chain_ids)
        for fname, env in (dp.get("campaign") or {}).items():
            if env is None:  # a removal
                continue
            check(env.get("decision") in dec_ids, f"campaign.{fname} names a decision not in the change")

        sel = dp.get("selection") or {}
        pinned = set(ids)
        for ct in sel.get("contested") or []:
            if ct["status"] != "open":
                continue
            vals = {_canon(v["value"]) for v in ct["values"]}
            check(len(vals) >= 2, f"contest {ct['id']} has no disagreement")
            new_pins = {f["id"] for f in ch["facts_append"]}
            for v in ct["values"]:
                check(v["fact_id"] not in new_pins, f"open contest {ct['id']} value was pinned anyway")
            addr = f"{clan['id']}#selection.contested[{ct['id']}]"
            if ct["id"] not in {c.get("id") for c in ((clan.get("data") or {}).get("selection") or {}).get("contested") or []}:
                check(any(d["kind"] == "contest" and addr in d["targets"] for d in ch["decisions"]),
                      f"contest {ct['id']} opened without a contest decision")
        return ch, data, facts

    def reasoning(self, body, clan, ch, data, facts, findings, dec_ids, chain_ids):
        """Every decision of a required kind says why (§3): pin, contest,
        finding, and an edit that writes campaign, selection or report. Where
        reasoning is given it has the shape, and every id it cites resolves in
        the document as it stands after the change — a pin, a finding, a
        decision, a material, a contest or a gap, a value a contest holds, a
        source a pin or a contested value rests on — or is an address on it,
        or a source the reply names in result.sources. For pins and findings the certainty is the
        DERIVED confidence, never self-reported."""
        doc = clan["id"]
        withheld = {h.get("id") for d in chain_of(clan) for h in d.get("withheld") or [] if isinstance(h, dict)}
        sel = data.get("selection") or {}
        known = ({f["id"] for f in facts} | {s for f in facts for s in f.get("sources") or []}
                 | {f["id"] for f in findings} | set(dec_ids) | chain_ids | withheld
                 | set((data.get("materials") or {}).keys())
                 | {c.get("id") for c in sel.get("contested") or []} | {g.get("id") for g in sel.get("gaps") or []}
                 | {v.get("fact_id") for c in sel.get("contested") or [] for v in c.get("values") or []}
                 | {x for c in sel.get("contested") or [] for v in c.get("values") or [] for x in v.get("sources") or []}
                 | set((body.get("result") or {}).get("sources") or {}) | {doc})
        conf = {f["id"]: f.get("confidence") for f in facts}
        fi_conf = {f["id"]: f.get("confidence") for f in findings}
        for d in ch["decisions"]:
            paths = [t.partition("#")[2] for t in d.get("targets") or []]
            required = d["kind"] in REASONED_KINDS or (
                d["kind"] == "edit" and any(re.split(r"[.\[]", p)[0] in AGENT_FIELDS for p in paths))
            r = d.get("reasoning")
            if r is None:
                check(not required, f"decision {d['id']} ({d['kind']}, {d.get('action')}) carries no reasoning")
                continue
            for problem in reasoning_problems(r):
                check(False, f"decision {d['id']} ({d.get('action')}): {problem}")
            for p in r["because"]:
                for c in p.get("cites") or []:
                    check(c in known or c.startswith(doc + "#"),
                          f"decision {d['id']} ({d.get('action')}) reasoning cites {c!r}, which does not resolve")
            level = r["certainty"]["level"]
            if d["kind"] == "finding":
                fis = [fi_conf[p[9:-1]] for p in paths if p.startswith("findings[") and p[9:-1] in fi_conf]
                if fis:
                    check(level == fis[0], f"decision {d['id']} certainty {level} != the finding's derived "
                                           f"confidence {fis[0]}")
            if d["kind"] == "pin":
                pins = [conf[p[6:-1]] for p in paths if p.startswith("facts[") and conf.get(p[6:-1]) in CONF]
                if pins:
                    low = min(pins, key=CONF.index)
                    check(level == low, f"decision {d['id']} certainty {level} != the lowest derived confidence "
                                        f"of its pins ({low})")

    def poll(self, job_id, clan, task):
        deadline = time.monotonic() + self.job_timeout
        last, delay, seen = -1, 0.1, []
        while True:
            st, body = self.c.task("job_status", {"job_id": job_id}, clan)
            self.envelope(st, body, "job_status")
            job = body["job"]
            check(job["id"] == job_id, "job_status answered for another job")
            check(body["handler"].split("@")[0] == self.pipeline.get(task, task + "@").split("@")[0],
                  "job_status handler is not the job's handler")
            check(job["progress"]["done"] >= last, "progress went backwards")
            last = job["progress"]["done"]
            seen.append(job["state"])
            if job["state"] == "failed":
                raise Fail(f"job failed: {job['error']}")
            if job["state"] == "done":
                check(job["progress"]["done"] == job["progress"]["total"], "done before progress completed")
                return body, seen
            check(time.monotonic() < deadline, f"job not done after {self.job_timeout}s")
            time.sleep(delay)
            delay = min(5.0, delay * 1.5)

    # -- cases --------------------------------------------------------------
    def case(self, name, fn):
        try:
            fn()
            self.results.append((name, True, ""))
            print(f"PASS  {name}")
        except Fail as e:
            self.results.append((name, False, str(e)))
            print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            self.results.append((name, False, repr(e)))
            print(f"FAIL  {name}\n      {traceback.format_exc()}")

    def run(self):
        c = self.c
        state = {}

        def healthz():
            st, _ = c.request("GET", "/healthz")
            check(st == 200, f"/healthz returned {st}")
        self.case("GET /healthz is 200", healthz)

        if c.token:
            def unauth():
                st, body = c.task("extract_ask", {"prompt": BRIEF}, doc_clan(), auth=False)
                check(st in (401, 403), f"no credentials returned {st}")
            self.case("auth: a request without credentials is refused", unauth)

        # ---- extract_ask ----
        def extract():
            clan = doc_clan()
            st, body = c.task("extract_ask", extract_input(), clan)
            body = self.envelope(st, body, "extract_ask", states={"done"})
            ch, data, _ = self.change(body, clan)
            camp = ch["data_patch"].get("campaign") or {}
            check(camp, "extract_ask wrote no fields from an unambiguous brief")
            texts = {"prompt": PROMPT, "att": BRIEF}
            mats = data.get("materials") or {}
            for fname, env in camp.items():
                spans = [env["source"]] if "source" in env else []
                spans += [p["source"] for p in (env.get("item_provenance") or {}).values() if "source" in p]
                for sp in spans:
                    check(sp["material_id"] in mats, f"campaign.{fname} cites material {sp['material_id']} not in materials")
                    if "quote" in sp:
                        check(any(sp["quote"] in t for t in texts.values()),
                              f"campaign.{fname} quote is not verbatim in the material: {sp['quote']!r}")
            att = [m for m in mats.values() if m.get("sha256") == "sha256:" + BRIEF_SHA]
            check(att, "the attachment is not in materials by its sha256")
            state["extract"] = body
        self.case("extract_ask: done in one response, change validates, spans are verbatim", extract)

        def extract_values():
            camp = state["extract"]["change"]["data_patch"]["campaign"]
            check("markets" in camp and {"IE", "GB"} <= set(camp["markets"]["value"]),
                  "markets IE and GB are named in the brief")
            bb = camp.get("budget_band")
            check(bb and bb["value"] == "250k_1m", f"budget €400k is band 250k_1m, got {bb and bb['value']}")
            check("quote" not in bb.get("source", {}), "budget_band span carries a quote")
            check("400" not in json.dumps(bb), "budget_band envelope carries the figure")
            check("in_market" not in camp, "in_market written though the brief gives no date (abstain, never guess)")
            for fname, env in camp.items():
                check(env["origin"] in ("extracted", "proposed"), f"campaign.{fname} origin {env['origin']}")
        self.case("extract_ask: extracts what the brief says, abstains on what it does not", extract_values)

        def extract_proposed():
            clan = doc_clan(with_roster=True)
            st, body = c.task("extract_ask", extract_input(), clan)
            body = self.envelope(st, body, "extract_ask", states={"done"})
            ch, _, _ = self.change(body, clan)
            for env in (ch["data_patch"].get("campaign") or {}).values():
                if env["origin"] == "proposed":
                    known = {f["id"] for f in facts_of(clan)}
                    check(set(env["fact_ids"]) <= known, "a proposed field cites a fact that is not pinned")
        self.case("extract_ask: proposed fields cite pinned facts", extract_proposed)

        def extract_human_owned():
            clan = doc_clan()
            clan["data"]["campaign"]["markets"] = {"value": ["FR"], "origin": "confirmed", "confirmed_from": "extracted",
                                                    "gate": "research", "by": "human:u_test",
                                                    "source": {"material_id": "mat_test01", "locator": "¶1"},
                                                    "decision": "d_TESTCONF01"}
            st, body = c.task("extract_ask", extract_input(), clan)
            body = self.envelope(st, body, "extract_ask", states={"done"})
            ch, _, _ = self.change(body, clan)
            check("markets" not in (ch["data_patch"].get("campaign") or {}), "re-extraction overwrote a confirmed field")
        self.case("extract_ask: never overwrites a confirmed field", extract_human_owned)

        # ---- research_lens ----
        def research():
            clan = research_clan()
            st, body = c.task("research_lens", {}, clan)
            body = self.envelope(st, body, "research_lens", states={"queued", "running"})
            check(body["job"]["progress"]["total"] == len(LENSES) * 2,
                  f"total {body['job']['progress']['total']} != 8 lenses x 2 markets")
            done, seen = self.poll(body["job"]["id"], clan, "research_lens")
            ch, data, facts = self.change(done, clan)
            sel = data.get("selection") or {}
            runs = {(r["lens"], r["market"]) for r in sel.get("lenses_run", [])}
            check(runs >= {(l, m) for l in LENSES for m in ("IE", "GB")}, "lenses_run misses a lens x market")
            check(set(sel.get("coverage", {})) >= set(LENSES), "coverage misses a lens")
            cbm = sel.get("coverage_by_market")
            if cbm:
                for l in LENSES:
                    vs = [cbm[m][l] for m in ("IE", "GB")]
                    want = "filled" if all(v == "filled" for v in vs) else "empty" if all(v == "empty" for v in vs) else "thin"
                    check(sel["coverage"][l] == want, f"coverage[{l}] breaks the merge rule")
            for f in ch["facts_append"]:
                check(f.get("market") in (None, "IE", "GB"), f"{f['id']} is for a market not researched")
            state["research"] = (clan, ch, facts)
        self.case("research_lens: queued -> polled to done; change validates; lens x market covered", research)

        def research_subset():
            clan = research_clan()
            st, body = c.task("research_lens", {"lenses": ["media_spend"], "markets": ["IE"]}, clan)
            body = self.envelope(st, body, "research_lens", states={"queued", "running"})
            check(body["job"]["progress"]["total"] == 1, "one lens x one market is one run")
            done, _ = self.poll(body["job"]["id"], clan, "research_lens")
            ch, data, _ = self.change(done, clan)
            runs = [(r["lens"], r["market"]) for r in data["selection"]["lenses_run"]]
            check(("media_spend", "IE") in runs, "the requested run is not recorded")
            check(("media_spend", "GB") not in runs, "a market that was not requested was run")
        self.case("research_lens: runs only the lenses and markets asked for", research_subset)

        def stale_base():
            clan = research_clan()
            st, body = c.task("research_lens", {"lenses": ["category_codes"]}, clan)
            body = self.envelope(st, body, "research_lens", states={"queued", "running"})
            later = dict(clan, version="4")
            deadline = time.monotonic() + self.job_timeout
            while True:
                st, b = c.task("job_status", {"job_id": body["job"]["id"]}, later)
                if st == 409:
                    return self.error(st, b, 409, {"version_conflict"})
                self.envelope(st, b, "job_status")
                if b["job"]["state"] == "done":
                    self.change(b, clan, expect_base="3")
                    return
                check(time.monotonic() < deadline, "job never finished")
                time.sleep(0.2)
        self.case("stale base: the change carries the version it READ (or 409 version_conflict)", stale_base)

        # ---- synthesise_findings ----
        def synth():
            check("research" in state, "needs the research case")
            rclan, rch, facts = state["research"]
            facts = copy.deepcopy(facts)
            if facts:  # one stale pin, to exercise the confidence rule
                facts[0]["stale"] = {"detected_at": "2026-01-01T00:00:00Z", "current_version": 2,
                                     "current_fact_id": "f_STALECURRENT1"}
            clan = dict(rclan, facts=facts, data=merge_patch(rclan["data"], rch["data_patch"]))
            st, body = c.task("synthesise_findings", {}, clan)
            body = self.envelope(st, body, "synthesise_findings", states={"queued", "running"})
            done, _ = self.poll(body["job"]["id"], clan, "synthesise_findings")
            ch, _, _ = self.change(done, clan)
            check(ch["findings_append"], "no findings from a document with pins")
            ids = {fi["id"] for fi in ch["findings_append"]}
            for d in ch["decisions"]:
                if d["kind"] == "finding":
                    check(any(t.endswith(f"#findings[{i}]") for i in ids for t in d["targets"]), "finding decision targets")
            check(sum(d["kind"] == "finding" for d in ch["decisions"]) >= 1, "no finding decision")
        self.case("synthesise_findings: proposed findings cite pins; confidence derived", synth)

        def synth_empty():
            clan = research_clan()
            st, body = c.task("synthesise_findings", {}, clan)
            if st == 400:
                return self.error(st, body, 400, {"invalid_input"})
            body = self.envelope(st, body, "synthesise_findings")
            if body["job"]["state"] != "done":
                body, _ = self.poll(body["job"]["id"], clan, "synthesise_findings")
            ch, _, _ = self.change(body, clan)
            check(not ch["findings_append"], "findings invented without pins to cite")
        self.case("synthesise_findings: no pins -> 400 or no findings, never invented", synth_empty)

        # ---- errors ----
        def e(name, status, types, fn):
            def run():
                st, body = fn()
                self.error(st, body, status, types)
            self.case(name, run)

        e("error: unknown task -> 400 unknown_task", 400, {"unknown_task"},
          lambda: c.task("summon_brief", {}, doc_clan()))
        e("error: pipeline handler with an unknown major -> 400 unknown_handler", 400, {"unknown_handler"},
          lambda: c.task("extract_ask", extract_input(), doc_clan(pipeline={"tasks": {"extract_ask": {"handler": "extract_ask@99"}}})))
        e("error: pipeline names an unregistered handler -> 400 unknown_handler", 400, {"unknown_handler"},
          lambda: c.task("extract_ask", extract_input(), doc_clan(pipeline={"tasks": {"extract_ask": {"handler": "no_such_handler@1"}}})))
        e("error: task the document's pipeline does not declare -> 400 unknown_task", 400, {"unknown_task"},
          lambda: c.task("synthesise_findings", {}, doc_clan(pipeline={"tasks": {"extract_ask": {"handler": "extract_ask@1"}}})))
        e("error: unknown job -> 404", 404, {"unknown_job", "not_found"},
          lambda: c.task("job_status", {"job_id": "job_does_not_exist"}, doc_clan()))
        e("error: job_status without job_id -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.task("job_status", {}, doc_clan()))

        def other_doc():
            clan = research_clan()
            st, body = c.task("research_lens", {"lenses": ["media_spend"], "markets": ["IE"]}, clan)
            self.envelope(st, body, "research_lens")
            return c.task("job_status", {"job_id": body["job"]["id"]}, dict(clan, id="00000000-0000-4000-8000-000000000000"))
        e("error: a job polled from another document -> 404", 404, {"unknown_job", "not_found"}, other_doc)
        e("error: body is not JSON -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.request("POST", "/v1/tasks", raw=b"{not json"))
        e("error: wrong request_kind -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.request("POST", "/v1/tasks", {"request_kind": "agent", "payload": {"task": "extract_ask",
                                                                                       "input": extract_input()}, "clan": doc_clan()}))
        e("error: missing payload.task -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.request("POST", "/v1/tasks", {"request_kind": "middleware", "payload": {"input": {}}, "clan": doc_clan()}))
        e("error: scope in the request (M3) -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.task("research_lens", {"scope": {"org": "org/someone-else"}}, research_clan()))
        e("error: unknown lens -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.task("research_lens", {"lenses": ["astrology"]}, research_clan()))
        e("error: market 'UK' (the UK is GB) -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.task("research_lens", {"markets": ["UK"]}, research_clan()))

        def no_markets():
            clan = research_clan()
            del clan["data"]["campaign"]["markets"]
            return c.task("research_lens", {}, clan)
        e("error: research without markets (gate research) -> 400 invalid_input", 400, {"invalid_input"}, no_markets)
        e("error: extract_ask with nothing to read -> 400 invalid_input", 400, {"invalid_input"},
          lambda: c.task("extract_ask", {"prompt": "", "attachments": []}, doc_clan()))

        def no_doc():
            clan = doc_clan()
            del clan["id"]
            return c.task("extract_ask", extract_input(), clan)
        e("error: no clan.id -> 400 invalid_input", 400, {"invalid_input"}, no_doc)

        intake_cases(self)

        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed  (schemas: {VALIDATOR})")
        print("not exercised: 409 version_conflict (only an implementation that holds the document can raise it; "
              "the stale-base case accepts it), 500 internal (no request can provoke it on purpose), "
              "answer_question text when allow_text is false (no identify question the contract defines "
              "disallows text)")
        return not failed


# ---------------------------------------------------------------------------
# Fixtures — fabricated for testing. Not a real client, brand or brief.
# ---------------------------------------------------------------------------
DOC = "5b0c7e1a-2d4f-4c8b-9e61-7a3f0d2c9b18"
PROMPT = "Start the campaign from Aoife's email."
BRIEF = """EXAMPLE — fabricated for testing.

From: Aoife Brennan <aoife@example.invalid>
Subject: Harbour Tonic relaunch brief

Hi team,

We're relaunching Harbour Tonic in Ireland and GB.

The problem, in a sentence: shoppers think of Harbour Tonic as a mixer for gin and nothing else.

What we need: Harbour Tonic to be chosen on its own as an adult soft drink.

We see Saltmarsh Soda as the one to beat.

Budget is in the region of €400k working media and production across both markets.

We need a 30" TVC, social cutdowns and 6-sheet posters. TV is a must.

Thanks,
Aoife
Marketing Director, Harbour Drinks Co
"""
BRIEF_SHA = hashlib.sha256(BRIEF.encode()).hexdigest()


def extract_input():
    return {"prompt": PROMPT, "attachments": [{"name": "brief-email.txt", "sha256": BRIEF_SHA, "text": BRIEF}]}


def stated(value, gate):
    return {"value": value, "origin": "stated", "gate": gate, "by": "human:u_test", "decision": "d_TESTOPEN01"}


def doc_clan(with_roster=False, pipeline=None):
    clan = {"id": DOC, "version": "1", "app": {"app_id": "campaign-research", "version": "1"},
            "data": {"campaign": {"brand": stated({"ref": "brand/harbour-tonic", "name": "Harbour Tonic"}, "created")}},
            "facts": [], "findings": [], "decision_chain": {"decisions": []}, "context": "", "lineage": None}
    if pipeline is not None:
        clan["pipeline"] = pipeline
    if with_roster:
        clan["facts"] = [roster_fact("f_TESTROST01", "roster.categories.primary", "drinks.soft_drinks"),
                         roster_fact("f_TESTROST02", "roster.client_org", "org/harbour-drinks-co")]
    return clan


def roster_fact(fid, key, value):
    return {"id": fid, "entity": "brand/harbour-tonic", "key": key, "value": value, "unit": "code",
            "as_of": "2026-01-15", "retrieved_at": "2026-09-01", "sources": ["src_r0st"], "confidence": "high",
            "licence": "client-confidential", "status": "active", "version": 1, "supersedes": None,
            "origin": f"fact://brand/harbour-tonic/{key}@1", "decision": "d_TESTLOOK01",
            "pinned_at": "2026-09-01T09:00:00Z", "pin_reason": "roster row", "layer": "brand", "method": "report"}


def research_clan():
    clan = doc_clan()
    clan["version"] = "3"
    c = clan["data"]["campaign"]
    c["categories"] = stated(["drinks.soft_drinks", "drinks.mixers"], "research")
    c["markets"] = stated(["IE", "GB"], "research")
    c["competitor_set"] = stated([{"ref": "brand/saltmarsh-soda", "name": "Saltmarsh Soda"},
                                  {"ref": "brand/pier-mixers", "name": "Pier Mixers"}], "none")
    return clan


# ---------------------------------------------------------------------------
# The chat intake (§8): start_campaign, answer_question, compose_report
# ---------------------------------------------------------------------------
# The suite plays the host (§5) and the view (§8.3) in miniature: it holds the
# document, applies every reply's change the way the host does (field by field
# against the read-set, skipping a field whose decisions are all in the chain),
# rebuilds the projection, and writes the person's answers as human writes. It
# asserts only what the contract and Contract 3 §16-17 say.

STAGES = ["extract", "identify", "select", "research", "synthesise", "report"]
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def new_doc_id():
    return str(uuid.uuid4())


def addr_of(path):
    for m in ("intake.messages.", "materials."):
        if path.startswith(m):
            return f"{m[:-1]}[{path[len(m):]}]"
    return path


def field_paths(patch):
    """The fields a data_patch writes, at the granularity the host judges them."""
    out = []
    for top, sub in patch.items():
        if top == "intake" and isinstance(sub, dict) and isinstance(sub.get("messages"), dict):
            out += [f"intake.messages.{k}" for k in sub["messages"]]
        elif top in ("campaign", "selection", "materials") and isinstance(sub, dict) and sub:
            out += [f"{top}.{k}" for k in sub]
        else:
            out.append(top)
    return out


def sub_patch(patch, path):
    node = patch
    for p in path.split("."):
        node = node[p]
    for p in reversed(path.split(".")):
        node = {p: node}
    return node


def sha_json(obj):
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def msg_id():
    """A ULID-style message id, as the view mints: sorts in creation order."""
    ms, t = int(time.time() * 1000), ""
    for _ in range(10):
        t = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"[ms % 32] + t
        ms //= 32
    time.sleep(0.002)
    return "msg_" + t + uuid.uuid4().hex[:8].upper()


class Host:
    """The document and the one write funnel."""

    def __init__(self, suite, doc_id, data, facts=(), chain=()):
        self.s, self.id = suite, doc_id
        self.data, self.facts, self.findings = copy.deepcopy(data), list(facts), []
        self.chain = list(chain)
        self.history = []  # (version, data) after every write
        self._seq = 0
        self.commit()

    def commit(self):
        pins = {}
        for f in self.facts:
            p = {k: f[k] for k in ("layer", "entity", "key") if k in f}
            p.setdefault("layer", "brand")
            if "market" in f:
                p["market"] = f["market"]
            p.update({k: f[k] for k in ("value", "unit", "as_of", "confidence", "licence")})
            p["stale"] = "stale" in f
            pins[f["id"]] = p
        fnd = {f["id"]: {"statement": f["statement"], "status": f["status"], "confidence": f["confidence"],
                         "cites": f["cites"]} for f in self.findings}
        self.data["projection"] = {"built_from": {"facts_sha256": sha_json({"facts": self.facts}),
                                                  "findings_sha256": sha_json({"findings": self.findings}),
                                                  "built_at": now_iso()},
                                   "pins": pins, "findings": fnd}
        self.version = sha_json([self.data, self.facts, self.findings, self.chain])
        self.history.append((self.version, copy.deepcopy(self.data)))
        for label, inst, schema in (("data", self.data, self.s.data_schema),
                                    ("facts", {"facts": self.facts}, self.s.facts_schema),
                                    ("findings", {"findings": self.findings}, self.s.findings_schema)):
            errs = validate(inst, schema)
            check(not errs, f"the document's {label} fails its schema after a write: " + "; ".join(errs[:6]))

    def clan(self):
        return {"id": self.id, "revision": self.version, "version": self.version,
                "app": {"app_id": "campaign-research", "version": "1"},
                "data": copy.deepcopy(self.data), "facts": copy.deepcopy(self.facts),
                "findings": copy.deepcopy(self.findings),
                "pipeline": {"tasks": {t: {"handler": h} for t, h in self.s.pipeline.items()}},
                "decision_chain": {"decisions": copy.deepcopy(self.chain)}, "context": "", "lineage": None}

    def human_write(self, patch, targets, action, rationale):
        self._seq += 1
        did = "d_TESTHUMAN" + f"{self._seq:03d}" + hashlib.sha256(self.id.encode()).hexdigest()[:4].upper()
        for env in (patch.get("campaign") or {}).values():
            env["decision"] = did
        self.chain.append({"id": did, "kind": "edit", "agent": "human", "actor": "human:u_test", "action": action,
                           "targets": [f"{self.id}#{t}" for t in targets], "rationale": rationale,
                           "timestamp": now_iso()})
        self.data = merge_patch(self.data, patch)
        self.commit()
        return did

    def apply(self, ch):
        """§5: returns the fields applied; Fail on what the host would contest or refuse."""
        check(ch["doc"] == self.id, "change.doc is not the open document")
        known = {d["id"] for d in self.chain}
        dp, rs = ch["data_patch"] or {}, ch.get("read") or {}
        applied, new = [], False
        for p in field_paths(dp):
            a = f"{self.id}#{addr_of(p)}"
            naming = [d for d in ch["decisions"] if any(t == a or t.startswith(a + "[") for t in d["targets"])]
            check(naming, f"staged write {p} is named by no decision's targets")
            if all(d["id"] in known for d in naming):
                continue  # a repeated stage: skipped entirely
            key = next((k for k in rs if p == k or p.startswith(k + ".")), None)
            check(key is not None, f"change.read does not cover {p}")
            cur = get_path(self.data, p)
            want = get_path(merge_patch(self.data, sub_patch(dp, p)), p)
            check(cur == get_path({"x": rs[key]}, "x" + p[len(key):]) or cur == want,
                  f"{p}: the document holds something other than what the job read — the job would contest "
                  f"itself or a person's write")
            self.data = merge_patch(self.data, sub_patch(dp, p))
            applied.append(p)
            new = True
        have = {f["id"]: f for f in self.facts}
        for f in ch["facts_append"]:
            if f["id"] in have:
                check(_canon(have[f["id"]]) == _canon(f), f"pin {f['id']} re-sent with different content")
            else:
                self.facts.append(f)
                have[f["id"]] = f
                new = True
        have = {f["id"]: f for f in self.findings}
        for f in ch["findings_append"]:
            if f["id"] not in have:
                self.findings.append(f)
                new = True
        for d in ch["decisions"]:
            if d["id"] not in known:
                self.chain.append(dict(d, actor="process:middleware"))
                known.add(d["id"])
                new = True
        if new:
            self.commit()
        return applied if new else None


def start_doc(prompt, attachment=None, facts=(), chain=()):
    """A new document as the view leaves it before sending start_campaign: the
    prompt and the attachment indexed as materials, the person's message written."""
    doc = new_doc_id()
    pmat = "mat_p" + hashlib.sha256(prompt.encode()).hexdigest()[:10]
    mats = {pmat: {"kind": "prompt", "name": "prompt", "sha256": "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                   "received_at": now_iso(), "licence": "client-confidential"}}
    atts = []
    if attachment:
        name, text = attachment
        amat = "mat_a" + hashlib.sha256(text.encode()).hexdigest()[:10]
        sha = hashlib.sha256(text.encode()).hexdigest()
        mats[amat] = {"kind": "email", "name": name, "sha256": "sha256:" + sha, "received_at": now_iso(),
                      "licence": "client-confidential"}
        atts.append({"material_id": amat, "name": name, "sha256": sha, "text": text})
    msg = {"role": "user", "by": "human:u_test", "at": now_iso(), "text": prompt, "material_id": pmat}
    if atts:
        msg["attachments"] = [a["material_id"] for a in atts]
    first = msg_id()
    data = {"campaign": {}, "materials": mats, "intake": {"messages": {first: msg}}}
    chain = list(chain) + [{"id": "d_TESTOPEN01", "kind": "edit", "agent": "human", "actor": "human:u_test",
                            "action": "create", "targets": [f"{doc}#intake.messages[{first}]"] +
                            [f"{doc}#materials[{m}]" for m in mats], "rationale": "Started a campaign in the chat.",
                            "timestamp": now_iso()}]
    return doc, data, {"prompt": prompt, "attachments": atts}, list(facts), chain


class Run:
    """One start_campaign job driven to an end, the way the host and the view drive it."""

    def __init__(self, suite, host, inp):
        self.s, self.h, self.inp = suite, host, inp
        self.job_id = None
        self.start_idx = len(host.history)
        self.stages, self.states, self.questions, self.changes = [], [], [], []
        self.messages = {}
        self.answered_fields = set()

    def reply(self, st, body, task):
        s = self.s
        if task == "answer_question":
            body = s.envelope(st, body, "job_status")
            check(body["task"] == "start_campaign", "answer_question's reply must describe the start_campaign job")
        else:
            body = s.envelope(st, body, task if task != "job_status" else "job_status")
        check(body["task"] == "start_campaign", f"task {body['task']} is not the job's task")
        declared = s.pipeline.get("start_campaign", "start_campaign@1")
        check(body["handler"].split("@")[0] == "start_campaign" and
              body["handler"].split("@")[1].split(".")[0] == declared.split("@")[1],
              f"handler {body['handler']} is not start_campaign@{declared.split('@')[1]}.x")
        job = body["job"]
        if self.job_id is None:
            self.job_id = job["id"]
        check(job["id"] == self.job_id, "the reply describes another job")
        check(job.get("stage") in STAGES, f"job.stage {job.get('stage')!r} is not one of the six")
        check(job["progress"]["total"] == 6, "start_campaign progress counts the six stages")
        if self.stages:
            check(STAGES.index(job["stage"]) >= STAGES.index(self.stages[-1]), "job.stage went backwards")
        self.stages.append(job["stage"])
        self.states.append(job["state"])
        q = job.get("question")
        if job["state"] == "needs_input":
            check(isinstance(q, dict), "needs_input without job.question")
            errs = validate(q, {"$ref": "#/definitions/question", "definitions": s.data_schema["definitions"]})
            check(not errs, "job.question fails definitions/question: " + "; ".join(errs[:4]))
            check(q.get("address", "").startswith(self.h.id + "#campaign."), "an identify question names its field")
            check(job["stage"] == "identify", "only identify asks")
        else:
            check(q is None, "job.question is non-null outside needs_input")
        msgs = body["result"].get("messages")
        check(isinstance(msgs, list), "result.messages missing")
        for m in msgs:
            check(isinstance(m, dict) and re.fullmatch(r"msg_[0-9A-Za-z]{4,}", str(m.get("id"))) and
                  isinstance(m.get("text"), str) and m["text"] and m.get("stage") in STAGES, f"bad result.messages entry {m}")
            self.messages[m["id"]] = m
        if body.get("change") is not None:
            self.check_staged(body)
            self.h.apply(body["change"])
            self.changes.append(body["change"])
        if job["state"] == "needs_input":
            held = [m for m in (self.h.data.get("intake") or {}).get("messages", {}).values()
                    if m.get("question", {}).get("id") == q["id"]]
            check(len(held) == 1 and _canon(held[0]["question"]) == _canon(q),
                  "the question is not in intake.messages, once, as job.question")
            if not self.questions or self.questions[-1]["id"] != q["id"]:
                self.questions.append(q)
        if job["state"] == "failed":
            raise Fail(f"start_campaign failed: {job['error']}")
        return body

    def check_staged(self, body):
        s, h, ch = self.s, self.h, body["change"]
        s.change(body, h.clan(), staged=True)
        seen = h.history[self.start_idx - 1:]
        check(ch["base_version"] in {v for v, _ in seen}, "change.base_version is no version the job could have read")
        for k, v in (ch.get("read") or {}).items():
            check(any(get_path(d, k) == v for _, d in seen),
                  f"change.read[{k!r}] is no value the document held while the job ran")
        dp = ch["data_patch"] or {}
        check("projection" not in dp, "data_patch touches projection")
        for f, env in (dp.get("campaign") or {}).items():
            cur = (h.data.get("campaign") or {}).get(f) or {}
            check(cur.get("origin") not in ("confirmed", "stated"),
                  f"the job writes campaign.{f}, which belongs to the person ({cur.get('origin')})")
            check(f not in self.answered_fields, f"the job writes campaign.{f}, which the person answered")
            if env is not None:
                check(env.get("origin") in ("extracted", "proposed"), f"campaign.{f} written as {env.get('origin')}")
        for mid, m in ((dp.get("intake") or {}).get("messages") or {}).items():
            check(m is not None and m.get("role") == "agent", f"the middleware writes a non-agent message {mid}")
            check(m.get("job_id") == self.job_id and m.get("stage") in STAGES, f"agent message {mid} lacks job_id/stage")
        if "report" in dp:
            check(isinstance(dp["report"], dict) and "headline" in dp["report"], "report is written whole")
        for p in field_paths(dp):
            a = f"{h.id}#{addr_of(p)}"
            check(any(t == a or t.startswith(a + "[") for d in ch["decisions"] for t in d["targets"]),
                  f"staged write {p} is named by no decision in the change")

    def start(self, answer):
        c, h = self.s.c, self.h
        st, body = c.task("start_campaign", self.inp, h.clan())
        body = self.reply(st, body, "start_campaign")
        check(body["job"]["state"] in ("queued", "running", "needs_input", "done"), "start_campaign is a long task")
        deadline = time.monotonic() + self.s.job_timeout
        delay = 0.05
        needs_polls = 0
        while body["job"]["state"] != "done":
            check(time.monotonic() < deadline, f"start_campaign not done after {self.s.job_timeout}s")
            if body["job"]["state"] == "needs_input":
                q = body["job"]["question"]
                if needs_polls == 0:  # a poll while waiting changes nothing and keeps the question
                    st, b2 = c.task("job_status", {"job_id": self.job_id}, h.clan())
                    b2 = self.reply(st, b2, "job_status")
                    check(b2["job"]["state"] == "needs_input" and b2["job"]["question"]["id"] == q["id"],
                          "a poll of a waiting job moved it")
                    needs_polls += 1
                act = answer(self, q)
                if act is None:
                    return body
                body = self.answer(q, *act)
                needs_polls = 0
                continue
            time.sleep(delay)
            delay = min(2.0, delay * 1.5)
            st, body = c.task("job_status", {"job_id": self.job_id}, h.clan())
            body = self.reply(st, body, "job_status")
        # the done poll carries the last change; a later poll repeats it and changes nothing
        st, again = c.task("job_status", {"job_id": self.job_id}, h.clan())
        again = self.s.envelope(st, again, "job_status")
        check(again["job"]["state"] == "done", "a done job moved")
        if again.get("change") is not None:
            self.check_staged(again)
            check(self.h.apply(again["change"]) is None, "a re-poll of a done job changed the document")
        return body

    def answer(self, q, option=None, text=None):
        """The view: the person's message and, for an option, the field — one human write — then answer_question."""
        h = self.h
        mid = msg_id()
        ans = {"question_id": q["id"]}
        ans.update({"option_id": option["id"]} if option else {"text": text})
        patch = {"intake": {"messages": {mid: {"role": "user", "by": "human:u_test", "at": now_iso(),
                                               "text": option["label"] if option else text, "job_id": self.job_id,
                                               "answer": ans}}}}
        targets = [f"intake.messages[{mid}]"]
        if option:
            field = q["address"].partition("#campaign.")[2]
            env = {"value": option["value"], "gate": self.s.data_schema["properties"]["campaign"]["properties"][field]
                   ["properties"]["gate"]["const"], "by": "human:u_test"}
            if option["origin"] == "stated":
                env["origin"] = "stated"
            else:
                env.update(origin="confirmed", confirmed_from=option["origin"])
                for k in ("source", "fact_ids"):
                    if k in option:
                        env[k] = option[k]
            patch["campaign"] = {field: env}
            targets.append(f"campaign.{field}")
            self.answered_fields.add(field)
        h.human_write(patch, targets, "answer_question", f"Answered {q['id']}.")
        st, body = self.s.c.task("answer_question", {"job_id": self.job_id, **ans}, h.clan())
        return self.reply(st, body, "answer_question")


def pick_first(run, q):
    """Answer any question: the first candidate, or free text naming what the fixtures hold."""
    cands = [o for o in q["options"] if "value" in o]
    if cands:
        return (cands[0], None)
    field = q["address"].partition("#campaign.")[2]
    return (None, {"markets": "Ireland and GB", "brand": "Harbour Tonic"}.get(field, "tonic"))


def intake_invariants(host, job_id=None):
    """check_example.py's intake, skipped-lens and report rules, over the live document."""
    errors = []
    data, doc = host.data, host.id
    camp = data.get("campaign") or {}
    sel = data.get("selection") or {}
    materials = data.get("materials") or {}
    msgs = (data.get("intake") or {}).get("messages") or {}
    pin_ids = {f["id"] for f in host.facts}
    finding_by_id = {f["id"]: f for f in host.findings}
    dec_ids = {d["id"] for d in host.chain}
    for f in host.findings:
        for c in f["cites"]:
            if c not in pin_ids:
                errors.append(f"finding {f['id']} cites unpinned fact {c}")

    def refs(o, keys):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys:
                    yield k, v
                else:
                    yield from refs(v, keys)
        elif isinstance(o, list):
            for v in o:
                yield from refs(v, keys)
    for k, ids in refs(camp, ("fact_ids", "finding_ids")):
        for i in ids:
            if (k == "fact_ids" and i not in pin_ids) or (k == "finding_ids" and i not in finding_by_id):
                errors.append(f"campaign cites unknown {i}")
    for src in ({k: v for k, v in data.items() if k != "report"}, host.facts, host.findings):
        for k, v in refs(src, ("decision", "decided_by", "opened_by")):
            if isinstance(v, str) and v not in dec_ids:
                errors.append(f"unknown decision {v}")
    ordered = sorted(msgs.items(), key=lambda kv: (kv[1]["at"], kv[0]))
    asked = {}
    for mid, m in ordered:
        for a in m.get("attachments", []):
            if a not in materials:
                errors.append(f"intake {mid}: attachment {a} is not a material")
        if "material_id" in m:
            mat = materials.get(m["material_id"])
            if mat is None or mat["kind"] != "prompt" or \
                    mat["sha256"] != "sha256:" + hashlib.sha256(m["text"].encode("utf-8")).hexdigest():
                errors.append(f"intake {mid}: material_id is not this text's prompt material")
        q = m.get("question")
        if q:
            if q["id"] in asked:
                errors.append(f"question {q['id']} asked twice")
            asked[q["id"]] = (q, m["job_id"])
            fld = q.get("address", "").partition("#campaign.")[2]
            if q.get("address") and (not q["address"].startswith(doc + "#") or fld not in camp_schema_fields(host)):
                errors.append(f"question {q['id']}: bad address")
            if len({o["id"] for o in q["options"]}) != len(q["options"]):
                errors.append(f"question {q['id']}: option ids repeat")
            for o in q["options"]:
                if "value" in o and fld:
                    vs = dict(host.s.data_schema["properties"]["campaign"]["properties"][fld]["properties"]["value"],
                              definitions=host.s.data_schema["definitions"])
                    if validate(o["value"], vs):
                        errors.append(f"question {q['id']}: option {o['id']} value is not a campaign.{fld} value")
                for fid in o.get("fact_ids", []):
                    if fid not in pin_ids:
                        errors.append(f"question {q['id']}: option {o['id']} cites unpinned {fid}")
                if "source" in o and o["source"]["material_id"] not in materials:
                    errors.append(f"question {q['id']}: option {o['id']} cites an unknown material")
        ans = m.get("answer")
        if ans:
            if ans["question_id"] not in asked:
                errors.append(f"intake {mid}: answers a question no earlier message asked")
                continue
            q, job = asked[ans["question_id"]]
            if m.get("job_id") != job:
                errors.append(f"intake {mid}: answer names another job")
            if "option_id" in ans:
                opt = next((o for o in q["options"] if o["id"] == ans["option_id"]), None)
                if opt is None or "value" not in opt:
                    errors.append(f"intake {mid}: option is not a candidate")
                else:
                    env = camp.get(q["address"].partition("#campaign.")[2], {})
                    want = "stated" if opt["origin"] == "stated" else "confirmed"
                    if env.get("value") != opt["value"] or env.get("origin") != want:
                        errors.append(f"intake {mid}: the answered field does not hold the pick as {want}")
            elif not q["allow_text"]:
                errors.append(f"intake {mid}: text answer to a no-text question")
    for d in host.chain:
        for tg in d.get("targets", []):
            mm = re.fullmatch(re.escape(doc) + r"#intake\.messages\[(.+)\]", tg)
            if mm and mm.group(1) not in msgs:
                errors.append(f"decision {d['id']} targets unknown message {mm.group(1)}")
    skipped = sel.get("lenses_skipped", [])
    ran = {(r["lens"], r["market"]) for r in sel.get("lenses_run", [])}
    cbm = sel.get("coverage_by_market", {})
    for sk in skipped:
        for (lens, mkt) in ran:
            if lens == sk["lens"] and sk.get("market") in (None, mkt):
                errors.append(f"lens {lens}/{mkt} is both skipped and run")
        for mkt, cov in cbm.items():
            if sk["lens"] in cov and sk.get("market") in (None, mkt):
                errors.append(f"lens {sk['lens']}/{mkt} is skipped but has coverage")
    for lens, merged in (sel.get("coverage") or {}).items():
        vals = [cbm[m][lens] for m in cbm if lens in cbm[m]]
        want = "filled" if all(v == "filled" for v in vals) else "empty" if all(v == "empty" for v in vals) else "thin"
        if vals and merged != want:
            errors.append(f"coverage.{lens} breaks the merge rule")
    rpt = data.get("report")
    if rpt:
        errors += report_errors(host, rpt)
    return errors


def camp_schema_fields(host):
    return set(host.s.data_schema["properties"]["campaign"]["properties"])


def report_errors(host, rpt):
    errors = []
    data, doc = host.data, host.id
    camp, sel = data.get("campaign") or {}, data.get("selection") or {}
    pin_by_id = {f["id"]: f for f in host.facts}
    finding_by_id = {f["id"]: f for f in host.findings}
    gap_ids = {g["id"] for g in sel.get("gaps", [])}
    contest_ids = {c["id"] for c in sel.get("contested", [])}
    names = [(camp.get("name") or {}).get("value", ""), ((camp.get("brand") or {}).get("value") or {}).get("name", "")] + \
        [c.get("name", "") for c in (camp.get("competitor_set") or {}).get("value", [])]

    def claim(line, where):
        if not line.get("cites"):
            errors.append(f"report {where}: a claim without a cite")
        allowed = set()
        for c in line.get("cites", []):
            if c in pin_by_id:
                v = pin_by_id[c]["value"]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    allowed |= {repr(v), f"{v:g}"}
                    if pin_by_id[c]["unit"] == "proportion":
                        allowed |= {f"{v * 100:g}", f"{round(v * 100)}"}
                else:
                    allowed |= set(NUM.findall(str(v)))
            elif c in finding_by_id:
                if finding_by_id[c]["status"] == "rejected":
                    errors.append(f"report {where}: cites rejected finding {c}")
                allowed |= set(NUM.findall(finding_by_id[c]["statement"]))
            else:
                errors.append(f"report {where}: cite {c} is neither a pin nor a finding")
        for n in names:
            allowed |= set(NUM.findall(n))
        for n in NUM.findall(line["text"]):
            if n not in allowed:
                errors.append(f"report {where}: states {n}, which no cited pin or finding holds")
    claim(rpt["headline"], "headline")
    for i, s in enumerate(rpt["summary"]):
        claim(s, f"summary[{i}]")
    ids = [s["id"] for s in rpt["sections"]]
    if len(set(ids)) != len(ids):
        errors.append("report: section ids repeat")
    for s in rpt["sections"]:
        for i, b in enumerate(s["blocks"]):
            w = f"{s['id']}[{i}]"
            if b["kind"] == "claim":
                claim(b, w)
            elif b["kind"] == "pins":
                errors += [f"report {w}: unpinned {f}" for f in b["fact_ids"] if f not in pin_by_id]
            elif b["kind"] == "finding":
                f = finding_by_id.get(b["finding_id"])
                if f is None or f["status"] == "rejected":
                    errors.append(f"report {w}: finding {b['finding_id']} unknown or rejected")
            elif b["kind"] == "gap" and b["gap_id"] not in gap_ids:
                errors.append(f"report {w}: unknown gap")
            elif b["kind"] == "contest" and b["contest_id"] not in contest_ids:
                errors.append(f"report {w}: unknown contest")
    D3 = ["brand", "categories", "markets", "competitor_set"]
    want = [f for f in D3 if (camp.get(f) or {}).get("origin") in ("extracted", "proposed")] + \
        [f for f, e in camp.items() if f not in D3 and e.get("origin") == "proposed"]
    got = [c["address"].partition("#campaign.")[2] for c in rpt["confirm"]]
    if any(not c["address"].startswith(doc + "#") for c in rpt["confirm"]) or sorted(got) != sorted(want):
        errors.append(f"report.confirm {got} != the fields to confirm {want}")
    nr = {(n["lens"], n.get("market")) for n in rpt["not_researched"]}
    for sk in sel.get("lenses_skipped", []):
        if (sk["lens"], sk.get("market")) not in nr:
            errors.append(f"report.not_researched omits skipped {sk['lens']}/{sk.get('market')}")
    return errors


def intake_cases(suite):
    c, e = suite.c, None
    state = {}

    def invariants_ok(host):
        errs = intake_invariants(host)
        check(not errs, "the document breaks Contract 3 §16-17: " + "; ".join(errs[:6]))

    def final_checks(run, host):
        data = host.data
        check(run.states[-1] == "done", "the job did not finish")
        rpt = data.get("report")
        check(isinstance(rpt, dict), "no report after start_campaign")
        bf = data["projection"]["built_from"]
        check(rpt["based_on"]["facts_sha256"] == bf["facts_sha256"] and
              rpt["based_on"]["findings_sha256"] == bf["findings_sha256"],
              "report.based_on is not the projection hashes of the document it describes")
        check(rpt["based_on"]["version"] in {v for v, _ in host.history}, "report.based_on.version is no version the host held")
        check(rpt["handler"].startswith("start_campaign@"), "the report stage's report names start_campaign's handler")
        agent = [m for m in data["intake"]["messages"].values() if m["role"] == "agent" and m.get("job_id") == run.job_id]
        check({m["stage"] for m in agent} >= set(STAGES), "a stage wrote no agent message")
        for mid in run.messages:
            check(mid in data["intake"]["messages"], f"result.messages {mid} never landed in intake.messages")
        invariants_ok(host)

    # ---- unambiguous: a Brand: label, a roster row pinned, markets in the brief ----
    def unambiguous():
        facts = [roster_fact("f_TESTROST01", "roster.categories.primary", "drinks.soft_drinks"),
                 roster_fact("f_TESTROST02", "roster.categories.secondary", "drinks.mixers"),
                 roster_fact("f_TESTROST03", "roster.client_org", "org/harbour-drinks-co")]
        chain = [{"id": "d_TESTLOOK01", "kind": "pin", "agent": "napkin-host/0.3", "action": "lookup",
                  "targets": [], "rationale": "roster row", "timestamp": now_iso()}]
        doc, data, inp, facts, chain = start_doc(INTAKE_PROMPT, ("brief-email.txt", BRIEF), facts, chain)
        for d in chain:
            d["targets"] = d["targets"] or [f"{doc}#facts[{f['id']}]" for f in facts]
        host = Host(suite, doc, data, facts, chain)
        run = Run(suite, host, inp)
        run.start(lambda r, q: (_ for _ in ()).throw(Fail(f"asked {q['text']!r} though the prompt is unambiguous")))
        check(run.states[0] in ("queued", "running"), "start_campaign answers queued or running")
        seen = []
        for s_ in run.stages:
            if not seen or seen[-1] != s_:
                seen.append(s_)
        check(seen[-1] == "report", "the job never reached the report stage")
        camp = host.data["campaign"]
        b = camp.get("brand")
        check(b and b["origin"] == "extracted" and b["value"]["ref"] == "brand/harbour-tonic",
              "the labelled brand is written extracted")
        check(b["source"].get("quote", "") in INTAKE_PROMPT + BRIEF, "the brand's span is not verbatim")
        cats = camp.get("categories")
        check(cats and cats["origin"] == "proposed" and set(cats["fact_ids"]) <= {f["id"] for f in facts},
              "categories come from the pinned roster row, proposed, citing it")
        check(cats["value"][0] == "drinks.soft_drinks", "the roster's primary category ranks first")
        check({"IE", "GB"} <= set(camp["markets"]["value"]), "markets IE and GB are in the brief")
        runs = {(r["lens"], r["market"]) for r in host.data["selection"]["lenses_run"]}
        check(("effectiveness_evidence", "GB") not in runs,
              "the prompt limits effectiveness to Ireland, yet it was researched in GB")
        check(runs, "nothing was researched")
        final_checks(run, host)
        state["unambiguous"] = (host, run)
    suite.case("start_campaign: an unambiguous prompt runs extract..report to done; every staged change "
               "validates and applies; the document meets Contract 3 §16-17", unambiguous)

    # ---- ambiguous: two brands, neither named the client's ----
    def ambiguous():
        doc, data, inp, _, chain = start_doc(AMBIG_PROMPT, ("two-briefs.txt", AMBIG))
        host = Host(suite, doc, data, (), chain)
        run = Run(suite, host, inp)

        def answer(r, q):
            fld = q["address"].partition("#campaign.")[2]
            if fld == "brand" and "brand" not in r.answered_fields:
                names = {o["value"]["name"] for o in q["options"] if "value" in o}
                check({"Harbour Tonic", "Saltmarsh Soda"} <= names, f"the brand question offers {names}")
                check(any("value" not in o for o in q["options"]) and q["allow_text"],
                      "the brand question has a 'none of these' escape and allows text")
                return (next(o for o in q["options"] if o.get("value", {}).get("name") == "Harbour Tonic"), None)
            return pick_first(r, q)
        run.start(answer)
        check(run.questions and run.questions[0]["address"] == f"{doc}#campaign.brand",
              "two brands and no client named: the first question is which brand is the client's")
        for ch in run.changes:
            check("brand" not in ((ch["data_patch"] or {}).get("campaign") or {}),
                  "the middleware wrote campaign.brand; the person answered it")
        b = host.data["campaign"]["brand"]
        check(b["origin"] == "confirmed" and b["by"] == "human:u_test", "the answered brand is the person's")
        final_checks(run, host)
    suite.case("start_campaign: two brands -> needs_input 'which is the client's?' with candidates + escape; "
               "a poll while waiting changes nothing; the answer continues the job to done", ambiguous)

    # ---- free text becomes candidates, then a pick ----
    def free_text():
        doc, data, inp, _, chain = start_doc(AMBIG_PROMPT, ("two-briefs.txt", AMBIG))
        host = Host(suite, doc, data, (), chain)
        run = Run(suite, host, inp)
        typed = {"done": False}

        def answer(r, q):
            fld = q["address"].partition("#campaign.")[2]
            if fld == "brand" and not typed["done"]:
                typed["done"] = True
                return (None, "harbour tonic")
            if fld == "brand" and "brand" not in r.answered_fields:
                check(q["id"] != r.questions[0]["id"], "the follow-up is a new question")
                cands = [o for o in q["options"] if "value" in o]
                check(cands, "free text resolved to no candidate")
                hit = [o for o in cands if o["value"]["name"].lower() == "harbour tonic"]
                check(hit, f"no candidate is the brand the text names: {[o['value'] for o in cands]}")
                return (hit[0], None)
            return pick_first(r, q)
        run.start(answer)
        brand_qs = [q for q in run.questions if q["address"].endswith("#campaign.brand")]
        check(len(brand_qs) >= 2, "a free-text answer did not lead to a follow-up question")
        for ch in run.changes:
            check("brand" not in ((ch["data_patch"] or {}).get("campaign") or {}),
                  "free text became a field value without the person picking it")
        final_checks(run, host)
    suite.case("answer_question: free text -> a new question whose candidates are what the text names; "
               "the pick settles the field", free_text)

    # ---- compose_report ----
    def compose():
        check("unambiguous" in state, "needs the unambiguous case")
        host, _ = state["unambiguous"]
        clan = host.clan()
        st, body = c.task("compose_report", {}, clan)
        body = suite.envelope(st, body, "compose_report", states={"done"})
        check(body["job"].get("stage") == "report", "compose_report's job.stage is report")
        ch, _, _ = suite.change(body, clan)
        dp = ch["data_patch"]
        check("report" in dp and set(field_paths(dp)) - {"report"} and
              all(p.startswith("intake.messages.") for p in set(field_paths(dp)) - {"report"}),
              "compose_report writes the report and one agent message, nothing else")
        rpt = dp["report"]
        check(rpt["handler"] == body["handler"], "report.handler is the response's handler")
        check(rpt["based_on"]["version"] == clan["version"], "based_on.version is the version it read")
        bf = clan["data"]["projection"]["built_from"]
        check(rpt["based_on"]["facts_sha256"] == bf["facts_sha256"] and
              rpt["based_on"]["findings_sha256"] == bf["findings_sha256"], "based_on hashes are the projection's")
        for p in field_paths(dp):
            a = f"{host.id}#{addr_of(p)}"
            check(any(t == a for d in ch["decisions"] for t in d["targets"]), f"{p} is named by no decision")
        host.apply(ch)
        invariants_ok(host)
    suite.case("compose_report: done in one response; the report is recomposed whole from the document, "
               "every claim cited", compose)

    # ---- errors ----
    def err(name, status, types, fn):
        def go():
            st, body = fn()
            suite.error(st, body, status, types)
        suite.case(name, go)

    def waiting():
        """A job left in needs_input on its own document."""
        if "waiting" not in state:
            doc, data, inp, _, chain = start_doc(AMBIG_PROMPT, ("two-briefs.txt", AMBIG))
            host = Host(suite, doc, data, (), chain)
            run = Run(suite, host, inp)
            run.start(lambda r, q: None)
            check(run.states[-1] == "needs_input", "the ambiguous prompt did not wait for an answer")
            state["waiting"] = (host, run, run.questions[-1])
        return state["waiting"]

    def ans(extra, q_id=None):
        host, run, q = waiting()
        return c.task("answer_question", {"job_id": run.job_id, "question_id": q_id or q["id"], **extra}, host.clan())

    def opt(pred):
        host, run, q = waiting()
        return next(o for o in q["options"] if pred(o))

    err("answer_question: a question id that is not the open question -> 400 invalid_input", 400, {"invalid_input"},
        lambda: ans({"option_id": opt(lambda o: "value" in o)["id"]}, q_id="q_NOTTHEQUESTION"))
    err("answer_question: both option_id and text -> 400 invalid_input", 400, {"invalid_input"},
        lambda: ans({"option_id": opt(lambda o: "value" in o)["id"], "text": "Harbour Tonic"}))
    err("answer_question: neither option_id nor text -> 400 invalid_input", 400, {"invalid_input"},
        lambda: ans({}))
    err("answer_question: the escape's id (answered with text) -> 400 invalid_input", 400, {"invalid_input"},
        lambda: ans({"option_id": opt(lambda o: "value" not in o)["id"]}))
    err("answer_question: an option id not among the candidates -> 400 invalid_input", 400, {"invalid_input"},
        lambda: ans({"option_id": "no_such_option"}))
    err("start_campaign while a start_campaign job on the document waits -> 409 job_state", 409, {"job_state"},
        lambda: c.task("start_campaign", waiting()[1].inp, waiting()[0].clan()))
    err("compose_report while a start_campaign job on the document is unfinished -> 409 job_state", 409, {"job_state"},
        lambda: c.task("compose_report", {}, waiting()[0].clan()))
    err("answer_question: unknown job -> 404", 404, {"unknown_job", "not_found"},
        lambda: c.task("answer_question", {"job_id": "job_does_not_exist", "question_id": "q_NOPE0001",
                                           "text": "x"}, waiting()[0].clan()))
    err("answer_question: a job from another document -> 404", 404, {"unknown_job", "not_found"},
        lambda: c.task("answer_question", {"job_id": waiting()[1].job_id, "question_id": waiting()[2]["id"],
                                           "text": "x"}, dict(waiting()[0].clan(), id=new_doc_id())))

    def done_job():
        check("unambiguous" in state, "needs the unambiguous case")
        host, run = state["unambiguous"]
        return c.task("answer_question", {"job_id": run.job_id, "question_id": "q_ANYQUESTION1", "text": "x"}, host.clan())
    err("answer_question for a job that is not needs_input (done) -> 409 job_state", 409, {"job_state"}, done_job)

    def not_indexed():
        doc, data, inp, _, chain = start_doc(INTAKE_PROMPT, ("brief-email.txt", BRIEF))
        inp = copy.deepcopy(inp)
        inp["attachments"][0]["material_id"] = "mat_notindexed01"
        return c.task("start_campaign", inp, Host(suite, doc, data, (), chain).clan())
    err("start_campaign: an attachment whose material_id is not in materials -> 400 invalid_input", 400,
        {"invalid_input"}, not_indexed)

    def wrong_sha():
        doc, data, inp, _, chain = start_doc(INTAKE_PROMPT, ("brief-email.txt", BRIEF))
        inp = copy.deepcopy(inp)
        inp["attachments"][0]["sha256"] = hashlib.sha256(b"other bytes").hexdigest()
        return c.task("start_campaign", inp, Host(suite, doc, data, (), chain).clan())
    err("start_campaign: an attachment whose sha256 is not its material's -> 400 invalid_input", 400,
        {"invalid_input"}, wrong_sha)

    def nothing():
        doc, data, _, _, chain = start_doc("x")
        return c.task("start_campaign", {"prompt": "", "attachments": []}, Host(suite, doc, data, (), chain).clan())
    err("start_campaign: nothing to read -> 400 invalid_input", 400, {"invalid_input"}, nothing)

    def no_cites():
        doc, data, _, _, chain = start_doc("x")
        return c.task("compose_report", {}, Host(suite, doc, data, (), chain).clan())
    err("compose_report: no pin and no finding to cite -> 400 invalid_input", 400, {"invalid_input"}, no_cites)


INTAKE_PROMPT = ("Brand: Harbour Tonic\n\nAoife's relaunch brief is attached. Pull together the landscape and the "
                 "audience before kick-off. Effectiveness cases only matter for Ireland.")
AMBIG_PROMPT = "Two briefs came in together. Start the campaign for the one we won."
AMBIG = """EXAMPLE — fabricated for testing.

Subject: two briefs

Two brands in this brief: Harbour Tonic and Saltmarsh Soda.

Both want a relaunch in Ireland and GB next spring, aimed at adults who drink less.

Budget is in the region of €200k working media per brand.
"""


def facts_of(clan):
    f = clan.get("facts") or []
    return list(f.get("facts", []) if isinstance(f, dict) else f)


def findings_of(clan):
    f = clan.get("findings") or []
    return list(f.get("findings", []) if isinstance(f, dict) else f)


def chain_of(clan):
    d = clan.get("decision_chain") or []
    return list(d.get("decisions", []) if isinstance(d, dict) else d)


def finding_confidence(cited):
    lvl = min(CONF.index(f["confidence"]) for f in cited)
    if len(cited) == 1 or any(f.get("stale") for f in cited):
        lvl -= 1
    return CONF[max(0, lvl)]


def parse_pipeline(text):
    """task -> declared handler, from app/pipeline.yaml (no YAML dependency)."""
    out, in_tasks, cur = {}, False, None
    for line in text.splitlines():
        if re.match(r"^tasks:\s*$", line):
            in_tasks = True
            continue
        if in_tasks and re.match(r"^\S", line):
            break
        m = re.match(r"^  ([a-z_]+):\s*$", line)
        if in_tasks and m:
            cur = m.group(1)
        m = re.match(r"^    handler:\s*([a-z_]+@\d+)\s*$", line)
        if in_tasks and cur and m:
            out[cur] = m.group(1)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--schema-dir", default="app/templates/campaign-research")
    ap.add_argument("--token", default=None)
    ap.add_argument("--job-timeout", type=float, default=900)
    a = ap.parse_args()
    ok = Suite(Client(a.base_url, a.token), a.schema_dir, a.job_timeout).run()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
