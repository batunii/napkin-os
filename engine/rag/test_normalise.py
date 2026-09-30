"""Tests for normalise.py — every translation is a pure function, so each test is one
input, one expected output. When a new spelling shows up in the corpus, add a row here
first, watch it fail, then add it to the table.

Run:  cd engine/rag && python3 -m pytest test_normalise.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import normalise as n  # noqa: E402
from contract import SCHEMA  # noqa: E402


# ---- cleaning ---------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ('"Brand Strategy"', "brand strategy"),
    ("'Media  Effectiveness'", "media effectiveness"),
    ("  Comms Planning ", "comms planning"),
    (None, ""),
])
def test_clean(raw, expected):
    """clean() strips quotes and surrounding whitespace and lower-cases, turning None into
    an empty string."""
    assert n.clean(raw) == expected


def test_snake():
    """snake() turns "&"-joined phrases into lower snake_case."""
    assert n.snake("Social & Environmental Change") == "social_environmental_change"
    assert n.snake("Travel & Tourism") == "travel_tourism"


# ---- sector -> category ---------------------------------------------------------
@pytest.mark.parametrize("sector,expected", [
    ("Food & Drink", "food_drink"),
    ("Government & Public Sector", "public_sector"),
    ("Retail", "retail"),
    ("Financial Services", "financial_services"),
    ("FMCG", "fmcg"),
    ("Automotive", "automotive"),
    ("Travel & Tourism", "travel"),
    ("Alcohol", "alcohol"),
    ("Charity & NFP", "charity"),
    ("Telecoms", "telecoms"),
    ("Entertainment & Media", "media_entertainment"),
    ("Healthcare", "healthcare"),
    ("Fashion & Beauty", "fashion_beauty"),
    ("Technology", "technology"),
    ("Other", "other"),
    ("general", None),          # Cannes placeholder: unknown, not 'other'
    ("", None),
    ("Book Design, Typography", None),   # D&AD discipline is not a client category
])
def test_category_from_sector(sector, expected):
    """category_from_sector() maps each known IPA sector label to its contract category,
    and returns None for unusable or unknown sectors rather than guessing."""
    assert n.category_from_sector(sector) == expected


def test_every_sector_target_is_a_contract_category():
    """Every value category_from_sector() can produce is a real contract category."""
    allowed = set(SCHEMA.enum_values("category"))
    assert set(n.SECTOR_TO_CATEGORY.values()) <= allowed


# ---- award tier -----------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Gold", "gold"), ("Silver", "silver"), ("Bronze", "bronze"), ("Grand Prix", "grand_prix"),
    ("Gold Cannes Lions", "gold"), ("Grand Prix Cannes Lions", "grand_prix"),
    ("WOOD Pencil", "wood_pencil"), ("Yellow Pencil", "yellow_pencil"), ("Black Pencil", "black_pencil"),
    ("Shortlist", "shortlist"), ("Special Mention", "other"),
    ("", None), (None, None), ("not recorded", None),
])
def test_award_tier(raw, expected):
    """award_tier() maps each raw award-tier label to its contract value, an unrecognised
    named tier to "other", and blank or "not recorded" input to None."""
    assert n.award_tier(raw) == expected


def test_award_tier_values_are_in_contract():
    """Every value award_tier() can produce, including for an unrecognised label, is a
    real contract enum value."""
    allowed = set(SCHEMA.enum_values("award_tier"))
    for raw in ("Gold", "Grand Prix Cannes Lions", "WOOD Pencil", "Shortlist", "weird"):
        assert n.award_tier(raw) in allowed


# ---- IPA enums ---------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Turnaround", "turnaround"), ("Brand Building", "brand_building"), ("Sustained Success", "sustained_success"),
    ("Social & Environmental Change", "social_environmental_change"), ("Launch", "launch"),
    ("Activation", "activation"), ("Challenger", "challenger"), ("Other", "other"), ("", None), ("Made up", None),
])
def test_effectiveness_type(raw, expected):
    """effectiveness_type() maps each known IPA effectiveness label to its contract value
    and returns None for blank or unrecognised input."""
    assert n.effectiveness_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Reframing", "reframing"), ("Emotional", "emotional"), ("Rational", "rational"), ("Challenger", "challenger"),
    ("Humor", "humor"), ("Humour", "humor"), ("Community", "community"), ("Purpose", "purpose"), ("Other", "other"),
])
def test_strategic_territory(raw, expected):
    """strategic_territory() maps each label to its contract value, folding the British
    and American spellings of humour/humor to the same result."""
    assert n.strategic_territory(raw) == expected


# ---- discipline / lions -------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ('"Brand Strategy"', "brand_strategy"), ("'Media Effectiveness'", "media_effectiveness"),
    ("Thinking Models", "thinking_models"), ("Behavioural Science", "behavioural_science"),
    ("Trends Foresight", "trends_foresight"), ("Something New", "other"), ("", None),
])
def test_discipline(raw, expected):
    """discipline() maps each known playbook discipline label to its contract value, an
    unrecognised one to "other", and blank input to None."""
    assert n.discipline(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Film", "film"), ("PR", "pr"), ("Creative Strategy", "creative_strategy"), ("Glass", "other"), ("", None),
])
def test_lions_category(raw, expected):
    """lions_category() maps each known Cannes Lions category to its contract value, an
    unrecognised one to "other", and blank input to None."""
    assert n.lions_category(raw) == expected


# ---- section role + bucket ----------------------------------------------------------------
@pytest.mark.parametrize("source,heading,level,expected", [
    ("ipa", "Insight", "child", "insight"),
    ("ipa", "Strategic Approach", "child", "strategic_approach"),
    ("ipa", "The Idea", "child", "idea"),
    ("ipa", "Execution", "child", "execution"),
    ("ipa", "Results", "child", "results"),
    ("ipa", "What Made It Work", "child", "what_made_it_work"),
    ("ipa", "When the chips are down (2024)", "parent", "whole"),
    ("playbook", "SECTION 1: IDENTITY CARD", "chunk", "identity_card"),
    ("playbook", "SECTION 2: STEP-BY-STEP APPLICATION PROCESS", "chunk", "process"),
    ("playbook", "3. GUIDING QUESTIONS PER STEP", "chunk", "guiding_questions"),
    ("playbook", "SECTION 4: WORKED EXAMPLE A (HIGH INVOLVEMENT / THINK)", "chunk", "worked_example"),
    ("playbook", "6. COMMON MISTAKES & HOW TO AVOID THEM", "chunk", "common_mistakes"),
    ("playbook", "SECTION 7: OUTPUT TEMPLATE", "chunk", "output_template"),
    ("playbook", "OUTPUT TEMPLATES", "chunk", "output_template"),
    ("playbook", "SECTION 8: DECISION RULES", "chunk", "decision_rules"),
    ("playbook", "SECTION 9: SOURCE BIBLIOGRAPHY", "chunk", "bibliography"),
    ("playbook", "THOUGHT PROCESS TEMPLATE", "chunk", "thought_process"),
    ("playbook", "HOW THIS THINKING TOOL WORKS", "chunk", "how_it_works"),
    ("playbook", "STEP-BY-STEP RESEARCH PROCESS", "chunk", "process"),
    ("cannes", "Why is this work relevant for Film?", "child", "entry_answer"),
    ("cannes", "Describe the Impact", "child", "entry_answer"),
    ("dandad", "Overview", "child", "overview"),
    ("template", "Design Principles", "chunk", "template_section"),
    ("playbook", "Some Unexpected Heading", "chunk", "other"),
    ("playbook", "SECTION 7: TEMPLATES", "chunk", "output_template"),
    ("playbook", "SECTION 2: WHAT IT IS & WHEN IT HAPPENS", "chunk", "how_it_works"),
    ("ipa", "2. Sector & Category Trends", "chunk", "synthesis"),
])
def test_section_role(source, heading, level, expected):
    """section_role() maps each source/heading/level combination to the expected role,
    including numbered and re-worded playbook section headings and an unmatched heading
    falling to "other"."""
    assert n.section_role(source, heading, level) == expected


@pytest.mark.parametrize("source,role,doc_kind,expected", [
    ("ipa", "insight", "ipa_effectiveness_case", "exemplars"),
    ("ipa", "whole", "ipa_effectiveness_case", "exemplars"),
    ("ipa", "other", "synthesis", "craft"),
    ("playbook", "process", "Comms Planning", "craft"),
    ("playbook", "worked_example", "Comms Planning", "exemplars"),
    ("playbook", "common_mistakes", "Comms Planning", "rules"),
    ("playbook", "decision_rules", "Comms Planning", "rules"),
    ("playbook", "other", "Comms Planning", "craft"),
    ("cannes", "entry_answer", "cannes_case", "exemplars"),
    ("template", "template_section", None, "instructions"),
    ("template", "other", None, "instructions"),
])
def test_bucket(source, role, doc_kind, expected):
    """bucket() maps each source/role/doc_kind combination to the correct prompt bucket."""
    assert n.bucket(source, role, doc_kind) == expected


def test_role_and_bucket_values_are_in_contract():
    """The role-to-bucket and source-to-bucket lookup tables, and the not-embedded roles
    set, only use values the contract actually defines."""
    roles = set(SCHEMA.enum_values("section_role")); buckets = set(SCHEMA.enum_values("bucket"))
    assert set(n._ROLE_BUCKET) <= roles
    assert set(n._ROLE_BUCKET.values()) <= buckets
    assert set(n._SOURCE_BUCKET.values()) <= buckets
    assert n.NOT_EMBEDDED_ROLES <= roles


# ---- client -> category (Cannes enrichment) ------------------------------------------------
@pytest.mark.parametrize("client,expected", [
    ("APPLE", "technology"), ("OPEN AI", "technology"), ("CLAUDE", "technology"),
    ("COINBASE", "financial_services"), ("AXA", "financial_services"), ("SHIELD INSURANCE", "financial_services"),
    ("AMAZON", "retail"), ("IKEA", "retail"), ("INTERMARCHÉ", "retail"),
    # the two the creative director changed on 2026-09-17
    ("THE REALREAL", "luxury"), ("LA UNION NEWSPAPER AND ARTICLE 19", "charity"),
    ("KITKAT", "food_drink"), ("HEINZ KETCHUP & MUSTARD", "food_drink"), ("UBER EATS", "food_drink"),
    ("STELLA ARTOIS", "alcohol"), ("ANDREX", "fmcg"), ("DOVE", "fashion_beauty"),
    ("ELI LILLY", "healthcare"), ("SPECSAVERS", "healthcare"),
    ("XBOX", "media_entertainment"), ("PARIS 2024", "media_entertainment"),
    ("INDIAN RAILWAYS", "travel"), ("ASUNIWA", "charity"), ("O2", "telecoms"),
    ("SOME BRAND WE HAVE NEVER SEEN", None), ("", None), (None, None),
])
def test_category_from_client(client, expected):
    """category_from_client() maps each known Cannes client name to its contract category,
    including the two the creative director changed on 2026-09-17, and returns None for an
    unrecognised or blank client rather than guessing."""
    assert n.category_from_client(client) == expected


def test_every_client_target_is_a_contract_category():
    """Every value category_from_client() can produce is a real contract category."""
    assert set(n.CLIENT_TO_CATEGORY.values()) <= set(SCHEMA.enum_values("category"))


def test_category_for_prefers_sector_then_client():
    """category_for() prefers a usable sector over the client lookup, falls back to client
    when the sector is unusable ("general") or absent, and never guesses when neither
    resolves."""
    assert n.category_for("Food & Drink", "APPLE") == "food_drink"   # sector wins when usable
    assert n.category_for("general", "APPLE") == "technology"        # Cannes: sector unusable
    assert n.category_for("", "IKEA") == "retail"
    assert n.category_for("general", "UNKNOWN BRAND") is None        # never guesses
