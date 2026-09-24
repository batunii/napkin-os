"""Reasoning: why every decision the middleware emits was made, in the shape
of the foundation spec's decisions (OS-layer contract §3, middleware-api §3).

    decided          one sentence: what was decided (always the code's)
    because          evidence points, each citing ids from what the decider held
    rejected         alternatives and why each lost (or only_option: why there was one)
    certainty        {level, why} — DERIVED here from the rules, never the model's
    would_change_if  what new evidence would reverse it
    attention        optional: why a person should look

Where a decision comes from a model call, the MODEL writes `because`,
`rejected`, `only_option`, `would_change_if` and `attention` (see
`MODEL_SCHEMA`), and `from_model` keeps only what survives the cite check:
a point whose cites are not all ids the model was given is dropped, and so is
a point that states a figure without citing where it is. When nothing
survives, the code's own points from the evidence it holds stand in, and the
decision is marked for attention. Deterministic decisions are written here
whole by the code.
"""

from __future__ import annotations

import re

LEVELS = ("low", "medium", "high")

GUIDE = """
Also return `reasoning` for your answer as a whole, in this shape:
- because: the evidence, point by point. Each point cites the ids from the input it rests on
  (material_id, pin id, finding id, source_id). A point that states a figure must cite where the
  figure is. Cite only ids that appear in the input.
- rejected: the alternatives you considered and why each lost. Empty only when there was
  genuinely one option; then say why in only_option (otherwise only_option is null).
- would_change_if: what new evidence would reverse it.
- attention: a short reason a person should look (an uncertain call, thin evidence, a conflict,
  something skipped that might matter), or null.
Do not rate your own confidence: certainty is derived from the evidence elsewhere."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

MODEL_SCHEMA = _obj({
    "because": {"type": "array", "items": _obj({"point": {"type": "string"},
                                                "cites": {"type": "array", "items": {"type": "string"}}})},
    "rejected": {"type": "array", "items": _obj({"option": {"type": "string"}, "why": {"type": "string"}})},
    "only_option": _NULLABLE_STR,
    "would_change_if": {"type": "string"},
    "attention": _NULLABLE_STR,
})

_DIGIT = re.compile(r"\d")


def states_figure(text: str) -> bool:
    return bool(_DIGIT.search(text or ""))


def point(text: str, *cites) -> dict:
    flat = []
    for c in cites:
        flat += list(c) if isinstance(c, (list, tuple, set)) else [c]
    p = {"point": text}
    cs = [c for c in dict.fromkeys(flat) if isinstance(c, str) and c.strip()]
    if cs:
        p["cites"] = cs
    return p


def rej(option: str, why: str) -> dict:
    return {"option": option, "why": why}


def certainty(level: str, why: str) -> dict:
    assert level in LEVELS, level
    return {"level": level, "why": why}


def lowest(levels, default="high") -> str:
    ls = [l for l in levels if l in LEVELS]
    return min(ls, key=LEVELS.index) if ls else default


def step_down(level: str) -> str:
    return LEVELS[max(0, LEVELS.index(level) - 1)]


def make(decided: str, because: list, certainty_: dict, would_change_if: str, rejected=(), only_option=None,
         attention=None) -> dict:
    r = {"decided": decided, "because": [p for p in because if p], "rejected": list(rejected),
         "certainty": certainty_, "would_change_if": would_change_if}
    if not r["rejected"] and only_option:
        r["only_option"] = only_option
    if attention:
        r["attention"] = attention
    bad = problems(r)
    if bad:
        raise ValueError(f"reasoning for {decided!r}: {'; '.join(bad)}")
    return r


def problems(r) -> list[str]:
    """The same shape checks the SDK's `Reasoning::problems` makes."""
    if not isinstance(r, dict):
        return ["reasoning is not an object"]
    out = []
    blank = lambda s: not isinstance(s, str) or not s.strip()
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
        cites = p.get("cites") or []
        if not isinstance(cites, list) or any(blank(c) for c in cites):
            out.append(f"reasoning.because[{i}] has an empty cite")
        if not cites and states_figure(p["point"]):
            out.append(f"reasoning.because[{i}] states a figure and cites nothing")
    rejected = r.get("rejected") or []
    for i, x in enumerate(rejected):
        if not isinstance(x, dict) or blank(x.get("option")) or blank(x.get("why")):
            out.append(f"reasoning.rejected[{i}] needs an option and a why")
    if not rejected and blank(r.get("only_option")):
        out.append("reasoning.rejected is empty and only_option does not say why there was one option")
    c = r.get("certainty") or {}
    if not isinstance(c, dict) or c.get("level") not in LEVELS:
        out.append("reasoning.certainty.level is not one of: high, medium, low")
    elif blank(c.get("why")):
        out.append("reasoning.certainty.why is empty")
    if blank(r.get("would_change_if")):
        out.append("reasoning.would_change_if is empty")
    if "attention" in r and blank(r.get("attention")):
        out.append("reasoning.attention is present but empty")
    return out


def summary(r: dict) -> str:
    """The one-line rationale older readers show: decided, then the first point."""
    decided = r["decided"].strip()
    first = r["because"][0]["point"].strip() if r.get("because") else ""
    if not first:
        return decided
    sep = " " if decided.endswith((".", "!", "?")) else ". "
    return f"{decided}{sep}Because: {first}"


def give(decision: dict, r: dict) -> dict:
    """Attach `r` to a decision; its rationale becomes the one-line summary."""
    decision["reasoning"] = r
    decision["rationale"] = summary(r)
    return decision


def from_model(raw, *, decided: str, known, certainty_: dict, fallback: list, would_change_if: str,
               rejected=(), only_option=None, attention=None, always=()) -> tuple[dict, list[str]]:
    """The model's reasoning, cite-checked, completed by the code.

    `known`: every id the model was given. `fallback`: the code's own points
    from the evidence, used when none of the model's survive. `rejected`: the
    code's alternatives, kept after the model's. `only_option`,
    `would_change_if`: the code's, used where the model gave none.
    `attention`: joined with the model's. `always`: the
    code's points on evidence the model never saw (pins it did not read),
    kept after the model's. Returns the reasoning and notes on what was
    dropped."""
    raw = raw if isinstance(raw, dict) else {}
    known = set(known)
    notes, because = [], []
    for p in raw.get("because") or []:
        text = (p.get("point") or "").strip() if isinstance(p, dict) else ""
        cites = [c for c in dict.fromkeys(p.get("cites") or [])] if isinstance(p, dict) else []
        if not text:
            continue
        unknown = [c for c in cites if c not in known]
        if unknown:
            notes.append(f"dropped a point citing {', '.join(map(str, unknown[:3]))}, not in what it was given")
            continue
        if not cites and states_figure(text):
            notes.append("dropped a point that states a figure and cites nothing")
            continue
        because.append(point(text, cites))
    attn = [a for a in (attention, raw.get("attention")) if isinstance(a, str) and a.strip()]
    if not because:
        because = list(fallback)
        if fallback:
            attn.append("The model's reasons did not survive the cite check; these are the code's, from the "
                        "evidence.")
    because += [p for p in always if p not in because]
    rej_m = [rej(x["option"].strip(), x["why"].strip()) for x in raw.get("rejected") or []
             if isinstance(x, dict) and (x.get("option") or "").strip() and (x.get("why") or "").strip()]
    only = (raw.get("only_option") or "").strip() or only_option
    wci = (raw.get("would_change_if") or "").strip() or would_change_if
    r = make(decided, because, certainty_, wci, rejected=rej_m + [x for x in rejected if x not in rej_m],
             only_option=only,
             attention=" ".join(dict.fromkeys(attn)) or None)
    return r, notes
