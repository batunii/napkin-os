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
    vals = [cbm[m][lens] for m in cbm]
    want = "filled" if all(v == "filled" for v in vals) else "empty" if all(v == "empty" for v in vals) else "thin"
    if merged != want:
        errors.append(f"coverage.{lens}: {merged} but per-market merge gives {want}")

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
print("cold start ({} data, zero pins): OK")
if errors:
    print("\nERRORS:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("ALL CHECKS PASS")
