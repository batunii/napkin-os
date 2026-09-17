#!/usr/bin/env python3
"""
normalise.py — turn the corpus's free-text labels into the contract's closed enum values.

The awards bodies and playbook authors wrote labels for humans: "Food & Drink",
"Gold Cannes Lions", '"Brand Strategy"', "SECTION 6: COMMON MISTAKES & HOW TO AVOID THEM".
The contract wants machine-stable values: food_drink, gold, brand_strategy, common_mistakes.
This module is the ONLY place that translation happens, so a spelling fix is one edit.

Two rules, both deliberate:
  * Every function returns a contract value or None. It never invents a value: if a
    label is not in its table, the answer is None (or "other" where the contract has
    it), and the caller decides. Silent guesses would put unknown strings into filter
    fields, which is exactly what a closed list exists to prevent.
  * Lookups are keyed on a cleaned form (lower-case, quotes stripped, whitespace
    collapsed) so "Brand Strategy", '"Brand Strategy"' and "brand strategy" all hit
    the same row. The cleaning is cosmetic; it never changes meaning.

Everything here is a pure function of its input. That makes it trivially testable and
safe to run over 5,000 chunks.
"""
from __future__ import annotations

import re

# ---- shared cleaning -------------------------------------------------------
_WS = re.compile(r"\s+")


def clean(label) -> str:
    """Lower-case, strip surrounding quotes/whitespace, collapse inner whitespace."""
    s = str(label or "").strip().strip("'\"").strip()
    return _WS.sub(" ", s).lower()


def snake(label) -> str:
    """'Social & Environmental Change' -> 'social_environmental_change'."""
    s = clean(label)
    s = re.sub(r"[&/+]", " ", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


# ---- sector -> category (IPA / Effie awarding-body sector -> contract category) ----
# Keyed on clean(sector). Values are contract `category` enum members.
SECTOR_TO_CATEGORY: dict[str, str] = {
    "food & drink": "food_drink",
    "food and drink": "food_drink",
    "alcohol": "alcohol",
    "fmcg": "fmcg",
    "government & public sector": "public_sector",
    "government and public sector": "public_sector",
    "public sector": "public_sector",
    "retail": "retail",
    "financial services": "financial_services",
    "automotive": "automotive",
    "travel & tourism": "travel",
    "travel and tourism": "travel",
    "travel": "travel",
    "charity & nfp": "charity",
    "charity and nfp": "charity",
    "charity": "charity",
    "telecoms": "telecoms",
    "telecommunications": "telecoms",
    "entertainment & media": "media_entertainment",
    "media & entertainment": "media_entertainment",
    "healthcare": "healthcare",
    "health": "healthcare",
    "fashion & beauty": "fashion_beauty",
    "technology": "technology",
    "tech": "technology",
    "other": "other",
}


# ---- client -> category (enrichment output, not a spelling table) ----------------
# Cannes records `sector: general` on nearly every entry, so the sector lookup below
# yields nothing for that source. Every assignment here was made on 2026-09-17 by
# reading that entry's own Brand Context in the corpus, then checking the call against
# how the IPA corpus (whose sectors come from the awarding body) classifies analogous
# clients. The full audit trail, one row per entry with its evidence, is in
# engine/schema/REVIEW-cannes-categories.md — that file is what a creative director
# signs off in Phase 0.
#
# The rule, taken from the IPA corpus's own behaviour: classify by what the client
# SELLS in the campaign at hand, not by the technology it sells through. IPA reserves
# `Technology` for makers of the technology itself (IBM, Kodak, Motorola, Nikon,
# Polaroid, Sony Ericsson) — so a shop that happens to run on an app is still a shop.
#
# Precedent found in the IPA corpus for the non-obvious calls:
#   DOVE, AXE/LYNX -> fashion_beauty   (IPA files P&G, Unilever and L'Oréal under
#                                       Fashion & Beauty; their household campaigns
#                                       sit under FMCG — the sector follows the campaign)
#   newspapers     -> media_entertainment (IPA: The Guardian, The Economist,
#                                       Manchester Evening News)
#   PARIS 2024     -> media_entertainment (IPA: Rugby League World Cup 2021, Formula 1,
#                                       Manchester City FC. The Olympic *Delivery
#                                       Authority* is Government & Public Sector, but
#                                       that body built venues; this client staged the
#                                       ceremony)
#   INDIAN RAILWAYS-> travel           (IPA files rail OPERATORS under Travel & Tourism:
#                                       LNER, Virgin Trains, East Midlands Trains,
#                                       Eurostar, MTR. Government transport BODIES —
#                                       Transport for London, Dept for Transport — are
#                                       Public Sector. Indian Railways sells journeys)
#   SPECSAVERS     -> healthcare       (IPA carries Specsavers as BOTH Healthcare and
#                                       Retail depending on the campaign; this entry is
#                                       about hearing loss and driving hearing tests)
#   PEDIGREE       -> fmcg             (IPA: Mars Petcare and Nestle Purina Petcare ->
#                                       FMCG. The corpus is itself split on pet food —
#                                       Friskies Petcare sits under Food & Drink — so
#                                       this one is genuinely arguable)
#   ANDREX, ANGEL SOFT, ZIPLOC -> fmcg (IPA: Kimberly-Clark, SCA, Scott, Henkel
#                                       Consumer Adhesives -> FMCG)
#
# Four entries needed the file read because the brand is not widely known: ASUNIWA (a
# Japanese gender-equality advocacy body -> charity), LA UNION NEWSPAPER AND ARTICLE 19
# (a newspaper's press-freedom campaign -> media_entertainment), SHIELD INSURANCE (a Thai
# insurance broker -> financial_services), PARIS 2024 (the Olympic opening ceremony).
#
# FLAGGED FOR PHASE 0 — defensible either way, listed so they are argued with rather
# than rediscovered: delivery marketplaces have no home in this taxonomy, so they are
# classified by what they deliver (INSTACART groceries -> retail; UBER EATS restaurant
# meals -> food_drink); THE REALREAL sells pre-owned fashion but the campaign is about
# authentication, a retail-trust problem -> retail; SPECSAVERS and PEDIGREE as above.
#
# Keys are clean(client). A client absent from this table returns None (unknown), never
# a guess. Real client work takes its category from the brand clan, not from here.
CLIENT_TO_CATEGORY: dict[str, str] = {
    # technology — the product IS the technology (IPA precedent: device makers)
    "apple": "technology", "open ai": "technology", "openai": "technology",
    "claude": "technology", "life360": "technology",
    # financial services
    "coinbase": "financial_services", "thai life insurance": "financial_services",
    "shield insurance": "financial_services", "axa": "financial_services",
    "axa france": "financial_services", "nordea": "financial_services", "rocket": "financial_services",
    # retail — shops and marketplaces, including those that sell only through an app
    "the realreal": "retail", "john lewis & partners": "retail", "instacart": "retail",
    "intermarche": "retail", "intermarché": "retail", "amazon": "retail",
    "mercado livre": "retail", "ikea": "retail", "penny": "retail", "lidl": "retail",
    # food & drink — food and drink brands, restaurants, and prepared-food delivery
    "uber eats": "food_drink", "kfc": "food_drink", "nutter butter": "food_drink",
    "progresso": "food_drink", "heinz": "food_drink", "heinz ketchup & mustard": "food_drink",
    "kitkat": "food_drink", "coca-cola": "food_drink", "coca cola": "food_drink",
    # alcohol
    "hertog jan": "alcohol", "corona": "alcohol", "skol": "alcohol", "stella artois": "alcohol",
    # fmcg — household and personal paper goods, pet food
    "andrex": "fmcg", "pedigree": "fmcg", "angel soft": "fmcg", "ziploc": "fmcg",
    # beauty & personal care
    "dove": "fashion_beauty", "axe/lynx": "fashion_beauty", "axe": "fashion_beauty", "lynx": "fashion_beauty",
    # healthcare
    "eli lilly": "healthcare", "lilly": "healthcare", "specsavers": "healthcare",
    # media & entertainment — broadcasters, publishers, games, music, sporting events
    "electronic arts skate": "media_entertainment", "electronic arts": "media_entertainment",
    "xbox": "media_entertainment", "clash of clans": "media_entertainment", "hbo": "media_entertainment",
    "rimas music": "media_entertainment", "annahar newspaper": "media_entertainment",
    "la union newspaper and article 19": "media_entertainment", "paris 2024": "media_entertainment",
    # travel — operators that sell journeys (government transport bodies are public_sector)
    "procolombia": "travel", "indian railways": "travel",
    # charity
    "fuck cancer": "charity", "asuniwa": "charity", "itv x calm": "charity", "calm": "charity",
    # telecoms
    "o2": "telecoms",
}


def category_from_client(client) -> str | None:
    """Contract category for a known public brand, or None. Never guesses."""
    c = clean(client)
    return CLIENT_TO_CATEGORY.get(c) if c else None


def category_for(sector, client=None) -> str | None:
    """The chunk's client category: the awarding body's sector when it is usable, else
    the client lookup. Sector wins because it is the awarding body's own classification
    of that piece of work; the client table is the fallback for corpora (Cannes) that
    do not record one."""
    return category_from_sector(sector) or category_from_client(client)


def category_from_sector(sector) -> str | None:
    """Contract category for an awarding-body sector label, or None when unknown.
    Cannes writes 'general' — that is unknown, not 'other': the entry has a real
    category we just don't know yet (step 2 fills it from the client name)."""
    c = clean(sector)
    if not c or c == "general":
        return None
    return SECTOR_TO_CATEGORY.get(c)


# ---- award tier ------------------------------------------------------------
_TIER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"grand\s*prix"), "grand_prix"),
    (re.compile(r"\bgold\b"), "gold"),
    (re.compile(r"\bsilver\b"), "silver"),
    (re.compile(r"\bbronze\b"), "bronze"),
    (re.compile(r"\bblack\b.*pencil|pencil.*\bblack\b"), "black_pencil"),
    (re.compile(r"\bwhite\b.*pencil|pencil.*\bwhite\b"), "white_pencil"),
    (re.compile(r"\byellow\b.*pencil|pencil.*\byellow\b"), "yellow_pencil"),
    (re.compile(r"\bgraphite\b.*pencil|pencil.*\bgraphite\b"), "graphite_pencil"),
    (re.compile(r"\bwood\b.*pencil|pencil.*\bwood\b"), "wood_pencil"),
    (re.compile(r"shortlist"), "shortlist"),
]


def award_tier(label) -> str | None:
    """'Gold Cannes Lions' -> 'gold'; 'WOOD Pencil' -> 'wood_pencil'; unknown -> 'other'; empty -> None.
    Order matters: 'Grand Prix' is checked before the metals so a hypothetical
    'Grand Prix Gold' does not become gold."""
    c = clean(label)
    if not c or c in ("none", "not recorded"):
        return None
    for pat, value in _TIER_PATTERNS:
        if pat.search(c):
            return value
    return "other"


# ---- IPA enums ---------------------------------------------------------------
_EFFECTIVENESS = {
    "turnaround": "turnaround", "brand building": "brand_building", "sustained success": "sustained_success",
    "social & environmental change": "social_environmental_change",
    "social and environmental change": "social_environmental_change",
    "launch": "launch", "activation": "activation", "challenger": "challenger", "other": "other",
}
_TERRITORY = {
    "reframing": "reframing", "emotional": "emotional", "rational": "rational", "challenger": "challenger",
    "humor": "humor", "humour": "humor", "community": "community", "purpose": "purpose", "other": "other",
}


def effectiveness_type(label) -> str | None:
    c = clean(label)
    return _EFFECTIVENESS.get(c) if c else None


def strategic_territory(label) -> str | None:
    c = clean(label)
    return _TERRITORY.get(c) if c else None


# ---- playbook discipline -------------------------------------------------------
_DISCIPLINE = {
    "brand strategy": "brand_strategy", "comms planning": "comms_planning",
    "comms planning & creative briefing": "comms_planning",
    "thinking models": "thinking_models", "media effectiveness": "media_effectiveness",
    "cultural strategy": "cultural_strategy", "behavioural science": "behavioural_science",
    "behavioral science": "behavioural_science", "research methodology": "research_methodology",
    "planning craft": "planning_craft", "audience segmentation": "audience_segmentation",
    "competitive strategy": "competitive_strategy", "trends foresight": "trends_foresight",
    "trends & foresight": "trends_foresight",
}


def discipline(label) -> str | None:
    c = clean(label)
    if not c:
        return None
    return _DISCIPLINE.get(c, "other")


# ---- Cannes Lions category --------------------------------------------------------
_LIONS = {
    "film": "film", "outdoor": "outdoor", "direct": "direct", "pr": "pr", "media": "media",
    "creative strategy": "creative_strategy", "creative effectiveness": "creative_effectiveness",
}


def lions_category(label) -> str | None:
    c = clean(label)
    if not c:
        return None
    return _LIONS.get(c, "other")


# ---- section role + bucket ------------------------------------------------------------
# Headings are matched after stripping "SECTION n:" / "n." prefixes and anything in parentheses.
_HEADING_PREFIX = re.compile(r"^(section\s*\d+\s*[:.\-]?\s*|\d+\s*[.)]\s*)", re.I)
_PAREN = re.compile(r"\s*\(.*?\)\s*")

# (pattern on the cleaned heading, role). First match wins, so specific before general.
_ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # IPA / Effie case sections
    (re.compile(r"^insight"), "insight"),
    (re.compile(r"^strategic approach|^strategy$"), "strategic_approach"),
    (re.compile(r"^the idea|^idea$|^creative idea"), "idea"),
    (re.compile(r"^execution"), "execution"),
    (re.compile(r"^results"), "results"),
    (re.compile(r"^what made it work|^learnings|^why it worked"), "what_made_it_work"),
    # playbook sections
    (re.compile(r"identity card"), "identity_card"),
    (re.compile(r"step[- ]by[- ]step|application process|research process"), "process"),
    (re.compile(r"guiding questions"), "guiding_questions"),
    (re.compile(r"worked example"), "worked_example"),
    (re.compile(r"common mistakes"), "common_mistakes"),
    (re.compile(r"output template|^templates?$"), "output_template"),
    (re.compile(r"decision rules"), "decision_rules"),
    (re.compile(r"bibliography|sources?$|references"), "bibliography"),
    (re.compile(r"thought process"), "thought_process"),
    (re.compile(r"how this .* works|how it works|what it is"), "how_it_works"),
    # D&AD
    (re.compile(r"^overview"), "overview"),
]


def section_role(source: str | None, heading: str, level: str = "chunk") -> str:
    """Contract section_role for a chunk. Parents covering a whole document are 'whole'.
    Cannes children are entry-form answers ('Why is this work relevant for Film?') and
    get 'entry_answer' regardless of wording; templates get 'template_section'."""
    if level == "parent":
        return "whole"
    if source == "ipa" and level == "chunk":
        # The only IPA files chunked as plain windows are the cross-case meta documents
        # (pattern analysis, execution pack). They speak about all cases at once.
        return "synthesis"
    h = _PAREN.sub(" ", _HEADING_PREFIX.sub("", clean(heading))).strip()
    for pat, role in _ROLE_PATTERNS:
        if pat.search(h):
            return role
    if source == "cannes":
        return "entry_answer"
    if source == "template":
        return "template_section"
    return "other"


# section_role -> bucket. Anything not listed falls back per source.
_ROLE_BUCKET = {
    "insight": "exemplars", "strategic_approach": "exemplars", "idea": "exemplars", "execution": "exemplars",
    "results": "exemplars", "what_made_it_work": "exemplars", "entry_answer": "exemplars",
    "overview": "exemplars", "worked_example": "exemplars",
    "process": "craft", "guiding_questions": "craft", "output_template": "craft",
    "thought_process": "craft", "how_it_works": "craft", "synthesis": "craft",
    "common_mistakes": "rules", "decision_rules": "rules",
    "template_section": "instructions", "criteria": "instructions",
}
_SOURCE_BUCKET = {"ipa": "exemplars", "effie": "exemplars", "cannes": "exemplars", "dandad": "exemplars",
                  "dossier": "exemplars", "playbook": "craft", "template": "instructions"}

# Playbook sections that carry no retrievable lesson. Kept as parent payload, not embedded.
NOT_EMBEDDED_ROLES = frozenset({"identity_card", "bibliography"})


def bucket(source: str | None, role: str, doc_kind: str | None = None) -> str:
    """Which prompt slot a chunk can fill. IPA synthesis docs are craft even though the
    source is ipa; a whole-document parent takes its source's default."""
    if doc_kind and "synthesis" in clean(doc_kind):
        return "craft"
    if role in _ROLE_BUCKET:
        return _ROLE_BUCKET[role]
    return _SOURCE_BUCKET.get(source or "", "exemplars")
