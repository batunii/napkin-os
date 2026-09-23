"""Validate the campaign-research example against its three schemas and the
cross-member rules of Contract 3 (docs/contracts/campaign-clan.md), and assert
no schema property has a default.

    uv run --with jsonschema --with pyyaml python app/templates/campaign-research/tools/check_example.py

--write-projection rebuilds example/shared/data.yaml's projection block from the
facts and findings members first (what the host does after every change)."""
import hashlib, json, sys, os, re
import yaml, jsonschema


class StrLoader(yaml.SafeLoader):
    """serde_yaml semantics: timestamps stay strings."""


StrLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _unique_mapping(loader, node, deep=False):
    """A repeated key is an error, not a silent overwrite: intake.messages is a
    map keyed by message id, so a duplicate id would otherwise vanish."""
    seen = set()
    for k, _ in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", k.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)

T = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(T, "example")
load = lambda p: yaml.load(open(p, encoding="utf-8"), Loader=StrLoader)
sha = lambda p: "sha256:" + hashlib.sha256(open(p, "rb").read()).hexdigest()

facts_p = os.path.join(EX, "shared/facts.yaml")
findings_p = os.path.join(EX, "shared/findings.yaml")
data_p = os.path.join(EX, "shared/data.yaml")
facts, findings = load(facts_p), load(findings_p)

if "--write-projection" in sys.argv:
    pins = {}
    for f in facts["facts"]:
        p = {"layer": f["layer"], "entity": f["entity"], "key": f["key"]}
        if "market" in f:
            p["market"] = f["market"]
        p.update({"value": f["value"], "unit": f["unit"], "as_of": f["as_of"], "confidence": f["confidence"],
                  "licence": f["licence"]})
        if "method" in f:
            p["method"] = f["method"]
        p["stale"] = "stale" in f
        if "stale" in f:
            p["current_version"] = f["stale"]["current_version"]
        pins[f["id"]] = p
    fnd = {}
    for f in findings["findings"]:
        e = {"statement": f["statement"], "status": f["status"], "confidence": f["confidence"], "cites": f["cites"]}
        if "verification" in f:
            e["fact_id"] = f["verification"]["fact_id"]
        fnd[f["id"]] = e
    proj = {"projection": {"built_from": {"facts_sha256": sha(facts_p), "findings_sha256": sha(findings_p),
                                          "built_at": "2026-09-23T08:02:01Z"},
                           "pins": pins, "findings": fnd}}
    text = open(data_p, encoding="utf-8").read()
    marker = "\n# ---- projection (HOST-WRITTEN"
    if marker in text:
        text = text[: text.index(marker)]
    text = text.rstrip("\n") + "\n" + marker + ", READ-ONLY — rebuilt from shared/facts.yaml and shared/findings.yaml; never patch) ----\n"
    body = yaml.safe_dump(proj, allow_unicode=True, sort_keys=False, width=200)
    open(data_p, "w", encoding="utf-8").write(text + body)

data = load(data_p)
errors = []


def check(schema_file, inst, label):
    schema = json.load(open(os.path.join(T, schema_file)))
    jsonschema.Draft7Validator.check_schema(schema)
    v = jsonschema.Draft7Validator(schema)
    errs = sorted(v.iter_errors(inst), key=lambda e: list(e.path))
    for e in errs:
        errors.append(f"{label}: {'/'.join(map(str, e.path))}: {e.message[:200]}")
    print(f"{label}: {'OK' if not errs else f'{len(errs)} errors'}")
    return schema


schema = check("schema.json", data, "data.yaml vs schema.json")
check("facts.schema.json", facts, "facts.yaml vs facts.schema.json")
check("findings.schema.json", findings, "findings.yaml vs findings.schema.json")

# --- contract rules the schemas cannot express ---------------------------------
def walk(o, path=""):
    if isinstance(o, dict):
        if "default" in o and not path.endswith("/properties"):
            errors.append(f"schema has a default at {path}")
        for k, v in o.items():
            walk(v, f"{path}/{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            walk(v, f"{path}/{i}")

for sf in ["schema.json", "facts.schema.json", "findings.schema.json"]:
    walk(json.load(open(os.path.join(T, sf))), sf)

camp = schema["properties"]["campaign"]["properties"]
if len(camp) != 19:
    errors.append(f"expected 19 campaign fields, schema has {len(camp)}")
for name, s in camp.items():
    g = s.get("x-gate")
    if g not in ("created", "research", "brief", "none") or s["properties"]["gate"]["const"] != g:
        errors.append(f"campaign.{name}: bad gate {g}")
    if name not in data["campaign"]:
        errors.append(f"example: campaign.{name} not populated")

pin_ids = {f["id"] for f in facts["facts"]}
finding_by_id = {f["id"]: f for f in findings["findings"]}
decision_ids = {d["id"] for d in load(os.path.join(EX, "agent/decision-chain.yaml"))["decisions"]}


def refs(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("fact_ids", "synthesis_finding_ids", "finding_ids"):
                yield k, v
            else:
                yield from refs(v)
    elif isinstance(o, list):
        for v in o:
            yield from refs(v)

for k, ids in refs(data["campaign"]):
    for i in ids:
        if k == "fact_ids" and i not in pin_ids:
            errors.append(f"campaign cites unpinned fact {i}")
        if k != "fact_ids" and i not in finding_by_id:
            errors.append(f"campaign cites unknown finding {i}")
for f in findings["findings"]:
    for c in f["cites"]:
        if c not in pin_ids:
            errors.append(f"finding {f['id']} cites unpinned fact {c}")
    if "verification" in f and f["verification"]["fact_id"] not in pin_ids:
        errors.append(f"verified finding {f['id']} did not become a pinned fact")

# every decision reference resolves
def dec_refs(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("decision", "decided_by", "opened_by") and isinstance(v, str):
                yield v
            else:
                yield from dec_refs(v)
    elif isinstance(o, list):
        for v in o:
            yield from dec_refs(v)
for src in (data, facts, findings):
    for d in dec_refs(src):
        if d not in decision_ids:
            errors.append(f"unknown decision {d}")

# origin URI consistent with entity/key/version/layer
for f in facts["facts"]:
    ent = f["entity"]
    etype, _, rest = ent.partition("/")
    path = rest if etype == f["layer"] else ent
    want = f"fact://{f['layer']}/{path}/{f['key']}@{f['version']}"
    if f["origin"] != want:
        errors.append(f"{f['id']}: origin {f['origin']} != {want}")

# projection mirrors members
pr = data.get("projection", {})
if pr.get("built_from", {}).get("facts_sha256") != sha(facts_p) or pr.get("built_from", {}).get("findings_sha256") != sha(findings_p):
    errors.append("projection.built_from does not match member hashes")
if set(pr.get("pins", {})) != pin_ids:
    errors.append("projection.pins does not mirror facts.yaml")

# selection coverage merge rule
cbm = data["selection"]["coverage_by_market"]
for lens, merged in data["selection"]["coverage"].items():
    vals = [cbm[m][lens] for m in cbm if lens in cbm[m]]  # a skipped lens x market has no coverage
    want = "filled" if all(v == "filled" for v in vals) else "empty" if all(v == "empty" for v in vals) else "thin"
    if merged != want:
        errors.append(f"coverage.{lens}: {merged} but per-market merge gives {want}")

# --- chat intake (Contract 3 §16) ----------------------------------------------
DOC = "7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17"
materials = data.get("materials", {})
msgs = data.get("intake", {}).get("messages", {})
ordered = sorted(msgs.items(), key=lambda kv: (kv[1]["at"], kv[0]))
asked = {}  # question id -> (message at, question, job_id)
for mid, m in ordered:
    for a in m.get("attachments", []):
        if a not in materials:
            errors.append(f"intake {mid}: attachment {a} is not a material")
    if "material_id" in m:
        mat = materials.get(m["material_id"])
        if mat is None or mat["kind"] != "prompt":
            errors.append(f"intake {mid}: material_id {m['material_id']} is not a prompt material")
        elif mat["sha256"] != "sha256:" + hashlib.sha256(m["text"].encode("utf-8")).hexdigest():
            errors.append(f"intake {mid}: prompt material {m['material_id']} sha256 is not the hash of the message text")
    q = m.get("question")
    if q:
        if q["id"] in asked:
            errors.append(f"intake {mid}: question id {q['id']} asked twice")
        asked[q["id"]] = (m["at"], q, m["job_id"])
        field = q.get("address", "").partition("#campaign.")[2]
        if q.get("address") and (not q["address"].startswith(DOC + "#") or field not in camp):
            errors.append(f"question {q['id']}: address {q.get('address')} is not a campaign field of this document")
        if len({o["id"] for o in q["options"]}) != len(q["options"]):
            errors.append(f"question {q['id']}: option ids repeat")
        for o in q["options"]:
            if "value" in o and field in camp:
                ve = list(jsonschema.Draft7Validator(dict(camp[field]["properties"]["value"], definitions=schema["definitions"])).iter_errors(o["value"]))
                if ve:
                    errors.append(f"question {q['id']}: option {o['id']} value is not a campaign.{field} value: {ve[0].message[:120]}")
            for fid in o.get("fact_ids", []):
                if fid not in pin_ids:
                    errors.append(f"question {q['id']}: option {o['id']} cites unpinned fact {fid}")
            if "source" in o and o["source"]["material_id"] not in materials:
                errors.append(f"question {q['id']}: option {o['id']} cites unknown material")
    ans = m.get("answer")
    if ans:
        if ans["question_id"] not in asked:
            errors.append(f"intake {mid}: answers {ans['question_id']}, which no earlier agent message asked")
            continue
        _, q, job = asked[ans["question_id"]]
        if m.get("job_id") != job:
            errors.append(f"intake {mid}: answer names job {m.get('job_id')}, the question was job {job}")
        if "option_id" in ans:
            opt = next((o for o in q["options"] if o["id"] == ans["option_id"]), None)
            if opt is None or "value" not in opt:
                errors.append(f"intake {mid}: option {ans['option_id']} is not a candidate of {q['id']} (an escape is answered with text)")
            elif q.get("address"):
                env = data["campaign"].get(q["address"].partition("#campaign.")[2], {})
                want_origin = "stated" if opt["origin"] == "stated" else "confirmed"
                if env.get("value") != opt["value"] or env.get("origin") != want_origin or \
                        (want_origin == "confirmed" and env.get("confirmed_from") != opt["origin"]):
                    errors.append(f"intake {mid}: the answered field {q['address']} does not hold the picked option as a {want_origin} value")
        elif not q["allow_text"]:
            errors.append(f"intake {mid}: text answer to {q['id']}, which does not allow text")
for d in load(os.path.join(EX, "agent/decision-chain.yaml"))["decisions"]:
    for tg in d.get("targets", []):
        mm = re.fullmatch(re.escape(DOC) + r"#intake\.messages\[(.+)\]", tg)
        if mm and mm.group(1) not in msgs:
            errors.append(f"decision {d['id']} targets unknown message {mm.group(1)}")

# --- selection.lenses_skipped: skipped is not empty ---------------------------
skipped = data["selection"].get("lenses_skipped", [])
ran = {(r["lens"], r["market"]) for r in data["selection"]["lenses_run"]}
for s in skipped:
    for (lens, mkt) in ran:
        if lens == s["lens"] and s.get("market") in (None, mkt):
            errors.append(f"lens {lens}/{mkt} is both skipped and run")
    for mkt, cov in cbm.items():
        if s["lens"] in cov and s.get("market") in (None, mkt):
            errors.append(f"lens {s['lens']}/{mkt} is skipped but has coverage")

# --- report (Contract 3 §17) ---------------------------------------------------
rpt = data.get("report")
if rpt:
    gap_ids = {g["id"] for g in data["selection"]["gaps"]}
    contest_ids = {c["id"] for c in data["selection"]["contested"]}
    pin_by_id = {f["id"]: f for f in facts["facts"]}
    names = [data["campaign"]["name"]["value"], data["campaign"]["brand"]["value"]["name"]] + \
        [c["name"] for c in data["campaign"].get("competitor_set", {}).get("value", [])]
    NUM = re.compile(r"\d+(?:[.,]\d+)?")

    def figures_ok(text, cites, where):
        allowed = set()
        for c in cites:
            if c in pin_by_id:
                v = pin_by_id[c]["value"]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    allowed |= {repr(v), f"{v:g}"}
                    if pin_by_id[c]["unit"] == "proportion":
                        allowed |= {f"{v * 100:g}", f"{round(v * 100)}"}
                else:
                    allowed |= set(NUM.findall(str(v)))
            elif c in finding_by_id:
                allowed |= set(NUM.findall(finding_by_id[c]["statement"]))
        for n in names:
            allowed |= set(NUM.findall(n))
        for n in NUM.findall(text):
            if n not in allowed:
                errors.append(f"report {where}: states {n}, which no cited pin or finding holds")

    def claim(line, where):
        if not line.get("cites"):
            errors.append(f"report {where}: a claim without a cite")
        for c in line.get("cites", []):
            if c not in pin_ids and c not in finding_by_id:
                errors.append(f"report {where}: cite {c} is neither a pin nor a finding")
            elif c in finding_by_id and finding_by_id[c]["status"] == "rejected":
                errors.append(f"report {where}: cites rejected finding {c}")
        figures_ok(line["text"], line.get("cites", []), where)

    claim(rpt["headline"], "headline")
    for i, s in enumerate(rpt["summary"]):
        claim(s, f"summary[{i}]")
    sec_ids = [s["id"] for s in rpt["sections"]]
    if len(set(sec_ids)) != len(sec_ids):
        errors.append("report: section ids repeat")
    for s in rpt["sections"]:
        for i, b in enumerate(s["blocks"]):
            w = f"{s['id']}[{i}]"
            k = b["kind"]
            if k == "claim":
                claim(b, w)
            elif k == "pins":
                for f in b["fact_ids"]:
                    if f not in pin_ids:
                        errors.append(f"report {w}: pins block names unpinned fact {f}")
            elif k == "finding":
                f = finding_by_id.get(b["finding_id"])
                if f is None or f["status"] == "rejected":
                    errors.append(f"report {w}: finding {b['finding_id']} is unknown or rejected")
            elif k == "gap" and b["gap_id"] not in gap_ids:
                errors.append(f"report {w}: unknown gap {b['gap_id']}")
            elif k == "contest" and b["contest_id"] not in contest_ids:
                errors.append(f"report {w}: unknown contest {b['contest_id']}")
    # the example's report is current: composed after the last change to facts and findings
    bo = rpt["based_on"]
    if bo["facts_sha256"] != sha(facts_p) or bo["findings_sha256"] != sha(findings_p):
        errors.append("report.based_on hashes do not match the members (the example's report must be current)")
    # the confirm list: D3's short list while unconfirmed, then every other proposed field
    D3 = ["brand", "categories", "markets", "competitor_set"]
    want = [f for f in D3 if data["campaign"].get(f, {}).get("origin") in ("extracted", "proposed")] + \
        [f for f, e in data["campaign"].items() if f not in D3 and e["origin"] == "proposed"]
    got = [c["address"].partition("#campaign.")[2] for c in rpt["confirm"]]
    if any(not c["address"].startswith(DOC + "#") for c in rpt["confirm"]) or sorted(got) != sorted(want):
        errors.append(f"report.confirm {got} != the fields to confirm {want}")
    nr = {(n["lens"], n.get("market")) for n in rpt["not_researched"]}
    for s in skipped:
        if (s["lens"], s.get("market")) not in nr:
            errors.append(f"report.not_researched omits skipped lens {s['lens']}/{s.get('market')}")

# fields citing a rejected finding (the view flags these)
flagged = [n for n, e in data["campaign"].items() if any(finding_by_id[i]["status"] == "rejected" for i in e.get("finding_ids", []))]

# cold start: template data {} validates, and zero pins validates
jsonschema.Draft7Validator(schema).validate({})
jsonschema.Draft7Validator(json.load(open(os.path.join(T, "facts.schema.json")))).validate({"facts": []})

origins = {}
for n, e in data["campaign"].items():
    origins.setdefault(e["origin"], []).append(n)
print("origins:", {k: len(v) for k, v in origins.items()})
print("pins:", len(pin_ids), "stale:", sum(1 for f in facts["facts"] if "stale" in f),
      "layers:", sorted({f["layer"] for f in facts["facts"]}))
print("findings:", {s: sum(1 for f in findings["findings"] if f["status"] == s) for s in ("proposed", "verified", "rejected")})
print("flagged fields:", flagged)
print("contests:", [(c["id"], c["status"]) for c in data["selection"]["contested"]])
print("intake:", len(msgs), "messages,", len(asked), "question(s),", sum(1 for m in msgs.values() if "answer" in m), "answer(s)")
print("lenses skipped:", [f"{s['lens']}/{s.get('market', '*')}" for s in skipped])
if rpt:
    print("report:", len(rpt["sections"]), "sections,", sum(1 for s in rpt["sections"] for b in s["blocks"] if b["kind"] == "claim") + 1 + len(rpt["summary"]),
          "claims, all cited;", len(rpt["confirm"]), "to confirm")
print("cold start ({} data, zero pins): OK")
if errors:
    print("\nERRORS:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("ALL CHECKS PASS")
