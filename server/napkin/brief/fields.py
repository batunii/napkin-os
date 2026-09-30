"""Brief Maker's document shape (middleware-api.md §10.4–§10.5): the eighteen
fields, the field map, locks, who holds a field, and the patch paths a staged
change is judged by.

The field map (app key -> class, rubric, loop, drafter) is Brief Maker's
declaration in its `pipeline.yaml` (§10.2). When the document carries a
pipeline with a `fields` map, that map is used; otherwise the built-in copy
below, which is the same declaration.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

KEYS = [
    "project_name", "client", "background", "objectives.commercial", "objectives.behavioural",
    "objectives.attitudinal", "audience", "competitor_context", "insight", "single_minded_proposition",
    "reasons_to_believe", "desired_response.think", "desired_response.feel", "desired_response.do",
    "tone_and_world", "budget_and_scope", "mandatories", "open_questions",
]
ARRAY_KEYS = {"reasons_to_believe", "tone_and_world", "mandatories", "open_questions"}
NESTED = ("objectives", "desired_response")

BUILTIN_FIELD_MAP = {
    "project_name": {"class": "captured"},
    "client": {"class": "captured"},
    "background": {"class": "captured", "rubric": "background"},
    "objectives.commercial": {"class": "captured", "rubric": "objectives"},
    "objectives.behavioural": {"class": "captured", "rubric": "objectives"},
    "objectives.attitudinal": {"class": "captured", "rubric": "objectives"},
    "audience": {"class": "captured", "rubric": "audience"},
    "competitor_context": {"class": "captured", "rubric": "competitor_context"},
    "budget_and_scope": {"class": "captured", "rubric": "budget_scope"},
    "mandatories": {"class": "captured", "rubric": "mandatories"},
    "tone_and_world": {"class": "captured", "rubric": "tone_world_assets"},
    "insight": {"class": "drafted", "rubric": "insight", "loop": "loop4_insight"},
    "single_minded_proposition": {"class": "drafted", "rubric": "smp", "loop": "loop5_proposition"},
    "reasons_to_believe": {"class": "drafted", "rubric": "reasons_to_believe", "loop": "loop6_substantiation"},
    "desired_response.think": {"class": "drafted", "rubric": "desired_response", "loop": "loop5_proposition",
                               "drafter": "desired_response"},
    "desired_response.feel": {"class": "drafted", "rubric": "desired_response", "loop": "loop5_proposition",
                              "drafter": "desired_response"},
    "desired_response.do": {"class": "drafted", "rubric": "desired_response", "loop": "loop5_proposition",
                            "drafter": "desired_response"},
    "open_questions": {"class": "composed"},
}

LABELS = {
    "project_name": "project name", "client": "client", "background": "background",
    "objectives.commercial": "commercial objective", "objectives.behavioural": "behavioural objective",
    "objectives.attitudinal": "attitudinal objective", "audience": "audience",
    "competitor_context": "competitor context", "insight": "insight",
    "single_minded_proposition": "single-minded proposition", "reasons_to_believe": "reasons to believe",
    "desired_response.think": "desired response (think)", "desired_response.feel": "desired response (feel)",
    "desired_response.do": "desired response (do)", "tone_and_world": "tone and world",
    "budget_and_scope": "budget and scope", "mandatories": "mandatories", "open_questions": "open questions",
    "desired_response": "desired response",
}

RUBRIC = json.loads((Path(__file__).resolve().parent / "golden_brief.schema.json").read_text())
RUBRIC_FIELDS = {f["id"]: f for f in RUBRIC["fields"]}


def field_map(clan: dict) -> dict:
    """The declared map, or the built-in one. Only the eighteen keys count."""
    fm = ((clan or {}).get("pipeline") or {}).get("fields") if isinstance((clan or {}).get("pipeline"), dict) else None
    out = copy.deepcopy(BUILTIN_FIELD_MAP)
    if isinstance(fm, dict):
        for k, v in fm.items():
            if k in out and isinstance(v, dict) and v.get("class") in ("captured", "drafted", "composed"):
                out[k] = {x: v[x] for x in ("class", "rubric", "loop", "drafter") if isinstance(v.get(x), str)}
    return out


def drafter_of(fm: dict, key: str) -> str:
    """The drafter (group) a drafted key belongs to: its `drafter`, else itself."""
    return fm[key].get("drafter") or key


def group_keys(fm: dict, group: str) -> list[str]:
    return [k for k in KEYS if fm[k]["class"] == "drafted" and drafter_of(fm, k) == group]


def rubric_groups(fm: dict) -> list[tuple[str, list[str]]]:
    """(rubric field id, [app keys]) in the order the fields appear."""
    out: dict[str, list[str]] = {}
    for k in KEYS:
        r = fm[k].get("rubric")
        if r in RUBRIC_FIELDS:
            out.setdefault(r, []).append(k)
    return list(out.items())


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def get(data: dict, key: str):
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return copy.deepcopy(node)


def filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return any(filled(x) for x in v)
    if isinstance(v, dict):
        return any(filled(x) for x in v.values())
    return True


def clean(key: str, v):
    """A value in the field's bare shape, or None (never blank: omitted, not blank)."""
    if key in ARRAY_KEYS:
        items = v if isinstance(v, list) else [v]
        items = [str(x).strip() for x in items if isinstance(x, str) and x.strip()]
        return items or None
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def put(patch: dict, key: str, value) -> dict:
    top, _, leaf = key.partition(".")
    if leaf:
        patch.setdefault(top, {})[leaf] = value
    else:
        patch[top] = value
    return patch


def locked(data: dict, key: str) -> bool:
    lf = data.get("locked_fields") if isinstance(data.get("locked_fields"), list) else []
    return key in lf or key.split(".")[0] in lf


def _is_human(d: dict) -> bool:
    who = d.get("actor") if d.get("actor") else d.get("agent")
    return isinstance(who, str) and who.startswith("human")


def wrote(d: dict, doc: str, key: str) -> bool:
    """Did decision `d` write `key` (§10.5, the view's own rule)?"""
    top = key.split(".")[0]
    targets = d.get("targets")
    if targets:
        return f"{doc}#{key}" in targets or f"{doc}#{top}" in targets
    fc = d.get("fields_changed") or []
    if top not in fc:
        return False
    if key == top:
        return True
    siblings = {f"{top}.{x}" for x in re.findall(rf"(?<![\w.]){re.escape(top)}\.(\w+)", str(d.get("action") or ""))}
    return not (siblings - {key})


def holder(doc: str, data: dict, decisions: list, key: str) -> str | None:
    """Why a person holds `key` — the middleware proposes instead of writing —
    or None (§10.5)."""
    # `decisions` is the chain as the host sends it, newest first: the latest
    # writer is the first match. Which came first is the chain's order, never
    # the stamps (Contract 4 §3).
    last = next((d for d in decisions if d.get("kind") in ("edit", "resolve") and wrote(d, doc, key)), None)
    if last is not None and _is_human(last):
        return "a person wrote it last"
    if last is None and filled(get(data, key)):
        return "it holds a value no decision wrote"
    addr = f"{doc}#{key}"
    for i, v in enumerate(decisions):
        if v.get("kind") == "verdict" and v.get("polarity") == "bad" and _is_human(v) \
                and addr in (v.get("targets") or []):
            if not any(d.get("kind") == "edit" and wrote(d, doc, key) for d in decisions[:i]):
                return "a person's bad verdict on it is unanswered"
    return None


def last_writer(doc: str, decisions: list, key: str) -> str | None:
    # Newest first (see holder): the first match is the latest writer.
    return next((d.get("id") for d in decisions
                 if d.get("kind") in ("edit", "resolve") and wrote(d, doc, key) and d.get("action") != "propose"), None)


# ---------------------------------------------------------------------------
# Patch paths (what a staged change is judged by, §3)
# ---------------------------------------------------------------------------

def field_paths(patch: dict) -> list[str]:
    out = []
    for top, sub in patch.items():
        if top in NESTED + ("materials", "passages") and isinstance(sub, dict) and sub:
            out += [f"{top}.{k}" for k in sub]
        else:
            out.append(top)
    return out


def address(path: str) -> str:
    for m in ("materials.", "passages."):
        if path.startswith(m):
            return f"{m[:-1]}[{path[len(m):]}]"
    return path


def read_of(data: dict, patch: dict) -> dict:
    return {p: get(data, p) for p in field_paths(patch)}
