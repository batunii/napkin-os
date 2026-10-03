"""The gap-filler (ADR 0019): stored facts first, the web only where the store had nothing, every web fact
remembered; the proof rewrite kept only when it passes its checks."""
import io
import json
import json as _json

import gap_filler as gf
import parse_brief as pb


def test_the_market_comes_from_the_facts_else_the_brief():
    assert gf.infer_market("anything", [{"market": "ie"}, {"market": "GB"}, {"market": "IE"}]) == "IE"
    assert gf.infer_market("Win dairy drinkers in Ireland and the United Kingdom; Irish shoppers", []) == "IE"
    assert gf.infer_market("no country here", None) is None


def test_a_proposal_or_a_flagged_proof_is_a_need_a_clients_field_is_not():
    fields = {"smp": {"value": "Real polenta, no pot"}, "competitor_context": {"value": "x", "proposed": True},
              "tone_world_assets": {"value": "warm", "source": "client_stated"},
              "reasons_to_believe": {"value": ["a"], "review": {"status": "failed_checks"}}}
    got = {n["field"]: n for n in gf.needs(fields, "Mamaliga", "food")}
    assert set(got) >= {"competitor_context", "reasons_to_believe"} and "tone_world_assets" not in got
    assert "Real polenta, no pot" in got["reasons_to_believe"]["query"] and got["competitor_context"]["lens"] == "brands_positioning"


def test_the_store_remembers_and_recalls_by_brand_and_market(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_FACT_STORE", str(tmp_path))
    gf.remember([{"id": "g_1", "value": "v"}, {"id": "g_1", "value": "dup"}], "Barry's Tea", "IE")
    gf.remember([{"id": "g_2", "value": "w"}], "Barry's Tea", "IE")
    assert [f["id"] for f in gf.recall("Barry's Tea", "IE")] == ["g_1", "g_2"]
    assert gf.recall("Barry's Tea", "GB") == []


def test_stored_facts_count_only_when_jev_says_they_help(monkeypatch):
    import jev_checks
    monkeypatch.setattr(jev_checks, "facts_relevant", lambda need, lines: [0.9, 0.2, 0.7])
    held = [{"id": "a", "key": "k", "value": 1}, {"id": "b", "key": "k", "value": 2}, {"id": "c", "key": "k", "value": 3}]
    assert [f["id"] for f in gf.stored({"query": "q"}, held)] == ["a", "c"]
    monkeypatch.setattr(jev_checks, "facts_relevant", lambda need, lines: None)      # jev down: none trusted
    assert gf.stored({"query": "q"}, held) == []


def test_the_web_tier_calls_the_research_port_and_turns_quotes_into_facts(monkeypatch):
    monkeypatch.setenv("BRIEF_RESEARCH_URL", "http://port")
    sent = {}

    def fake_open(req, timeout=None):
        sent["url"], sent["body"] = req.full_url, _json.loads(req.data)
        reply = {"sources": [{"url": "https://ex.ie/a", "title": "A", "published_at": "2025-05-01",
                              "excerpts": [{"quote": "Lyons holds 36% of tea"}, {"quote": ""}]}], "queries": ["q"]}
        return io.BytesIO(json.dumps(reply).encode())
    monkeypatch.setattr(gf.urllib.request, "urlopen", fake_open)
    rows = gf.search({"field": "competitor_context", "lens": "brands_positioning", "query": "rivals"}, "Barry's Tea", "IE")
    assert sent["url"] == "http://port/v1/research" and sent["body"]["market"] == "IE" and sent["body"]["entity"] == "brand/barry-s-tea"
    assert len(rows) == 1 and rows[0]["value"] == "Lyons holds 36% of tea" and rows[0]["key"] == "gap.competitor_context"
    assert rows[0]["sources"] == [{"title": "A", "uri": "https://ex.ie/a"}] and rows[0]["as_of"] == "2025-05-01"


def test_fill_uses_the_store_first_and_the_web_only_for_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_FACT_STORE", str(tmp_path))
    monkeypatch.setenv("BRIEF_RESEARCH_URL", "http://port")
    monkeypatch.setattr(gf, "stored", lambda need, pool: [pool[0]] if need["field"] == "audience" else [])
    asked = []
    monkeypatch.setattr(gf, "search", lambda need, b, m: asked.append(need["field"]) or
                        [{"id": "g_W", "value": "web fact", "key": "gap." + need["field"]}])
    needs = [{"field": "audience", "lens": "consumer_culture", "query": "q1"},
             {"field": "budget_scope", "lens": "media_spend", "query": "q2"}]
    got = gf.fill(needs, "Oatly", "GB", [{"id": "f_held", "value": "held"}])
    assert asked == ["budget_scope"]                                     # the store answered the audience
    assert got["by_field"] == {"audience": {"stored": 1, "web": 0}, "budget_scope": {"stored": 0, "web": 1}}
    assert [f["id"] for f in gf.recall("Oatly", "GB")] == ["g_W"]       # web facts are remembered


def test_with_no_port_the_web_tier_says_why(monkeypatch):
    monkeypatch.delenv("BRIEF_RESEARCH_URL", raising=False)
    monkeypatch.setattr(gf, "stored", lambda need, pool: [])
    got = gf.fill([{"field": "audience", "lens": "consumer_culture", "query": "q"}], "X", "IE", [])
    assert got["by_field"]["audience"]["why"].startswith("web search off")


def test_a_proof_rewrite_with_found_facts_is_kept_only_if_it_passes(monkeypatch):
    found = [{"id": "g_P", "key": "gap.reasons_to_believe", "value": "Blind test: 8 in 10 could not tell"}]
    monkeypatch.setattr(gf, "fill", lambda needs, b, m, held: {"facts": found, "by_field": {"reasons_to_believe": {"stored": 0, "web": 1}}})
    flagged = {"value": ["weak"], "review": {"status": "failed_checks"}}
    ctx = {"brand": "B", "category": "food", "market": "RO", "report": {}}
    golden = {"smp": {"value": "Real polenta"}, "reasons_to_believe": dict(flagged)}
    fills, rows, by_id, research = {"reasons_to_believe": golden["reasons_to_believe"]}, [], {}, []
    qs = pb._gap_fill_proof(fills, [{"blocks_field": "reasons_to_believe"}], golden, lambda: ctx, rows, by_id, research,
                            lambda fid, note: ({"value": ["8 in 10 could not tell [F:g_P]"]}, []))
    assert fills["reasons_to_believe"]["gap_fill"]["outcome"] == "proved" and by_id["g_P"] and research
    assert ctx["report"]["reasons_to_believe"]["outcome"] == "proved" and qs == []
    golden2 = {"smp": {"value": "Real polenta"}, "reasons_to_believe": dict(flagged)}
    fills2 = {"reasons_to_believe": golden2["reasons_to_believe"]}
    pb._gap_fill_proof(fills2, [], golden2, lambda: ctx, [], {}, [],
                       lambda fid, note: ({"value": ["x"], "review": {"status": "failed_checks"}}, []))
    assert fills2["reasons_to_believe"]["value"] == ["weak"] and ctx["report"]["reasons_to_believe"]["outcome"] == "not_proved"
