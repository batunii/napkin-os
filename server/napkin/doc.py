"""Reading the request's `clan` context and building what a `change` carries.

Everything here is contract shape (middleware-api.md §3, Contract 3): the
campaign field table, lenses, addresses, read-sets, merge patches,
decisions. No policy lives here — that is `napkin.rules`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from . import BACKEND
from .util import bad, iso, norm_sha

LENSES = [
    "market_structure", "brands_positioning", "consumer_culture", "category_codes",
    "rhythm_moments", "media_spend", "regulation_clearance", "effectiveness_evidence",
]
LENS_TITLES = {
    "market_structure": "The market", "brands_positioning": "Brands and comparators",
    "consumer_culture": "Who it is for", "category_codes": "Category codes",
    "rhythm_moments": "Timing", "media_spend": "Media", "regulation_clearance": "Clearance",
    "effectiveness_evidence": "Effectiveness",
}
# Every fact key lives under a namespace that names its lens, so a pin can be
# placed in the report without asking anyone. Extra namespaces other writers
# use are mapped too (a pin taken by another writer still finds its lens).
LENS_NAMESPACE = {
    "market_structure": "market", "brands_positioning": "positioning",
    "consumer_culture": "consumer", "category_codes": "codes", "rhythm_moments": "rhythm",
    "media_spend": "media", "regulation_clearance": "regulation",
    "effectiveness_evidence": "effectiveness",
}
KEY_LENS = {v: k for k, v in LENS_NAMESPACE.items()}
KEY_LENS.update({"awareness": "brands_positioning", "launch": "brands_positioning",
                 "product": "brands_positioning", "share": "market_structure"})

STAGES = ["extract", "identify", "select", "research", "synthesise", "report"]

CAMPAIGN_FIELDS = [
    "id", "name", "brand", "client_org", "categories", "markets", "campaign_type",
    "problem", "objective", "audience_stated", "audience", "competitor_set",
    "success_measures", "budget_band", "in_market", "channels_mandated",
    "deliverables", "constraints", "ask_source",
]
GATES = {
    "id": "created", "name": "created", "brand": "created", "client_org": "created",
    "categories": "research", "markets": "research", "campaign_type": "none",
    "problem": "brief", "objective": "brief", "audience_stated": "brief",
    "audience": "brief", "competitor_set": "none", "success_measures": "none",
    "budget_band": "brief", "in_market": "none", "channels_mandated": "none",
    "deliverables": "none", "constraints": "none", "ask_source": "created",
}
LIST_FIELDS = {"categories", "markets", "competitor_set", "success_measures",
               "channels_mandated", "deliverables", "constraints"}
# Minted by the host when a human opens the document, or filled by research.
NOT_EXTRACTED = {"id", "ask_source", "name", "audience"}
IDENTIFY_FIELDS = {"brand", "client_org", "categories"}
CONF = ["low", "medium", "high"]
LICENCE_RANK = ["open", "licensed-internal", "client-confidential"]

ISO_3166 = set("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN
BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL
GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM
JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP
NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD
SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())
MARKET_NAMES = {"IE": "Ireland", "GB": "GB", "US": "the US", "FR": "France", "DE": "Germany", "ES": "Spain",
                "IT": "Italy", "NL": "the Netherlands", "BE": "Belgium", "PT": "Portugal", "PL": "Poland",
                "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland", "AT": "Austria",
                "CH": "Switzerland", "CA": "Canada", "AU": "Australia", "NZ": "New Zealand"}


def market_list(codes) -> str:
    names = [MARKET_NAMES.get(c, c) for c in codes]
    if not names:
        return "no market"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def lens_of_key(key) -> str | None:
    return KEY_LENS.get(str(key or "").split(".")[0])


# ---------------------------------------------------------------------------
# The clan context
# ---------------------------------------------------------------------------

def ctx_facts(clan: dict) -> list:
    f = clan.get("facts")
    if isinstance(f, dict):
        f = f.get("facts")
    return [x for x in (f or []) if isinstance(x, dict)]


def ctx_findings(clan: dict) -> list:
    f = clan.get("findings")
    if isinstance(f, dict):
        f = f.get("findings")
    return [x for x in (f or []) if isinstance(x, dict)]


def ctx_decisions(clan: dict) -> list:
    d = clan.get("decision_chain")
    if isinstance(d, dict):
        d = d.get("decisions")
    return [x for x in (d or []) if isinstance(x, dict)]


def ctx_data(clan: dict) -> dict:
    d = clan.get("data")
    return d if isinstance(d, dict) else {}


def field_env(data: dict, name: str) -> dict | None:
    env = (data.get("campaign") or {}).get(name)
    return env if isinstance(env, dict) else None


def field_value(data: dict, name: str):
    env = field_env(data, name)
    return env.get("value") if env else None


def known_ids(clan: dict) -> set:
    """Decision ids the document holds, with those a contest withheld (§4)."""
    ids = set()
    for d in ctx_decisions(clan):
        ids.add(d.get("id"))
        for h in d.get("withheld") or []:
            ids.add(h.get("id") if isinstance(h, dict) else h)
    return ids


def human_owned(data: dict, decisions: list, doc: str, fname: str) -> str | None:
    """Why a job must not write this field (Contract 3 §2.2), or None."""
    env = field_env(data, fname)
    if env and env.get("origin") in ("confirmed", "stated"):
        return f"origin {env['origin']}: the field belongs to a human"
    addr = f"{doc}#campaign.{fname}"
    for v in decisions:
        if v.get("kind") == "verdict" and v.get("polarity") == "bad" and addr in (v.get("targets") or []):
            answered = any(d.get("kind") == "edit" and addr in (d.get("targets") or [])
                           and str(d.get("timestamp", "")) > str(v.get("timestamp", "")) for d in decisions)
            if not answered:
                return "an unanswered bad verdict stands on it"
    return None


# ---------------------------------------------------------------------------
# Patches, read-sets, addresses
# ---------------------------------------------------------------------------

def field_paths(patch: dict) -> list:
    """The fields a data_patch writes, at the granularity the host judges them:
    campaign.<f>, selection.<k>, materials.<id>, intake.messages.<id>, report."""
    out = []
    for top, sub in patch.items():
        if top == "intake" and isinstance(sub, dict) and isinstance(sub.get("messages"), dict):
            out += [f"intake.messages.{k}" for k in sub["messages"]]
        elif top in ("campaign", "selection", "materials") and isinstance(sub, dict) and sub:
            out += [f"{top}.{k}" for k in sub]
        else:
            out.append(top)
    return out


def address(path: str) -> str:
    """Patch path -> address path: map keys go in brackets (Contract 3 §2.4)."""
    for m in ("intake.messages.", "materials."):
        if path.startswith(m):
            return f"{m[:-1]}[{path[len(m):]}]"
    return path


def get_dotted(data, dotted):
    for k in dotted.split("."):
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return copy.deepcopy(data)


def read_of(data: dict, patch: dict) -> dict:
    return {p: get_dotted(data, p) for p in field_paths(patch)}


def sub_patch(patch: dict, path: str) -> dict:
    node = patch
    for p in path.split("."):
        node = node[p]
    for p in reversed(path.split(".")):
        node = {p: node}
    return node


def deep_merge(a: dict, b: dict) -> dict:
    """Two merge patches (b after a) as one."""
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and v:
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_patch(target, patch):
    """RFC 7396 merge patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = apply_patch(out.get(k), v)
    return out


def decision(doc, did, kind, handler, action, rationale, targets, cites=(), timestamp=None, **extra) -> dict:
    d = {
        "id": did, "kind": kind, "agent": handler, "action": action,
        "rationale": rationale, "targets": [f"{doc}#{t}" for t in targets],
        "cites": list(dict.fromkeys(cites)), "handler": handler, "backend": BACKEND,
        "timestamp": timestamp or iso(),
    }
    d.update(extra)
    return d


def empty_change(doc, base) -> dict:
    return {"doc": doc, "base_version": base, "read": {}, "data_patch": {},
            "facts_append": [], "findings_append": [], "decisions": []}


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

class Material:
    """One piece of supplied material with its paragraphs, so spans can be
    located (¶n) in the ORIGINAL text."""

    def __init__(self, mid, text, name, kind, sha, known):
        self.id, self.text, self.name, self.kind, self.sha, self.known = mid, text, name, kind, sha, known
        self.paragraphs = [(i + 1, m.start(), m.end())
                           for i, m in enumerate(re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", text, re.S))]

    def locator(self, start: int) -> str:
        para = next((p for p, s, e in self.paragraphs if s <= start < e), 1)
        return f"¶{para}"


def build_materials(inp: dict, data: dict):
    """(materials with text, ids of attachments with no text). The prompt is a
    material (kind prompt, sha256 of its UTF-8 bytes)."""
    known = data.get("materials") if isinstance(data.get("materials"), dict) else {}
    by_sha = {norm_sha(v.get("sha256", "")): k for k, v in known.items() if isinstance(v, dict)}
    mats, unread = [], []
    prompt = inp.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise bad("input.prompt must be a string")
    if prompt and prompt.strip():
        sha = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
        mid = by_sha.get(sha) or ("mat_p" + sha[7:17])
        mats.append(Material(mid, prompt, "prompt", "prompt", sha, mid in known))
    atts = inp.get("attachments", [])
    if not isinstance(atts, list):
        raise bad("input.attachments must be a list")
    for a in atts:
        if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a.get("sha256"):
            raise bad("each attachment needs a name and a sha256")
        sha = norm_sha(a["sha256"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", sha):
            raise bad("an attachment has a malformed sha256")
        mid = a.get("material_id") if a.get("material_id") in known else (by_sha.get(sha) or ("mat_a" + sha[7:17]))
        text = a.get("text")
        if not isinstance(text, str) or not text.strip():
            unread.append(mid)
            continue
        mats.append(Material(mid, text, a["name"], "other", sha, mid in known))
    return mats, unread


def canon(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)
