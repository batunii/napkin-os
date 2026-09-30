"""Fake components for tests: a model client that answers the Messages API
shape with canned structured outputs computed from the request's input, and a
research port with deterministic sources. Fast, deterministic, no network.

The fakes stand in for components only; everything the middleware decides is
exercised for real (quote checks, tiering, merge, cite rule, identify rules).
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from napkin.doc import LENS_NAMESPACE
from napkin.rules.markets import COUNTRIES

STOP_CAPS = {"EXAMPLE", "TV", "TVC", "UK", "GB", "IE", "EU", "EV", "OOH", "BVOD", "I"}


def reasoning(points, rejected=(), attention=None, only=None):
    """A model's reasoning block: `points` are (text, [cites])."""
    return {"because": [{"point": t, "cites": list(c)} for t, c in points],
            "rejected": [{"option": o, "why": w} for o, w in rejected], "only_option": only,
            "would_change_if": "the client says otherwise", "attention": attention}


def _h(*parts) -> int:
    return int(hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:8], 16)


class FakeModel:
    """`client.with_options(...).messages.create(**kw)` -> a Messages-shaped response."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}
        self.messages = self
        import threading
        self._lock = threading.Lock()
        self.fail_checks = {}  # (purpose, check) -> how many times to fail it (brief judge)

    def with_options(self, **_):
        return self

    def create(self, **kw):
        user = kw["messages"][0]["content"]
        if isinstance(user, list):  # image parts, then the text part
            user = next(p["text"] for p in user if p.get("type") == "text")
        purpose = re.match(r"Task: (\w+)", user).group(1)
        payload = json.loads(re.search(r"<input>\n(.*)\n</input>", user, re.S).group(1))
        with self._lock:
            self.calls.append((purpose, payload, kw))
        fn = self.overrides.get(purpose) or getattr(self, "r_" + purpose, None) or brief_responder(self, purpose)
        out = fn(payload)
        text = out if isinstance(out, str) else json.dumps(out)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))

    # -- responders -------------------------------------------------------------
    @staticmethod
    def r_extract(p):
        out = {"brand": None, "client_org": None, "markets": [], "campaign_type": None, "problem": None,
               "objective": None, "audience_stated": None, "competitor_set": [], "success_measures": [],
               "budget": None, "in_market": None, "channels_mandated": [], "deliverables": [], "constraints": []}
        mids = [m["material_id"] for m in p["materials"]]
        # One point cites a material it was never given: the cite check drops it.
        out["grounds"] = reasoning([("The prompt states the markets and the budget", mids[:1]),
                                      ("An invented source", ["mat_NOTGIVEN"])],
                                     rejected=[("fill the audience", "the prompt does not describe one")])
        for m in p["materials"]:
            t, mid = m["text"], m["material_id"]
            for rx, code in COUNTRIES:
                for x in re.finditer(rx, t):
                    if code not in [c["code"] for c in out["markets"]]:
                        out["markets"].append({"code": code, "quote": x.group(0), "material_id": mid})
            if (x := re.search(r"€\s?(\d+)k", t)):
                out["budget"] = {"amount": int(x.group(1)) * 1000, "currency": "EUR", "quote": x.group(0),
                                 "material_id": mid}
            if (x := re.search(r"The problem[^:\n]*:\s*(.+)", t)):
                out["problem"] = {"quote": x.group(1), "material_id": mid}
            if (x := re.search(r"What we need:\s*(.+)", t)):
                out["objective"] = {"quote": x.group(1), "material_id": mid}
            if (x := re.search(r"([A-Z]\w+(?: [A-Z]\w+)*) as the one to beat", t)):
                out["competitor_set"].append({"name": x.group(1), "quote": x.group(0), "material_id": mid})
            if (x := re.search(r"TV is a must", t)):
                out["channels_mandated"].append({"slug": "tv", "quote": x.group(0), "material_id": mid})
            if (x := re.search(r"relaunching", t)):
                out["campaign_type"] = {"value": "rebrand" if False else "launch", "quote": x.group(0),
                                        "material_id": mid}
        return out

    @staticmethod
    def r_identify(p):
        brands, cats, client = [], [], None
        for m in p["materials"]:
            t, mid = m["text"], m["material_id"]
            if (x := re.search(r"^Brand:\s*(.+)$", t, re.M)):
                brands.append({"name": x.group(1).strip(), "quote": x.group(0), "material_id": mid,
                               "basis": "brand_label", "comparator": False})
            if (x := re.search(r"brands in this brief: ([A-Z][\w ]+?) and ([A-Z][\w ]+?)\.", t)):
                for g in (1, 2):
                    brands.append({"name": x.group(g), "quote": x.group(g), "material_id": mid, "basis": "none",
                                   "comparator": False})
            if (x := re.search(r"([A-Z]\w+(?: [A-Z]\w+)*) as the one to beat", t)):
                brands.append({"name": x.group(1), "quote": x.group(0), "material_id": mid, "basis": "none",
                               "comparator": True})
            for w in re.findall(r"\b[A-Z]{2,}\b", t):
                if w not in STOP_CAPS and w not in [b["name"] for b in brands]:
                    brands.append({"name": w, "quote": w, "material_id": mid, "basis": "none", "comparator": False})
            if (x := re.search(r"Director, (.+)", t)):
                client = {"name": x.group(1).strip(), "quote": x.group(1).strip(), "material_id": mid}
            for rx, leaf in ((r"\b[Tt]onic\b|soft drink", "soft_drinks.carbonates"),
                             (r"\b(?:EV|Ev|electric)\b", "automotive.ev_charging"), (r"\bhybrid\b", "automotive.hybrid")):
                if (x := re.search(rx, t)) and leaf not in [c["leaf"] for c in cats]:
                    cats.append({"leaf": leaf, "quote": x.group(0), "material_id": mid})
        mids = [m["material_id"] for m in p["materials"]]
        why = reasoning([("The material labels the brand", mids[:1])],
                        rejected=[(f"{b['name']} as the client's", "a comparator") for b in brands if b["comparator"]],
                        only="one brand is named")
        return {"brands": brands, "client_org": client, "categories": cats[:2], "grounds": why}

    @staticmethod
    def r_select(p):
        out = []
        for sent in re.split(r"(?<=[.!?])\s+", p["prompt"]):
            if re.search(r"[Ee]ffectiveness", sent) and re.search(r"\bonly\b", sent):
                named = [c for rx, c in COUNTRIES if re.search(rx, sent)]
                out.append({"lens": "effectiveness_evidence", "run": True,
                            "skip_markets": [m for m in p["markets"] if m not in named], "reason": sent})
        pid = [p["prompt_material_id"]] if p.get("prompt_material_id") else []
        return {"lenses": out, "grounds": reasoning(
            [("The prompt limits effectiveness to some markets" if out else "The prompt asks for everything", pid)],
            rejected=[("research everything everywhere", "the prompt limits a lens")] if out else [],
            only=None if out else "the prompt limits nothing")}

    @staticmethod
    def r_classify_category(p):
        return {"leaves": []}

    @staticmethod
    def r_extract_facts(p):
        facts = []
        ind, fl = {}, {}
        for s in p["sources"]:
            for q in s["excerpts"]:
                if (x := re.search(r"indicator for [A-Z]{2} stood at (\d+)% in (\d{4})", q)):
                    ind.setdefault((x.group(1), x.group(2)), []).append({"source_id": s["source_id"],
                                                                          "quote": x.group(0)})
                if (x := re.search(r"flagship launched in (\d{4})", q)):
                    fl.setdefault(x.group(1), []).append({"source_id": s["source_id"], "quote": x.group(0)})
        cat = p["categories"][0]["code"]
        for (v, y), ev in ind.items():
            facts.append({"about": "category", "category": cat, "competitor": None, "key_suffix": "indicator",
                          "value_number": int(v) / 100, "value_text": None, "value_boolean": None,
                          "unit": "proportion", "as_of": f"{y}-12-31", "market_specific": True, "evidence": ev})
        for y, ev in fl.items():
            facts.append({"about": "category", "category": cat, "competitor": None, "key_suffix": "flagship_year",
                          "value_number": None, "value_text": y, "value_boolean": None, "unit": "date",
                          "as_of": None, "market_specific": False, "evidence": ev})
        return {"facts": facts, "not_found": [p["wanted"][-1]]}

    @staticmethod
    def r_synthesise(p):
        by = {}
        for pin in p["pins"]:
            if pin["lens"]:
                by.setdefault(pin["lens"], []).append(pin["id"])
        out = [{"lens": l, "statement": f"The {l.replace('_', ' ')} picture reads differently by market.",
                "cites": ids[:2], "markets": [],
                # A figure with no cite is dropped; the cited point stands.
                "grounds": reasoning([("The pins differ by market", ids[:2]), ("It is 99% sure", [])],
                                       rejected=[("one finding per market", "the pins compare")])}
               for l, ids in by.items()]
        out.append({"lens": "market_structure", "statement": "It grew 99% last year.",
                    "cites": list(by.get("market_structure", [])[:1]) or [p["pins"][0]["id"]], "markets": [],
                    "grounds": reasoning([("Growth", ["f_NOTAPIN"])], only="one reading")})
        return {"findings": out, "audience": None}

    @staticmethod
    def r_layout(p):
        return fake_layout(p)

    @staticmethod
    def r_report(p):
        secs, first = [], None
        for l in p["lenses"]:
            ids = [x["id"] for x in l["findings"]] or [x["id"] for x in l["pins"]]
            first = first or ids[0]
            claims = [{"text": f"{l['title']} is covered by the research.", "cites": ids[:2]},
                      {"text": "A made-up figure of 42 appears here.", "cites": ids[:1]}]
            secs.append({"lens": l["lens"], "claims": claims})
        return {"grounds": reasoning([("The headline leads with what the research covers", [first])],
                                       rejected=[("lead with a single market", "the research spans markets")]),
                "headline": {"text": f"{p['brand']}: the research is in.", "cites": [first]},
                "summary": [{"text": "Read the sections below.", "cites": [first]},
                            {"text": "Nothing is cited here.", "cites": []}],
                "sections": secs}


def fake_layout(p):
    """A layout the way an agent writes one, with the mistakes the rule must catch."""
    nums = [x["id"] for x in p["pins"] if isinstance(x["value"], (int, float))]
    fi = [x["id"] for x in p["findings"]]
    big = "".join(f'<clan-field ref="{i}" as="big"></clan-field>' for i in nums[:3])
    return {"html": (
        f'<header class="cl-head" onclick="x()"><span class="cl-eyebrow">Research</span>'
        f'<h1 class="cl-title">{p["headline"]["text"]}</h1><script>steal()</script></header>'
        f'<div class="cl-nums evil">{big}</div>'
        f'<section class="cl-block"><p>Grounded. <clan-cite refs="{" ".join(nums[:1])}"></clan-cite></p>'
        + (f'<p><clan-field ref="{fi[0]}" as="claim"></clan-field></p>' if fi else "")
        + '<p>This sentence has no evidence at all.</p>'
        f'<p>A made-up 73% share. <clan-cite refs="{nums[0]}"></clan-cite></p>'
        '<p>Invented: <clan-field ref="f_NOTREAL01"></clan-field> and more.</p>'
        f'<clan-chart kind="bar" refs="{" ".join(nums[:3])}" title="By lens"></clan-chart>'
        '<img src="x" onerror="bad()"><a href="javascript:bad()">link</a></section>'
        '<footer class="cl-foot"><clan-sources></clan-sources></footer>')}


class FakeResearch:
    """The research port, deterministic: two corroborating sources (primary +
    secondary) on a per-market figure, and a tertiary source whose
    market-independent year differs by market (so two markets contest it)."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def search(self, query, lens, market, entity=None, category=None, max_sources=6, attribution=None):
        from napkin.research import ResearchError
        self.calls.append((lens, market))
        if (lens, market) in self.fail_on:
            raise ResearchError("research returned 502")
        v = _h(lens, market, category) % 60 + 5
        stat = f"The {lens} indicator for {market} stood at {v}% in 2025."
        srcs = [{"id": "s1", "url": f"https://www.cso.ie/en/{lens}/{market.lower()}", "publisher": "CSO",
                 "title": f"{lens} {market}", "retrieved_at": "2026-09-20", "published_at": "2026-03-01",
                 "excerpts": [stat]},
                {"id": "s2", "url": f"https://www.simi.ie/{lens}-{market.lower()}", "publisher": "SIMI",
                 "title": f"{lens} {market} (trade)", "retrieved_at": "2026-09-20", "published_at": None,
                 "excerpts": [f"Industry figures: {stat}"]}]
        if lens == "market_structure":
            year = 2019 + (["IE", "GB", "FR", "DE"].index(market) if market in ("IE", "GB", "FR", "DE") else 0)
            srcs.append({"id": "s3", "url": f"https://blog.example.net/{market.lower()}-flagship",
                         "publisher": "A blog", "title": "Flagship", "retrieved_at": "2026-09-20", "published_at": None,
                         "excerpts": [f"The category flagship launched in {year}, the blog says."]})
        return {"sources": srcs, "trace": {"backend": "fake", "queries": [query]}}


def lens_key(lens, suffix):
    return f"{LENS_NAMESPACE[lens]}.{suffix}"


# ---------------------------------------------------------------------------
# Brief Maker: responders for the draft_brief / regenerate_field purposes
# ---------------------------------------------------------------------------

BRIEF_TEXT = """Client: Harbour Drinks Ltd
Project: Midweek Tonic

Problem: Harbour Tonic is losing midweek drinkers to low-alcohol beer.
Background: Sales fell 12% in 2025 as midweek occasions shrank.
Objective (commercial): Grow midweek volume by 8% by Q4 2026.
Objective (behavioural): Get lapsed drinkers to choose Harbour Tonic on a weeknight.
Audience: Thirty-something professionals who cut back midweek but still want a treat.
Competitors: Saltmarsh Soda owns refreshment; low-alcohol beers own the weeknight.
Budget: 200k euro across social and OOH.
Deliverables: Two social films and six OOH sites.
Mandatory: Harbour logo on every asset.
Tone: Warm, grown-up, a little wry.
Key message: The grown-up weeknight treat.
Proof: Only 20 calories per serve."""

LABEL_KEYS = {"Problem": "business_problem", "Background": "background_context", "Audience": "target_audience",
              "Competitors": "competitors_market", "Budget": "budget", "Deliverables": "deliverables",
              "Mandatory": "mandatories", "Tone": "tone_and_brand", "Key message": "key_message", "Proof": "proof_points"}

INSIGHTS = ["Midweek drinkers cut back because they want to feel in control, which means the job is to make a "
            "treat feel like a choice.",
            "People like tonic.",
            "Weeknights feel grey because nothing marks them, which means a small ritual can mark the evening.",
            "Professionals ration pleasure because they fear losing the week, which means permission matters."]
INSIGHT_SHARP = "Midweek drinkers cut back because they want control, which means a treat must feel chosen."
SMPS = ["The grown-up weeknight treat you choose.", "Refreshing. Low calorie. Tasty.", "Your weeknight, marked.",
        "Tonic for grown-ups."]


def _mids(p):
    return [m["material_id"] for m in p.get("materials", [])]


def _ids(p, prefix):
    if prefix == "psg_":
        return [x["id"] for x in p.get("passages", [])]
    if prefix == "cap_":
        return [x["id"] for x in p.get("capture", [])]
    return [x["id"] for x in p.get("pins", [])]


def _grounds(p, extra_bad=True):
    psg, cap = _ids(p, "psg_"), _ids(p, "cap_")
    pts = []
    if psg:
        pts.append(("The precedent shows the shape of a tension", psg[:1]))
    if len(psg) > 1:
        pts.append(("A second pack agrees", [x for x in psg if x != psg[0]][-1:]))
    if cap:
        pts.append(("The client describes the audience cutting back", cap[:1]))
    if extra_bad:
        pts += [("It is 99% certain", []), ("An invented passage", ["psg_00000000000000000000"])]
    return reasoning(pts, rejected=[("a category truth", "every rival could say it")])


def brief_responder(model, purpose):
    """Responders for Brief Maker's purposes, which are families
    (draft_<rubric>, judge_<rubric>, ...)."""

    def capture(p):
        items, htw = [], []
        for m in p["materials"]:
            t, mid = m["text"], m["material_id"]
            for line in t.splitlines():
                x = re.match(r"^(Objective \((\w+)\)|[A-Z][\w ]+?):\s*(.+)$", line.strip())
                if not x:
                    continue
                label, otype, val = x.group(1), x.group(2), x.group(3)
                if otype:
                    items.append({"key": "objective", "value": val, "status": "fact", "quote": line.strip(),
                                  "material_id": mid, "objective_type": otype})
                elif label in LABEL_KEYS:
                    items.append({"key": LABEL_KEYS[label], "value": val, "status": "fact", "quote": val,
                                  "material_id": mid, "objective_type": None})
            if "low-alcohol beers own the weeknight" in t:
                htw.append({"kind": "winning_themes", "point": "Own the weeknight",
                            "evidence": "low-alcohol beers own the weeknight", "material_id": mid})
        mids = _mids(p)
        if mids:
            # a fact whose quote is not in the material (dropped) and an inference (kept, as an assumption)
            items.append({"key": "decision_makers", "value": "The CMO decides", "status": "fact",
                          "quote": "The CMO signs off on everything", "material_id": mids[0], "objective_type": None})
            items.append({"key": "strategic_angle", "value": "Own the weeknight treat", "status": "assumption",
                          "quote": None, "material_id": mids[0], "objective_type": None})
        text = "\n".join(m["text"] for m in p["materials"])
        client = re.search(r"^Client: (.+)$", text, re.M)
        proj = re.search(r"^Project: (.+)$", text, re.M)
        return {"items": items, "how_to_win": htw,
                "open_questions": [{"question": "How will the work be judged?", "why_it_matters": "No criteria given",
                                    "priority": "important"}],
                "client": {"value": client.group(1), "quote": client.group(0), "material_id": mids[0]} if client else None,
                "project_name": {"value": proj.group(1), "quote": proj.group(0), "material_id": mids[0]} if proj else None}

    def scorecard(p):
        mid = (_mids(p) or [None])[0]
        dims = [{"dimension": d, "verdict": "pass", "evidence": None, "material_id": None, "fix": None}
                for d in ("objectives_quality", "single_minded_message", "budget_interlock", "strategic_clarity",
                          "language")]
        dims[0]["evidence"], dims[0]["material_id"] = "Grow midweek volume by 8% by Q4 2026.", mid
        dims.append({"dimension": "audience_vividness", "verdict": "vague", "evidence": "words that are not there",
                     "material_id": mid, "fix": "Picture one person."})
        dims.append({"dimension": "evaluation_criteria", "verdict": "missing", "evidence": None, "material_id": None,
                     "fix": "Agree how the work is judged."})
        return {"dimensions": dims, "single_mindedness": {"verdict": "single", "split_into": []},
                "summary": "A clear brief with a thin audience and no criteria."}

    def transcribe(p):
        return {"text": "Brief card\nProblem: Harbour Tonic is losing midweek drinkers to low-alcohol beer.",
                "visuals": ["a tonic bottle on a kitchen table"]}

    def draft_insight(p):
        n = p.get("n", 4)
        return {"candidates": [{"value": INSIGHTS[i % 4], "grounds": _grounds(p)} for i in range(n)]}

    def draft_smp(p):
        n = p.get("n", 4)
        return {"candidates": [{"value": SMPS[i % 4], "grounds": _grounds(p)} for i in range(n)]}

    def rank(p):
        idx = [c["index"] for c in p["candidates"]]
        order = idx[1:2] + idx[:1] + idx[2:]  # ranks a weak one first: the auto gate must skip it
        return {"ranking": order, "why_winner": "the purest", "losers": [{"index": i, "why": "less ownable"}
                                                                        for i in idx if i != order[0]]}

    def sharpen_insight(p):
        return {"value": INSIGHT_SHARP, "grounds": _grounds(p)}

    def sharpen_smp(p):
        return {"value": p["draft"], "grounds": _grounds(p)}

    def draft_rtb(p):
        g = reasoning([("Only 20 calories per serve, the client says", _ids(p, "cap_")[:1]),
                       ("The proof the client gives", _ids(p, "cap_")[:1])], only="the client gave one proof")
        return {"value": ["Only 20 calories per serve", "Made by a family firm since 1921"], "grounds": g}

    def draft_dr(p):
        v = {"think": "A weeknight can have a treat.", "feel": "In control and rewarded.",
             "do": "Pick Harbour Tonic on a weeknight."}
        if p.get("redraft_only"):
            v[p["redraft_only"]] = "Choose a Harbour Tonic after work."
        return {"value": v, "grounds": _grounds(p, extra_bad=False)}

    def revise(p):
        v = p["current_draft"]
        if isinstance(v, str):
            v = "Midweek drinkers cut back because they fear losing the week, which means a treat must feel earned."
        return {"value": v, "grounds": _grounds(p, extra_bad=False)}

    def judge(p):
        out = {}
        for t in p["tests"]:
            key = (purpose, t["id"])
            with model._lock:
                left = model.fail_checks.get(key, 0)
                if left:
                    model.fail_checks[key] = left - 1
            out[t["id"]] = ({"verdict": "fail", "reason": f"{t['id']} is not met", "fix": f"make {t['id']} hold"}
                            if left else {"verdict": "pass", "reason": "holds", "fix": None})
        return out

    def coherence(p):
        out = {}
        for r in p["rules"]:
            key = (purpose, r["id"])
            with model._lock:
                left = model.fail_checks.get(key, 0)
                if left:
                    model.fail_checks[key] = left - 1
            out[r["id"]] = ({"verdict": "fail", "reason": "they pull apart", "fix": "bring them into line"} if left
                            else {"verdict": "pass", "reason": "holds together", "fix": None})
        return out

    table = {"capture": capture, "scorecard": scorecard, "transcribe": transcribe, "draft_insight": draft_insight,
             "draft_smp": draft_smp, "rank_insight": rank, "rank_smp": rank, "sharpen_insight": sharpen_insight,
             "sharpen_smp": sharpen_smp, "draft_reasons_to_believe": draft_rtb, "draft_desired_response": draft_dr,
             "judge_coherence": coherence}
    if purpose in table:
        return table[purpose]
    if purpose.startswith("revise_"):
        return revise
    if purpose.startswith("judge_"):
        return judge
    raise AttributeError(f"no fake responder for {purpose}")


# ---------------------------------------------------------------------------
# Retrieval: a fake napkin.retrieval/1 service (an httpx MockTransport handler)
# ---------------------------------------------------------------------------

PACK_TEXT = {
    ("playbooks", "playbook", (), 2): {
        ("insight.md", "Finding the tension"): "An insight names a human tension: what people want against what "
                                               "holds them back, and why it matters now.",
        ("proposition.md", "One thing"): "A single-minded proposition says one thing, derived from the insight, "
                                         "that a rival could not say.",
        ("qa.md", "Decision rules"): "Pressure-test the proposition: one idea, ownable, true to the insight, "
                                     "supported by every reason to believe.",
    },
    ("cannes", "case", ("loop4_insight", "loop6_substantiation"), 1): {
        ("case-harbour.md", "The insight"): "Midweek drinkers were not quitting; they were trading down to "
                                            "something they could be proud of.",
        ("case-harbour.md", "Results"): "Sales rose while the category fell, and the brand became the grown-up "
                                        "choice.",
    },
}


def _passage(pack, source, section, text, rank, k):
    sha = hashlib.sha256(text.encode()).hexdigest()
    slug_ = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
    return {"id": "psg_" + hashlib.sha256(f"{pack}\n{source}\n{section}\n{text}".encode()).hexdigest()[:20],
            "uri": f"passage://{pack}/{source}#{slug_}@{sha[:16]}", "pack": pack, "scope": "house",
            "licence": "licensed-internal", "source": source, "section": section,
            "citation": f"{source} › {section}", "text": text, "text_sha256": sha, "truncated": False,
            "rank": rank, "score": round(1 - (rank - 1) / k, 3), "metadata": {"source": pack}}


class FakeRetrievalService:
    def __init__(self, fail=None, tamper=False, empty=False):
        self.requests, self.fail, self.tamper, self.empty = [], list(fail or []), tamper, empty

    def packs_list(self):
        return [{"tag": tag, "id": tag, "kind": kind, "scope": "house", "licence": "licensed-internal", "k": k,
                 "loops": list(loops), "passages": len(secs), "filterable": ["category"],
                 "version": "sha256:" + hashlib.sha256(tag.encode()).hexdigest()[:16]}
                for (tag, kind, loops, k), secs in PACK_TEXT.items()]

    def __call__(self, request):
        import httpx
        self.requests.append(request)
        if self.fail:
            st = self.fail.pop(0)
            return httpx.Response(st, json={"error": {"type": "unknown_pack" if st == 404 else "upstream_failed",
                                                      "message": "no"}})
        if not request.headers.get("x-napkin-org"):
            return httpx.Response(400, json={"error": {"type": "missing_scope", "message": "no org"}})
        if request.url.path == "/v1/packs":
            return httpx.Response(200, json={"packs": self.packs_list(), "embed_model": None, "backend": "fake"})
        body = json.loads(request.content)
        want = body.get("packs") or [p["tag"] for p in self.packs_list()]
        words = set(re.findall(r"[a-z]{4,}", body["query"].lower()))
        cands = []
        for (tag, _kind, _loops, _k), secs in PACK_TEXT.items():
            if tag not in want:
                continue
            for (src, sec), text in secs.items():
                cands.append((-len(words & set(re.findall(r"[a-z]{4,}", text.lower()))), tag, src, sec, text))
        cands.sort()
        k = body["k"]
        ps = [] if self.empty else [_passage(t, s, sec, x, i + 1, k) for i, (_, t, s, sec, x) in enumerate(cands[:k])]
        if self.tamper and ps:
            ps[0] = dict(ps[0], text="TAMPERED")
        vers = {p["tag"]: p["version"] for p in self.packs_list() if p["tag"] in want}
        return httpx.Response(200, json={"passages": ps, "trace": {"backend": "fake", "embed_model": None,
                                                                   "packs": vers}})
