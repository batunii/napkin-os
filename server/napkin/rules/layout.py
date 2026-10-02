"""The layout rule: what an agent-written report layout may hold (Contract 5 §4).

A layout is HTML in the OS's vocabulary. The view sanitises it again when it
renders it; this is the writer's side, and it does more than the view can:

- only the allowed elements and attributes survive, and `class` keeps only
  `cl-*` names — everything else is dropped whole;
- every `ref`/`refs` names something the document holds: a pin, a finding, a
  contest, a gap; a chart names only pins with a number;
- a block of prose (`p`, `li`, `blockquote`, `figcaption`) carries its
  evidence: at least one `clan-*` element inside it;
- a block's own text states no figure the pins and findings it references do
  not hold (the report's cite rule, Contract 3 §17). Years are not figures.

A block that fails is dropped and named; the caller decides whether what is
left is still a report.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from .figures import allowed_numbers_for, unsupported_figures

TAGS = {"section", "div", "header", "footer", "article", "aside", "h1", "h2", "h3", "p", "span", "strong", "em",
        "b", "i", "small", "br", "hr", "ol", "ul", "li", "table", "thead", "tbody", "tr", "td", "th", "figure",
        "figcaption", "blockquote", "clan-field", "clan-chart", "clan-quote", "clan-gap", "clan-sources",
        "clan-cite"}
VOID = {"br", "hr"}
CLAN = {t for t in TAGS if t.startswith("clan-")}
ATTRS = {"class", "ref", "refs", "as", "caption", "kind", "title", "rest", "labels", "source", "compact", "colspan",
         "rowspan", "data-tone"}
BLOCKS = {"p", "li", "blockquote", "figcaption"}
AS = {"inline", "big", "stat", "cell", "claim"}
KINDS = {"bar", "line", "stack"}
YEAR = re.compile(r"^(19|20)\d\d$")


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.kids = tag, attrs, parent, []


class _Tree(HTMLParser):
    """HTML into a small tree of allowed nodes; a disallowed element is skipped
    with everything inside it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.at = self.root
        self.skip = 0
        self.dropped_tags = set()

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag not in VOID:
                self.skip += 1
            return
        if tag not in TAGS:
            self.dropped_tags.add(tag)
            if tag not in VOID and tag not in ("img", "input", "meta", "link", "source", "wbr"):
                self.skip = 1
            return
        keep = {}
        for k, v in attrs:
            k = k.lower()
            if k not in ATTRS:
                continue
            if k == "class":
                v = " ".join(c for c in (v or "").split() if re.fullmatch(r"cl-[a-z0-9-]+|compact", c))
                if not v:
                    continue
            keep[k] = v if v is not None else ""
        n = _Node(tag, keep, self.at)
        self.at.kids.append(n)
        if tag not in VOID:
            self.at = n

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not self.skip and self.at.tag == tag:
            self.at = self.at.parent

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        n = self.at
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.at = n.parent

    def handle_data(self, data):
        if not self.skip:
            self.at.kids.append(data)


def _text(n) -> str:
    """The node's own words: text outside any clan-* element inside it."""
    out = []
    for k in n.kids:
        if isinstance(k, str):
            out.append(k)
        elif k.tag not in CLAN:
            out.append(_text(k))
    return " ".join(out)


def _refs(n) -> list[str]:
    out = []
    for k in n.kids:
        if isinstance(k, str):
            continue
        if k.tag in CLAN:
            out += [r for r in re.split(r"[\s,]+", k.attrs.get("ref", "") + " " + k.attrs.get("refs", "")) if r]
        out += _refs(k)
    return out


def _has_clan(n) -> bool:
    return any(not isinstance(k, str) and (k.tag in CLAN or _has_clan(k)) for k in n.kids)


def _render(n) -> str:
    if isinstance(n, str):
        return escape(n, quote=False)
    inner = "".join(_render(k) for k in n.kids)
    if n.tag == "#root":
        return inner
    attrs = "".join(f' {k}="{escape(v)}"' if v != "" else f" {k}" for k, v in n.attrs.items())
    return f"<{n.tag}{attrs}>" if n.tag in VOID else f"<{n.tag}{attrs}>{inner}</{n.tag}>"


def check(html: str, pins: dict, findings: dict, contests: dict, gaps: dict, names=(),
          paths=()) -> tuple[str, list[str], dict]:
    """-> (the layout as it may be written, what was dropped and why, counts).

    `pins`, `findings`: by id, as the document holds them (a rejected finding is
    not a ref). `contests`, `gaps`: by id."""
    tree = _Tree()
    tree.feed(str(html or ""))
    tree.close()
    dropped = [f"<{t}> is not in the vocabulary" for t in sorted(tree.dropped_tags)]
    live_findings = {k: v for k, v in findings.items() if v.get("status") != "rejected"}

    paths = set(paths)

    def known(r):
        return r in pins or r in live_findings or r in contests or r in gaps or r in paths
    known.gaps = set(gaps)

    counts = {"fields": 0, "charts": 0, "blocks": 0}

    def walk(n):
        keep = []
        for k in n.kids:
            if isinstance(k, str):
                keep.append(k)
                continue
            if k.tag in CLAN:
                why = _check_clan(k, known, pins)
                if why:
                    dropped.append(why)
                    continue
                counts["fields" if k.tag != "clan-chart" else "charts"] += 1
                keep.append(k)
                continue
            walk(k)
            if k.tag in BLOCKS:
                own = _text(k).strip()
                if not own and not _has_clan(k):
                    continue
                if not _has_clan(k):
                    dropped.append(f"<{k.tag}> “{own[:60]}” carries no evidence: no clan-* element inside it")
                    continue
                refs = _refs(k)
                figs = [x for x in unsupported_figures(own, allowed_numbers_for(refs, pins, live_findings, names))
                        if not YEAR.match(x)]
                if figs:
                    dropped.append(f"<{k.tag}> “{own[:60]}” states {', '.join(sorted(set(figs)))}, which nothing it "
                                   f"references holds")
                    continue
                counts["blocks"] += 1
            elif k.tag in ("h1", "h2", "h3", "td", "th"):
                figs = [x for x in unsupported_figures(_text(k), allowed_numbers_for(_refs(k), pins, live_findings, names))
                        if not YEAR.match(x)]
                if figs:
                    dropped.append(f"<{k.tag}> states {', '.join(sorted(set(figs)))} outside a field")
                    continue
            keep.append(k)
        n.kids = keep

    walk(tree.root)
    return _render(tree.root), dropped, counts


def gaps_of(known) -> set:
    return getattr(known, "gaps", set())


def _check_clan(k, known, pins) -> str | None:
    t, a = k.tag, k.attrs
    k.kids = []  # a clan-* element's content is the OS's to render
    if t == "clan-sources":
        return None
    if t == "clan-chart":
        refs = [r for r in re.split(r"[\s,]+", a.get("refs", "")) if r]
        if a.get("kind", "bar") not in KINDS:
            return f"<clan-chart> kind {a.get('kind')!r} is not bar, line or stack"
        bad = [r for r in refs if not (r in pins and isinstance(pins[r].get("value"), (int, float))
                                       and not isinstance(pins[r].get("value"), bool))]
        if not refs or bad:
            return f"<clan-chart> charts {', '.join(bad) or 'nothing'}: only pins with a number"
        return None
    if t == "clan-cite":
        refs = [r for r in re.split(r"[\s,]+", a.get("refs", "")) if r]
        bad = [r for r in refs if not known(r)]
        return (f"<clan-cite> cites {', '.join(bad) or 'nothing'} the document does not hold" if (bad or not refs)
                else None)
    ref = a.get("ref", "")
    if not known(ref):
        return f"<{t} ref={ref!r}> names nothing the document holds"
    # Each element shows one kind of thing; a ref of another kind is not a
    # field it can render (a gap is a <clan-gap>, a pin is not one).
    if t == "clan-field" and ref in gaps_of(known):
        return f"<clan-field ref={ref!r}> names a gap: use <clan-gap>"
    if t == "clan-gap" and not ref.startswith("gap_"):
        return f"<clan-gap ref={ref!r}> is not a gap"
    if t == "clan-field" and a.get("as", "inline") not in AS:
        return f"<clan-field> as {a.get('as')!r} is not one of {', '.join(sorted(AS))}"
    if t == "clan-quote" and ref not in pins:
        return f"<clan-quote ref={ref!r}> is not a pin"
    return None
