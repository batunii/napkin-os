#!/usr/bin/env python3
"""Contract suite for the retrieval port — napkin.retrieval/1 (contract 5, §3, §8.3).

    python3 retrieval_contract.py --base-url http://127.0.0.1:8797 --org org/dev-agency
    python3 retrieval_contract.py --base-url URL --org org/a --agency-pack acme-cases \\
        --agency-owner org/a --other-org org/b                    # the S6 checks
    python3 retrieval_contract.py --base-url URL --org org/a --no-live   # spends nothing

    --token T          bearer token, when the service is configured with one
    --query TEXT       the live query (default: an insight question)
    --packs a,b        the packs the live retrieve names (default: every listed house pack)

Names no implementation: the passage id and uri are recomputed here from the
§3.2 formula, so a real service and the mock are held to the same identity.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite import Client, Result, error_type, is_error_shape  # noqa: E402

PACK_KEYS = {"tag", "id", "kind", "scope", "licence", "k", "loops", "passages", "filterable", "version"}
PASSAGE_KEYS = {"id", "uri", "pack", "scope", "licence", "source", "section", "citation", "text", "text_sha256",
                "truncated", "rank", "score", "metadata"}
VERSION_RE = re.compile(r"^sha256:[0-9a-f]{8,64}$")
DEFAULT_QUERY = "find the human insight and cultural tension for midweek drinkers cutting back"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def identity(p: dict) -> tuple[str, str, str]:
    sha = hashlib.sha256(p["text"].encode("utf-8")).hexdigest()
    pid = "psg_" + hashlib.sha256("\n".join([p["pack"], p["source"], p["section"], p["text"]])
                                  .encode("utf-8")).hexdigest()[:20]
    uri = f"passage://{p['pack']}/{p['source']}#{slug(p['section'])}@{sha[:16]}"
    return pid, uri, sha


def check_retrieve(r: Result, resp, k: int, asked: list, label: str) -> list:
    if not r.check(isinstance(resp, dict) and isinstance(resp.get("passages"), list)
                   and isinstance(resp.get("trace"), dict), f"{label}: {{passages, trace}}"):
        return []
    ps = resp["passages"]
    r.check("error" not in resp, f"{label}: a 200 carries no error key")
    r.check(len(ps) <= k, f"{label}: {len(ps)} passages <= k {k}")
    r.check([p.get("rank") for p in ps] == list(range(1, len(ps) + 1)), f"{label}: rank is 1..n")
    scores = [p.get("score") for p in ps]
    r.check(all(isinstance(s, (int, float)) for s in scores) and all(a >= b for a, b in zip(scores, scores[1:])),
            f"{label}: score non-increasing")
    bad = []
    for i, p in enumerate(ps):
        w = f"passages[{i}]"
        if set(p) != PASSAGE_KEYS:
            bad.append(f"{w} keys {sorted(set(p) ^ PASSAGE_KEYS)}")
            continue
        if not (isinstance(p["text"], str) and p["text"] and len(p["text"]) <= 4000):
            bad.append(f"{w}.text")
            continue
        pid, uri, sha = identity(p)
        if p["text_sha256"] != sha:
            bad.append(f"{w}.text_sha256")
        if p["id"] != pid:
            bad.append(f"{w}.id does not recompute")
        if p["uri"] != uri:
            bad.append(f"{w}.uri does not recompute ({p['uri']} != {uri})")
        if p["citation"] != f"{p['source']} › {p['section']}":
            bad.append(f"{w}.citation")
        if p["pack"] not in asked:
            bad.append(f"{w}.pack {p['pack']} was not asked for")
        if p["scope"] not in ("house", "agency") or p["licence"] not in ("open", "licensed-internal",
                                                                          "client-confidential"):
            bad.append(f"{w}.scope/licence")
        if not isinstance(p["truncated"], bool) or not isinstance(p["metadata"], dict):
            bad.append(f"{w}.truncated/metadata")
    r.check(not bad, f"{label}: every passage well-formed, id/uri/sha recompute" +
            (f" ({'; '.join(bad[:4])})" if bad else ""))
    tp = resp["trace"].get("packs")
    r.check(isinstance(resp["trace"].get("backend"), str) and isinstance(tp, dict)
            and all(t in tp and VERSION_RE.match(str(tp[t])) for t in asked),
            f"{label}: trace.backend, and trace.packs has a version per searched pack")
    return ps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--org", required=True)
    ap.add_argument("--token")
    ap.add_argument("--agency-pack")
    ap.add_argument("--agency-owner")
    ap.add_argument("--other-org")
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--packs")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args()
    c = Client(a.base_url, a.token, a.timeout)
    r = Result()

    def hdr(org):
        h = {"X-Napkin-Handler": "retrieval_contract@1.0", "X-Napkin-Job": "-"}
        if org:
            h["X-Napkin-Org"] = org
        return h

    def packs(org):
        return c.call("GET", "/v1/packs", headers=hdr(org))

    def retrieve(body, org=a.org):
        return c.call("POST", "/v1/retrieve", body, headers=hdr(org))

    r.section("health")
    st, body, _, _ = c.call("GET", "/healthz", timeout=10)
    r.check(st == 200 and isinstance(body, dict) and body.get("ok") is True, f"GET /healthz -> {st}")

    r.section("GET /v1/packs")
    st, body, _, _ = packs(a.org)
    listed = (body or {}).get("packs") if isinstance(body, dict) else None
    r.check(st == 200 and isinstance(listed, list) and set(body) == {"packs", "embed_model", "backend"},
            f"-> {st}, {{packs, embed_model, backend}}")
    listed = listed or []
    r.check(all(isinstance(p, dict) and set(p) == PACK_KEYS for p in listed), "every pack has the documented keys")
    r.check(all(VERSION_RE.match(str(p.get("version"))) for p in listed), "every pack has a version")
    tags = [p.get("tag") for p in listed]
    r.check(len(tags) == len(set(tags)), "tags unique")
    r.check(all(p.get("kind") in ("case", "playbook", "template", "digest") and p.get("scope") in ("house", "agency")
                for p in listed), "kind and scope from the documented sets")
    house = [p["tag"] for p in listed if p.get("scope") == "house"]
    asked = a.packs.split(",") if a.packs else house[:5]

    r.section("scope and closed requests")
    st, body, _, _ = packs(None)
    r.check(st == 400 and error_type(body) == "missing_scope", f"GET /v1/packs without X-Napkin-Org -> {st}")
    st, body, _, _ = retrieve({"query": "x", "k": 1}, org=None)
    r.check(st == 400 and error_type(body) == "missing_scope", f"retrieve without X-Napkin-Org -> {st}")
    st, body, _, _ = retrieve({"query": "x", "k": 1, "scope": "house"})
    r.check(st == 400 and error_type(body) == "invalid_input", f"`scope` in the body -> {st}")
    for name, extra in (("fill", {"fill": "insight"}), ("document", {"document": {}})):
        st, body, _, _ = retrieve({"query": "x", "k": 1, **extra})
        r.check(st == 400 and error_type(body) == "invalid_input" and is_error_shape(body),
                f"unknown field `{name}` -> {st}")
    for k in (0, 21, "3"):
        st, body, _, _ = retrieve({"query": "x", "k": k})
        r.check(st == 400 and error_type(body) == "invalid_input", f"k={k!r} -> {st}")
    st, body, _, _ = retrieve({"query": "", "k": 1})
    r.check(st == 400, f"empty query -> {st}")
    if asked:
        st, body, _, _ = retrieve({"query": "x", "k": 1, "packs": asked[:1],
                                   "where": {f"zz_not_filterable_{secrets.token_hex(3)}": "x"}})
        r.check(st == 400 and error_type(body) == "invalid_input", f"where on a key not filterable -> {st}")
    st, body, _, _ = retrieve({"query": "x", "k": 1, "packs": [f"no-such-pack-{secrets.token_hex(3)}"]})
    r.check(st == 404 and error_type(body) == "unknown_pack", f"an unknown pack -> {st} {error_type(body)}")

    s6 = bool(a.agency_pack and a.agency_owner and a.other_org)
    if s6:
        r.section("S6: agency packs are visible only to their owner")
        st, body, _, _ = packs(a.agency_owner)
        mine = [p for p in (body or {}).get("packs", []) if p.get("tag") == a.agency_pack]
        r.check(st == 200 and len(mine) == 1 and mine[0].get("scope") == "agency",
                f"the owner sees {a.agency_pack} with scope agency")
        st, body, _, _ = packs(a.other_org)
        r.check(st == 200 and a.agency_pack not in [p.get("tag") for p in (body or {}).get("packs", [])],
                "another org does not see it listed")
        st, body, _, _ = retrieve({"query": "x", "k": 1, "packs": [a.agency_pack]}, org=a.other_org)
        r.check(st == 404 and error_type(body) == "unknown_pack", f"another org naming it -> {st} (never 403)")

    if a.no_live:
        return r.report(f"retrieval contract against {a.base_url} [no live]")

    r.section(f"live retrieve over {asked}")
    k = 3
    st, first, _, dt = retrieve({"query": a.query, "k": k, "packs": asked, "purpose": "loop4_insight"})
    print(f"  ..    {st} in {dt:.1f}s")
    ps1 = []
    if r.check(st == 200, f"retrieve -> {st}" + ("" if st == 200 else f" {first}")):
        ps1 = check_retrieve(r, first, k, asked, "live")
        for p in ps1:
            print(f"  psg   {p['citation']}  {p['text'][:70]!r}")
    st, second, _, dt = retrieve({"query": a.query, "k": k + 1, "packs": asked})
    if r.check(st == 200, f"a second retrieve -> {st} in {dt:.1f}s"):
        ps2 = check_retrieve(r, second, k + 1, asked, "second")
        by = {(p["pack"], p["source"], p["section"], p["text"]): p["id"] for p in ps1}
        same = [(p["id"], by[(p["pack"], p["source"], p["section"], p["text"])]) for p in ps2
                if (p["pack"], p["source"], p["section"], p["text"]) in by]
        r.check(all(x == y for x, y in same), f"the same passage has the same id in both responses "
                                              f"({len(same)} shared)")

    if s6:
        st, body, _, dt = retrieve({"query": a.query, "k": 2, "packs": [a.agency_pack]}, org=a.agency_owner)
        if r.check(st == 200, f"the owner retrieves from {a.agency_pack} -> {st} in {dt:.1f}s"):
            ps = check_retrieve(r, body, 2, [a.agency_pack], "agency")
            r.check(all(p["scope"] == "agency" for p in ps), "agency passages carry scope: agency")
    return r.report(f"retrieval contract against {a.base_url}")


if __name__ == "__main__":
    sys.exit(main())
