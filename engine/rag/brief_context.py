#!/usr/bin/env python3
"""
brief_context.py — turn campaign-clan pairs into the four blocks the brief prompt needs.

This is the only retrieval entry point the brief pipeline calls. It takes the campaign
clan as filtered key-value pairs and returns four budgeted, citable blocks:

    exemplars     precedent for this problem        award cases, worked examples
    craft         how to think about this field     framework process, guiding questions
    rules         never / always                    common mistakes, decision rules
    instructions  what a good brief looks like      briefing templates, criteria

Five properties are forced on this design by how the brief actually calls a model, and
each one is a constraint rather than a preference:

1.  RETRIEVE ONCE. The blocks go into the prompt prefix that the draft, the judge and
    the revise call all share. Retrieving again mid-run would change that prefix and
    throw away the prompt cache on every later call. So this function runs once per
    brief and its output is frozen.

2.  BUDGET IN TOKENS, NOT CHUNKS. "Five hits" is not a size — an IPA case is ~550
    tokens and a playbook section can be 400 words. Each block is filled until its
    token budget is spent, so the prefix has a known ceiling.

3.  EVERY HIT IS CITABLE. Each carries a short stable id the draft cites and the judge
    can verify. A claim of precedent with no id, or an id that is not in the context,
    is a grounding failure the judge can catch mechanically rather than by reading.

4.  RULES ARE SELECTED BY FILTER, NOT BY SIMILARITY. A never-do-this constraint applies
    whether or not it resembles the query. The filter defines the eligible set; ranking
    only decides what fits when that set is larger than the budget, and client-scoped
    constraints sort ahead of house-wide ones.

5.  WIDEN RATHER THAN RETURN NOTHING. An empty precedent block is worse than a broader
    one. If the filters are too narrow the scope is relaxed one step at a time, and
    what was dropped is recorded in the trace so the brief can say so.
"""
from __future__ import annotations

import functools
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import normalise  # noqa: E402

# ---- budgets -------------------------------------------------------------------
# ~8k tokens total. Sized against the measured corpus: an IPA case parent is ~550
# tokens, a playbook section ~350, a rule ~120. See the plan's token model.
# ~8k total. Precedent earns the largest share because the simulation showed it is the
# block that consistently returns on-point material; pitfalls earn the smallest because
# their relevance is only moderate — they are general planning advice, not rules about
# this brief. Rebalanced from an even split after reading real output, not guessed.
DEFAULT_BUDGET = {"exemplars": 3800, "craft": 2500, "rules": 900, "instructions": 800}

# A per-hit ceiling, so one long item cannot consume a whole block. Playbook "Common
# Mistakes" sections are long-form teaching — median 501 tokens, p90 725 — so some cap is
# needed. But the cap must not become a length filter standing in for a quality filter:
# at 260 tokens only 15% of the material was even eligible, which selects brevity rather
# than relevance. 350 keeps 27% eligible and still fits two or three items in the block.
MAX_HIT_TOKENS = {"rules": 350}

# The budget is a TARGET, not a wall. A hard maximum exists only so one pathological
# chunk cannot blow the prompt; it is deliberately loose (1.6x) because overshooting by
# a few hundred tokens costs fractions of a cent, while silently returning a worse answer
# costs a planner their trust in the panel.
HARD_MAX_FACTOR = 1.6
CANDIDATES = 40           # pulled per bucket before collapsing and budgeting

# Which pair keys mean what. Unknown keys are not discarded — their values join the
# query text, so a campaign clan that grows a new field degrades to "still searchable"
# rather than "silently ignored".
FILTER_PAIRS = {"category": "category", "sector": "category", "campaign_type": "campaign_type"}
KEYWORD_PAIRS = ("brand", "client", "product", "competitor", "competitors", "agency")
QUERY_ORDER = ("problem", "objective", "challenge", "audience", "insight", "market",
               "product", "brand", "client", "tone", "proposition")
SKIP_PAIRS = {"run_id", "id", "created_at", "updated_at", "owner", "status", "stage"}

# `campaign_type` is a review-origin field: it arrives with dossiers and NO corpus chunk
# carries it, so hard-filtering on it matches nothing and widens on every brief. The
# corpus expresses the same idea in the awarding body's vocabulary — `effectiveness_type`,
# populated on 3,413 chunks — so a campaign type is translated into that where the two
# taxonomies genuinely share a concept. always_on / seasonal / rebrand have no equivalent
# and become query text instead of a filter — deliberately under-claiming, since a
# rebrand is not necessarily a turnaround. Once dossiers carry campaign_type, it joins
# this filter alongside effectiveness_type.
CAMPAIGN_TO_EFFECTIVENESS = {"launch": "launch"}

# Short, readable, stable citation prefixes. A citation is read by a model, checked by
# the judge, and eventually by a person auditing where a claim came from, so "85" or a
# 70-character filename will not do.
CITE_PREFIX = {"ipa": "ipa", "effie": "effie", "cannes": "cannes", "dandad": "dandad",
               "playbook": "pb", "template": "tpl", "dossier": "run"}


def estimate_tokens(text: str) -> int:
    """Deliberately conservative: 3.5 characters per token rather than the usual 4, so a
    block budgeted here never overruns the real count. Exact counting needs the model
    provider's tokenizer and a network call; budgeting does not warrant one."""
    return int(len(text or "") / 3.5) + 1


# ---- the unit of context ---------------------------------------------------------
@dataclass
class Hit:
    """One retrieved item as the prompt shows it: a citable id plus the text.

    `cite` is the short id the draft cites and the judge checks (see cite_for()); `doc_id`
    is the corpus document it came from; `bucket` is the block it was retrieved for.
    `metadata` is the chunk's full contract payload, kept so the egress check and the
    trace can read scope and tenant per hit."""
    cite: str
    doc_id: str
    source: str
    bucket: str
    title: str
    section: str
    header: str
    text: str
    score: float
    metadata: dict = field(default_factory=dict)
    # The validation stage's verdict, in the rag_io $defs/relevance shape plus `kept`
    # (passed | floor | unjudged | exempt). None when validation is off.
    relevance: dict | None = None

    def render(self) -> str:
        """The hit as it appears in the prompt: `[cite] header` on one line, then the
        body. Falls back to the title when the chunk has no context header. A hit kept
        only by the floor says so, so the model does not weigh it like real evidence."""
        head = self.header or self.title
        if (self.relevance or {}).get("kept") == "floor":
            head += "  [weak match: below the relevance threshold]"
        body = self.text.strip()
        return f"[{self.cite}] {head}\n{body}"

    @property
    def tokens(self) -> int:
        """Estimated token cost of the rendered hit, citation line included."""
        return estimate_tokens(self.render())


@dataclass
class Block:
    """One of the four prompt blocks after budgeting: the hits that made it in, plus the
    counters the trace reports, so a thin or overfull block is visible rather than silent.

    `dropped` counts every hit left out for any reason (budget, per-hit cap, one per
    client, egress); `over_target` and `truncated` are set by _fill()."""
    bucket: str
    hits: list[Hit] = field(default_factory=list)
    budget: int = 0
    dropped: int = 0
    over_target: bool = False      # the target was exceeded to keep the best hit
    truncated: int = 0             # hits cut because they exceeded the hard maximum

    @property
    def tokens(self) -> int:
        """Estimated tokens of every hit in the block."""
        return sum(h.tokens for h in self.hits)

    def text(self) -> str:
        """The hits rendered and joined by blank lines, in rank order."""
        return "\n\n".join(h.render() for h in self.hits)


@dataclass
class BriefContext:
    """What build() returns: the four blocks plus everything needed to explain them.

    Frozen once built (module docstring, rule 1): the same object feeds the draft, judge
    and revise calls. `filters` holds the filter each bucket was actually searched with,
    after any widening; `widened` collects plan notes, widening steps and egress drops as
    readable lines."""
    blocks: dict[str, Block]
    query: str
    keywords: list[str]
    filters: dict
    widened: list[str] = field(default_factory=list)
    # The scopes this run was AUTHORISED for. Recorded separately from what was actually
    # served, because the interesting question is the gap between them.
    scopes: list[str] = field(default_factory=lambda: ["global"])
    # The tenants this run was authorised for. `house` is the licensed corpus that
    # belongs to nobody; anything else is one agency's own material.
    tenants: list[str] = field(default_factory=lambda: ["house"])
    # Hits the egress check refused, structured rather than prose. A free-text line in
    # `widened` reads fine and cannot be queried, and this is the field you would grep
    # across every brief ever generated if a boundary were ever found wrong.
    egress: list[dict] = field(default_factory=list)
    # The validation stage's run record (judge.ValidationResult.as_dict() plus per-bucket
    # counts and admission refusals), or None when validation is off.
    validation: dict | None = None

    @property
    def tokens(self) -> int:
        """Estimated tokens across all four blocks: the size of the retrieved prefix."""
        return sum(b.tokens for b in self.blocks.values())

    def citations(self) -> dict[str, Hit]:
        """cite -> Hit, for the judge's grounding check."""
        return {h.cite: h for b in self.blocks.values() for h in b.hits}

    def hard_constraints(self) -> list[Hit]:
        """Rules that came from a human rejecting something: verdict == 'rejected'. These
        are the only items the judge must check the draft against one by one. Empty until
        Track B ships dossiers — and it matters that they stay distinguishable, because a
        reviewer's rejection and a textbook's advice carry very different authority."""
        return [h for h in self.blocks.get("rules", Block("rules")).hits
                if h.metadata.get("verdict") == "rejected"]

    def pitfalls(self) -> list[Hit]:
        """Everything else in the rules bucket: known mistakes from the planning
        literature. Advisory, not enforceable."""
        rejected = {id(h) for h in self.hard_constraints()}
        return [h for h in self.blocks.get("rules", Block("rules")).hits if id(h) not in rejected]

    def prompt_text(self, headings: dict[str, str] | None = None) -> str:
        """Render the blocks as the prompt section the brief model reads.

        Order: instructions, constraints, pitfalls, craft, precedent. The rules block is
        split into constraints (reviewer rejections, which the model must check one by
        one) and pitfalls (advisory), so the model can tell a human rejection from
        textbook advice. When the precedent block holds three or fewer cases and dropped
        none, a note says that is every matching case in the corpus, so a short list is
        not mistaken for a curated shortlist. Empty blocks are omitted.

        `headings` replaces the default titles and must then supply all three keys
        (exemplars, craft, instructions); a missing key raises KeyError when that block
        has hits."""
        h = headings or {
            "exemplars": "PRECEDENT — comparable work, and worked examples of the craft",
            "craft": "CRAFT — how planners approach this",
            "instructions": "BRIEF STANDARD — what a good brief contains",
        }
        parts = []
        b = self.blocks.get("instructions")
        if b and b.hits:
            parts.append(f"## {h['instructions']}\n\n{b.text()}")

        hard = self.hard_constraints()
        if hard:
            parts.append("## CONSTRAINTS — a reviewer rejected these before. "
                         "Check the draft against every one.\n\n"
                         + "\n\n".join(x.render() for x in hard))
        soft = self.pitfalls()
        if soft:
            parts.append("## PITFALLS — common mistakes in this kind of work. "
                         "Advisory, not rules.\n\n" + "\n\n".join(x.render() for x in soft))

        b = self.blocks.get("craft")
        if b and b.hits:
            parts.append(f"## {h['craft']}\n\n{b.text()}")

        b = self.blocks.get("exemplars")
        if b and b.hits:
            note = ""
            if b.dropped == 0 and len(b.hits) <= 3:
                note = (f"\n\n(These are every case in the corpus matching this brief's "
                        f"filters — {len(b.hits)}, not a shortlist of many. Weight them accordingly.)")
            parts.append(f"## {h['exemplars']}\n\n{b.text()}{note}")
        return "\n\n".join(parts)

    def scopes_served(self) -> dict[str, int]:
        """scope -> how many hits carrying it reached the prompt. The rollup that answers
        'did anything but house material inform this brief', in one glance."""
        out: dict[str, int] = {}
        for b in self.blocks.values():
            for h in b.hits:
                s = str(h.metadata.get("scope") or "global")
                out[s] = out.get(s, 0) + 1
        return dict(sorted(out.items()))

    def tenants_served(self) -> dict[str, int]:
        """tenant -> how many hits carrying it reached the prompt. Anything other than
        `house` means an agency's own material informed this brief."""
        out: dict[str, int] = {}
        for b in self.blocks.values():
            for h in b.hits:
                t = str(h.metadata.get("tenant") or "house")
                out[t] = out.get(t, 0) + 1
        return dict(sorted(out.items()))

    def trace(self) -> dict:
        """What was retrieved and why — logged per brief so a later verdict can be tied
        back to the precedent that informed the field it judges.

        Scope is recorded per hit, not just per run. If a scope boundary is ever found
        wrong, the only answerable version of 'which briefs were affected' is one that
        names the offending chunk in the brief that used it — a run-level summary says a
        breach happened somewhere, which is not an answer anyone can act on. It cannot be
        reconstructed later either: the index is mutable, chunks are retagged and
        superseded, so a scope not written down at retrieval time is gone. Cheap now,
        impossible retrospectively."""
        return {
            "query": self.query,
            "keywords": self.keywords,
            "filters": {k: str(v) for k, v in self.filters.items()},
            "widened": self.widened,
            "tokens": self.tokens,
            "scopes": list(self.scopes),            # what this run was allowed to see
            "scopes_served": self.scopes_served(),  # what it actually got
            "tenants": list(self.tenants),          # whose material it was allowed to read
            "tenants_served": self.tenants_served(),
            "egress": list(self.egress),            # what was refused on the way out
            "validation": self.validation,          # which backend judged, what it kept
            "blocks": {
                b.bucket: {"hits": len(b.hits), "tokens": b.tokens, "budget": b.budget,
                           "dropped": b.dropped, "over_target": b.over_target,
                           "truncated": b.truncated, "cites": [h.cite for h in b.hits],
                           # cite -> scope. Additive: `cites` keeps its shape for the
                           # existing consumers, and this answers the per-hit question.
                           "scope_of": {h.cite: str(h.metadata.get("scope") or "global")
                                        for h in b.hits},
                           "tenant_of": {h.cite: str(h.metadata.get("tenant") or "house")
                                         for h in b.hits},
                           "relevance_of": {h.cite: h.relevance for h in b.hits if h.relevance}}
                for b in self.blocks.values()
            },
        }


# ---- planning: pairs -> query + filters -------------------------------------------
def _clean_value(v) -> str:
    """A pair value as one clean string: a list is joined with commas (empty items
    skipped), None becomes ''. Pairs come from forms and model output, so either shape can
    arrive."""
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(x) for x in v if x)
    return str(v or "").strip()


def plan(pairs: dict) -> tuple[str, list[str], dict, list[str]]:
    """Campaign pairs -> (dense query text, exact keyword terms, contract filters).

    Deterministic on purpose. A model could write a better query, but this runs before
    every brief and a deterministic plan is reproducible, free, and debuggable — the
    trace shows exactly why a case was retrieved. A model pass over the query text is a
    step-8 experiment, measurable against the golden set like anything else."""
    pairs = {k.lower(): v for k, v in (pairs or {}).items()}
    filters: dict = {}
    notes: list[str] = []

    for key, target in FILTER_PAIRS.items():
        raw = _clean_value(pairs.get(key))
        if not raw or target in filters:
            continue
        if target == "category":
            # The `category` key carries CONTRACT ENUM values (_gist is prompted with
            # SCHEMA.enum_values). The `sector` key carries awarding-body free text.
            # Enum first, spelling table second — the other order silently drops 8 of 18.
            value = normalise.category_value(raw) or normalise.category_from_sector(raw)
        else:
            value = normalise.snake(raw)
        if value:
            filters[target] = value
        else:
            notes.append(f"plan: could not resolve {key}={raw!r} — searching unfiltered")

    extra_query: list[str] = []
    ct = filters.pop("campaign_type", None)
    if ct:
        mapped = CAMPAIGN_TO_EFFECTIVENESS.get(ct)
        if mapped:
            filters["effectiveness_type"] = mapped
        else:
            extra_query.append(f"{ct.replace('_', ' ')} campaign")

    keywords: list[str] = []
    for key in KEYWORD_PAIRS:
        for tok in re.split(r"[,/;]| and ", _clean_value(pairs.get(key))):
            tok = tok.strip()
            if tok:
                keywords.append(tok)

    seen: set[str] = set()
    parts: list[str] = []
    for key in QUERY_ORDER:
        v = _clean_value(pairs.get(key))
        if v and key not in seen:
            parts.append(v); seen.add(key)
    for key, v in pairs.items():                       # nothing is silently dropped
        if key in seen or key in SKIP_PAIRS or key in FILTER_PAIRS or key in KEYWORD_PAIRS:
            continue
        val = _clean_value(v)
        if val and len(val.split()) <= 60:
            parts.append(val)
    query = ". ".join(p.rstrip(".") for p in parts + extra_query if p)
    return query, keywords, filters, notes


def scopes_for(pairs: dict, filters: dict, *, brand: str | None = None,
               notes: list[str] | None = None) -> list[str]:
    """Which scopes a lesson may carry to apply here: always global, plus this category,
    plus this brand IF the caller is authorised for one. The OR is what stops a narrow
    brand filter burying house lessons.

    `brand` is the AUTHORISED brand and the only thing that can unlock brand scope. It
    comes from the caller — today asserted explicitly, once auth exists resolved from the
    session (foundation-spec M3: a handler never computes its own scope).

    `pairs` is NOT trusted for this. Its client/brand keys are model output over an
    uploaded document: research.py asks a model for {"client": ...} against raw
    attachment text, and whatever comes back used to become `brand:<that>` directly. Text
    inside a file the user dropped in therefore chose which brand's private material the
    retrieval was allowed to see. Attachments are the least trustworthy input in the
    system and scope is the most consequential output, so the edge between them is cut.

    Note that field provenance does NOT rescue this. Loop-1 marks fields client_stated |
    inferred | missing, and `client_stated` reads like an authorisation — it is not. It
    means the model found the name verbatim in the attachment, which is exactly the
    untrusted channel. Both values are model output over the same file.

    Unauthorised means no brand scope at all, not a guess: the brief falls back to house
    and category material, which is a thinner brief rather than a leak."""
    out = ["global"]
    if filters.get("category"):
        out.append(f"category:{filters['category']}")

    flat = {k.lower(): v for k, v in (pairs or {}).items()}
    claimed = normalise.snake(_clean_value(flat.get("brand") or flat.get("client")))
    authorised = normalise.snake(_clean_value(brand)) if brand else ""

    if authorised:
        out.append(f"brand:{authorised}")
        # A disagreement is reported, never reconciled. Widening to both would hand the
        # attachment the leak it was refused; picking silently would hide that the
        # document in front of the user is not the brand this run is scoped to.
        if claimed and claimed != authorised:
            if notes is not None:
                notes.append(f"scope: document claims client {claimed!r} but this run is "
                             f"authorised for {authorised!r} — using {authorised!r}")
    elif claimed and notes is not None:
        notes.append(f"scope: document claims client {claimed!r}, but no authorised brand "
                     f"was supplied — brand-scoped material withheld")
    return out


def tenants_for(tenant: str | None) -> list[str]:
    """Which tenants a chunk may belong to for this run: always `house` — the licensed
    craft corpus, which belongs to nobody — plus this agency if one is named.

    Like `brand` in scopes_for(), `tenant` comes from the caller and never from the
    document. Naming no agency yields house material only, which is the correct answer
    for an unauthenticated run rather than a degraded one."""
    out = ["house"]
    t = normalise.snake(_clean_value(tenant)) if tenant else ""
    if t and t != "house":
        out.append(t)
    return out


def bucket_filters(bucket: str, filters: dict, scopes: list[str],
                   tenants: list[str] | None = None) -> dict:
    """The filter for one bucket. Common to all four: never serve superseded material,
    and never serve production-stage chunks (D&AD) to the brief."""
    # Scope is the confidentiality mechanism, so it applies to EVERY bucket, not just
    # rules. Corpus chunks are all scope=global and `scopes` always contains "global",
    # so this narrows nothing that exists today — but the moment Track B lands dossiers,
    # an unscoped bucket is the path by which one client's private material reaches
    # another client's brief. That is the failure the plan calls relationship-ending.
    # `tenant` sits beside `scope` for the same reason and on every bucket. Scope keeps
    # one brand's lessons away from another inside an agency; tenant keeps one agency's
    # material away from another entirely. A run that names no agency sees `house` only.
    base: dict = {"bucket": bucket,
                  "status": {"ne": "superseded"},
                  "stage": {"ne": "production"},
                  "scope": {"in": scopes},
                  "tenant": {"in": tenants or ["house"]}}
    if bucket == "exemplars":
        if filters.get("category"):
            base["category"] = filters["category"]
        if filters.get("effectiveness_type"):
            base["effectiveness_type"] = filters["effectiveness_type"]
    return base


# ---- retrieval ------------------------------------------------------------------
def _scope_rank(md: dict) -> int:
    """Client-specific constraints outrank house-wide ones. A rejection recorded against
    this brand is a harder fact than a general heuristic."""
    scope = str(md.get("scope") or "global")
    return {"global": 0}.get(scope, 2 if scope.startswith("brand:") else 1)


def _collapse(rows: list[tuple[float, dict]], store) -> list[tuple[float, dict]]:
    """One hit per document, keeping the best-scoring chunk, then presented as the whole
    parent where there is one. A query that matches a case's Results section should put
    the whole case in front of the model, not that paragraph alone — and five slots
    should hold five cases, not three cases and two of their own sections."""
    best: dict[str, tuple[float, dict]] = {}
    for score, r in rows:
        md = r.get("metadata") or {}
        key = str(md.get("doc_id") or r.get("id"))
        if key not in best or score > best[key][0]:
            best[key] = (score, r)
    out = []
    for score, r in best.values():
        md = r.get("metadata") or {}
        pid = md.get("parent_id")
        if pid and hasattr(store, "get"):
            parent = store.get(pid)
            if parent:
                r = parent
        out.append((score, r))
    return sorted(out, key=lambda x: x[0], reverse=True)


_SLUG = re.compile(r"[^a-z0-9]+")


def cite_for(md: dict, doc_id: str) -> str:
    """A short, stable, human-readable citation. Playbook doc_ids are bare numbers ("85")
    and template doc_ids are whole filenames, neither of which tells a reader or a judge
    anything, so both are prefixed and the long ones are slugged and truncated."""
    source = str(md.get("source") or "")
    prefix = CITE_PREFIX.get(source, source or "doc")
    base = doc_id if doc_id.startswith(f"{prefix}_") or doc_id.startswith(f"{prefix}:") else None
    if base is None:
        name = str(md.get("framework_name") or "").strip().strip('"') or doc_id
        slug = _SLUG.sub("-", name.lower()).strip("-")[:24].rstrip("-")
        base = f"{prefix}_{slug or _SLUG.sub('-', doc_id.lower()).strip('-')[:28]}"
    role = str(md.get("section_role") or "")
    return base if md.get("level") == "parent" or role in ("", "whole") else f"{base}#{role}"


def _to_hit(score: float, r: dict, bucket: str) -> Hit:
    """Turn a store row into a Hit for `bucket`. The title prefers framework_name, then
    title, then doc_id; the score is rounded to four places for the trace."""
    md = r.get("metadata") or {}
    doc_id = str(md.get("doc_id") or r.get("id"))
    title = str(md.get("framework_name") or md.get("title") or doc_id)
    cite = cite_for(md, doc_id)
    return Hit(cite=cite, doc_id=doc_id, source=str(md.get("source") or ""), bucket=bucket,
               title=title, section=str(r.get("section") or ""), header=str(r.get("header") or ""),
               text=str(r.get("text") or ""), score=round(float(score), 4), metadata=md)


def _client_of(h: Hit) -> str:
    """The key _fill() uses to keep one exemplar per client: the cleaned client name, else
    the framework name, else the doc_id."""
    return normalise.clean(h.metadata.get("client") or h.metadata.get("framework_name") or h.doc_id)


def _truncate(h: Hit, limit: int) -> Hit:
    """Cut a hit to `limit` tokens, marking the cut. Used only when a single hit exceeds
    the hard maximum — a truncated best answer still tells the reader what it is and
    where to look it up, which a dropped one does not."""
    keep = max(int(limit * 3.5) - 120, 200)          # estimate_tokens uses 3.5 chars/token
    body = h.text[:keep].rstrip()
    return Hit(cite=h.cite, doc_id=h.doc_id, source=h.source, bucket=h.bucket, title=h.title,
               section=h.section, header=h.header, score=h.score, metadata=h.metadata,
               text=body + f"\n… [truncated — read {h.cite} in full for the rest]")


def _fill(hits: list[Hit], budget: int, max_hit: int | None = None,
          one_per_client: bool = False, hard_max_factor: float = HARD_MAX_FACTOR) -> Block:
    """Fill a block toward its token target, in rank order.

    The target is not a wall. Three rules, in order of precedence:

    1.  THE TOP-RANKED HIT IS ALWAYS INCLUDED, whatever its size. Returning the fifth-best
        constraint while silently dropping the best one for being long is a worse answer,
        not a smaller one — and the reader has no way to tell it happened. This is the
        rule that matters; the others exist to bound it.
    2.  After that, a hit is taken while it fits the target, and skipped if it does not.
        Skipping rather than truncating, because half an award case is not evidence.
    3.  A hard maximum (1.6x the target) bounds the whole block so one pathological chunk
        cannot blow the prompt. A hit above it is TRUNCATED with a marker rather than
        dropped, so the reader still gets the best answer and knows it was cut.

    Overshooting the target is recorded on the block and surfaces in the trace, so it is
    visible rather than silent. Going a few hundred tokens over costs fractions of a
    cent; quietly serving a worse answer costs a planner their trust in the panel."""
    block = Block(bucket=hits[0].bucket if hits else "", budget=budget)
    hard_max = int(budget * hard_max_factor)
    spent = 0
    seen_clients: set[str] = set()
    for h in hits:
        client = _client_of(h) if one_per_client else ""
        if client and client in seen_clients:
            block.dropped += 1
            continue
        first = not block.hits
        if first:
            # rule 1: the best hit always goes in, bounded only by the hard maximum
            if h.tokens > hard_max:
                h = _truncate(h, hard_max)
                block.truncated += 1
            block.hits.append(h)
            spent += h.tokens
        elif max_hit and h.tokens > max_hit:
            block.dropped += 1
            continue
        elif spent + h.tokens <= budget:
            block.hits.append(h)
            spent += h.tokens
        else:
            block.dropped += 1
            continue
        if client:
            seen_clients.add(client)
    block.over_target = spent > budget
    return block


def build(pairs: dict, *, index_dir=None, budget: dict | None = None,
          candidates: int = CANDIDATES, brand: str | None = None,
          tenant: str | None = None, context: str = "", admission: dict | None = None,
          chain=None) -> BriefContext:
    """The entry point. Campaign pairs in, four budgeted citable blocks out.

    `brand` is the authorised brand for this run and the only way to unlock brand-scoped
    material — see scopes_for(). `tenant` is the agency, and the only way to reach
    anything that agency produced. Omitting either is safe, not lax: the run gets the
    licensed house corpus and category material only."""
    import rag

    budget = {**DEFAULT_BUDGET, **(budget or {})}
    query, keywords, filters, notes = plan(pairs)
    scopes = scopes_for(pairs, filters, brand=brand, notes=notes)
    tenants = tenants_for(tenant)

    store = rag.open_store(index_dir)
    search_text = " ".join([query] + keywords).strip() or "advertising strategy"
    qvec, _ = rag.embed([search_text], "query")
    qvec = rag._norm(qvec[0])

    blocks: dict[str, Block] = {}
    widened: list[str] = list(notes)
    used_filters: dict = {}

    # Validation (plan steps 3-4). `chain` None means the process-wide chain from
    # RAG_VALIDATOR; unset there means validation off and build() behaves exactly as
    # before. Retrieval widens only when a validator is live, never narrows.
    chain = default_chain() if chain is None else chain
    if not chain.empty:
        candidates = chain.pool_width(candidates)
    hits_by: dict[str, list[Hit]] = {}

    for bucket in ("exemplars", "craft", "rules", "instructions"):
        where = bucket_filters(bucket, filters, scopes, tenants)
        rows = rag.search_vec(store, qvec, search_text, k=candidates, where=where)

        # widen one step at a time rather than return an empty block
        if not rows and bucket == "exemplars":
            # Category before problem type, on the creative director's ruling: the best
            # precedent for a bank brief is often a beer campaign that solved the same
            # PROBLEM — a low-interest category, a distinctiveness deficit, a behaviour
            # that needs a nudge. Sector is the weakest predictor of whether a case is
            # useful, so it is the first thing to give up when the filter is too narrow.
            for drop in ("category", "effectiveness_type"):
                if drop in where:
                    where = {k: v for k, v in where.items() if k != drop}
                    widened.append(f"{bucket}: dropped {drop}")
                    rows = rag.search_vec(store, qvec, search_text, k=candidates, where=where)
                    if rows:
                        break

        used_filters[bucket] = where
        rows = _collapse(rows, store)
        hits = [_to_hit(s, r, bucket) for s, r in rows]
        if bucket == "rules":
            hits.sort(key=lambda h: (-_scope_rank(h.metadata), -h.score))
        hits_by[bucket] = hits

    validation = _apply_validation(hits_by, chain, query=search_text, context=context,
                                   admission=admission)
    order = (os.environ.get("RAG_ORDER") or "score").strip().lower()
    for bucket, hits in hits_by.items():
        blocks[bucket] = _fill(hits, budget[bucket], MAX_HIT_TOKENS.get(bucket),
                               one_per_client=(bucket == "exemplars"))
        # Edge ordering is applied AFTER the budget, to what the reader will see, and only
        # to the similarity-ranked blocks; rules keep constraints first by design.
        if order == "edge" and bucket in EDGE_ORDER_BUCKETS:
            blocks[bucket].hits = edge_order(blocks[bucket].hits)
    if validation is not None:
        validation["order"] = order

    # Egress check. Every filter above constrains what we ASK for; nothing until now
    # checked what came BACK. A hit outside the allowed scopes is a confidentiality
    # breach, and check_grounding() cannot catch it — a leaked chunk that is present in
    # the context block is, by its definition, perfectly grounded.
    allowed = set(scopes)
    allowed_tenants = set(tenants)
    egress: list[dict] = []
    for b in blocks.values():
        for h in list(b.hits):
            sc = str(h.metadata.get("scope") or "global")
            tn = str(h.metadata.get("tenant") or "house")
            # Tenant is checked first: another agency's material is the worse breach, and
            # its scope value is meaningless here anyway — `brand:bmw` from a different
            # agency is not this agency's BMW.
            bad = ("tenant", tn, sorted(allowed_tenants)) if tn not in allowed_tenants else \
                  ("scope", sc, sorted(allowed)) if sc not in allowed else None
            if bad:
                field_name, got, allow = bad
                b.hits.remove(h)
                b.dropped += 1
                widened.append(f"EGRESS: dropped {h.cite} — {field_name} {got!r} not in {allow}")
                # Also recorded structurally. An egress drop is the filter failing and the
                # egress check catching it, so it is evidence of a bug somewhere upstream —
                # it should be findable by query, not only by reading run logs.
                egress.append({"cite": h.cite, "bucket": b.bucket, "on": field_name,
                               "scope": sc, "tenant": tn, "doc_id": h.doc_id,
                               "allowed": allow})

    return BriefContext(blocks=blocks, query=query, keywords=keywords,
                        filters=used_filters, widened=widened, scopes=list(scopes),
                        tenants=list(tenants), egress=egress, validation=validation)


# ---- validation and ordering (plan steps 3-4) ------------------------------------
EDGE_ORDER_BUCKETS = ("exemplars", "craft")
VALIDATION_FLOOR = 2          # Sai: when every chunk fails, the top-scoring come back flagged
JUDGE_MIN_SHARE = 5           # hits judged per bucket at least; ~a bucket's budget holds 5-9
CONTEXT_MAX_CHARS = 4000      # brief context handed to the validator, clipped


def edge_order(items: list) -> list:
    """Strongest at both ends, weakest in the middle: [1,2,3,4,5,6] -> [1,3,5,6,4,2].

    Models attend most to the start and end of a context block ('lost in the middle'),
    so the best item goes first, the second-best last, and so on inwards. Pure and
    stable; off unless RAG_ORDER=edge, pending the golden_critic A/B (plan step 6)."""
    front, back = [], []
    for i, x in enumerate(items):
        (front if i % 2 == 0 else back).append(x)
    return front + back[::-1]


@functools.lru_cache(maxsize=1)
def default_chain():
    """The process-wide validation chain from RAG_VALIDATOR, built once so its breaker
    state survives across briefs. RAG_VALIDATOR is therefore read ONCE per process:
    changing it needs a restart (or default_chain.cache_clear() in tests). Raises BackendNotConfigured if a named backend cannot
    be built — a configuration error is a hard error (judgement invariant M4)."""
    import judge
    return judge.chain_from_env()


def _passage_text(h: Hit) -> str:
    """What a validator reads for a hit: header then body, as calibration was fitted on."""
    return f"{h.header}\n{h.text}" if h.header else h.text


def _apply_validation(hits_by: dict[str, list[Hit]], chain, *, query: str, context: str = "",
                      admission: dict | None = None) -> dict | None:
    """Admit, judge and select every bucket's hits in place. Returns the run record, or
    None when validation is off (empty chain) and there were no admission rules.

    One validator call per brief: every bucket's candidates are combined, deduplicated by
    cite and interleaved by rank, so a capacity-limited backend (local judges 20 per
    call) sees the best of every bucket rather than only exemplars. The verdicts are
    split back per bucket, and judge.select() applies the floor per bucket, so a bucket
    is emptied only by admission rules, never by the relevance gate.

    Reviewer rejections (rules with verdict=rejected) are exempt from the relevance gate:
    a human's never-do-this applies whether or not it resembles the query. Admission
    rules (judge_code.admit: excluded ids, oversized chunks, recency) run first on every
    hit, exempt or not."""
    import judge_code
    adm = admission or {}
    refused: list[dict] = []
    if adm:
        excl = frozenset(str(x) for x in adm.get("exclude_doc_ids") or ())
        for bucket, hits in hits_by.items():
            kept = []
            for h in hits:
                md = h.metadata if h.metadata.get("doc_id") else {**h.metadata, "doc_id": h.doc_id}
                why = judge_code.admit(md, h.text, exclude_doc_ids=excl,
                                       recency_years=adm.get("recency_years"))
                if why:
                    refused.append({"cite": h.cite, "bucket": bucket, "reason": why})
                else:
                    kept.append(h)
            hits_by[bucket] = kept
    if chain.empty:
        return {"admission_refused": refused} if refused else None

    from judge import select
    from judge_base import Passage, Query
    # RAG_VALIDATION_MODE: `order` (default) re-sorts each bucket by the validator's score
    # and drops nothing; `gate` also drops what fails the threshold (floor per bucket).
    # Order is the default because, on 392 labelled brief pairs, nemotron's gate kept
    # only 1/24 useful client exemplars and 2/32 useful client craft chunks (step 6.2,
    # labelset.py evaluate) — a QA reranker's threshold does not transfer to briefs.
    mode = (os.environ.get("RAG_VALIDATION_MODE") or "order").strip().lower()
    exempt = lambda h: h.bucket == "rules" and h.metadata.get("verdict") == "rejected"
    # Judge only what can reach the prompt: each non-empty bucket gets an equal share of
    # the lead backend's capacity (at least JUDGE_MIN_SHARE), taken in rank order. A
    # bucket's budget holds ~5-9 hits, so its tail past the share cannot reach the prompt
    # anyway — and keeping an unjudged tail while its judged top was rejected would invert
    # the ranking (measured on a live brief: 19 unjudged low-ranked hits survived).
    live = [hs for hs in hits_by.values() if any(not exempt(h) for h in hs)]
    share = max(JUDGE_MIN_SHARE, chain.pool_width(0) // max(1, len(live)))
    # Round-robin by rank across buckets, deduplicated, so truncation at capacity is fair.
    pool: list[Hit] = []
    seen: set[str] = set()
    queues = [[h for h in hits if not exempt(h)][:share] for hits in hits_by.values()]
    for rank in range(max((len(q) for q in queues), default=0)):
        for q in queues:
            if rank < len(q) and q[rank].cite not in seen:
                seen.add(q[rank].cite); pool.append(q[rank])
    result = chain.judge(Query(text=query, context=(context or "")[:CONTEXT_MAX_CHARS]),
                         [Passage(h.cite, _passage_text(h)) for h in pool])
    by_cite = dict(zip((h.cite for h in pool), result.aligned()))
    per_bucket: dict[str, dict] = {}
    for bucket, hits in hits_by.items():
        keep_first = [h for h in hits if exempt(h)]
        for h in keep_first:
            h.relevance = {"value": True, "score": None, "backend": "reviewer_rejection",
                           "kept": "exempt"}
        chosen = []
        if mode != "gate":
            live_hits = [h for h in hits if not exempt(h)]
            def okey(i):
                """Judged by score (else raw) desc, then unjudged in fused order."""
                v = by_cite.get(live_hits[i].cite)
                return (1, 0.0, i) if v is None else (0, -(v.score if v.score is not None else (v.raw or 0.0)), i)
            for i in sorted(range(len(live_hits)), key=okey):
                h, v = live_hits[i], by_cite.get(live_hits[i].cite)
                h.relevance = ({**v.as_contract(), "kept": "ordered"} if v is not None
                               else {"value": True, "score": None, "backend": "none", "kept": "unjudged"})
                chosen.append(h)
            counts = {"ordered": sum(1 for h in chosen if h.relevance["kept"] == "ordered"),
                      "unjudged": sum(1 for h in chosen if h.relevance["kept"] == "unjudged"),
                      "exempt": len(keep_first), "rejected": 0}
            per_bucket[bucket] = counts
            hits_by[bucket] = keep_first + chosen
            continue
        for h, v, kept in select([(h, by_cite.get(h.cite)) for h in hits if not exempt(h)],
                                 floor=VALIDATION_FLOOR, default_width=share):
            h.relevance = ({**v.as_contract(), "kept": kept} if v is not None
                           else {"value": True, "score": None, "backend": "none", "kept": kept})
            chosen.append(h)
        counts: dict[str, int] = {}
        for h in keep_first + chosen:
            counts[h.relevance["kept"]] = counts.get(h.relevance["kept"], 0) + 1
        # Counted directly from verdicts, never by subtraction: a hit the backend never saw
        # (truncated at its real capacity, or nobody answered) is unjudged, not rejected.
        # The subtraction version reported a total outage as a total rejection.
        chosen_ids = {id(h) for h in chosen}
        counts["rejected"] = sum(1 for h in hits if not exempt(h) and id(h) not in chosen_ids
                                 and by_cite.get(h.cite) is not None)
        counts["beyond_share"] = max(0, sum(1 for h in hits if not exempt(h)) - share)
        per_bucket[bucket] = counts
        hits_by[bucket] = keep_first + chosen
    out = result.as_dict()
    out["contract"] = result.as_contract()
    out["per_bucket"] = per_bucket
    out["calls"] = 1 if pool else 0
    out["mode"] = mode
    if refused:
        out["admission_refused"] = refused
    return out


if __name__ == "__main__":                       # manual check
    import json
    demo = {"brand": "BMW", "category": "Automotive", "campaign_type": "launch",
            "product": "new hybrid series",
            "problem": "buyers see hybrids as a compromise rather than an upgrade",
            "audience": "urban professionals 30-45 considering their first electrified car",
            "objective": "shift consideration without discounting"}
    ctx = build(demo, index_dir=Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(json.dumps(ctx.trace(), indent=1))
    print("\n" + "=" * 70 + "\n")
    print(ctx.prompt_text()[:2500])
