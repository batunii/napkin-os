"""The metadata filter language. Run: python3 -m pytest test_filters.py -q"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import filters as F  # noqa: E402

MD = {"source": "ipa", "status": "active", "scope": "brand:bmw", "as_of": "2022-01-01",
      "category": "automotive", "level": "parent"}


def test_plain_value_still_means_equality():
    assert F.normalise({"source": "ipa"}) == {"source": ("eq", "ipa")}
    assert F.matches(MD, {"source": "ipa"})
    assert not F.matches(MD, {"source": "cannes"})


def test_equality_is_exact_not_substring():
    # the old local filter substring-matched, so category=retail also matched 'retailer'
    assert not F.matches({"category": "automotive"}, {"category": "auto"})
    assert F.matches({"category": "automotive"}, {"category": "automotive"})


@pytest.mark.parametrize("where,expected", [
    ({"status": {"ne": "superseded"}}, True),
    ({"status": {"ne": "active"}}, False),
    ({"scope": {"in": ["global", "brand:bmw"]}}, True),
    ({"scope": {"in": ["global", "brand:audi"]}}, False),
    ({"scope": {"nin": ["brand:bmw"]}}, False),
    ({"as_of": {"gte": "2020-01-01"}}, True),
    ({"as_of": {"gte": "2023-01-01"}}, False),
    ({"as_of": {"lt": "2023-01-01"}}, True),
    ({"level": {"exists": True}}, True),
    ({"run_id": {"exists": False}}, True),
    ({"source": "ipa", "category": "automotive"}, True),
    ({"source": "ipa", "category": "retail"}, False),
])
def test_operators(where, expected):
    assert F.matches(MD, where) is expected


def test_absent_field_asymmetry_is_deliberate():
    old = {"source": "ipa"}                      # a chunk written before `status` existed
    assert F.matches(old, {"status": {"ne": "superseded"}})    # not-superseded includes it
    assert not F.matches(old, {"status": "active"})           # but it cannot claim to be active
    assert not F.matches(old, {"status": {"in": ["active"]}})
    assert not F.matches(old, {"as_of": {"gte": "2020-01-01"}})


def test_bad_filters_raise_rather_than_silently_pass():
    for bad in ({"a": {"ne": 1, "eq": 2}}, {"a": {"nope": 1}}, {"a": {"in": "not-a-list"}}):
        with pytest.raises(F.FilterError):
            F.matches(MD, bad)


def test_qdrant_translation():
    q = F.to_qdrant({"source": "ipa", "status": {"ne": "superseded"},
                     "scope": {"in": ["global", "brand:bmw"]}, "as_of": {"gte": "2020-01-01"}})
    assert {"key": "metadata.source", "match": {"value": "ipa"}} in q["must"]
    assert {"key": "metadata.status", "match": {"value": "superseded"}} in q["must_not"]
    assert {"key": "metadata.scope", "match": {"any": ["global", "brand:bmw"]}} in q["must"]
    assert {"key": "metadata.as_of", "datetime_range": {"gte": "2020-01-01"}} in q["must"]
    assert F.to_qdrant(None) is None


def test_describe_is_readable():
    s = F.describe({"source": "ipa", "status": {"ne": "superseded"}, "scope": {"in": ["global", "brand:bmw"]}})
    assert s == "source=ipa AND status ne superseded AND scope in (global, brand:bmw)"
