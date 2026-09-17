"""The brief retrieval API: pairs in, four budgeted citable blocks out.
Run: cd engine/rag && python3 -m pytest test_brief_context.py -q
"""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import brief_context as bc  # noqa: E402

PAIRS = {"brand": "BMW", "category": "Automotive", "campaign_type": "launch",
         "product": "new hybrid series", "problem": "hybrids read as a compromise",
         "audience": "urban professionals 30-45", "run_id": "r-991",
         "unexpected_new_field": "sustainability is the brand's lead message"}


# ---- planning ----------------------------------------------------------------
def test_plan_maps_filters_keywords_and_query():
    q, kw, f = bc.plan(PAIRS)
    # campaign_type is review-origin (no corpus chunk has it) so it is translated into
    # the awarding body's vocabulary, which the corpus does carry
    assert f == {"category": "automotive", "effectiveness_type": "launch"}
    assert "BMW" in kw
    assert "hybrids read as a compromise" in q and "urban professionals 30-45" in q
    assert "sustainability is the brand's lead message" in q   # unknown key still searchable
    assert "r-991" not in q                                    # bookkeeping is skipped


def test_plan_survives_empty_and_unknown_category():
    q, kw, f = bc.plan({})
    assert (q, kw, f) == ("", [], {})
    _, _, f2 = bc.plan({"category": "Underwater Basket Weaving"})
    assert "category" not in f2            # never guesses a closed-list value


def test_unmapped_campaign_type_becomes_query_text_not_a_filter():
    q, _, f = bc.plan({"campaign_type": "always_on", "problem": "keep the brand present"})
    assert "effectiveness_type" not in f and "campaign_type" not in f
    assert "always on campaign" in q          # not dropped: it is meaningful query text
    # and one that IS the same concept in both taxonomies does become a filter
    _, _, f2 = bc.plan({"campaign_type": "launch"})
    assert f2 == {"effectiveness_type": "launch"}


@pytest.mark.parametrize("md,doc_id,expected", [
    ({"source": "ipa", "level": "parent"}, "ipa_0481", "ipa_0481"),
    ({"source": "cannes", "level": "parent"}, "cannes_0012", "cannes_0012"),
    ({"source": "playbook", "level": "chunk", "section_role": "process",
      "framework_name": "FCB Grid"}, "01", "pb_fcb-grid#process"),
    ({"source": "template", "level": "chunk", "section_role": "template_section",
      "framework_name": "World-Class Advertising Briefing Template for Agencies and Ads Operating Systems"},
     "World-Class Advertising Briefing Template", "tpl_world-class-advertising#template_section"),
    ({"source": "ipa", "level": "child", "section_role": "insight"}, "ipa_0007", "ipa_0007#insight"),
])
def test_citations_are_short_readable_and_stable(md, doc_id, expected):
    assert bc.cite_for(md, doc_id) == expected
    assert len(bc.cite_for(md, doc_id)) <= 46


def test_scopes_or_across_global_category_and_brand():
    _, _, f = bc.plan(PAIRS)
    assert bc.scopes_for(PAIRS, f) == ["global", "category:automotive", "brand:bmw"]


def test_bucket_filters_always_exclude_superseded_and_production():
    _, _, f = bc.plan(PAIRS)
    scopes = bc.scopes_for(PAIRS, f)
    ex = bc.bucket_filters("exemplars", f, scopes)
    assert ex["status"] == {"ne": "superseded"} and ex["stage"] == {"ne": "production"}
    assert ex["category"] == "automotive" and ex["effectiveness_type"] == "launch"
    rules = bc.bucket_filters("rules", f, scopes)
    assert rules["scope"] == {"in": ["global", "category:automotive", "brand:bmw"]}
    assert "category" not in rules          # a house rule is not category-filtered
    craft = bc.bucket_filters("craft", f, scopes)
    assert "category" not in craft


# ---- budgeting ---------------------------------------------------------------
def _hit(cite, chars, bucket="exemplars", score=1.0, scope="global"):
    return bc.Hit(cite=cite, doc_id=cite, source="ipa", bucket=bucket, title=cite,
                  section="S", header="H", text="x" * chars, score=score,
                  metadata={"scope": scope})


def test_fill_stops_at_the_budget_and_counts_what_it_dropped():
    hits = [_hit("a", 350), _hit("b", 350), _hit("c", 350)]
    per = hits[0].tokens
    block = bc._fill(hits, per * 2 + 1)
    assert [h.cite for h in block.hits] == ["a", "b"] and block.dropped == 1
    assert block.tokens <= block.budget


def test_a_hit_that_does_not_fit_is_skipped_not_truncated():
    block = bc._fill([_hit("huge", 100_000), _hit("small", 100)], 200)
    assert [h.cite for h in block.hits] == ["small"]      # later small hit still gets in
    assert "x" * 100 in block.hits[0].text


def test_token_estimate_is_conservative():
    assert bc.estimate_tokens("x" * 350) >= 100           # 3.5 chars/token, not 4


# ---- collapse ----------------------------------------------------------------
class _FakeStore:
    def __init__(self, rows): self._rows = {r["id"]: r for r in rows}
    def get(self, cid): return self._rows.get(cid)


def _row(cid, doc, level, parent=None, text="body"):
    return {"id": cid, "source": f"{doc}.md", "section": level, "text": text,
            "header": f"{doc} header", "metadata": {"doc_id": doc, "level": level, "source": "ipa",
                                                    "parent_id": parent, "section_role": level}}


def test_collapse_keeps_one_hit_per_document_and_presents_the_parent():
    parent = _row("p1", "ipa_0001", "parent", text="the whole case")
    child = _row("c1", "ipa_0001", "child", parent="p1", text="just the results section")
    other = _row("p2", "ipa_0002", "parent", text="another case")
    store = _FakeStore([parent, child, other])
    out = bc._collapse([(0.9, child), (0.5, parent), (0.4, other)], store)
    assert len(out) == 2                                  # one per document
    assert out[0][1]["text"] == "the whole case"          # child hit, parent presented
    assert out[0][0] == 0.9                               # child's score is kept


def test_citations_are_stable_and_carry_the_section_for_children():
    parent_hit = bc._to_hit(0.9, _row("p1", "ipa_0001", "parent"), "exemplars")
    child_hit = bc._to_hit(0.8, _row("c1", "ipa_0001", "child", parent="p1"), "exemplars")
    assert parent_hit.cite == "ipa_0001"
    assert child_hit.cite == "ipa_0001#child"
    assert "[ipa_0001]" in parent_hit.render()


# ---- rules ordering ----------------------------------------------------------
def test_client_scoped_constraints_outrank_house_rules():
    hits = [_hit("house", 100, "rules", score=0.9, scope="global"),
            _hit("brand", 100, "rules", score=0.1, scope="brand:bmw"),
            _hit("cat", 100, "rules", score=0.5, scope="category:automotive")]
    hits.sort(key=lambda h: (-bc._scope_rank(h.metadata), -h.score))
    assert [h.cite for h in hits] == ["brand", "cat", "house"]


# ---- assembled context -------------------------------------------------------
def test_context_renders_citations_and_a_trace():
    ctx = bc.BriefContext(
        blocks={"exemplars": bc.Block("exemplars", [_hit("ipa_0001", 60)], 1000),
                "rules": bc.Block("rules", [_hit("pb_22", 40, "rules")], 500)},
        query="q", keywords=["BMW"], filters={"exemplars": {"category": "automotive"}})
    assert set(ctx.citations()) == {"ipa_0001", "pb_22"}
    txt = ctx.prompt_text()
    # a playbook mistake is advisory, so it renders as a pitfall, and still precedes precedent
    assert txt.index("PITFALLS") < txt.index("PRECEDENT")
    t = ctx.trace()
    assert t["blocks"]["exemplars"]["cites"] == ["ipa_0001"] and t["tokens"] == ctx.tokens


# ---- hard constraints vs advisory pitfalls -----------------------------------
def _rule(cite, verdict=None, chars=100):
    h = _hit(cite, chars, "rules")
    h.metadata = {"scope": "global", "verdict": verdict}
    return h


def test_reviewer_rejections_and_textbook_advice_are_rendered_separately():
    ctx = bc.BriefContext(
        blocks={"rules": bc.Block("rules", [_rule("run_12#tone", "rejected"), _rule("pb_9#common_mistakes")], 1200)},
        query="q", keywords=[], filters={})
    assert [h.cite for h in ctx.hard_constraints()] == ["run_12#tone"]
    assert [h.cite for h in ctx.pitfalls()] == ["pb_9#common_mistakes"]
    txt = ctx.prompt_text()
    assert "a reviewer rejected these before" in txt and "Advisory, not rules" in txt
    assert txt.index("CONSTRAINTS") < txt.index("PITFALLS")


def test_no_constraints_section_when_there_are_no_rejections():
    ctx = bc.BriefContext(blocks={"rules": bc.Block("rules", [_rule("pb_9#common_mistakes")], 1200)},
                          query="q", keywords=[], filters={})
    txt = ctx.prompt_text()
    assert "CONSTRAINTS" not in txt and "PITFALLS" in txt


def test_a_single_long_item_cannot_crowd_out_several_short_ones():
    hits = [_hit("essay", 2000, "rules"), _hit("a", 200, "rules"), _hit("b", 200, "rules")]
    block = bc._fill(hits, 1200, max_hit=260)
    assert [h.cite for h in block.hits] == ["a", "b"] and block.dropped == 1


def test_thin_precedent_is_declared_rather_than_passed_off_as_a_shortlist():
    thin = bc.Block("exemplars", [_hit("ipa_1", 200), _hit("ipa_2", 200)], 3500, dropped=0)
    ctx = bc.BriefContext(blocks={"exemplars": thin}, query="q", keywords=[], filters={})
    assert "every case in the corpus" in ctx.prompt_text()
    deep = bc.Block("exemplars", [_hit("ipa_1", 200), _hit("ipa_2", 200)], 3500, dropped=9)
    ctx2 = bc.BriefContext(blocks={"exemplars": deep}, query="q", keywords=[], filters={})
    assert "every case in the corpus" not in ctx2.prompt_text()


def test_precedent_shows_one_case_per_client_so_four_brands_beat_two_twice():
    def h(cite, client):
        x = _hit(cite, 80)
        x.metadata = {"client": client}
        return x
    hits = [h("a", "Marmite"), h("b", "Marmite"), h("c", "Audi"), h("d", "Nissan")]
    block = bc._fill(hits, 10_000, one_per_client=True)
    assert [x.cite for x in block.hits] == ["a", "c", "d"] and block.dropped == 1
    # off by default, so craft/rules/instructions are unaffected
    assert len(bc._fill(hits, 10_000).hits) == 4


def test_precedent_heading_does_not_claim_everything_is_award_winning():
    ctx = bc.BriefContext(blocks={"exemplars": bc.Block("exemplars", [_hit("pb_x#worked_example", 50)], 500)},
                          query="q", keywords=[], filters={})
    assert "award-winning" not in ctx.prompt_text()
