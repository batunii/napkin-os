"""draft_brief, planned (brief/planned.py): Sam plans the whole brief and runs the research it
asks for, Dara writes every part in one call, Jude reviews the whole brief once, Dara rewrites
what he names, Jude scores again; and the run log records all of it."""

import json

from napkin.brief.fields import KEYS
from napkin.brief.planned import DIMS

from conftest import Server
from fakes import FakeModel
from test_brief import Host, check_change_rules, retrieval, run

THIN = "Can you make me a brief? Brewline, an Irish coffee chain, is launching an oat-milk iced latte this " \
       "summer and wants more under-30s coming in."


def planned_model(seen):
    m = FakeModel()

    def plan(p):
        seen["plan"] = p
        cat = p["categories"][0]["code"]
        return {"strategy": "Make the oat iced latte the way under-30s start their summer at Brewline.",
                "parts": {k: {"must_say": "it", "rests_on": "assumption"} for k in p["parts"]},
                "brand": "Brewline", "competitors": ["Insomnia", "Costa"],
                "research": [{"lens": "market_structure", "market": "ie", "focus": "who sells iced coffee",
                              "why": "who else sells it", "for_parts": ["competitor_context"]},
                             {"lens": "consumer_culture", "market": "IE", "focus": "under-30s' café habits",
                              "why": "the audience", "for_parts": ["audience"]},
                             {"lens": "media_spend", "market": "IE", "focus": "x", "why": "over the cap",
                              "for_parts": []}],
                "category": cat,
                "ask_client": [{"question": "What is the launch budget?", "part": "budget_and_scope"}]}

    def draft(p):
        seen["draft"] = p
        fid = p["facts"][0]["id"]
        psg = p["library"][0]["id"] if p["library"] else None
        out = {}
        for k in p["parts"]:
            arr = k in ("reasons_to_believe", "tone_and_world", "mandatories", "open_questions")
            out[k] = {"value": [f"{k} item"] if arr else f"The {k} for Brewline", "basis": "assumption",
                      "cites": [], "builds_on_client": False, "why": "a planner's proposal"}
        out["competitor_context"] = {"value": "The category has a flagship.", "basis": "research", "cites": [fid],
                                     "builds_on_client": False, "why": "the market research"}
        out["audience"] = {"value": "Under-30s in Dublin who meet friends in cafés", "basis": "client",
                           "cites": [], "builds_on_client": True, "why": "the client named under-30s"}
        out["mandatories"] = {"value": ["The logo"], "basis": "research", "cites": ["f_MADEUP"],
                              "builds_on_client": False, "why": "made up"}
        if psg:
            out["insight"] = {"value": "Iced coffee is how they declare summer.", "basis": "library", "cites": [psg],
                              "builds_on_client": False, "why": "a precedent"}
        out["client"] = {"value": "Brewline", "basis": "client", "cites": [], "builds_on_client": False,
                         "why": "named"}
        return out

    def review(p, score):
        return {"dimensions": [{"dimension": d, "verdict": "pass" if i < score else "vague", "evidence": "e",
                                "fix": "f"} for i, d in enumerate(DIMS)],
                "chain": {"verdict": "pass", "note": "it holds"},
                "claims": [{"part": "competitor_context", "claim": "a flagship", "cite": "x", "verdict": "partly",
                            "note": "the fact gives a year, not a name"}],
                "notes": [{"parts": ["objectives.commercial", "objectives.behavioural"], "severity": "fix",
                           "note": "The objectives need a measure and a date."},
                          {"parts": ["tone_and_world"], "severity": "consider", "note": "Could be warmer."}],
                "revise": [{"part": "objectives.commercial", "instruction": "add a measure"}] if score < 5 else [],
                "questions": ["What is the launch budget?", "How will the work be judged?"],
                "single_mindedness": {"verdict": "single", "split_into": []}, "summary": "A clear brief."}

    m.overrides.update({
        "plan_brief": plan, "draft_brief_whole": draft,
        "review_brief": lambda p: (seen.setdefault("reviews", []).append(p), review(p, 4))[1],
        "review_brief_again": lambda p: (seen.setdefault("reviews", []).append(p), review(p, 6))[1],
        "rewrite_brief_parts": lambda p: (seen.__setitem__("rewrite", p), {k: {
            "value": "Grow under-30 visits by a measured share this summer", "basis": "assumption", "cites": [],
            "builds_on_client": False, "why": "rewritten on Jude's note"} for k in p["parts"]})[1],
    })
    return m


def test_the_planned_brief_plans_researches_writes_whole_and_reviews_once(tmp_path):
    seen = {}
    s = Server(tmp_path, model=planned_model(seen), retrieval=retrieval(),
               settings_kw={"brief_flow": "planned", "brief_research_max": 2, "runlog_dir": str(tmp_path / "runs")})
    try:
        host = Host()
        replies = run(s, host, inp={"prompt": THIN, "attachments": []})
        research_calls = list(s.research.calls)
    finally:
        s.stop()
    last = replies[-1]
    assert last["job"]["state"] == "done" and last["job"]["progress"] == {"done": 4, "total": 4}
    # the research the plan asked for, capped: two units, the market read as IE
    assert sorted(research_calls) == [("consumer_culture", "IE"), ("market_structure", "IE")]
    assert host.facts, "what the research found is pinned into the brief"
    assert seen["draft"]["facts"], "Dara writes from the facts"
    assert seen["draft"]["plan"]["strategy"].startswith("Make the oat iced latte")
    # every part written; the research claim cites a pinned fact; one citing nothing it was given is assumed
    by = {t.split("#", 1)[1]: d for d in host.chain if d.get("action") == "draft" for t in d["targets"]}
    assert by["competitor_context"]["basis"] == "research" and by["competitor_context"]["cites"][0].startswith("f_")
    assert by["mandatories"]["basis"] == "assumption"
    assert by["competitor_context"]["reasoning"]["attention"].startswith("Check:"), "Jude's partly-supported claim"
    assert host.data["objectives"]["commercial"].startswith("Grow under-30 visits"), "Dara rewrote it on Jude's note"
    # Jude: one note per issue (two notes, not one per part), the fix marked bad
    notes = [d for d in host.chain if d["kind"] == "verdict"]
    assert len(notes) == 2 and {d["polarity"] for d in notes} == {"bad", "good"}
    assert sorted(t.split("#", 1)[1] for t in notes[-1]["targets"] + notes[0]["targets"]) == \
        ["objectives.behavioural", "objectives.commercial", "tone_and_world"]
    # the questions once each, from the plan, the draft and the review
    oq = host.data["open_questions"]
    assert oq.count("What is the launch budget?") == 1 and "How will the work be judged?" in oq
    assert host.data["review"]["judge"]["health"] == round(100 * 13 / 14)
    assert any(d["action"] == "plan" and d["agent"].endswith("/synthesise") for d in host.chain)
    check_change_rules(host)
    # the run log: every stage, call, search and decision, with tokens and cost; no bodies by default
    logs = list((tmp_path / "runs").rglob("draft_brief-*.jsonl"))
    assert len(logs) == 1
    evs = [json.loads(x) for x in logs[0].read_text().splitlines()]
    kinds = [e["ev"] for e in evs]
    assert kinds[0] == "job_start" and kinds[-1] == "job_end"
    assert [e["stage"] for e in evs if e["ev"] == "stage_end"] == ["extract", "context", "draft", "judge"]
    purposes = [e["purpose"] for e in evs if e["ev"] == "model"]
    for p in ("plan_brief", "draft_brief_whole", "review_brief", "rewrite_brief_parts", "review_brief_again"):
        assert p in purposes
    assert sum(1 for e in evs if e["ev"] == "research") == 2
    assert all(e["in_tokens"] == 100 and e["cost"] is not None and "payload" not in e
               for e in evs if e["ev"] == "model")
    assert any(e["ev"] == "note" and e["what"] == "research_capped" for e in evs)
    end = evs[-1]
    assert end["state"] == "done" and end["total"]["model_calls"] == len(purposes)
    assert end["total"]["decisions"] == sum(1 for e in evs if e["ev"] == "decision")
    review_notes = [e for e in evs if e["ev"] == "note" and e["what"].startswith("review_brief")]
    assert [n["score"] for n in review_notes] == [11, 13]


def test_the_run_log_is_off_unless_a_directory_is_set(tmp_path):
    seen = {}
    s = Server(tmp_path, model=planned_model(seen), retrieval=retrieval(), settings_kw={"brief_flow": "planned"})
    try:
        run(s, Host(), inp={"prompt": THIN, "attachments": []})
    finally:
        s.stop()
    assert not list(tmp_path.rglob("*.jsonl"))


class FakeJev:
    """jev: a part whose text says "vague" needs rewriting (0.9), any other does not (0.1)."""

    def __init__(self):
        self.calls = []

    def ask(self, state, questions):
        self.calls.append(questions)
        out = {}
        for n, q in questions.items():
            if q["type"] == "noul":
                out[n] = 0.9 if "vague" in str(q["instructions"].get("text", "")).lower() else \
                    0.8 if n == "chain" else 0.1
            else:
                out[n] = ("pass", {"pass": 0.8, "vague": 0.1, "missing": 0.1})
        return out


def test_the_check_loop_rewrites_what_jev_flags_and_jude_explains(tmp_path):
    seen = {}
    m = planned_model(seen)
    base_draft = m.overrides["draft_brief_whole"]

    def draft(p):
        out = base_draft(p)
        out["tone_and_world"] = {"value": ["vague and nice"], "basis": "assumption", "cites": [],
                                 "builds_on_client": False, "why": "x"}
        out["background"] = {"value": "A vague background", "basis": "assumption", "cites": [],
                             "builds_on_client": False, "why": "x"}
        return out

    def explain(p):
        seen["explain"] = p
        return {"parts": {k: {"rework": k == "tone_and_world", "why": f"{k} is generic",
                              "instruction": "make it specific" if k == "tone_and_world" else ""}
                          for k in list(p["flagged"]) + p["empty"]},
                "chain": {"issue": None, "parts": []}, "dimensions": [], "questions": ["What is the budget?"]}

    def rewrite(p):
        seen["rewrite"] = p
        return {k: {"value": ["sunny", "quick-witted"], "basis": "assumption", "cites": [], "builds_on_client": False,
                    "why": "rewritten"} for k in p["parts"]}
    m.overrides.update({"draft_brief_whole": draft, "explain_flags": explain, "rewrite_brief_parts": rewrite})
    jev = FakeJev()
    s = Server(tmp_path, model=m, retrieval=retrieval(), jev=jev,
               settings_kw={"brief_flow": "planned", "brief_judge": "jev", "runlog_dir": str(tmp_path / "runs")})
    try:
        host = Host()
        run(s, host, inp={"prompt": THIN, "attachments": []})
    finally:
        s.stop()
    assert sorted(seen["explain"]["flagged"]) == ["background", "tone_and_world"]
    assert list(seen["rewrite"]["rewrite"]) == ["tone_and_world"], "Jude kept the background: only one rewrite"
    assert host.data["tone_and_world"] == ["sunny", "quick-witted"]
    d = next(d for d in host.chain if d.get("action") == "draft" and d["targets"][0].endswith("#tone_and_world"))
    assert "Rewritten on Jude's note: tone_and_world is generic" in d["reasoning"]["because"][0]["point"]
    assert not [x for x in host.chain if x["kind"] == "verdict"], "no verdict per part: the reasons sit on the rewrites"
    assert "What is the budget?" in host.data["open_questions"]
    assert host.data["review"]["judge"]["health"] == 100
    assert len(jev.calls) == 2, "jev before and after the rewrite"
    check_change_rules(host)
    evs = [json.loads(x) for x in next((tmp_path / "runs").rglob("*.jsonl")).read_text().splitlines()]
    assert sum(1 for e in evs if e["ev"] == "jev") == 2
    j = next(e for e in evs if e["ev"] == "note" and e["what"] == "jude_on_flags")
    assert j["disagreed_with_jev"] == ["background"]
    assert not any(e["ev"] == "model" and e["purpose"] == "review_brief" for e in evs), "no whole-brief review call"


def test_a_missing_dimension_reaches_dara_and_client_is_only_what_ellis_captured(tmp_path):
    seen = {}
    m = planned_model(seen)
    base_draft = m.overrides["draft_brief_whole"]

    def draft(p):
        out = base_draft(p)
        out["background"] = {"value": "Sales are flat.", "basis": "client", "cites": [], "builds_on_client": False,
                             "why": "the client said so"}  # the thin brief says nothing about sales
        return out

    class MissingEval(FakeJev):
        def ask(self, state, questions):
            out = super().ask(state, questions)
            if len(self.calls) == 1:
                out["dim_evaluation_criteria"] = ("missing", {"missing": 0.9, "vague": 0.05, "pass": 0.05})
            return out

    def explain(p):
        seen["explain"] = p
        return {"parts": {k: {"rework": False, "why": "fine", "instruction": ""} for k in list(p["flagged"]) + p["empty"]},
                "chain": {"issue": None, "parts": []},
                "dimensions": [{"dimension": "evaluation_criteria", "parts": ["desired_response.do"],
                                "instruction": "say how the work will be judged"}], "questions": []}
    m.overrides.update({"draft_brief_whole": draft, "explain_flags": explain,
                        "rewrite_brief_parts": lambda p: (seen.__setitem__("rewrite", p), {k: {
                            "value": "Visit twice in June; judged on repeat visits", "basis": "assumption",
                            "cites": [], "builds_on_client": False, "why": "x"} for k in p["parts"]})[1]})
    s = Server(tmp_path, model=m, retrieval=retrieval(), jev=MissingEval(),
               settings_kw={"brief_flow": "planned", "brief_judge": "jev"})
    try:
        host = Host()
        run(s, host, inp={"prompt": THIN, "attachments": []})
    finally:
        s.stop()
    assert seen["explain"]["missing_dimensions"].keys() == {"evaluation_criteria"}
    assert seen["rewrite"]["rewrite"] == {"desired_response.do": "say how the work will be judged"}
    assert host.data["desired_response"]["do"].startswith("Visit twice in June")
    bg = next(d for d in host.chain if d.get("action") == "draft" and d["targets"][0].endswith("#background"))
    assert bg["basis"] == "assumption", "the client said nothing about sales: not the client's words"


def test_a_file_read_as_pages_still_lands_in_a_brief_the_schema_takes(tmp_path):
    # 2026-10-01: the record of a file read as pages carried an extra key the schema refuses, so
    # the host refused the whole brief and the planner saw an empty document
    import base64
    import hashlib
    seen = {}
    m = planned_model(seen)
    m.overrides["read_document"] = lambda p: (seen.__setitem__("read", p), {
        "text": "Page 1\nSamaritans tender\n[Bar chart: calls by age, 18-34 up 12%]", "pages": 1})[1]
    pdf = b"%PDF-1.7 << /Type /Page >> scan"
    att = {"name": "tender deck.pdf", "sha256": hashlib.sha256(pdf).hexdigest(), "text": "Samaritans tender",
           "media_type": "application/pdf",
           "document": {"media_type": "application/pdf", "data": base64.b64encode(pdf).decode()}}
    s = Server(tmp_path, model=m, retrieval=retrieval(), settings_kw={"brief_flow": "planned"})
    try:
        host = Host()
        run(s, host, inp={"prompt": THIN, "attachments": [att]})
    finally:
        s.stop()
    assert seen["read"]["name"] == "tender deck.pdf", "the deck was read as pages"
    mats = host.data["materials"]
    rec = next(v for v in mats.values() if v["name"] == "tender deck.pdf")
    assert set(rec["transcribed"]) <= {"model", "backend"}
    assert host.data.get("insight"), "the brief landed"
    check_change_rules(host)  # the whole document validates against Brief Maker's schema


class HeldJev(FakeJev):
    """jev reading the facts held for a planned search: the market's players are held, the
    audience's habits are not."""

    def ask(self, state, questions):
        if "held" in questions:
            self.calls.append(questions)
            return {"held": 0.9 if "who sells" in str(state.get("need")) else 0.2}
        return super().ask(state, questions)


def carried_facts(n=100):
    """What a brief made from research carries: many pins, each with the source it was read from."""
    out = [{"id": f"f_CARRIED{i:04d}", "entity": "category/food_drink.coffee_shops", "key": f"market.player_{i}",
            "value": f"Chain {i}", "unit": "text", "market": "IE", "as_of": "2026-01-01",
            "retrieved_at": "2026-09-20", "sources": ["src_cso0001"], "quotes": {"src_cso0001": f"Chain {i} sells"},
            "confidence": "medium", "status": "active", "version": 1, "layer": "category",
            "origin": f"fact://category/food_drink.coffee_shops/market.player_{i}@1", "decision": "d_CARRIED001",
            "pinned_at": "2026-09-20T00:00:00Z", "pin_reason": "carried", "licence": "open", "method": "report"}
           for i in range(n)]
    return out, [{"id": "src_cso0001", "uri": "https://www.cso.ie/coffee", "publisher": "CSO",
                  "title": "Coffee shops in Ireland", "tier": "primary", "domain": "cso.ie", "licence": "open"}]


def test_a_brief_from_research_sees_every_fact_with_its_source_and_does_not_search_what_it_holds(tmp_path):
    seen, jev = {}, HeldJev()
    s = Server(tmp_path, model=planned_model(seen), retrieval=retrieval(), jev=jev,
               settings_kw={"brief_flow": "planned", "brief_research_max": 3, "brief_judge": "review"})
    try:
        host = Host()
        host.facts, host.sources = carried_facts(100)
        run(s, host, inp={"prompt": THIN, "attachments": []})
        research_calls = list(s.research.calls)
    finally:
        s.stop()
    # Sam and Dara see every fact the brief holds (not the first 80), each with where it was read
    assert len(seen["plan"]["facts"]) >= 100 and len(seen["draft"]["facts"]) >= 100
    assert seen["plan"]["facts"][0]["source"]["publisher"] == "CSO"
    # the market's players are held: that search is skipped; the audience is not: it runs
    assert ("market_structure", "IE") not in research_calls and ("consumer_culture", "IE") in research_calls
    held = [d for d in host.chain if d.get("action") == "research_held"]
    assert held and "jev" in json.dumps(held[0]["reasoning"])
