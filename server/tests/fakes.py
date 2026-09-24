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

    def with_options(self, **_):
        return self

    def create(self, **kw):
        user = kw["messages"][0]["content"]
        purpose = re.match(r"Task: (\w+)", user).group(1)
        payload = json.loads(re.search(r"<input>\n(.*)\n</input>", user, re.S).group(1))
        self.calls.append((purpose, payload, kw))
        fn = self.overrides.get(purpose) or getattr(self, "r_" + purpose)
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
        out["reasoning"] = reasoning([("The prompt states the markets and the budget", mids[:1]),
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
        return {"brands": brands, "client_org": client, "categories": cats[:2], "reasoning": why}

    @staticmethod
    def r_select(p):
        out = []
        for sent in re.split(r"(?<=[.!?])\s+", p["prompt"]):
            if re.search(r"[Ee]ffectiveness", sent) and re.search(r"\bonly\b", sent):
                named = [c for rx, c in COUNTRIES if re.search(rx, sent)]
                out.append({"lens": "effectiveness_evidence", "run": True,
                            "skip_markets": [m for m in p["markets"] if m not in named], "reason": sent})
        pid = [p["prompt_material_id"]] if p.get("prompt_material_id") else []
        return {"lenses": out, "reasoning": reasoning(
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
                "reasoning": reasoning([("The pins differ by market", ids[:2]), ("It is 99% sure", [])],
                                       rejected=[("one finding per market", "the pins compare")])}
               for l, ids in by.items()]
        out.append({"lens": "market_structure", "statement": "It grew 99% last year.",
                    "cites": list(by.get("market_structure", [])[:1]) or [p["pins"][0]["id"]], "markets": [],
                    "reasoning": reasoning([("Growth", ["f_NOTAPIN"])], only="one reading")})
        return {"findings": out, "audience": None}

    @staticmethod
    def r_report(p):
        secs, first = [], None
        for l in p["lenses"]:
            ids = [x["id"] for x in l["findings"]] or [x["id"] for x in l["pins"]]
            first = first or ids[0]
            claims = [{"text": f"{l['title']} is covered by the research.", "cites": ids[:2]},
                      {"text": "A made-up figure of 42 appears here.", "cites": ids[:1]}]
            secs.append({"lens": l["lens"], "claims": claims})
        return {"reasoning": reasoning([("The headline leads with what the research covers", [first])],
                                       rejected=[("lead with a single market", "the research spans markets")]),
                "headline": {"text": f"{p['brand']}: the research is in.", "cites": [first]},
                "summary": [{"text": "Read the sections below.", "cites": [first]},
                            {"text": "Nothing is cited here.", "cites": []}],
                "sections": secs}


class FakeResearch:
    """The research port, deterministic: two corroborating sources (primary +
    secondary) on a per-market figure, and a tertiary source whose
    market-independent year differs by market (so two markets contest it)."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def search(self, query, lens, market, entity=None, category=None, max_sources=6):
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
