"""The identify rules (middleware-api.md §8.6). Pure decisions over verified
candidates; the model only proposes candidates, these rules decide.

Subject brand — exactly one brand clearly the client: a `Brand:` label, named
in the material as ours / the client's, or the only brand in the material
(and not named as a comparator). Otherwise ask; never guess.

Categories — ranked at most two real leaves as buttons (each alone, and both
together when there are two) plus "Something else". A typed answer maps to
the tree: a vertical's name offers its leaves, a leaf's name offers the leaf.
"""

from __future__ import annotations

import re

from ..util import slug

LABEL_CUE = re.compile(r"\bbrand\s*:", re.I)
OURS_CUE = re.compile(r"\b(?:our|we|we're|my|client|client's|clients?)\b", re.I)


def opt_id(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", slug(text)).strip("_") or "opt")[:40]


def brand_ref(name: str) -> str:
    return "brand/" + slug(name)


def checked_basis(b: dict) -> str:
    """The claimed basis, kept only when the verified quote carries its cue."""
    q = (b.get("span") or {}).get("quote", "")
    basis = b.get("basis")
    if slug(b.get("name", "")) not in slug(q):
        return "none"  # the evidence must name the brand it is evidence for
    if basis == "brand_label" and LABEL_CUE.search(q):
        return "brand_label"
    if basis == "named_as_ours" and OURS_CUE.search(q):
        return "named_as_ours"
    return "none"


def decide_subject(brands: list[dict]) -> tuple[str, object]:
    """brands: verified [{ref, name, span, basis, comparator}] in material order.
    -> ("subject", brand) | ("ask", options) | ("ask_text", None)"""
    uniq: dict[str, dict] = {}
    for b in brands:
        if b["ref"] not in uniq:
            uniq[b["ref"]] = dict(b)
        else:
            u = uniq[b["ref"]]
            u["comparator"] = u.get("comparator") or b.get("comparator")
            if checked_basis(b) != "none" and checked_basis(u) == "none":
                u["basis"], u["span"] = b["basis"], b["span"]
    bs = list(uniq.values())
    claimed = [b for b in bs if checked_basis(b) != "none" and not b.get("comparator")]
    if len(claimed) == 1:
        return "subject", claimed[0]
    if len(bs) == 1 and not bs[0].get("comparator") and not claimed:
        return "subject", bs[0]
    if bs:
        opts = []
        for b in bs:
            o = {"id": opt_id(b["name"]), "label": b["name"], "value": {"ref": b["ref"], "name": b["name"]},
                 "origin": "extracted", "source": b["span"]}
            if o["id"] not in {x["id"] for x in opts}:
                opts.append(o)
        opts.append({"id": "none", "label": "None of these"})
        return "ask", opts
    return "ask_text", None


def category_options(cands: list[tuple[str, dict | None]], origin: str, labels: dict) -> list[dict]:
    """cands: ranked [(leaf, span|None)]. origin extracted needs a span each."""
    opts = []
    for leaf, span in cands:
        o = {"id": leaf.replace(".", "_"), "label": labels.get(leaf, leaf), "value": [leaf], "origin": origin}
        if origin == "extracted":
            o["source"] = span
        opts.append(o)
    if len(cands) == 2:
        o = {"id": "both", "label": f"Both: {labels.get(cands[0][0], cands[0][0])} and "
                                    f"{labels.get(cands[1][0], cands[1][0])}",
             "value": [cands[0][0], cands[1][0]], "origin": origin}
        if origin == "extracted":
            o["source"] = cands[0][1]
        opts.append(o)
    opts.append({"id": "other", "label": "Something else"})
    return opts


def typed_leaf_options(leaves: list[str], labels: dict) -> list[dict]:
    """A typed answer's leaves as candidates (origin stated: the person's words)."""
    opts = [{"id": l.replace(".", "_"), "label": labels.get(l, l), "value": [l], "origin": "stated"} for l in leaves]
    opts.append({"id": "other", "label": "Something else"})
    return opts


def brand_options_from_text(text: str, pool: list[tuple[str, str]]) -> tuple[list[dict], bool]:
    """Free text -> candidate brands (origin stated). pool: [(ref, name)] the
    layers and the material know. The last resort is the text as a new brand."""
    t = slug(text)
    opts, refs = [], set()
    if len(t) >= 2:
        for ref, name in pool:
            s = slug(name)
            if ref not in refs and (s == t or (len(t) >= 3 and (t in s or s in t))):
                refs.add(ref)
                opts.append({"id": opt_id(name), "label": name, "value": {"ref": ref, "name": name},
                             "origin": "stated"})
        if "brand/" + t not in refs and not any(slug(o["value"]["name"]) == t for o in opts):
            name = text.strip()
            oid = opt_id(name) if opt_id(name) not in {o["id"] for o in opts} else "typed"
            opts.append({"id": oid, "label": f"{name} (new brand)", "value": {"ref": "brand/" + t, "name": name},
                         "origin": "stated"})
    found = bool(opts)
    opts.append({"id": "none", "label": "None of these"})
    return opts, found
