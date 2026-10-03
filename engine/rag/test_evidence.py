"""ADR 0021: the evidence appendix, its marks, the fact-check and how the client brief shows them."""
import brief_render as br
import evidence as ev

FACTS = {"f_1": {"id": "f_1", "value": 0.27, "unit": "proportion", "confidence": "low", "as_of": "2014-10-06",
                 "sources": [{"title": "Kantar share", "publisher": "Kantar", "uri": "https://x/k"}]},
         "f_2": {"id": "f_2", "value": "116 123", "unit": "code", "confidence": "high"}}
FIELDS = {"reasons_to_believe": {"value": ["27% share", "Free line"], "fact_refs": [{"item": 0, "id": "f_1"}, {"item": 1, "id": "f_2"}]},
          "desired_response": {"value": {"do": "Ring 116 123"}, "fact_refs": [{"item": "do", "id": "f_2"}]},
          "competitor_context": {"value": "Lyons leads", "proposed": True},
          "insight": {"value": "People feel they are not bad enough to call."}}


def test_the_appendix_numbers_cited_facts_once_with_source_and_status():
    a = ev.appendix(FIELDS, FACTS)
    assert [(x["n"], x["id"], x["status"]) for x in a] == [(1, "f_1", "unverified"), (2, "f_2", "verified")]
    assert a[0]["source"] == "Kantar share, Kantar" and a[0]["text"] == "27%" and a[1]["text"] == "116 123"
    assert ev.marks(FIELDS, a) == {("reasons_to_believe", 0): [1], ("reasons_to_believe", 1): [2], ("desired_response", "do"): [2]}


def test_the_check_leaves_proposals_out_and_keeps_only_known_verdicts():
    sent = {}

    def call(user, system=None, **kw):
        sent["user"], sent["route"] = user, kw.get("route")
        return {"claims": [{"field": "reasons_to_believe", "claim": "27% share", "verdict": "research_unverified", "evidence": "f_1"},
                           {"field": "insight", "claim": "nobody calls", "verdict": "unsupported"},
                           {"field": "x", "claim": "y", "verdict": "maybe"}]}
    got = ev.check(FIELDS, {"budget": {"value": "EUR 140k", "source_refs": [3]}}, FACTS, call)
    assert sent["route"] == "judge" and "Lyons leads" not in sent["user"] and "EUR 140k (sentences 3)" in sent["user"]
    assert [c["verdict"] for c in got["claims"]] == ["research_unverified", "unsupported"]


def test_apply_records_the_check_and_asks_about_unsupported_claims():
    brief = {}
    qs = ev.apply(brief, {"claims": [{"field": "insight", "claim": "nobody calls", "verdict": "unsupported", "evidence": ""},
                                     {"field": "reasons_to_believe", "claim": "27% share", "verdict": "research_unverified", "evidence": "f_1"}]})
    assert brief["fact_check"]["counts"]["unsupported"] == 1 and len(brief["fact_check"]["unverified"]) == 1
    assert len(qs) == 1 and qs[0]["priority"] == "high" and "nobody calls" in qs[0]["question"]
    ev.apply(brief, None)
    assert brief["fact_check"] == {"status": "did_not_run"}


def test_the_client_brief_shows_marks_appendix_and_what_is_not_claimed():
    brief = {"meta": {"project": "P"}, "loop2_brief": {}, "loop2_golden": {"fields": FIELDS},
             "evidence": {"appendix": ev.appendix(FIELDS, FACTS)},
             "fact_check": {"unverified": [{"field": "reasons_to_believe", "claim": "27% share"}],
                            "unsupported": [{"field": "insight", "claim": "nobody calls"}]}}
    md = br.render_client_brief(brief)
    assert "- 27% share [A1]" in md and "- Free line [A2]" in md and "**Do:** Ring 116 123 [A2]" in md
    assert "## Still to verify" in md and "## What we will not claim" in md and "- nobody calls (insight)" in md
    assert "- **A1** 27% (Kantar share, Kantar; 2014-10-06; unverified; <https://x/k>)" in md
