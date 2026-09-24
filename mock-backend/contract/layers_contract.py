#!/usr/bin/env python3
"""Contract suite for the layers port — napkin.layers/1 (contract 5, §4, §8.4).

    python3 layers_contract.py --base-url http://127.0.0.1:8797 [--token T]

Runs unchanged against any implementation: the mock backend's SQLite layers or
the real layers service. No live model calls; runs in seconds. Every run uses
fresh orgs, brands, keys and decision ids (a random run id), so it can be
re-run against a persistent store. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite import Client, Result, error_type, is_error_shape  # noqa: E402

ORIGIN_RE = re.compile(r"^fact://(brand|category)/.+/[a-z0-9_]+(\.[a-z0-9_]+)*@[1-9][0-9]*$")
LEAF_KEYS = {"code", "name", "vertical", "vertical_name", "aliases", "regulated", "provisional"}
ROW_KEYS = {"id", "layer", "entity", "key", "market", "value", "unit", "as_of", "retrieved_at", "status", "version",
            "supersedes", "licence", "method", "decision", "origin", "sources", "source_records"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token")
    ap.add_argument("--timeout", type=float, default=30)
    a = ap.parse_args()
    c = Client(a.base_url, a.token, a.timeout)
    r = Result()
    run = secrets.token_hex(4)
    A, B = f"org/ct-{run}-a", f"org/ct-{run}-b"
    X, Y = f"brand/ct-{run}-x", f"brand/ct-{run}-y"
    n = [0]

    def dec(**kw):
        n[0] += 1
        d = {"id": f"d_CT{run.upper()}{n[0]:04d}", "kind": "pin", "handler": "layers_contract@1.0",
             "action": "contract", "rationale": "contract suite", "cites": [],
             "reasoning": {"decided": "a contract check", "because": [{"point": "the suite", "cites": []}]}}
        d.update(kw)
        return d

    def hdr(org=A, brand=None, key=None):
        h = {"X-Napkin-Handler": "layers_contract@1.0", "X-Napkin-Job": "-"}
        if org:
            h["X-Napkin-Org"] = org
        if brand:
            h["X-Napkin-Brand"] = brand
        if key is not None:
            h["Idempotency-Key"] = key
        return h

    def get(path, **kw):
        return c.call("GET", "/v1/layers" + path, headers=hdr(**kw))

    def post(path, body, **kw):
        return c.call("POST", "/v1/layers" + path, body, headers=hdr(**kw))

    def put(path, body, **kw):
        return c.call("PUT", "/v1/layers" + path, body, headers=hdr(**kw))

    def key():
        return "ct-" + secrets.token_hex(12)

    def append(fact, decision, org=A, brand=None, k=None):
        return post("/facts", {"fact": fact, "decision": decision}, org=org, brand=brand,
                    key=key() if k is None else k)

    def fact(value, as_of="2025-12-31", **kw):
        f = {"layer": "category", "entity": "category/automotive.ev_charging", "key": f"contract_{run}.bev_share",
             "market": "IE", "value": value, "unit": "proportion", "as_of": as_of, "retrieved_at": "2026-09-20",
             "sources": [], "licence": "open", "method": "report"}
        f.update(kw)
        return f

    r.section("health")
    st, body, _, _ = c.call("GET", "/healthz")
    r.check(st == 200 and isinstance(body, dict) and body.get("ok") is True, f"GET /healthz -> {st}")

    r.section("categories")
    st, body, _, _ = get("/categories")
    leaves = body.get("leaves") if isinstance(body, dict) else None
    r.check(st == 200 and isinstance(leaves, list) and leaves, f"GET categories -> {st}, leaves non-empty")
    r.check(isinstance(body, dict) and isinstance(body.get("taxonomy_version"), str), "taxonomy_version is a string")
    r.check(bool(leaves) and all(isinstance(l, dict) and set(l) == LEAF_KEYS for l in leaves),
            "every leaf has exactly the documented keys")
    codes = [l["code"] for l in leaves or []]
    r.check(len(codes) == len(set(codes)), "leaf codes unique")
    if leaves:
        leaf = leaves[0]["code"]
        st, v, _, _ = get(f"/categories/{leaf}/vertical")
        r.check(st == 200 and isinstance(v, dict) and leaf in (v.get("leaves") or [])
                and v.get("code") == leaves[0]["vertical"], f"vertical_of({leaf}) lists it -> {st}")
    st, body, _, _ = get("/categories/nosuch.leaf/vertical")
    r.check(st == 404 and error_type(body) == "unknown_leaf", f"unknown leaf -> {st} {error_type(body)}")
    st, body, _, _ = post("/categories/find", {"text": "cider"})
    r.check(st == 200 and "alcohol.cider" in (body or {}).get("leaves", []), f"find('cider') finds alcohol.cider")

    r.section("scope comes from headers only")
    st, body, _, _ = get("/categories", org=None)
    r.check(st == 400 and error_type(body) == "missing_scope", f"no X-Napkin-Org -> {st} {error_type(body)}")
    st, body, _, _ = get(f"/facts?layer=brand&entity={X}")
    r.check(st == 400 and error_type(body) == "missing_scope", f"brand layer without X-Napkin-Brand -> {st}")
    st, body, _, _ = post("/categories/find", {"text": "cider", "org": B})
    r.check(st == 400 and error_type(body) == "invalid_input", f"`org` in the body -> {st} {error_type(body)}")
    st, body, _, _ = append({**fact(0.1), "layer": "brand", "entity": X}, dec(), brand=None)
    r.check(st == 400 and error_type(body) == "missing_scope", f"brand-layer append without the brand header -> {st}")
    r.check(is_error_shape(body), "errors are {error: {type, message}}")

    r.section("sources")
    uri1, uri2 = f"https://example.org/ct/{run}/one", f"https://example.org/ct/{run}/two"
    st, s1, _, _ = post("/sources", {"source": {"uri": uri1, "tier": "primary", "domain": "example.org",
                                                "licence": "open", "title": "One", "publisher": "Ex"}}, key=key())
    r.check(st == 200 and isinstance(s1, dict) and str(s1.get("id", "")).startswith("src_")
            and s1.get("created") is True, f"add_source -> {st} {s1}")
    st, again, _, _ = post("/sources", {"source": {"uri": uri1, "tier": "secondary", "domain": "example.org",
                                                   "licence": "open"}}, key=key())
    r.check(st == 200 and again == {"id": (s1 or {}).get("id"), "created": False},
            "an existing URI returns its id, created: false")
    st, s2, _, _ = post("/sources", {"source": {"uri": uri2, "tier": "secondary", "domain": "example.org",
                                                "licence": "open"}}, key=key())
    src1, src2 = (s1 or {}).get("id"), (s2 or {}).get("id")
    st, body, _, _ = post("/sources", {"source": {"uri": f"https://example.org/ct/{run}/nolicence",
                                                  "tier": "primary", "domain": "example.org"}}, key=key())
    r.check(st == 400 and error_type(body) == "invalid_input", f"a source without licence -> {st}")
    st, conf, _, _ = post("/sources", {"source": {"uri": f"human:ct-{run}", "tier": "reviewer-verified",
                                                  "domain": "", "licence": "client-confidential"}}, key=key())
    cid = (conf or {}).get("id")
    st, body, _, _ = get(f"/sources?ids={src1},{cid}")
    r.check(st == 200 and [s["id"] for s in (body or {}).get("sources", [])] == [src1, cid],
            "sources(ids) returns them in request order to the org that added the confidential one")
    st, body, _, _ = get(f"/sources?ids={cid},{src1},src_nosuchsource", org=B)
    r.check(st == 200 and [s["id"] for s in (body or {}).get("sources", [])] == [src1],
            "a client-confidential source is invisible to another org; unknown ids omitted")

    r.section("scope isolation")
    bf = {**fact("hello"), "layer": "brand", "entity": X, "key": f"contract_{run}.tagline", "market": None,
          "unit": "text", "licence": "client-confidential"}
    st, body, _, _ = append(bf, dec(), brand=X)
    r.check(st == 200 and (body or {}).get("outcome") == "created", f"brand fact under (A, X) -> {st}")
    q = f"/facts?layer=brand&entity={X}&key=contract_{run}.tagline"
    st, body, _, _ = get(q, brand=X)
    r.check(st == 200 and len((body or {}).get("facts", [])) == 1, "visible under (A, X)")
    st, body, _, _ = get(q, brand=Y)
    r.check(st == 200 and (body or {}).get("facts") == [], "invisible under (A, Y)")
    st, body, _, _ = get(q, org=B, brand=X)
    r.check(st == 200 and (body or {}).get("facts") == [], "invisible under (B, X)")

    r.section("append: the four outcomes on one identity (§4.4)")
    ent = "category/automotive.ev_charging"
    fq = f"/facts?layer=category&entity={ent}&key=contract_{run}.bev_share"
    d1 = dec()
    st, r1, _, _ = append(fact(0.2, sources=[src1], quotes={src1: "twenty per cent"}), d1)
    row1 = (r1 or {}).get("fact") or {}
    r.check(st == 200 and (r1 or {}).get("outcome") == "created" and row1.get("version") == 1
            and row1.get("status") == "active", f"created -> {st} {(r1 or {}).get('outcome')}")
    r.check(set(row1) == ROW_KEYS, "a fact row has exactly the documented keys")
    r.check(ORIGIN_RE.match(str(row1.get("origin", ""))) is not None, f"origin matches the URI grammar: "
                                                                     f"{row1.get('origin')}")
    r.check(row1.get("decision") == d1["id"], "the row names its decision")
    st, body, _, _ = get(fq, org=B)
    r.check(st == 200 and len((body or {}).get("facts", [])) == 1, "a category fact written under A is visible "
                                                                   "under B")
    st, r2, _, _ = append(fact(0.2, sources=[src2]), dec())
    row2 = (r2 or {}).get("fact") or {}
    r.check(st == 200 and (r2 or {}).get("outcome") == "corroborated" and row2.get("id") == row1.get("id")
            and set(row2.get("sources", [])) == {src1, src2}, "corroborated: same row, the second source linked")
    st, r3, _, _ = append(fact(0.25, as_of="2026-06-30", sources=[src1]), dec())
    row3 = (r3 or {}).get("fact") or {}
    r.check(st == 200 and (r3 or {}).get("outcome") == "superseded" and row3.get("version") == 2
            and row3.get("supersedes") == row1.get("id") and row3.get("status") == "active",
            f"superseded with a later as_of -> {(r3 or {}).get('outcome')} v{row3.get('version')}")
    st, body, _, _ = get(fq)
    r.check([f["id"] for f in (body or {}).get("facts", [])] == [row3.get("id")], "facts() returns only the current row")
    st, r4, _, _ = append(fact(0.3, as_of="2026-01-01", sources=[src2]), dec())
    row4 = (r4 or {}).get("fact") or {}
    r.check(st == 200 and (r4 or {}).get("outcome") == "contested" and row4.get("status") == "contested"
            and row4.get("version") == 3, f"contested with an earlier as_of -> {(r4 or {}).get('outcome')}")
    st, body, _, _ = get(fq)
    rows = (body or {}).get("facts", [])
    r.check(len(rows) == 2 and {f["status"] for f in rows} == {"contested"}, "both current rows are contested")
    st, r5, _, _ = append(fact(0.4, market="GB"), dec())
    r.check(st == 200 and ((r5 or {}).get("fact") or {}).get("version") == 4,
            "versions count per entity + key across markets")
    st, body, _, _ = get(fq + "&market=GB")
    r.check(st == 200 and [f["market"] for f in (body or {}).get("facts", [])] == ["GB"],
            "market filter: that market (or market-independent)")

    r.section("resolve")
    st, body, _, _ = get(f"/facts/by-uri?uri={row1.get('origin')}")
    got = (body or {}).get("fact") or {}
    r.check(st == 200 and got.get("id") == row1.get("id") and got.get("status") == "superseded"
            and got.get("value") == 0.2, "resolve returns the exact version, superseded, with its status")
    st, body, _, _ = get(f"/facts/by-uri?uri=fact://category/automotive.ev_charging/contract_{run}.nosuch@1")
    r.check(st == 404 and error_type(body) == "unknown_fact", f"unknown URI -> {st} {error_type(body)}")
    st, body, _, _ = get(f"/facts/by-uri?uri=fact://brand/ct-{run}-x/contract_{run}.tagline@1", brand=X)
    r.check(st == 200, "a brand-layer URI resolves under the scope that wrote it")
    st, body, _, _ = get(f"/facts/by-uri?uri=fact://brand/ct-{run}-x/contract_{run}.tagline@1", org=B, brand=X)
    r.check(st == 404, "and not under another org")

    r.section("validation, and atomicity: a refused append leaves no decision")
    bad_cases = {
        "missing licence": ({k: v for k, v in fact(0.5).items() if k != "licence"}, "invalid_input"),
        "object value": (fact({"a": 1}), "invalid_input"),
        "null value": (fact(None), "invalid_input"),
        "bad entity": (fact(0.5, entity="Category/EV"), "invalid_input"),
        "bad key": (fact(0.5, key="Bad Key"), "invalid_input"),
        "bad market": (fact(0.5, market="Ireland"), "invalid_input"),
        "status other than contested": (fact(0.5, status="active"), "invalid_input"),
        "quote for a source not cited": (fact(0.5, quotes={src1: "x"}), "invalid_input"),
        "unknown source id": (fact(0.5, sources=["src_nosuchsource"]), "invalid_input"),
        "another org's confidential source": (fact(0.5, sources=[cid]), None),
        "unknown leaf": (fact(0.5, entity="category/automotive.nosuch"), "unknown_leaf"),
    }
    for name, (f, want) in bad_cases.items():
        d = dec()
        org = B if name == "another org's confidential source" else A
        st, body, _, _ = append(f, d, org=org)
        r.check(st == 400 and (want is None or error_type(body) == want), f"{name} -> {st} {error_type(body)}")
        st2, _, _, _ = get(f"/decisions/{d['id']}", org=org)
        r.check(st2 == 404, f"  ...and GET /decisions/<its id> -> {st2}")
    d = dec()
    st, body, _, _ = c.call("POST", "/v1/layers/facts", {"fact": fact(0.5), "decision": d}, headers=hdr())
    r.check(st == 400 and error_type(body) == "invalid_input", f"a write with no Idempotency-Key -> {st}")
    st, body, _, _ = get(f"/decisions/{d1['id']}")
    r.check(st == 200 and (body or {}).get("decision") == d1, "a stored decision comes back whole, reasoning included")
    st, body, _, _ = get(f"/decisions/{d1['id']}", org=B)
    r.check(st == 404 and error_type(body) == "unknown_decision", "a decision is visible only in its scope")

    r.section("idempotency")
    k1 = key()
    body1 = fact(0.6, as_of="2026-09-01", key=f"contract_{run}.idem")
    dd = dec()
    st, first, h1, _ = append(body1, dd, k=k1)
    st2, second, h2, _ = append(body1, dd, k=k1)
    r.check(st == 200 and st2 == 200 and first == second, "a replay returns the first response")
    r.check(h2.get("idempotent-replay") == "true" and h1.get("idempotent-replay") != "true",
            "the replay carries Idempotent-Replay: true (and the first does not)")
    st, body, _, _ = get(f"/facts?layer=category&entity={ent}&key=contract_{run}.idem")
    r.check(st == 200 and [f["version"] for f in (body or {}).get("facts", [])] == [1], "no new version on replay")
    st, body, _, _ = append({**body1, "value": 0.7}, dd, k=k1)
    r.check(st == 409 and error_type(body) == "idempotency_conflict", f"same key, different body -> {st}")

    r.section("roster")
    ref = f"brand/ct-{run}-bmw"
    rd = dec(kind="roster")
    st, body, _, _ = put(f"/roster/{ref}", {"name": "CT BMW", "categories": ["automotive.ev_charging",
                                                                           "automotive.hybrid"],
                                            "client_org": f"org/ct-{run}-client", "sources": [src1],
                                            "decision": rd}, brand=X, key=key())
    r.check(st == 200 and len((body or {}).get("facts", [])) == 3, f"PUT roster -> {st}, three facts")
    st, ro, _, _ = get(f"/roster/{ref}", brand=X)
    r.check(st == 200 and (ro or {}).get("categories") == ["automotive.ev_charging", "automotive.hybrid"]
            and (ro or {}).get("client_org") == f"org/ct-{run}-client" and (ro or {}).get("name") == "CT BMW"
            and (ro or {}).get("ref") == ref, "GET roster round-trips name, categories and client")
    r.check(bool((ro or {}).get("facts")) and all(f["decision"] == rd["id"] and f["key"].startswith("roster.")
                                                    for f in ro["facts"]), "the roster's facts carry the decision")
    st, body, _, _ = get(f"/roster/{ref}", brand=Y)
    r.check(st == 404 and error_type(body) == "unknown_brand", "another brand scope does not see it")
    st, body, _, _ = get(f"/roster/brand/ct-{run}-nobody", brand=X)
    r.check(st == 404 and error_type(body) == "unknown_brand", f"unknown brand -> {st} {error_type(body)}")
    rd2 = dec(kind="roster")
    st, body, _, _ = put(f"/roster/brand/ct-{run}-bad", {"name": "Bad", "categories": ["automotive.nosuch"],
                                                          "sources": [], "decision": rd2}, brand=X, key=key())
    r.check(st == 400 and error_type(body) == "unknown_leaf", f"a roster with an unknown leaf -> {st}")
    st, _, _, _ = get(f"/decisions/{rd2['id']}", brand=X)
    r.check(st == 404, "  ...and wrote no decision (atomic)")
    st, body, _, _ = post("/brands/find", {"text": "ct bmw"})
    r.check(st == 200 and {"ref": ref, "name": "CT BMW"} in (body or {}).get("brands", []),
            "find_brands finds the rostered brand in its org")
    st, body, _, _ = post("/brands/find", {"text": "ct bmw"}, org=B)
    r.check(st == 200 and (body or {}).get("brands") == [], "and not in another org")
    st, body, _, _ = put(f"/brands/brand/ct-{run}-lunasa", {"name": "Lunasa"}, key=key())
    r.check(st == 200 and body == {"ref": f"brand/ct-{run}-lunasa", "name": "Lunasa"}, f"note_brand -> {st}")

    return r.report(f"layers contract against {a.base_url}")


if __name__ == "__main__":
    sys.exit(main())
