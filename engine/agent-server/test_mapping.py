"""Unit tests for mapping.py — run with:  python3 -m unittest test_mapping -v

Fixtures are synthetic-derived only (a golden-mode and a heuristic-mode run of
the de-branded sample brief). Never add real client-run outputs here.
"""

import json
import unittest
from pathlib import Path

from mapping import FIELD_TYPES, map_brief, build_context, _to_list

HERE = Path(__file__).resolve().parent
GOLDEN = json.loads((HERE / "fixtures" / "golden_brief_object.json").read_text())
HEURISTIC = json.loads((HERE / "fixtures" / "heuristic_brief_object.json").read_text())

# App schema shapes (mirrors app/templates/brief-maker/schema.json)
TOP_LEVEL_STRINGS = {"project_name", "client", "background", "audience",
                     "competitor_context", "insight", "single_minded_proposition",
                     "budget_and_scope"}
TOP_LEVEL_ARRAYS = {"reasons_to_believe", "tone_and_world", "mandatories", "open_questions"}
TOP_LEVEL_OBJECTS = {"objectives", "desired_response"}
META_KEYS = {"rationale", "context"}
ALLOWED = TOP_LEVEL_STRINGS | TOP_LEVEL_ARRAYS | TOP_LEVEL_OBJECTS | META_KEYS


class TestGoldenMode(unittest.TestCase):
    """map_brief() over a golden-mode brief object, with clan-supplied project name/client."""

    def setUp(self):
        """Map the golden fixture through map_brief with clan_data project_name/client set."""
        self.out = map_brief(GOLDEN, {"project_name": "Moving People", "client": "Northwind Motors"})

    def test_only_allowed_keys(self):
        """Checks the mapped output only contains keys from the app schema's allowed set."""
        self.assertTrue(set(self.out) <= ALLOWED, set(self.out) - ALLOWED)

    def test_types_match_schema(self):
        """Checks string/array/object fields in the output match the app schema's declared types."""
        for k in TOP_LEVEL_STRINGS & set(self.out):
            self.assertIsInstance(self.out[k], str, k)
        for k in TOP_LEVEL_ARRAYS & set(self.out):
            self.assertIsInstance(self.out[k], list, k)
            for item in self.out[k]:
                self.assertIsInstance(item, str, k)
        for k in TOP_LEVEL_OBJECTS & set(self.out):
            self.assertIsInstance(self.out[k], dict, k)
            for v in self.out[k].values():
                self.assertIsInstance(v, str, k)

    def test_strategy_fields_filled_from_golden(self):
        """Checks golden-mode output fills insight, SMP, reasons_to_believe, and desired_response
        keys restricted to think/feel/do."""
        self.assertIn("insight", self.out)
        self.assertIn("single_minded_proposition", self.out)
        self.assertTrue(self.out["reasons_to_believe"])
        self.assertEqual(set(self.out["desired_response"]) - {"think", "feel", "do"}, set())

    def test_objectives_dict_from_golden(self):
        """Checks the objectives dict carries a commercial key from the golden fixture."""
        self.assertIn("commercial", self.out["objectives"])

    def test_no_empty_values_emitted(self):
        """Checks no mapped value is None, empty string, empty list, or empty dict."""
        for k, v in self.out.items():
            self.assertNotIn(v, (None, "", [], {}), k)

    def test_clan_data_names_win(self):
        """Checks clan_data's project_name/client override whatever the brief object carries."""
        self.assertEqual(self.out["project_name"], "Moving People")
        self.assertEqual(self.out["client"], "Northwind Motors")

    def test_context_carries_citations_and_scorecard(self):
        """Checks the context string includes the brief-quality scorecard and a loops3_7 citation."""
        ctx = self.out["context"]
        self.assertIn("Brief quality", ctx)
        # the precedent the insight/SMP writers read, by title (audit RAG-10, 2026-09-28)
        self.assertIn("Precedent the strategy writers read", ctx)
        self.assertIn("Beware of Your Battery Changer (2024)", ctx)

    def test_no_client_brands_in_fixture(self):
        """Checks the golden fixture contains no real client/brand names, keeping it de-branded."""
        blob = json.dumps(GOLDEN).lower()
        for brand in ("volkswagen", "das auto", "betfair", "friskies"):
            self.assertNotIn(brand, blob)


class TestHeuristicMode(unittest.TestCase):
    """map_brief() over a heuristic-mode brief object (no golden fill, no clan_data)."""

    def setUp(self):
        """Map the heuristic fixture through map_brief with empty clan_data."""
        self.out = map_brief(HEURISTIC, {})

    def test_strategy_fields_omitted_not_blank(self):
        """Checks strategy fields are absent (not blank strings) when heuristic mode has no
        golden fill, and no value anywhere is empty."""
        # fill-vs-flag: no golden fill in heuristic mode → keys absent, never ""
        self.assertNotIn("insight", self.out)
        self.assertNotIn("single_minded_proposition", self.out)
        for v in self.out.values():
            self.assertNotIn(v, (None, "", [], {}))

    def test_rationale_states_heuristic_mode(self):
        """Checks the rationale text mentions heuristic mode."""
        self.assertIn("euristic", self.out["rationale"])

    def test_capture_fields_still_map(self):
        """Checks capture fields (background, audience) still map through in heuristic mode."""
        self.assertIn("background", self.out)
        self.assertIn("audience", self.out)

    def test_objectives_grouping_tolerates_missing_type(self):
        """Checks objectives without an objective_type still group into a valid known key."""
        # heuristic items carry no objective_type → everything lands somewhere valid
        obj = self.out.get("objectives", {})
        self.assertTrue(set(obj) <= {"commercial", "behavioural", "attitudinal"})


class TestRegenContract(unittest.TestCase):
    """FIELD_TYPES, the type map that governs single-field regeneration."""

    def test_field_types_use_literal_dotted_keys(self):
        """Checks nested fields are declared under literal dotted keys, and the bare parent
        key (e.g. "objectives") is not itself a regen-able field."""
        self.assertIn("objectives.commercial", FIELD_TYPES)
        self.assertIn("desired_response.think", FIELD_TYPES)
        self.assertNotIn("objectives", FIELD_TYPES)  # nested objects forbidden in regen

    def test_array_fields_declared(self):
        """Checks each list-typed top-level field is declared as "array" in FIELD_TYPES."""
        for k in ("reasons_to_believe", "tone_and_world", "mandatories", "open_questions"):
            self.assertEqual(FIELD_TYPES[k], "array")


class TestCoercions(unittest.TestCase):
    """_to_list()'s string/list coercion rules, and build_context()'s research-summary handling."""

    def test_string_splits_on_strong_separators(self):
        """Checks strings split on ·, ;, and newline separators into list items."""
        self.assertEqual(_to_list("a · b · c"), ["a", "b", "c"])
        self.assertEqual(_to_list("a; b"), ["a", "b"])
        self.assertEqual(_to_list("x\ny"), ["x", "y"])

    def test_comma_split_only_when_multiple(self):
        """Checks a comma splits into multiple items, but a single comma-free item stays whole."""
        self.assertEqual(_to_list("one, two, three"), ["one", "two", "three"])
        self.assertEqual(_to_list("single item"), ["single item"])

    def test_wrapped_list_unwraps(self):
        """Checks a list of {"value": ...} wrapper dicts unwraps to a plain list of values."""
        self.assertEqual(_to_list([{"value": "a"}, {"value": "b"}]), ["a", "b"])

    def test_empty_inputs(self):
        """Checks None and "" both coerce to an empty list."""
        self.assertEqual(_to_list(None), [])
        self.assertEqual(_to_list(""), [])

    def test_research_summary_lands_in_context(self):
        """Checks a research_summary passed to build_context appears under a "**Research:**" heading."""
        ctx = build_context(GOLDEN, research_summary="- finding (source.md › S1)")
        self.assertIn("**Research:**", ctx)


if __name__ == "__main__":
    unittest.main()


def test_context_claims_only_precedent_a_writer_read():
    """Audit RAG-10 (2026-09-28): 'precedent' lists the insight/SMP writers' evidence_ids
    only; other retrieved sources are counted as background; nothing is claimed on the
    digest fallback (N5)."""
    brief = {"loops3_7": {"enabled": True, "sources_used": ["ipa_0015 › Insight", "pb_x › Rules", "pb_y › How"]},
             "loop2_golden": {"fields": {"insight": {"value": "i", "evidence_ids": ["ipa_0015"]},
                                         "smp": {"value": "s"}}}}
    ctx = build_context(brief)
    assert "**Precedent the strategy writers read:**\n- ipa_0015" in ctx
    assert "**Retrieved for the strategy notes (background):** 3 playbook and case sections." in ctx
    assert "grounded in precedent" not in ctx
    fb = {**brief, "loops3_7": {**brief["loops3_7"], "fallback": {"to": "digests", "reason": "store down"}}}
    ctx2 = build_context(fb)
    assert "Precedent" not in ctx2 and "Retrieval fell back to digests" in ctx2
