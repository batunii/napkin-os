"""The report's layout: how the report looks, composed by the agent (Contract 5).

The structured report (`report.py`) decides what the report says: the
headline, the summary and each lens's claims, every one cite-checked. This
decides how it looks. One structured-output call lays the report out in the
OS's vocabulary — where the big numbers go, what is charted, what sits in a
band — with every figure a `<clan-field>` the view renders from the record, so
the agent cannot state a value nobody recorded. The layout rule
(`rules/layout.py`) checks it in code; what fails is dropped, and a layout
with too little left is replaced by one built here from the structured report.
"""

from __future__ import annotations

import logging
from html import escape

from ..rules import layout as rule

log = logging.getLogger("napkin.layout")

SYSTEM = """You lay out a research report an advertising planner reads, as HTML in a fixed vocabulary.
The words and the evidence are decided: the headline, summary and claims you are given, each with the ids
it cites. Your job is the page: order, emphasis, what becomes a big number, what is charted, what is quoted.
Make it read like a considered piece of editorial work, specific to this research, not a template.

Rules:
- Every figure is an element, never typed: <clan-field ref="f_..." as="big|stat|inline|cell"> for a pin
  (optional caption="short label"), <clan-field ref="fi_..." as="claim"> for a finding's statement,
  <clan-field ref="ct_..."> for a value two sources disagree on. The page renders the value from the record.
- Every paragraph or list item carries its evidence inside it: a <clan-field>, or <clan-cite refs="id id">
  naming what the sentence rests on. A sentence with no evidence is removed.
- Prose may state a number only if something the same paragraph references holds it; prefer describing
  ("the larger channel") and letting the fields show the numbers.
- Charts: <clan-chart kind="bar|line|stack" refs="f_a f_b f_c" title="..." labels="A,B,C"> over pins with
  numbers only; kind="stack" may add rest="label" for an unmeasured remainder.
- The ask a person gave or confirmed (problem, objective, markets…) is <clan-field ref="campaign.problem">;
  the person can edit it there. Use it for their words, never to restate a figure.
- A pin with a quote may be a pull quote: <clan-quote ref="f_...">. A gap: <clan-gap ref="gap_...">.
- End with <clan-sources></clan-sources>.
- Elements: section div header footer article aside h1 h2 h3 p span strong em b i small br hr ol ul li
  table thead tbody tr td th figure figcaption blockquote, and the clan-* ones. No script, style, links,
  images or inline styles. class may only use: cl-head (with compact), cl-eyebrow, cl-title, cl-dek, cl-nums,
  cl-cols, cl-split, cl-grid, cl-prose, cl-figure, cl-cap, cl-label, cl-band (data-tone="soft" for a light
  band), cl-block, cl-callout, cl-row, cl-table, cl-list, cl-foot.
- Say each thing once: no sentence or finding twice, and a figure at most twice (a big number and one
  mention in prose is enough).
- person_wording, when given, is text a person rewrote in the previous version of this report. Keep
  their words verbatim wherever that part of the report still stands; they chose them.
- Use only ids you are given. Write the headline as the page's <h1 class="cl-title">."""


def schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["html"],
            "properties": {"html": {"type": "string"}}}


def _label(p) -> str:
    k = str(p.get("key", "")).split(".", 1)[-1].replace("_", " ")
    return k[:1].upper() + k[1:] + (f" · {p['market']}" if p.get("market") else "")


def compose(report: dict, pins: dict, findings: dict, contests: dict, gaps: dict, caps, names=(),
            brand: str = "", markets=(), ask: dict | None = None,
            person_wording=()) -> tuple[str, str, list[str]]:
    """-> (layout html, who laid it out: `agent` or `built`, what the layout rule dropped)."""
    payload = {
        "brand": brand, "markets": list(markets),
        "headline": report["headline"], "summary": report["summary"],
        "sections": [{"title": s["title"], "lens": s.get("lens"),
                      "claims": [{"text": b["text"], "cites": b["cites"]} for b in s["blocks"] if b["kind"] == "claim"],
                      "pins": [i for b in s["blocks"] if b["kind"] == "pins" for i in b["fact_ids"]],
                      "findings": [b["finding_id"] for b in s["blocks"] if b["kind"] == "finding"],
                      "contests": [b["contest_id"] for b in s["blocks"] if b["kind"] == "contest"],
                      "gaps": [b["gap_id"] for b in s["blocks"] if b["kind"] == "gap"]}
                     for s in report["sections"]],
        "pins": [{"id": i, "label": _label(p), "value": p.get("value"), "unit": p.get("unit"),
                  "market": p.get("market"), "as_of": p.get("as_of"), "has_quote": bool(p.get("quotes"))}
                 for i, p in pins.items()],
        "findings": [{"id": i, "statement": f.get("statement"), "status": f.get("status")}
                     for i, f in findings.items() if f.get("status") != "rejected"],
        "contests": [{"id": i, "key": c.get("key"), "values": [v.get("value") for v in c.get("values") or []]}
                     for i, c in contests.items() if c.get("status") == "open"],
        "gaps": [{"id": i, "wanted": g.get("wanted") or g.get("key")} for i, g in gaps.items()],
        "ask": [{"ref": f"campaign.{k}", "value": v} for k, v in (ask or {}).items()],
    }
    if person_wording:
        payload["person_wording"] = list(person_wording)
    paths = {f"campaign.{k}" for k in (ask or {})}
    dropped: list[str] = []
    try:
        raw = caps.model.structured("layout", SYSTEM, payload, schema(), max_tokens=8000)
        html, dropped, counts = rule.check(raw.get("html", ""), pins, findings, contests, gaps, names, paths)
        # What failed is gone; a layout left with too little evidence to be the
        # report is replaced by one built from it.
        if counts["fields"] + counts["charts"] >= 3 and counts["blocks"] >= 1:
            return html, "agent", dropped
        dropped.append(f"the agent's layout kept {counts['fields']} field(s) and {counts['blocks']} "
                       f"paragraph(s) after the rule; built one instead")
    except Exception as e:  # the report still has a layout
        log.warning("layout unavailable: %s", e)
        dropped.append(f"model: {e}")
    html, more, _ = rule.check(built(report, pins, contests, gaps), pins, findings, contests, gaps, names, paths)
    return html, "built", dropped + more


def built(report: dict, pins: dict, contests: dict, gaps: dict) -> str:
    """The layout built from the structured report, when the agent's is unusable:
    the headline, up to three numbers the summary leans on, each section's claims
    with their evidence, its pins as a table, its findings, contests and gaps."""
    e = lambda s: escape(str(s or ""), quote=True)

    def cite(ids):
        ids = [i for i in ids if i]
        return f' <clan-cite refs="{e(" ".join(ids))}"></clan-cite>' if ids else ""

    lead = [c for c in list(report["headline"]["cites"]) + [c for s in report["summary"] for c in s["cites"]]
            if c in pins and isinstance(pins[c].get("value"), (int, float))]
    lead = list(dict.fromkeys(lead))[:3]
    h = ['<header class="cl-head"><span class="cl-eyebrow">Research report</span>'
         f'<h1 class="cl-title">{e(report["headline"]["text"])}</h1></header>']
    if lead:
        h.append('<div class="cl-nums">' + "".join(f'<clan-field ref="{e(c)}" as="big"></clan-field>' for c in lead)
                 + "</div>")
    if report["summary"]:
        h.append('<section class="cl-block"><ul class="cl-list">'
                 + "".join(f"<li>{e(s['text'])}{cite(s['cites'])}</li>" for s in report["summary"]) + "</ul></section>")
    for s in report["sections"]:
        body = []
        for b in s["blocks"]:
            if b["kind"] == "claim":
                body.append(f"<p>{e(b['text'])}{cite(b['cites'])}</p>")
            elif b["kind"] == "pins":
                rows = [i for i in b["fact_ids"] if i in pins]
                if rows:
                    body.append('<table class="cl-table"><tbody>' + "".join(
                        f'<tr><td>{e(_label(pins[i]))}</td><td><clan-field ref="{e(i)}" as="cell"></clan-field></td></tr>'
                        for i in rows) + "</tbody></table>")
            elif b["kind"] == "finding":
                body.append(f'<p><clan-field ref="{e(b["finding_id"])}" as="claim"></clan-field></p>')
            elif b["kind"] == "contest" and b["contest_id"] in contests:
                body.append(f'<p>Sources disagree: <clan-field ref="{e(b["contest_id"])}"></clan-field></p>')
            elif b["kind"] == "gap" and b["gap_id"] in gaps:
                body.append(f'<clan-gap ref="{e(b["gap_id"])}"></clan-gap>')
        if body:
            h.append(f'<section class="cl-block"><h2>{e(s["title"])}</h2>' + "".join(body) + "</section>")
    h.append('<footer class="cl-foot"><clan-sources></clan-sources></footer>')
    return "".join(h)
