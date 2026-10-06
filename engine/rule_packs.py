"""
rule_packs.py: the house rule packs a brief's "Safety and legal" section quotes (P3, ADR 0022, 2026-10-03).

A rule pack is one YAML file in engine/rule_packs/: the advertising rules of one market (ie, gb, ro), one
category (alcohol, gambling, food and drink high in fat, salt or sugar, charity fundraising) or one topic
(suicide and self-harm). Every rule carries its primary source: title, publisher, url, section and a
verbatim quote, so a person can check it in one click. These are not the knowledge packs in packs.py
(retrieval corpora); they are short lists of rules, read whole.

  markets_in(text, facts)   the ISO countries a brief is about, most named first
  load()                    every pack on disk, parsed
  match(markets, category, text, reviewed_only)
                            the packs that apply, each with only the rules for those markets
  line(rule)                one rule as a writer sees it: [R:id] (markets; media) rule (source, section)

A pack starts `reviewed: false`. Until a person has checked every source and set it true, a run uses it only
when BRIEF_RULE_PACKS_UNREVIEWED=1 (tests and trials), and the brief says the rules are unreviewed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

DIR = Path(__file__).resolve().parent / "rule_packs"

# Country names and adjectives a brief uses -> ISO 3166-1 alpha-2. gap_filler.COUNTRIES plus the short forms.
COUNTRY_WORDS = {"ireland": "IE", "irish": "IE", "republic of ireland": "IE",
                 "united kingdom": "GB", "uk": "GB", "great britain": "GB", "britain": "GB", "british": "GB",
                 "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
                 "romania": "RO", "romanian": "RO", "moldova": "MD", "germany": "DE", "german": "DE",
                 "france": "FR", "french": "FR", "spain": "ES", "italy": "IT", "netherlands": "NL",
                 "poland": "PL", "united states": "US", "usa": "US", "american": "US", "austria": "AT",
                 "hungary": "HU", "bulgaria": "BG"}


def markets_in(text: str, facts=None) -> list:
    """The countries the brief is about, most named first: the research facts' `market` codes count once
    each, then every country word in the text. 'Northern Ireland' counts for GB, not IE."""
    counts: dict = {}
    for f in facts or []:
        m = str((f or {}).get("market") or "").upper()
        if re.fullmatch(r"[A-Z]{2}", m):
            counts[m] = counts.get(m, 0) + 1
    low = str(text or "").lower()
    ni = low.count("northern ireland")
    if ni:
        counts["GB"] = counts.get("GB", 0) + ni
        low = low.replace("northern ireland", " ")
    for word, code in COUNTRY_WORDS.items():
        n = len(re.findall(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low))
        if n:
            counts[code] = counts.get(code, 0) + n
    return [c for c, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n > 0]


def load(directory: "Path | None" = None) -> list:
    """Every *.yaml pack in the directory, parsed; a file that does not parse is skipped with a note."""
    out = []
    for p in sorted((directory or DIR).glob("*.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            print(f"[!] rule pack {p.name} skipped: {e}")
            continue
        if isinstance(d, dict) and d.get("id") and isinstance(d.get("rules"), list):
            out.append(d)
    return out


def _unreviewed_ok() -> bool:
    return os.environ.get("BRIEF_RULE_PACKS_UNREVIEWED", "0").lower() in ("1", "true", "yes")


def match(markets: list, category: str, text: str, reviewed_only: "bool | None" = None, packs=None) -> list:
    """The packs that apply to a brief: a market pack for any of its markets, a category pack for its
    category, a topic pack whose pattern is in its text. Each comes back with only the rules for the brief's
    markets (or ALL). Unreviewed packs are left out unless BRIEF_RULE_PACKS_UNREVIEWED=1."""
    if reviewed_only is None:
        reviewed_only = not _unreviewed_ok()
    mk = {str(m).upper() for m in markets or []}
    low = str(text or "").lower()
    out = []
    for p in packs if packs is not None else load():
        if reviewed_only and not p.get("reviewed"):
            continue
        when = p.get("applies_when") or {}
        hit = (bool(mk & {str(m).upper() for m in when.get("markets") or []})
               or (category and category in (when.get("categories") or []))
               or any(re.search(t, low) for t in when.get("topics") or []))
        if not hit:
            continue
        rules = [r for r in p["rules"] if isinstance(r, dict) and r.get("id") and r.get("rule")
                 and ({str(m).upper() for m in r.get("markets") or ["ALL"]} & (mk | {"ALL"}) or not mk)]
        if rules:
            out.append({**p, "rules": rules})
    return out


def line(rule: dict) -> str:
    """One rule as a writer sees it: '[R:id] (IE; broadcast) rule (source title, section)'."""
    src = rule.get("source") or {}
    where = "; ".join(x for x in (",".join(rule.get("markets") or ["ALL"]), rule.get("media")) if x)
    cite = ", ".join(x for x in (src.get("title"), src.get("section")) if x)
    return f"[R:{rule['id']}] ({where}) {str(rule['rule']).strip()}" + (f" ({cite})" if cite else "")
