"""The report's page: the rules its HTML is written to and checked against, and the fallback built in code.

The report stage (`report.py`) makes one model call that returns both the record (headline, summary, claims,
each cite-checked) and the page (`html`), so the agent composes words and page together with a free hand.
This module holds what that page must obey and what happens to it afterwards. The page is HTML in the OS's
vocabulary, with every figure a `<clan-field>` the view renders from the record, so the agent cannot state a
value nobody recorded. The layout rule (`rules/layout.py`) checks it in code; what fails is dropped, and a
page with too little left is replaced by one built here from the structured report.
"""

from __future__ import annotations

import logging
import re
from html import escape

from ..rules import layout as rule

log = logging.getLogger("napkin.layout")

PAGE_RULES = """The page (`html`) is HTML in a fixed vocabulary, and you have a free hand with it: order the sections as
the research deserves (merge, split or drop a lens section), choose what leads, what becomes a big number, what
is charted, what is quoted, what sits in a band. Make it read like a considered piece of editorial work,
specific to this research, not a template. The page presents your headline as its <h1 class="cl-title">. It may
group, reorder and reword your summary and claims, but it may state nothing the ids it references do not hold.
You decide how everything is shown: what is a heading, what is large or bold, the order, the grouping, what is
charted or quoted. You do not decide whether: every fact, finding, contested value and gap in the input appears on
the page, as a big number, a chart, a table row, a sentence or a cite. Anything you leave off is added at the
end by code, in a plain list, so put it where it belongs instead.

The vocabulary (a rule in code removes whatever breaks it):
- Every figure is an element, never typed: <clan-field ref="f_..." as="big|stat|inline|cell"> for a pin
  (optional caption="short label"), <clan-field ref="fi_..." as="claim"> for a finding's statement,
  <clan-field ref="ct_..."> for a value two sources disagree on. The page renders the value from the record.
- Every paragraph or list item carries its evidence inside it: a <clan-field>, or <clan-cite refs="id id">
  naming what the sentence rests on. A sentence with no evidence is removed.
- Prose may state a number only if something the same paragraph references holds it; prefer describing
  ("the larger channel") and letting the fields show the numbers.
- Charts: <clan-chart kind="bar|line|stack" refs="f_a f_b f_c" title="..." labels="A,B,C"> over pins with
  numbers only; kind="stack" may add rest="label" for an unmeasured remainder.
- The ask a person gave or confirmed (problem, objective, markets...) is <clan-field ref="campaign.problem">;
  the person can edit it there. Use it for their words, never to restate a figure.
- A pin with a quote may be a pull quote: <clan-quote ref="f_...">. A gap: <clan-gap ref="gap_...">.
- End with <clan-sources></clan-sources>.
- Elements: section div header footer article aside h1 h2 h3 p span strong em b i small br hr ol ul li
  table thead tbody tr td th figure figcaption blockquote, and the clan-* ones. No script, style, links,
  images or inline styles. class may only use: cl-head (with compact), cl-eyebrow, cl-title, cl-dek, cl-nums,
  cl-cols, cl-split, cl-grid, cl-prose, cl-figure, cl-cap, cl-label, cl-band (data-tone="soft" for a light
  band), cl-block, cl-callout, cl-row, cl-table, cl-list, cl-foot.
- Avoid saying anything twice: a figure at most twice (a big number and one mention in prose is enough).
- person_wording, when given, is text a person rewrote in the previous version of this report. Keep
  their words verbatim wherever that part of the report still stands; they chose them.
- Use only ids you are given."""


def label(p) -> str:
    """A short human label for a pin: its measure, then its market."""
    k = str(p.get("key", "")).split(".", 1)[-1].replace("_", " ")
    return k[:1].upper() + k[1:] + (f" · {p['market']}" if p.get("market") else "")


def complete(html: str, report: dict, pins: dict, contests: dict, gaps: dict) -> str:
    """The agent decides how the page shows things, never whether: add, in a plain list before the sources, every
    fact, finding, contested value and gap of the report that the page does not reference, under its section."""
    e = lambda x: escape(str(x or ""), quote=True)
    shown = set(re.findall(r"\b(?:f|fi|ct|gap)_[0-9A-Za-z_]+", html))
    parts = []
    for sec in report["sections"]:
        rows, more = [], []
        for b in sec["blocks"]:
            if b["kind"] == "pins":
                rows += [i for i in b["fact_ids"] if i in pins and i not in shown]
            elif b["kind"] == "finding" and b["finding_id"] not in shown:
                more.append(f'<p><clan-field ref="{e(b["finding_id"])}" as="claim"></clan-field></p>')
            elif b["kind"] == "contest" and b["contest_id"] in contests and b["contest_id"] not in shown:
                more.append(f'<p>Sources disagree: <clan-field ref="{e(b["contest_id"])}"></clan-field></p>')
            elif b["kind"] == "gap" and b["gap_id"] in gaps and b["gap_id"] not in shown:
                more.append(f'<clan-gap ref="{e(b["gap_id"])}"></clan-gap>')
        if rows:
            more.insert(0, '<table class="cl-table"><tbody>' + "".join(
                f'<tr><td>{e(label(pins[i]))}</td><td><clan-field ref="{e(i)}" as="cell"></clan-field></td></tr>'
                for i in rows) + "</tbody></table>")
        if more:
            parts.append(f'<h3>{e(sec["title"])}</h3>' + "".join(more))
    if not parts:
        return html
    block = '<section class="cl-block"><h2>Also in the research</h2>' + "".join(parts) + "</section>"
    for marker in ('<footer', "<clan-sources"):
        at = html.find(marker)
        if at >= 0:
            return html[:at] + block + html[at:]
    return html + block


def finish(html: str | None, report: dict, pins: dict, findings: dict, contests: dict, gaps: dict, names=(),
           paths=frozenset()) -> tuple[str, str, list[str]]:
    """-> (page html, who laid it out: `agent` or `built`, what the layout rule dropped).
    The agent's page after the rule, with whatever it left off added (`complete`), unless it is missing or the
    rule left it with too little evidence to be the report; then one built here from the structured report."""
    dropped: list[str] = []
    if html:
        out, dropped, counts = rule.check(html, pins, findings, contests, gaps, names, paths)
        if counts["fields"] + counts["charts"] >= 3 and counts["blocks"] >= 1:
            whole = complete(out, report, pins, contests, gaps)
            if whole != out:
                whole, more, _ = rule.check(whole, pins, findings, contests, gaps, names, paths)
                dropped += more
                log.info("report page: the agent left facts off; added them before the sources")
            return whole, "agent", dropped
        dropped.append(f"the agent's page kept {counts['fields']} field(s) and {counts['blocks']} "
                       f"paragraph(s) after the rule; built one instead")
    else:
        dropped.append("no page from the model")
    out, more, _ = rule.check(built(report, pins, contests, gaps), pins, findings, contests, gaps, names, paths)
    return out, "built", dropped + more


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
                        f'<tr><td>{e(label(pins[i]))}</td><td><clan-field ref="{e(i)}" as="cell"></clan-field></td></tr>'
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
