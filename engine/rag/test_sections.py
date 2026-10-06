"""The sections beyond the 11 fields and the rule packs they quote (P3, ADR 0022, 2026-10-03). Offline: the
model is a stub."""
import json

import brief_render as br
import evidence
import rule_packs as rp
import sections as sc

PACKS = [
    {"id": "ie", "kind": "market", "reviewed": True, "applies_when": {"markets": ["IE"]},
     "rules": [{"id": "ie-substantiate", "markets": ["IE"], "media": "all", "rule": "Hold evidence for every claim.",
                "source": {"title": "ASAI Code", "section": "3.1", "url": "https://example.ie/code"}}]},
    {"id": "gambling", "kind": "category", "reviewed": False, "applies_when": {"categories": ["gambling_betting"]},
     "rules": [{"id": "gam-gb-u18", "markets": ["GB"], "rule": "No strong appeal to under-18s."},
               {"id": "gam-ro-warn", "markets": ["RO"], "rule": "Carry the minors warning."}]},
    {"id": "suicide", "kind": "topic", "reviewed": True, "applies_when": {"topics": [r"\bsuicid"]},
     "rules": [{"id": "sui-no-method", "markets": ["ALL"], "rule": "Never show or describe a method."}]},
]


def test_markets_are_found_in_the_brief_most_named_first():
    assert rp.markets_in("Oatly in Ireland and the United Kingdom; Irish tea drinkers") == ["IE", "GB"]
    assert rp.markets_in("A campaign in Northern Ireland") == ["GB"]
    assert rp.markets_in("Improve ROI by 10%") == []
    assert rp.markets_in("", [{"market": "RO"}, {"market": "RO"}]) == ["RO"]


def test_packs_match_by_market_category_and_topic_and_keep_only_the_markets_rules(monkeypatch):
    monkeypatch.delenv("BRIEF_RULE_PACKS_UNREVIEWED", raising=False)
    got = rp.match(["IE"], "charity", "a suicide prevention charity", packs=PACKS)
    assert [p["id"] for p in got] == ["ie", "suicide"]                    # the unreviewed gambling pack is out
    monkeypatch.setenv("BRIEF_RULE_PACKS_UNREVIEWED", "1")
    got = rp.match(["RO"], "gambling_betting", "betting", packs=PACKS)
    assert [p["id"] for p in got] == ["gambling"] and [r["id"] for r in got[0]["rules"]] == ["gam-ro-warn"]


def test_a_rule_line_carries_its_id_markets_and_source():
    line = rp.line(PACKS[0]["rules"][0])
    assert line.startswith("[R:ie-substantiate] (IE; all) Hold evidence") and "ASAI Code, 3.1" in line


def test_the_shipped_packs_parse_and_every_rule_has_a_source():
    for p in rp.load():
        assert p.get("reviewed") in (True, False) and p["rules"], p["id"]
        for r in p["rules"]:
            src = r.get("source") or {}
            assert r.get("id") and r.get("rule") and src.get("url") and src.get("quote"), (p["id"], r.get("id"))


def test_code_checks_drop_a_law_no_pack_holds_and_flag_an_unsourced_figure():
    raw = {"items": [
        {"label": "Substantiation", "text": "Hold evidence for claims.", "source": "pack", "refs": ["R:ie-substantiate"]},
        {"label": "Invented law", "text": "The Made Up Act bans this.", "source": "pack", "refs": ["R:nope"]},
        {"label": "Awareness", "measure": "prompted awareness", "baseline": "5%", "target": "12%",
         "source": "client", "refs": ["s4", "F:f_missing"]},
        {"label": "Share", "text": "share from research", "source": "research", "refs": []},
        {"label": "TV", "text": "1 x 30s spot", "source": "client", "refs": ["s2"]},
    ], "question": "What is the baseline?"}
    got = sc.checked("safety_legal", raw, facts={}, rule_ids={"ie-substantiate"}, source_text="awareness was 5% in 2025; a 30 second spot")
    labels = [it["label"] for it in got["items"]]
    assert "Invented law" not in labels and got["dropped"] == 1
    aw = next(it for it in got["items"] if it["label"] == "Awareness")
    assert aw["refs"] == ["s4"] and aw["figure_unchecked"] == ["12"]      # 5 is in the source, 12 is not
    assert next(it for it in got["items"] if it["label"] == "Share")["source"] == "proposed"
    assert "figure_unchecked" not in next(it for it in got["items"] if it["label"] == "TV")   # '1 x' is a format
    assert got["question"] == "What is the baseline?"


def _brief():
    return {"meta": {"project": "P"}, "loop2_brief": {},
            "loop1_capture": {"fields": {"budget": {"value": "EUR 140,000 including production", "source_refs": [3]},
                                         "deliverables": [{"value": "1 x 30s TV ad", "source_refs": [5]}]}},
            "loop2_golden": {"fields": {"objectives": {"value": {"attitudinal": "Grow awareness"}},
                                        "mandatories": {"value": "Include the helpline", "proposed": True}}}}


def test_run_writes_every_section_retries_an_empty_one_and_asks_for_what_is_missing(monkeypatch):
    monkeypatch.setenv("BRIEF_RULE_PACKS_UNREVIEWED", "1")
    monkeypatch.setattr(rp, "load", lambda directory=None: PACKS)
    seen = {}

    def call(user, system=None, max_tokens=None, route=None):
        sid = next(k for k, v in sc.SECTIONS.items() if f"SECTION: {v['title']}" in user)
        seen[sid] = seen.get(sid, 0) + 1
        assert route == "section" and "CLIENT DOCUMENT FACTS" in user and "(s3)" in user
        if sid == "practicalities":
            return {"items": []}                                          # never fills: retried once, then asked for
        if sid == "safety_legal":
            assert "[R:ie-substantiate]" in user and "[R:sui-no-method]" in user
            return {"items": [{"label": "Method", "text": "Never show a method.", "source": "pack", "refs": ["R:sui-no-method"]},
                              {"label": "Claims", "text": "Hold evidence.", "source": "pack", "refs": ["R:ie-substantiate"]}]}
        return {"items": [{"label": "x", "text": "a proposal", "source": "proposed", "refs": []}], "question": ""}

    out = sc.run(_brief(), {}, ["IE"], "charity", "a suicide prevention charity in Ireland", call)
    assert list(out["sections"]) == list(sc.ORDER)
    assert seen["practicalities"] == 2 and seen["measurement"] == 1
    assert out["meta"]["not_written"] == ["practicalities"]
    assert any(q.get("blocks_section") == "practicalities" and q["priority"] == "high" for q in out["questions"])
    safety = out["sections"]["safety_legal"]["items"]
    assert safety[1]["cite"] == "ASAI Code, 3.1"
    assert {p["id"] for p in out["meta"]["rule_packs"]} == {"ie", "suicide"}


def test_the_client_brief_shows_each_section_in_its_place_with_labels():
    b = _brief()
    b["sections"] = {"meta": {"unreviewed": True}, "sections": {
        "measurement": {"title": "How we will know it worked", "items": [
            {"label": "Awareness", "measure": "prompted awareness", "baseline": "5% (2025 tracker)", "target": "to agree",
             "method": "tracker", "source": "client", "refs": ["s4"]},
            {"label": "Sign-ups", "measure": "sign-ups", "baseline": "to set before launch", "target": "to agree",
             "method": "CRM", "source": "proposed", "refs": [], "figure_unchecked": ["40"]}]},
        "safety_legal": {"title": "Safety and legal lines", "items": [
            {"label": "method", "text": "Never show a method.", "source": "pack", "refs": ["R:x"], "cite": "Samaritans guidelines"}]},
        "practicalities": {"title": "Practicalities", "items": [
            {"label": "deliverables", "text": "1 x 30s TV ad", "source": "client", "refs": ["s5"]}]},
        "competitors": {"title": "Competitors and alternatives", "items": []}}}
    md = br.render_client_brief(b)
    assert md.index("## Objectives") < md.index("## How we will know it worked") < md.index("## Audience")
    assert "| Awareness | prompted awareness | 5% (2025 tracker) | to agree | tracker |" in md
    assert "_(proposed)_" in md and "_(figure to check)_" in md
    assert "not yet reviewed by a person" in md and "— Samaritans guidelines" in md
    assert md.index("## Mandatories") < md.index("## Safety and legal lines") < md.index("## Practicalities")
    assert "## Competitors and alternatives\n_To be agreed" in md
    assert "**Deliverables:** 1 x 30s TV ad" in md
    assert "1 x 30s TV ad" not in md.split("## Also in the client's documents")[-1] if "Also in" in md else True


def test_section_claims_reach_the_fact_check_and_its_questions_name_the_section():
    seen = {}

    def call(user, system=None, max_tokens=None, route=None):
        seen["user"] = user
        return {"claims": [{"field": "section:measurement", "claim": "awareness is 5%", "verdict": "unsupported"}]}

    extra = sc.claims_text({"measurement": {"items": [{"label": "Awareness", "baseline": "5%", "source": "client"},
                                                      {"label": "Guess", "text": "a proposal", "source": "proposed"}]}})
    assert list(extra) == ["section:measurement"] and len(extra["section:measurement"]) == 2
    assert extra["section:measurement"][1].startswith("(agency proposal: check only the facts it states)")
    res = evidence.check({}, {}, {}, call, extra=extra)
    assert "section:measurement" in seen["user"]
    brief = {}
    qs = evidence.apply(brief, res)
    assert "how we will know it worked section" in qs[0]["question"]


def test_the_app_gets_the_sections_in_reply_meta():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("mapping_t", pathlib.Path(__file__).resolve().parents[1] / "agent-server" / "mapping.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    b = _brief()
    b["sections"] = {"sections": {"measurement": {"title": "t", "items": [{"label": "a"}]}}, "meta": {"unreviewed": False}}
    meta = m.engine_meta(b)
    assert meta["sections"]["order"] == ["measurement"] and json.dumps(meta)


def test_a_legal_line_stated_by_a_rule_is_checked_against_the_rules_not_called_unsupported():
    seen = {}

    def call(user, system=None, max_tokens=None, route=None):
        seen["user"] = user
        return {"claims": [{"field": "section:safety_legal", "claim": "No gambling ads 06:00-23:00", "verdict": "pack",
                            "evidence": "R:gam-ro"}]}

    rules = ["[R:gam-ro] (RO; broadcast) No gambling ads from 06:00 to 23:00."]
    res = evidence.check({}, {}, {}, call, extra={"section:safety_legal": ["No gambling ads 06:00-23:00"]}, rules=rules)
    assert "RULES:\n[R:gam-ro]" in seen["user"]
    brief = {}
    assert evidence.apply(brief, res) == [] and brief["fact_check"]["counts"]["pack"] == 1


def test_the_second_wave_reads_the_first_and_never_the_other_way():
    users = {}

    def call(user, system=None, max_tokens=None, route=None):
        sid = next(k for k, v in sc.SECTIONS.items() if f"SECTION: {v['title']}" in user)
        users[sid] = user
        return {"items": [{"label": sid, "text": f"text of {sid}", "source": "proposed", "refs": []}]}

    sc.run(_brief(), {}, ["IE"], "", "", call)
    for sid in sc.FIRST_WAVE:
        assert "SECTIONS ALREADY WRITTEN" not in users[sid]
    for sid in sc.SECOND_WAVE:
        assert "SECTIONS ALREADY WRITTEN" in users[sid] and "text of safety_legal" in users[sid]
    assert set(sc.FIRST_WAVE) | set(sc.SECOND_WAVE) == set(sc.ORDER)


def test_an_item_the_fact_check_rejects_is_marked_and_shown_as_no_source():
    secs = {"competitors": {"title": "Competitors and alternatives", "items": [
        {"label": "Gender Pay Gap campaign", "text": "An earlier State campaign aimed at employers", "source": "client"},
        {"label": "Doing nothing", "text": "Employers keep hiring as before", "source": "proposed"}]}}
    n = sc.mark_unsupported(secs, [{"field": "section:competitors",
                                    "claim": "The Gender Pay Gap campaign was an earlier State campaign aimed at employers"},
                                   {"field": "background", "claim": "unrelated"}])
    assert n == 1 and secs["competitors"]["items"][0]["unsupported"] and "unsupported" not in secs["competitors"]["items"][1]
    md = "\n".join(br.section_lines("competitors", secs["competitors"]))
    assert "_(no source: to confirm)_" in md.split("\n")[1] + md.split("\n")[2]


def test_a_contact_detail_no_source_gives_is_flagged():
    raw = {"items": [{"label": "Signpost", "text": "Point to jo@samaritans.ie and samaritans.ie", "source": "client",
                      "refs": ["s1"]},
                     {"label": "Freephone", "text": "Call 116 123 or visit samaritans.ie", "source": "client", "refs": ["s2"]}]}
    got = sc.checked("safety_legal", raw, facts={}, rule_ids=set(), source_text="free on 116 123, see samaritans.ie")
    assert got["items"][0]["detail_unchecked"] == ["jo@samaritans.ie"]
    assert "detail_unchecked" not in got["items"][1]


def test_round4_bugs_tags_never_reach_the_reader_years_are_not_claims_and_the_header_is_honest():
    sec = {"title": "How we will know it worked", "items": [
        {"label": "Awareness", "measure": "prompted awareness", "baseline": "to set before launch", "target": "9% (s6)",
         "method": "tracker", "source": "client", "refs": ["s6"], "method_proposed": True}]}
    md = "\n".join(br.section_lines("measurement", sec))
    assert "(s6)" not in md and "| 9% |" in md and "_(method proposed)_" in md
    prop = {"title": "Practicalities", "items": [{"label": "budget", "text": "To confirm.", "source": "proposed", "refs": ["s3"]}]}
    assert "built on the client's documents where cited" in "\n".join(br.section_lines("practicalities", prop))
    got = sc.checked("competitors", {"items": [{"label": "Dairy", "text": "In 2020 dairy was worth 3.2bn", "source": "proposed"}]},
                     facts={}, rule_ids=set(), source_text="")
    assert got["items"][0]["figure_unchecked"] == ["3.2bn".rstrip("bn")]          # 2020 is a date; 3.2 is the claim

