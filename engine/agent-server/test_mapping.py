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
    def setUp(self):
        self.out = map_brief(GOLDEN, {"project_name": "Moving People", "client": "Northwind Motors"})

    def test_only_allowed_keys(self):
        self.assertTrue(set(self.out) <= ALLOWED, set(self.out) - ALLOWED)

    def test_types_match_schema(self):
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
        self.assertIn("insight", self.out)
        self.assertIn("single_minded_proposition", self.out)
        self.assertTrue(self.out["reasons_to_believe"])
        self.assertEqual(set(self.out["desired_response"]) - {"think", "feel", "do"}, set())

    def test_objectives_dict_from_golden(self):
        self.assertIn("commercial", self.out["objectives"])

    def test_no_empty_values_emitted(self):
        for k, v in self.out.items():
            self.assertNotIn(v, (None, "", [], {}), k)

    def test_clan_data_names_win(self):
        self.assertEqual(self.out["project_name"], "Moving People")
        self.assertEqual(self.out["client"], "Northwind Motors")

    def test_context_carries_citations_and_scorecard(self):
        ctx = self.out["context"]
        self.assertIn("Brief quality", ctx)
        self.assertIn("›", ctx)  # loops3_7 source › section citations

    def test_no_client_brands_in_fixture(self):
        blob = json.dumps(GOLDEN).lower()
        for brand in ("volkswagen", "das auto", "betfair", "friskies"):
            self.assertNotIn(brand, blob)


class TestHeuristicMode(unittest.TestCase):
    def setUp(self):
        self.out = map_brief(HEURISTIC, {})

    def test_strategy_fields_omitted_not_blank(self):
        # fill-vs-flag: no golden fill in heuristic mode → keys absent, never ""
        self.assertNotIn("insight", self.out)
        self.assertNotIn("single_minded_proposition", self.out)
        for v in self.out.values():
            self.assertNotIn(v, (None, "", [], {}))

    def test_rationale_states_heuristic_mode(self):
        self.assertIn("euristic", self.out["rationale"])

    def test_capture_fields_still_map(self):
        self.assertIn("background", self.out)
        self.assertIn("audience", self.out)

    def test_objectives_grouping_tolerates_missing_type(self):
        # heuristic items carry no objective_type → everything lands somewhere valid
        obj = self.out.get("objectives", {})
        self.assertTrue(set(obj) <= {"commercial", "behavioural", "attitudinal"})


class TestRegenContract(unittest.TestCase):
    def test_field_types_use_literal_dotted_keys(self):
        self.assertIn("objectives.commercial", FIELD_TYPES)
        self.assertIn("desired_response.think", FIELD_TYPES)
        self.assertNotIn("objectives", FIELD_TYPES)  # nested objects forbidden in regen

    def test_array_fields_declared(self):
        for k in ("reasons_to_believe", "tone_and_world", "mandatories", "open_questions"):
            self.assertEqual(FIELD_TYPES[k], "array")


class TestCoercions(unittest.TestCase):
    def test_string_splits_on_strong_separators(self):
        self.assertEqual(_to_list("a · b · c"), ["a", "b", "c"])
        self.assertEqual(_to_list("a; b"), ["a", "b"])
        self.assertEqual(_to_list("x\ny"), ["x", "y"])

    def test_comma_split_only_when_multiple(self):
        self.assertEqual(_to_list("one, two, three"), ["one", "two", "three"])
        self.assertEqual(_to_list("single item"), ["single item"])

    def test_wrapped_list_unwraps(self):
        self.assertEqual(_to_list([{"value": "a"}, {"value": "b"}]), ["a", "b"])

    def test_empty_inputs(self):
        self.assertEqual(_to_list(None), [])
        self.assertEqual(_to_list(""), [])

    def test_research_summary_lands_in_context(self):
        ctx = build_context(GOLDEN, research_summary="- finding (source.md › S1)")
        self.assertIn("**Research:**", ctx)


if __name__ == "__main__":
    unittest.main()
