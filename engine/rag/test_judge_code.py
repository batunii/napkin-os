"""Deterministic admission predicates (judge_code.admit).
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest test_judge_code.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import judge_code as jc  # noqa: E402

AS_OF = date(2026, 9, 23)


def _case(year, source="ipa", doc_id="ipa-001"):
    """Metadata for an award case with the given year (None leaves it out)."""
    md = {"source": source, "doc_id": doc_id}
    if year is not None:
        md["year"] = year
    return md


# ---- each reason ----------------------------------------------------------------
def test_a_clean_chunk_is_admitted():
    """No exclusion, normal size, recent year: admitted (None)."""
    assert jc.admit(_case("2025"), "short text", exclude_doc_ids={"other"},
                    recency_years=5, as_of=AS_OF) is None


def test_excluded_doc_id_is_refused():
    """A doc id in the caller's exclude list is refused as 'excluded'."""
    assert jc.admit(_case("2025", doc_id="ipa-001"), "t", exclude_doc_ids={"ipa-001"}) == "excluded"


def test_excluded_compares_as_strings_and_accepts_any_iterable():
    """An int doc id matches its string form, and a list works as well as a set."""
    assert jc.admit({"doc_id": 42}, "t", exclude_doc_ids=["42"]) == "excluded"
    assert jc.admit({"doc_id": "42"}, "t", exclude_doc_ids=(42,)) == "excluded"


def test_a_chunk_without_a_doc_id_cannot_be_excluded():
    """Absent doc_id: nothing to match, so the exclude list does not refuse it."""
    assert jc.admit({"source": "ipa"}, "t", exclude_doc_ids={"None", ""}) is None


def test_oversized_text_is_refused_and_the_cap_is_inclusive():
    """Exactly max_chars is admitted; one more character is 'oversized'."""
    assert jc.admit({}, "x" * jc.MAX_PASSAGE_CHARS) is None
    assert jc.admit({}, "x" * (jc.MAX_PASSAGE_CHARS + 1)) == "oversized"
    assert jc.admit({}, "x" * 11, max_chars=10) == "oversized"


def test_whitespace_padding_does_not_count_towards_the_cap():
    """A padded table (the aaker worked examples: 35,152 chars raw, 757 of content) is
    admitted; the same content without padding is admitted too. Fails if the raw length
    is measured."""
    padded = "| Brand | Awareness |" + " " * 30_000 + "| Nike | high |" + "\n" * 5_000
    assert len(padded) > jc.MAX_PASSAGE_CHARS
    assert jc.content_length(padded) < 100
    assert jc.admit({}, padded) is None


def test_real_content_over_the_cap_is_refused_even_with_padding():
    """Collapsing whitespace does not excuse long content: words over the cap are
    'oversized', and so are they with padding between them."""
    words = "word " * (jc.MAX_PASSAGE_CHARS // 5 + 10)
    assert jc.admit({}, words) == "oversized"
    assert jc.admit({}, words.replace(" ", "      ")) == "oversized"


def test_the_cap_boundary_is_on_collapsed_length():
    """Exactly max_chars of content admits whatever the padding; one more refuses."""
    at = "a " * 5 + "b"                    # content_length 11
    assert jc.content_length(at.replace(" ", "   \t\n ")) == 11
    assert jc.admit({}, at.replace(" ", "        "), max_chars=11) is None
    assert jc.admit({}, (at + "c").replace(" ", "        "), max_chars=11) == "oversized"


@pytest.mark.parametrize("text,n", [("", 0), (None, 0), ("  a  ", 1), ("a \n\t b", 3),
                                    ("a" * 7, 7)])
def test_content_length(text, n):
    """Whitespace runs count as one space and the ends are stripped."""
    assert jc.content_length(text) == n


def test_none_text_is_treated_as_empty():
    """A hit with no text is not oversized (and does not crash)."""
    assert jc.admit({}, None) is None


def test_old_award_case_is_refused():
    """An award year beyond the recency cap is 'too_old'."""
    assert jc.admit(_case("2010"), "t", recency_years=5, as_of=AS_OF) == "too_old"


def test_every_reason_is_in_the_closed_tuple():
    """admit() only ever returns None or a member of REFUSAL_REASONS."""
    got = {jc.admit(_case("2025", doc_id="d"), "t", exclude_doc_ids={"d"}),
           jc.admit(_case("2025"), "x" * 20, max_chars=10),
           jc.admit(_case("1990"), "t", recency_years=1, as_of=AS_OF)}
    assert got == set(jc.REFUSAL_REASONS)


# ---- boundary years -------------------------------------------------------------
@pytest.mark.parametrize("year,expected", [
    ("2021", None),        # exactly recency_years old: inclusive
    ("2020", "too_old"),   # one year past the cap
    ("2026", None),        # this year
    ("2027", None),        # a future award year is not old
])
def test_recency_boundary(year, expected):
    """With a 5-year cap measured from 2026, 2021 is in and 2020 is out."""
    assert jc.admit(_case(year), "t", recency_years=5, as_of=AS_OF) == expected


def test_recency_zero_admits_only_the_current_year():
    """recency_years=0 is a legal cap meaning 'this year only'."""
    assert jc.admit(_case("2026"), "t", recency_years=0, as_of=AS_OF) is None
    assert jc.admit(_case("2025"), "t", recency_years=0, as_of=AS_OF) == "too_old"


def test_int_year_is_honoured():
    """PyYAML can hand an int year through; it is capped like the string form."""
    assert jc.admit(_case(2010), "t", recency_years=5, as_of=AS_OF) == "too_old"
    assert jc.admit(_case(2024), "t", recency_years=5, as_of=AS_OF) is None


def test_no_cap_means_no_recency_refusal():
    """recency_years=None never refuses on age, however old the case."""
    assert jc.admit(_case("1950"), "t", as_of=AS_OF) is None


def test_as_of_defaults_to_today():
    """Without as_of the age is measured from today, so a very old case is refused."""
    assert jc.admit(_case("1990"), "t", recency_years=5) == "too_old"


def test_negative_recency_is_a_caller_error():
    """A negative cap (or a bool) is a bug at the call site, not a policy."""
    with pytest.raises(ValueError):
        jc.admit({}, "t", recency_years=-1)
    with pytest.raises(ValueError):
        jc.admit({}, "t", recency_years=True)


# ---- the absent-year rule -------------------------------------------------------
@pytest.mark.parametrize("year", [None, "", "varies", "2020s", "c. 1980s", "1898 / 2012",
                                  "c. 350 BC", "1990s-present", 2010.0, True, [2010]])
def test_absent_or_unparseable_year_admits(year):
    """Never guess an age: no clean four-digit year means admitted, even with a tight cap.
    The string values are real ones from _index_v3."""
    assert jc.admit(_case(year), "t", recency_years=1, as_of=AS_OF) is None


def test_playbook_framework_year_is_not_capped():
    """A playbook's year is when the framework was invented (FCB Grid, 1980), not how old
    the evidence is, so the recency cap does not apply to it."""
    md = {"source": "playbook", "doc_id": "01-fcb-grid", "year": "1980", "as_of": "2026-06-11"}
    assert jc.admit(md, "t", recency_years=5, as_of=AS_OF) is None


def test_unknown_or_absent_source_is_not_capped():
    """Only sources known to carry an award year are capped; anything else admits."""
    assert jc.admit({"year": "1990"}, "t", recency_years=5, as_of=AS_OF) is None
    assert jc.admit({"year": "1990", "source": "dossier"}, "t", recency_years=5, as_of=AS_OF) is None


@pytest.mark.parametrize("source", sorted(jc.RECENCY_SOURCES))
def test_every_case_source_is_capped(source):
    """ipa, effie, cannes and dandad all have award years and are all capped."""
    assert jc.admit(_case("2000", source=source), "t", recency_years=5, as_of=AS_OF) == "too_old"


def test_recency_sources_match_chunking():
    """RECENCY_SOURCES must be the set chunking uses to treat `year` as the award year."""
    import chunking
    assert jc.RECENCY_SOURCES == frozenset(chunking._CASE_SOURCES)


# ---- precedence -----------------------------------------------------------------
def test_reason_precedence_is_excluded_then_oversized_then_too_old():
    """When several rules fire, the first in the documented order is reported."""
    md = _case("1990", doc_id="d")
    big = "x" * 50
    assert jc.admit(md, big, exclude_doc_ids={"d"}, recency_years=1, as_of=AS_OF,
                    max_chars=10) == "excluded"
    assert jc.admit(md, big, recency_years=1, as_of=AS_OF, max_chars=10) == "oversized"


def test_none_metadata_is_treated_as_empty():
    """A hit with no metadata dict is judged on its text alone."""
    assert jc.admit(None, "t", exclude_doc_ids={"x"}, recency_years=1) is None


# ---- parse_year -----------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("2024", 2024), (" 2024 ", 2024), (2024, 2024), ("20245", None), ("24", None),
    (999, None), (False, None), (None, None), ("2024-01-01", None),
])
def test_parse_year(value, expected):
    """Only a four-digit int or four-digit string is a year."""
    assert jc.parse_year(value) == expected


# ---- the cap against the real index ---------------------------------------------
_INDEX = HERE / "_index_v3" / "chunks.jsonl"


@pytest.mark.skipif(not _INDEX.exists(), reason="local index _index_v3 not present")
def test_cap_refuses_only_the_known_long_chunks():
    """MAX_PASSAGE_CHARS was chosen on _index_v3's collapsed lengths: it must refuse
    exactly the two long worked examples of 91-pestle-steep-analysis.md (16,668 and
    12,121 chars of content), and admit the padded aaker worked examples and the PESTLE
    OUTPUT TEMPLATE. If a rebuild changes that, the cap's justification in the module
    docstring is stale."""
    refused = []
    admitted_padded = set()
    with open(_INDEX, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            text = row.get("text") or ""
            if jc.admit(row.get("metadata") or {}, text) == "oversized":
                refused.append((row.get("source"), jc.content_length(text)))
            elif len(text) > jc.MAX_PASSAGE_CHARS:
                admitted_padded.add(row.get("source"))
    assert sorted(refused) == [("91-pestle-steep-analysis.md", 12_121),
                               ("91-pestle-steep-analysis.md", 16_668)]
    assert admitted_padded == {"91-pestle-steep-analysis.md", "26-aaker-brand-equity-model.md"}
