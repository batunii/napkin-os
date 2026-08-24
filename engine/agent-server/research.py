"""Research agent — grounds a Brief Maker draft before the engine runs.

Two tracks, one dossier:
  corpus  — precedent retrieval across the award corpora (ipa · cannes · effie ·
            dandad) + planner playbooks, via the engine's own rag/retrieve.py —
            the same retrieval every Brief Maker instance shares.
  web     — live context on the client/market/competitors, behind RESEARCH_WEB:
            "off" (default) or "claude" (reference impl: headless `claude -p`
            with WebSearch — dev-mode; slot a search API in here for production).

gather() returns (dossier_text, summary_markdown) or (None, None).

The dossier NEVER enters the engine input: Loop-1 capture must remain a
faithful no-loss record of the client's own brief (the one hard rule — and the
ledger measures it, so injected text craters coverage). Strategy grounding
comes from the engine's own Loops 3–7 retrieval over the same corpora; the
dossier's job is the planner-facing context panel (and, later, regen guidance).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
sys.path.insert(0, str(ENGINE_ROOT))

import parse_brief  # noqa: E402

DOSSIER_HEADER = ("===== RESEARCH DOSSIER "
                  "(supporting context — not part of the client brief) =====")

_MAX_CORPUS_FINDINGS = 8
_SNIPPET_CHARS = 420


def _gist(text: str) -> dict | None:
    obj = parse_brief._json_call(
        "Summarise this client brief for a researcher. JSON only: "
        '{"client": "...", "market": "...", "problem": "...", "objective": "...", '
        '"audience": "...", "competitors": "..."}\n\nBRIEF:\n' + text[:6000],
        system="You brief researchers. Terse, factual, JSON only.",
        max_tokens=300,
        accept=lambda o: isinstance(o, dict) and o.get("problem"),
    )
    return obj if isinstance(obj, dict) else None


def _digest_track() -> list[dict]:
    """No vector store? Ground the context panel on pack digests instead —
    paraphrased pattern notes distilled offline (scripts/distil_pack.py) and
    shipped as plain files. Static per pack, but honest grounding beats none."""
    findings = []
    for root in (ENGINE_ROOT / "packs_dist",):
        if not root.is_dir():
            continue
        for digest in sorted(root.glob("*/digest.md")):
            pack = digest.parent.name
            text = digest.read_text(errors="replace").strip()
            if text:
                findings.append({"citation": f"{pack} digest",
                                 "text": text[:_SNIPPET_CHARS * 3], "score": 0.0})
    return findings[:_MAX_CORPUS_FINDINGS]


def _corpus_track(gist: dict) -> list[dict]:
    try:
        sys.path.insert(0, str(ENGINE_ROOT / "rag"))
        from retrieve import index_available, retrieve  # noqa: PLC0415
    except Exception:
        traceback.print_exc()
        return _digest_track()
    if not index_available():
        return _digest_track()
    queries = [q for q in (
        f"{gist.get('problem', '')} — campaigns that solved this",
        f"reaching {gist.get('audience', '')} in {gist.get('market', '')}",
        f"category strategy vs {gist.get('competitors', '')}",
    ) if len(q.strip(" —")) > 20]
    findings, seen = [], set()
    for q in queries:
        try:
            hits = retrieve(q, k=4)
        except Exception:
            traceback.print_exc()
            continue
        for h in hits:
            if h["citation"] in seen:
                continue
            seen.add(h["citation"])
            findings.append(h)
    findings.sort(key=lambda h: -h["score"])
    return findings[:_MAX_CORPUS_FINDINGS]


def _web_track(gist: dict) -> list[dict]:
    mode = os.environ.get("RESEARCH_WEB", "off").strip().lower()
    if mode != "claude":
        return []
    prompt = (
        "Research the current market context for an advertising brief. "
        f"Client: {gist.get('client', '?')}. Market: {gist.get('market', '?')}. "
        f"Problem: {gist.get('problem', '?')}. Competitors: {gist.get('competitors', '?')}.\n"
        "Use web search. Return 3-6 findings as a JSON array ONLY: "
        '[{"claim": "...", "url": "...", "date": "..."}]. '
        "Only findings you can source; no speculation."
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--allowedTools", "WebSearch", "--max-turns", "6"],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            print(f"[i] web track: claude exited {proc.returncode}", file=sys.stderr)
            return []
        result = json.loads(proc.stdout).get("result", "")
        start, end = result.find("["), result.rfind("]")
        items = json.loads(result[start:end + 1]) if start >= 0 <= end else []
        return [i for i in items if isinstance(i, dict) and i.get("claim") and i.get("url")][:6]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[i] web track unavailable: {e}", file=sys.stderr)
        return []


def gather(text: str, clan_data: dict | None = None) -> tuple[str | None, str | None]:
    gist = _gist(text)
    if not gist:  # keyless mode — no research possible
        return None, None
    corpus = _corpus_track(gist)
    web = _web_track(gist)
    if not corpus and not web:
        return None, None

    dossier = [DOSSIER_HEADER]
    summary = []
    if corpus:
        dossier.append("Precedent from the effectiveness/creative corpora:")
        for h in corpus:
            dossier.append(f"- [{h['citation']}] {h['text'][:_SNIPPET_CHARS].strip()}")
        summary.append("Precedent: " + "; ".join(h["citation"] for h in corpus[:4]))
    if web:
        dossier.append("Live market context (web):")
        for w in web:
            dossier.append(f"- {w['claim']} ({w['url']})")
        summary.append("Web: " + "; ".join(f"{w['claim'][:80]} ({w['url']})" for w in web[:3]))

    print(f"[✓] research: {len(corpus)} corpus + {len(web)} web findings")
    return "\n".join(dossier), "\n".join(f"- {s}" for s in summary)
