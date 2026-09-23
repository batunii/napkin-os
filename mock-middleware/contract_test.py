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
from pathlib import Path

API = "napkin.middleware/1"
LENSES = ["market_structure", "brands_positioning", "consumer_culture", "category_codes",
          "rhythm_moments", "media_spend", "regulation_clearance", "effectiveness_evidence"]
DECISION_KINDS = {"edit", "contest", "resolve", "verdict", "classify", "pin", "finding",
                  "verify", "approve", "lease", "backref"}
CONF = ["low", "medium", "high"]


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
        check(job["state"] in ("queued", "running", "done", "failed"), f"job.state {job['state']}")
        if states:
            check(job["state"] in states, f"job.state {job['state']} not in {states}")
        p = job["progress"]
        check(isinstance(p, dict) and all(isinstance(p.get(k), int) and not isinstance(p.get(k), bool)
                                          for k in ("done", "total")), "job.progress needs integer done/total")
        check(0 <= p["done"] <= p["total"], f"progress {p} out of range")
        if job["state"] == "done":
            check(job["finished_at"] is not None and job["error"] is None, "done job needs finished_at, no error")
        else:
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
    def change(self, body, clan, expect_base=None):
        ch = body["change"]
        check(isinstance(ch, dict), "done job without a change")
        for k in ("doc", "base_version", "data_patch", "facts_append", "findings_append", "decisions"):
            check(k in ch, f"change.{k} missing")
        check(ch["doc"] == clan["id"], "change.doc is not the document it was computed for")
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
                check(get_path(clan.get("data") or {}, k) == v,
                      f"change.read[{k!r}] is not what the document held when the job read it")
        for lst in ("facts_append", "findings_append", "decisions"):
            check(isinstance(ch[lst], list), f"{lst} must be a list")

        data = merge_patch(clan.get("data") or {}, dp)
        errs = validate(data, self.data_schema)
        check(not errs, "data after data_patch fails schema.json: " + "; ".join(errs[:8]))

        existing_facts = facts_of(clan)
        facts = existing_facts + ch["facts_append"]
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
        errs = validate({"findings": ch["findings_append"]}, self.findings_schema) + \
            validate({"findings": existing_fi + ch["findings_append"]}, self.findings_schema)
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
        for fname, env in (dp.get("campaign") or {}).items():
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

        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed  (schemas: {VALIDATOR})")
        print("not exercised: 409 version_conflict (only an implementation that holds the document can raise it; "
              "the stale-base case accepts it), 500 internal (no request can provoke it on purpose)")
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
