"""Tests for the RAG metadata contract and its loader.

Run:  cd engine/rag && python3 -m pytest test_contract.py -q

These protect the contract, not the code: if someone edits the JSON so a required
field loses its default, or an enum stops being lower snake_case, this is where it
shows up — before ingestion, not after.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import contract  # noqa: E402
from contract import SCHEMA, apply_defaults, indexed_fields, require_valid, validate  # noqa: E402

SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


# ---- the JSON itself -----------------------------------------------------
def test_the_contract_is_locked_only_after_creative_director_sign_off():
    """v1.2.0 is the sign-off. Locked means the closed lists are settled: changing a
    category value after ingestion means reprocessing, so it takes another argument with
    a creative director, not a tidy-up. The ruling is in REVIEW-cannes-categories.md."""
    if SCHEMA.locked:
        assert SCHEMA.version >= "1.2.", "locked implies the sign-off version"
        for v in ("gambling_betting", "luxury", "b2b"):
            assert v in SCHEMA.enum_values("category"), v


def test_contract_file_parses_and_names_its_version():
    raw = json.loads(contract.SCHEMA_PATH.read_text())
    assert raw["version"] == SCHEMA.version
    assert re.match(r"^\d+\.\d+\.\d+$", SCHEMA.version), "semver, so a reader can compare"


def test_every_field_and_enum_value_is_lower_snake_case():
    # The reason category is a closed list is that `FMCG` / `fmcg` must not diverge.
    # Enforce the casing rule on the contract itself, not just on incoming data.
    for f in SCHEMA.fields.values():
        assert SNAKE.match(f.name), f.name
        for v in f.values:
            assert SNAKE.match(v), f"{f.name}={v}"


def test_plan_fields_are_present_and_indexed():
    plan_indexed = {"scope", "category", "campaign_type", "stage", "verdict",
                    "reason_code", "status", "as_of", "reviewer_role"}
    assert plan_indexed <= set(indexed_fields())
    assert "run_id" in SCHEMA.fields and not SCHEMA.fields["run_id"].indexed
    assert "parent_id" in SCHEMA.fields


def test_existing_corpus_fields_survive():
    # chunking.py writes these today; the contract must not orphan the awards corpus.
    for k in ("source", "doc_id", "level", "parent_id", "strategy"):
        assert k in SCHEMA.fields, k


def test_required_fields_either_have_a_default_or_are_always_written():
    # A required field with no default is one the writer MUST supply. Keep that set
    # small and deliberate; everything else fails safe via apply_defaults().
    must_supply = {f.name for f in SCHEMA.fields.values() if f.required and f.default is None}
    assert must_supply == {"schema_version", "as_of", "source", "doc_id", "level"}, must_supply


def test_firmographics_are_excluded():
    assert {"revenue_band", "headcount_band", "audience_age_band"} <= set(SCHEMA.excluded)


# ---- apply_defaults -------------------------------------------------------
def test_defaults_fill_missing_and_none_but_never_overwrite():
    md = apply_defaults({"status": None, "verdict": "rejected"})
    assert md["status"] == "active"
    assert md["verdict"] == "rejected"
    assert md["scope"] == "global"
    assert md["schema_version"] == SCHEMA.version


def test_apply_defaults_returns_a_copy():
    src = {"source": "ipa"}
    apply_defaults(src)
    assert "status" not in src


# ---- validate --------------------------------------------------------------
def _corpus_chunk(**over) -> dict:
    base = {"source": "ipa", "doc_id": "xero-2024", "level": "child", "parent_id": "abc",
            "strategy": "case_parent_child", "as_of": "2024-01-01",
            "client": "Xero", "award_tier": "bronze"}          # extra frontmatter rides along
    base.update(over)
    return apply_defaults(base)


def test_valid_corpus_chunk_passes():
    assert validate(_corpus_chunk()) == []


def test_enum_is_case_sensitive():
    problems = validate(_corpus_chunk(category="FMCG"))
    assert any(p.startswith("category:") for p in problems)
    assert validate(_corpus_chunk(category="fmcg")) == []


def test_scope_pattern():
    assert validate(_corpus_chunk(scope="brand:acme")) == []
    assert validate(_corpus_chunk(scope="category:fmcg")) == []
    assert any("scope" in p for p in validate(_corpus_chunk(scope="Acme")))
    assert any("scope" in p for p in validate(_corpus_chunk(scope="brand:Acme Ltd")))


def test_as_of_must_be_a_real_iso_date():
    assert any("as_of" in p for p in validate(_corpus_chunk(as_of="2024")))
    assert any("as_of" in p for p in validate(_corpus_chunk(as_of="2024-13-01")))


def test_reason_code_requires_rejected_verdict():
    ok = _corpus_chunk(verdict="rejected", reason_code="cliche")
    assert validate(ok) == []
    bad = _corpus_chunk(verdict="accepted", reason_code="cliche")
    assert any("reason_code" in p for p in validate(bad))


def test_excluded_keys_are_reported():
    assert any("revenue_band" in p for p in validate(_corpus_chunk(revenue_band="100m_500m")))


def test_strict_keys_flags_unknown_frontmatter_only_when_asked():
    md = _corpus_chunk()
    assert validate(md) == []
    assert any("client" in p for p in validate(md, strict_keys=True))


def test_require_valid_raises_with_all_problems_listed():
    with pytest.raises(ValueError) as e:
        require_valid({"source": "ipa", "level": "child", "as_of": "nope"})
    msg = str(e.value)
    assert "doc_id" in msg and "as_of" in msg


# ---- chunker integration ---------------------------------------------------
def test_chunker_output_validates_against_contract(tmp_path):
    import chunking
    f = tmp_path / "ipa" / "xero.md"
    f.parent.mkdir()
    f.write_text(
        "---\nsource: ipa\ncategory: ipa_effectiveness_case\nframework_id: xero-2024\nframework_name: Xero UK\nyear: 2024\n"
        "award_tier: Bronze\nclient: Xero\n---\n# Xero UK\n\n## Insight\n"
        "Small business owners trust their accountant more than any software brand so the route runs through them.\n\n"
        "## Results\nRevenue grew forty percent over two years while penetration among accountants doubled.\n")
    chunks = chunking.chunk_file(f)
    assert {c["metadata"]["level"] for c in chunks} == {"parent", "child"}
    for c in chunks:
        md = c["metadata"]
        assert validate(md) == [], validate(md)
        assert md["doc_kind"] == "ipa_effectiveness_case"
        assert md["category"] is None                     # no sector in this fixture -> unknown, not guessed
        assert md["year"] == "2024" and md["as_of"] == "2024-01-01"
        assert md["award_tier"] == "bronze" and md["award_tier_raw"] == "Bronze"
        assert md["status"] == "active" and md["verdict"] == "none" and md["scope"] == "global"
        assert md["bucket"] == "exemplars"
        assert md["schema_version"] == SCHEMA.version
    roles = {c["metadata"]["section_role"] for c in chunks}
    assert roles == {"whole", "insight", "results"}


def test_qdrant_index_list_comes_from_contract():
    import store_qdrant
    idx = dict(store_qdrant._index_fields())
    assert set(idx) == {f"metadata.{n}" for n in indexed_fields()}
    assert idx["metadata.as_of"] == "datetime"
    assert idx["metadata.status"] == "keyword"


def test_as_of_falls_back_to_file_date_for_non_case_sources(tmp_path):
    import os, chunking
    from datetime import date
    f = tmp_path / "playbooks" / "fcb.md"
    f.parent.mkdir()
    f.write_text("---\nsource: playbook\nframework_id: \"01\"\nframework_name: FCB Grid\nyear: 1980\n---\n"
                 "# FCB Grid\n\n## Use\nPlot the category on think versus feel and high versus low involvement to pick a message strategy.\n")
    stamp = date(2025, 6, 1)
    os.utime(f, (0, int(__import__("time").mktime(stamp.timetuple()))))
    chunks = chunking.chunk_file(f)
    assert chunks and all(c["metadata"]["as_of"] == "2025-06-01" for c in chunks)
    assert all(c["metadata"]["year"] == "1980" for c in chunks)      # kept as payload, unused for as_of


def test_v1_1_fields_present_and_indexed():
    for f in ("bucket", "section_role", "effectiveness_type", "strategic_territory", "discipline", "lions_category", "award_tier"):
        assert SCHEMA.fields[f].type == "enum" and SCHEMA.fields[f].indexed, f
    assert not SCHEMA.fields["award_tier_raw"].indexed
    assert SCHEMA.fields["bucket"].default == "exemplars"
    assert SCHEMA.version >= "1.1."


def test_playbook_sections_get_roles_and_buckets(tmp_path):
    import chunking
    f = tmp_path / "playbooks" / "01-fcb-grid.md"
    f.parent.mkdir()
    body = "\n\n".join(f"## SECTION {i}: {h}\n\n" + ("Words about this section go here so it is long enough to keep. " * 3)
                        for i, h in enumerate(["IDENTITY CARD", "STEP-BY-STEP APPLICATION PROCESS", "WORKED EXAMPLE A",
                                               "COMMON MISTAKES & HOW TO AVOID THEM", "DECISION RULES", "SOURCE BIBLIOGRAPHY"], 1))
    f.write_text("---\nsource: playbook\nframework_id: \"01\"\nframework_name: FCB Grid\ncategory: \"Comms Planning\"\nyear: 1980\n---\n# FCB Grid\n\n" + body)
    chunks = chunking.chunk_file(f)
    by_role = {c["metadata"]["section_role"]: c["metadata"]["bucket"] for c in chunks}
    # v2: identity card and bibliography carry no retrievable lesson and are not embedded
    assert by_role == {"process": "craft", "worked_example": "exemplars",
                       "common_mistakes": "rules", "decision_rules": "rules"}
    assert all(c["metadata"]["discipline"] == "comms_planning" for c in chunks)
    assert all(validate(c["metadata"]) == [] for c in chunks)
