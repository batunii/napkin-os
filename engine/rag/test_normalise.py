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
    assert n.clean(raw) == expected


def test_snake():
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
    assert n.category_from_sector(sector) == expected


def test_every_sector_target_is_a_contract_category():
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
    assert n.award_tier(raw) == expected


def test_award_tier_values_are_in_contract():
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
    assert n.effectiveness_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Reframing", "reframing"), ("Emotional", "emotional"), ("Rational", "rational"), ("Challenger", "challenger"),
    ("Humor", "humor"), ("Humour", "humor"), ("Community", "community"), ("Purpose", "purpose"), ("Other", "other"),
])
def test_strategic_territory(raw, expected):
    assert n.strategic_territory(raw) == expected


# ---- discipline / lions -------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ('"Brand Strategy"', "brand_strategy"), ("'Media Effectiveness'", "media_effectiveness"),
    ("Thinking Models", "thinking_models"), ("Behavioural Science", "behavioural_science"),
    ("Trends Foresight", "trends_foresight"), ("Something New", "other"), ("", None),
])
def test_discipline(raw, expected):
    assert n.discipline(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Film", "film"), ("PR", "pr"), ("Creative Strategy", "creative_strategy"), ("Glass", "other"), ("", None),
])
def test_lions_category(raw, expected):
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
    assert n.bucket(source, role, doc_kind) == expected


def test_role_and_bucket_values_are_in_contract():
    roles = set(SCHEMA.enum_values("section_role")); buckets = set(SCHEMA.enum_values("bucket"))
    assert set(n._ROLE_BUCKET) <= roles
    assert set(n._ROLE_BUCKET.values()) <= buckets
    assert set(n._SOURCE_BUCKET.values()) <= buckets
    assert n.NOT_EMBEDDED_ROLES <= roles


# ---- client -> category (Cannes enrichment) ------------------------------------------------
@pytest.mark.parametrize("client,expected", [
    ("APPLE", "technology"), ("OPEN AI", "technology"), ("CLAUDE", "technology"),
    ("COINBASE", "financial_services"), ("AXA", "financial_services"), ("SHIELD INSURANCE", "financial_services"),
    ("AMAZON", "retail"), ("IKEA", "retail"), ("THE REALREAL", "retail"), ("INTERMARCHÉ", "retail"),
    ("KITKAT", "food_drink"), ("HEINZ KETCHUP & MUSTARD", "food_drink"), ("UBER EATS", "food_drink"),
    ("STELLA ARTOIS", "alcohol"), ("ANDREX", "fmcg"), ("DOVE", "fashion_beauty"),
    ("ELI LILLY", "healthcare"), ("SPECSAVERS", "healthcare"),
    ("XBOX", "media_entertainment"), ("PARIS 2024", "media_entertainment"),
    ("INDIAN RAILWAYS", "travel"), ("ASUNIWA", "charity"), ("O2", "telecoms"),
    ("SOME BRAND WE HAVE NEVER SEEN", None), ("", None), (None, None),
])
def test_category_from_client(client, expected):
    assert n.category_from_client(client) == expected


def test_every_client_target_is_a_contract_category():
    assert set(n.CLIENT_TO_CATEGORY.values()) <= set(SCHEMA.enum_values("category"))


def test_category_for_prefers_sector_then_client():
    assert n.category_for("Food & Drink", "APPLE") == "food_drink"   # sector wins when usable
    assert n.category_for("general", "APPLE") == "technology"        # Cannes: sector unusable
    assert n.category_for("", "IKEA") == "retail"
    assert n.category_for("general", "UNKNOWN BRAND") is None        # never guesses
