"""
brief_render.py — the brief object rendered for people (split out of parse_brief.py, 2026-09-27).

  render_client_brief       the client-facing brief (markdown)
  render_markdown           the review file: every loop, the ledger, the gates' notes
  render_loops37            Loops 3-7 evidence and syntheses for the review file
  render_golden_provenance  where each golden field came from (review file and lineage)
  write_rich_formats        .docx / .pdf through pandoc
  _scrub_markers            internal markers (sentence refs, candidate numbers) removed
                            from client-facing and review text (audit H11)

Re-exported by parse_brief; new code should import from here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


# A parenthesised aside about a sentence number goes whole; a bare 'sentence 12' or
# 'sentences 2-3' loses only the reference itself; '[5]' goes.
_MARKER_RE = re.compile(r"\(\s*sentences?\s+\d+[^)\n]{0,80}\)|\bsentences?\s+\d+(?:\s*[-–,]\s*\d+)*|\[\d+\]", re.I)


def _scrub_markers(text):
    """Strip the pipeline's internal references — '(sentence 35 says TBC)', 'sentences 2-3',
    '[5]' — from text that reaches a client or a reviewer (audit H11). Lists and dicts are
    scrubbed per item; other values pass through unchanged."""
    if isinstance(text, str):
        out = _MARKER_RE.sub("", text)
        out = re.sub(r"\s+([?.,;:!])", r"\1", out)     # 'impact ?' -> 'impact?'
        return re.sub(r"\s{2,}", " ", out).strip()
    if isinstance(text, list):
        return [_scrub_markers(x) for x in text]
    if isinstance(text, dict):
        return {k: _scrub_markers(v) for k, v in text.items()}
    return text


# ---------------------------------------------------------------------------
# 7. RENDER (Brain markdown mirror)
# ---------------------------------------------------------------------------

FIELD_TITLES = {
    "background_context": "Background / context", "business_problem": "Business problem",
    "objective": "Objective", "target_audience": "Target audience",
    "key_message": "Key message (single-minded)", "proof_points": "Proof points",
    "evaluation_criteria": "Evaluation criteria", "strategic_angle": "Strategic angle",
    "anti_target": "Not for / not targeting",
    "deliverables": "Deliverables", "mandatories": "Mandatories (non-negotiable)",
    "budget": "Budget", "timeline": "Timeline & key dates",
    "success_metrics": "Success metrics / KPIs", "competitors_market": "Competitors & market",
    "tone_and_brand": "Tone & brand", "decision_makers": "Decision-makers",
    "constraints": "Constraints",
}
VERDICT_ICON = {"pass": "✅", "vague": "⚠️", "missing": "❌"}
STATUS_TAG = {"fact": "", "assumption": " _(assumption)_", "gap": " _(gap)_"}


def _fmt(c):
    """Render one Captured entry for review.md: its value followed by its STATUS_TAG
    (nothing for a fact or an unknown status), or '_not stated_' when the entry is empty or
    its value is None or ''."""
    if not c or c.get("value") in (None, ""):
        return "_not stated_"
    return f"{c['value']}{STATUS_TAG.get(c.get('status', 'fact'), '')}"


def render_loops37(L, brief):
    """Append the Loops 3–7 markdown section. No-op when the stage didn't run
    (flag off), so Loop-1/Loop-2 output stays byte-for-byte identical."""
    s = brief.get("loops3_7")
    if not s:
        return
    L.append("## Loops 3–7 · RAG-grounded strategy  ")
    if not s.get("enabled"):
        L.append(f"_skipped — {s.get('reason', '')}_\n")
        return
    # Provenance from the run itself, not a constant: which path retrieved and which tier
    # embedded (a hardcoded model name here misreported every brief and hid fallbacks).
    embed = (s.get("retrieval_trace") or {}).get("embed") or "hosted nemotron-3-embed-1b"
    L.append(f"_intent: {s['intent']} · retrieval: {s.get('rag_path') or 'loops'} · embed: {embed} "
             f"· synthesis: {s['synthesis_mode']}_\n")
    for d in s["loops"].values():
        L.append(f"### {d['title']}\n")
        if d.get("synthesis"):
            L.append(f"{d['synthesis']}\n")
        if d["evidence"]:
            L.append("_Grounded in:_")
            for e in d["evidence"]:
                cat = f" · {e['category']}" if e.get("category") else ""
                L.append(f"- **{e['framework']}** ({e['citation']}{cat}) — {e['snippet']}")
            L.append("")
        else:
            L.append("_No playbook evidence retrieved for this loop._\n")
    if s.get("sources_used"):
        L.append(f"_Sources cited: {len(s['sources_used'])} playbook sections._\n")


def render_client_brief(brief) -> str:
    """The DELIVERABLE — only the final brief. Assembles the Golden Brief (facts +
    generated strategy) into a clean one-pager: no loop labels, no provenance tags,
    no ledgers, no scorecard, no 'Grounded in' citations. All of that machinery lives
    in review.md. This is what a creative director actually reads."""
    m = brief["meta"]
    gf = (brief.get("loop2_golden") or {}).get("fields", {}) or {}
    l2 = brief.get("loop2_brief", {}) or {}
    title = m.get("project") or m.get("client") or "Client brief"

    def gv(fid):                      # golden value, else loop-2 fallback for the FACTS only
        """Return golden field `fid`'s value. When it is empty, only background,
        objectives and audience fall back to the Loop 2 slot (problem, objective, audience);
        every other field, the generated strategy fields included, returns its empty value so
        the section renders as to be agreed."""
        f = gf.get(fid)
        v = f.get("value") if isinstance(f, dict) else f
        if v:
            return v
        # Strategy fields (insight, smp, reasons_to_believe, desired_response) must NEVER
        # fall back to a loop-2 value: if generation didn't clear the rubric the field is a
        # real gap (and carries an open question). Falling back would re-show the masterbrand
        # line while also flagging "agree the SMP" — the contradiction. Facts may fall back.
        fb = l2.get({"background": "problem", "objectives": "objective",
                     "audience": "audience"}.get(fid, ""))
        return fb.get("value") if isinstance(fb, dict) else fb   # loop-2 fields are {value,status}

    L = [f"# {title} — Brief", ""]
    TBD = "_To be agreed — see open questions._"

    def text_section(heading, value):
        """Append a '## heading' section: the value as text (internal sentence markers
        scrubbed), or the to-be-agreed placeholder when it is empty, then a blank line."""
        L.append(f"## {heading}")
        L.append(_scrub_markers(str(value)) if value else TBD)
        L.append("")

    text_section("Background", gv("background"))

    obj = gv("objectives")
    L.append("## Objectives")
    if isinstance(obj, dict):
        for k, lab in (("commercial", "Commercial"), ("behavioural", "Behavioural"),
                       ("attitudinal", "Attitudinal")):
            if obj.get(k):
                L.append(f"- **{lab}:** {obj[k]}")
    elif obj:
        L.append(str(obj))
    else:
        L.append(TBD)
    L.append("")

    text_section("Audience", gv("audience"))
    text_section("Competitor context", gv("competitor_context"))
    text_section("The insight", gv("insight"))
    text_section("Single-minded proposition", gv("smp"))

    rtb = gv("reasons_to_believe")
    L.append("## Reasons to believe")
    if isinstance(rtb, list) and rtb:
        L += [f"- {_scrub_markers(r if isinstance(r, str) else (r.get('value') if isinstance(r, dict) else r))}"
              for r in rtb]
    elif rtb:
        L.append(str(rtb))
    else:
        L.append(TBD)
    L.append("")

    dr = gv("desired_response")
    L.append("## Desired response")
    if isinstance(dr, dict):
        for k, lab in (("think", "Think"), ("feel", "Feel"), ("do", "Do")):
            if dr.get(k):
                L.append(f"- **{lab}:** {dr[k]}")
    elif dr:
        L.append(str(dr))
    else:
        L.append(TBD)
    L.append("")

    text_section("Tone & world", gv("tone_world_assets"))
    text_section("Budget & scope", gv("budget_scope"))
    text_section("Mandatories", gv("mandatories"))

    oqs = l2.get("open_questions") or []
    if oqs:
        L.append("## Open questions to resolve before research")
        seen = set()
        for q in oqs:
            txt = _scrub_markers(q if isinstance(q, str) else (q.get("question") or q.get("value") or ""))
            key = re.sub(r"[^a-z0-9]+", " ", txt.lower()).strip()   # dedupe near-identical questions
            if not key or key in seen:
                continue
            seen.add(key)
            pr = "" if isinstance(q, str) else (f"**[{q.get('priority')}]** " if q.get("priority") else "")
            L.append(f"- {pr}{txt}")
        L.append("")
    return "\n".join(L).strip() + "\n"


def render_markdown(brief):
    """Render review.md, the team-facing record of a run (not the client deliverable):
    the Loop 1 capture (FIELD_TITLES order, then any extra fields the LLM returned), the
    win-rules, the Loop 1 self-review with the no-loss ledger and its unmapped segments, the
    BetterBriefs scorecard when present, the Loop 2 slots, open questions and self-review,
    then the Loops 3–7 narrative and the generated-field RAG provenance when those ran.
    Returns the markdown text."""
    m, l1, l2 = brief["meta"], brief["loop1_capture"], brief["loop2_brief"]
    led = l1["no_loss_ledger"]
    title = m.get("project") or m.get("client") or "Client brief"
    L = [f"# Brief — {title}",
         f"_briefing tool v{m['parser_version']} · {m['parsed_at']} · "
         f"mode: {m['extraction_mode']}_\n"]

    L.append("## Loop 1 · Faithful capture  \n_IPA: background + objectives · no RAG_\n")
    f = l1["fields"]
    # Canonical fields first (ordered), then any extra LLM fields — never drop content.
    extra = [k for k in f if k not in FIELD_TITLES and k not in ("client", "project")]
    for key, tit in list(FIELD_TITLES.items()) + [(k, k.replace("_", " ").title()) for k in extra]:
        if key not in f:
            continue
        v = f[key]
        if isinstance(v, list):
            if v:
                L.append(f"**{tit}**\n")
                L += [f"- {_fmt(it)}" for it in v]; L.append("")
        else:
            L.append(f"**{tit}** — {_fmt(v)}\n")

    L.append("### Win-rules (what the brief reveals)\n")
    htw = l1["how_to_win"]
    titles = {"stated_evaluation_criteria": "How they'll judge us",
              "unstated_needs": "Unstated needs", "likely_landmines": "Landmines",
              "winning_themes": "Recurring themes", "proof_required": "Proof expected"}
    if any(htw.get(k) for k in titles):
        for k, t in titles.items():
            if htw.get(k):
                L.append(f"**{t}**\n")
                for it in htw[k]:
                    if not isinstance(it, dict):
                        L.append(f"- {it}"); continue
                    # LLM uses value/source_quote; heuristic uses point/evidence.
                    point = it.get("point") or it.get("value") or it.get("text") or ""
                    src = it.get("evidence") or it.get("source_quote")
                    ev = f"  \n  ↳ _{src}_" if src else ""
                    L.append(f"- {point}{ev}")
                L.append("")
    else:
        L.append("_Run with an LLM key for the full win-rules read._\n")

    r1 = l1["review"]
    L.append(f"### Loop 1 self-review — {'✅ pass' if r1['passed'] else '⚠️ needs a pass'}\n")
    L += [f"- {x}" for x in r1["flags"]] or ["- clean"]
    L.append(f"\n**No-loss ledger:** {led['coverage_pct']}% "
             f"({led['mapped_segments']}/{led['total_segments']} mapped)\n")
    if led["unmapped"]:
        L.append("_Review queue (nothing dropped silently):_\n")
        L += [f"- {u['segment']}" for u in led["unmapped"]]; L.append("")

    sc = brief.get("betterbriefs_scorecard")
    if sc:
        L.append("### BetterBriefs scorecard — quality of the client brief  \n"
                 f"_rubric: reference/betterbriefs · judge: {sc.get('mode')}_\n")
        L.append("| Dimension | Verdict | Evidence / fix |")
        L.append("|---|---|---|")
        for d in sc["dimensions"]:
            note = d["evidence"] + (f" → _{d['fix']}_" if d.get("fix") else "")
            L.append(f"| {d['dimension'].replace('_', ' ')} "
                     f"| {VERDICT_ICON.get(d['verdict'], '')} {d['verdict']} "
                     f"| {note.replace('|', '/').replace(chr(10), ' ')} |")
        L.append("")
        sm = sc.get("single_mindedness") or {}
        if sm.get("verdict") == "multiple":
            L.append("**⚠️ Not single-minded — one brief = one strategy. Split into:**\n")
            L += [f"- {s}" for s in sm.get("split_into", [])]; L.append("")
        if sc.get("summary"):
            L.append(f"_{sc['summary']}_\n")

    L.append("## Loop 2 · First-round agency brief  \n_IPA: objective + role_\n")
    for k, t in (("problem", "Problem"), ("objective", "Objective"),
                 ("audience", "Audience"), ("key_message", "Key message"),
                 ("evaluation_criteria", "Evaluation criteria"),
                 ("not_doing", "Not doing"), ("scope", "Scope")):
        if k in l2:                                  # old brief_objects lack new slots
            L.append(f"**{t}** — {_fmt(l2[k])}\n")
    L.append("### Open questions (ask before research)\n")
    if l2["open_questions"]:
        for q in l2["open_questions"]:
            if isinstance(q, str):                # LLM returns bare strings
                L.append(f"- {q}"); continue
            pr = f"**[{q.get('priority','')}]** " if q.get("priority") else ""
            why = q.get("why_it_matters") or q.get("why") or ""
            text = q.get("question") or q.get("value") or ""
            L.append(f"- {pr}{text}" + (f"  \n  _why: {why}_" if why else ""))
    else:
        L.append("_None._")
    r2 = l2["review"]
    L.append(f"\n### Loop 2 self-review — {'✅ pass' if r2['passed'] else '⚠️ gaps'}\n")
    L += [f"- {x}" for x in r2["flags"]] or ["- clean"]
    render_loops37(L, brief)                          # no-op unless Loops 3–7 ran
    render_golden_provenance(L, brief)                # per-field RAG citations (review-only)
    return "\n".join(L)


def render_golden_provenance(L, brief):
    """Surface, in review.md only, which RAG sources grounded each generated strategy
    field — the `evidence_ids` we stamp in fill_derivable_fields. Kept OUT of the
    client deliverable by design (a footnoted 'grounded in <case>' reads as harmful);
    this is where the provenance lives for a planner to audit or defend a route."""
    gf = (brief.get("loop2_golden") or {}).get("fields", {}) or {}
    gen = [(fid, f) for fid, f in gf.items()
           if isinstance(f, dict) and f.get("source") == "inferred" and f.get("method", "").startswith("gen:")]
    miss = [(fid, f) for fid, f in gf.items()
            if isinstance(f, dict) and f.get("source") == "missing" and f.get("reason")]
    prov = (brief.get("loop2_golden") or {}).get("provenance") or {}
    if not gen and not miss and not prov:
        return
    L.append("\n## Generated strategy — RAG provenance  \n"
             "_Review only; never rendered in the client brief._\n")
    if prov:
        # Which lines are the client's, which are our reading, which we wrote (Sai,
        # 2026-09-26: shown here and carried in the brief object, not on the client page).
        L.append("**Provenance of every field**\n")
        for fid, p in prov.items():
            conf = f" (confidence {p['confidence']:.2f})" if isinstance(p.get("confidence"), (int, float)) else ""
            L.append(f"- {fid.replace('_', ' ')}: {p['mark']}{conf}")
        L.append("")
    for fid, f in gen:
        label = fid.replace("_", " ")
        conf = f.get("confidence")
        cites = ", ".join(f.get("evidence_ids") or []) or "(no IPA cases cited — playbook-grounded)"
        L.append(f"**{label}** — _{f.get('method')}_, confidence {conf}")
        L.append(f"  \n  ↳ grounded in: {cites}")
        if f.get("rationale"):
            L.append(f"  \n  ↳ rationale: _{f['rationale']}_")
        if f.get("judge_note"):
            L.append(f"  \n  ↳ tournament: _{f['judge_note']}_")
        if f.get("alternatives"):
            L.append(f"  \n  ↳ runner-up: {json.dumps(f['alternatives'])[:200]}")
        L.append("")
    for fid, f in miss:
        L.append(f"**{fid.replace('_', ' ')}** — _missing_: {f.get('reason')}")
        L.append("")


# ---------------------------------------------------------------------------
# RICH OUTPUT (docx / pdf via pandoc)
# ---------------------------------------------------------------------------

# pdflatex/xelatex have no colour-emoji glyphs; map the few we emit to ASCII
# so the PDF renders cleanly. (docx keeps the originals — Word has the fonts.)
_PDF_GLYPHS = {"✅": "[PASS]", "⚠️": "[!]", "⚠": "[!]", "❌": "[FAIL]", "✗": "[FAIL]",
               "🟢": "[+]", "🟡": "[~]", "🔴": "[-]", "↳": ">", "•": "-", "→": "->"}


def _find_xelatex() -> str | None:
    """Return the path of the xelatex binary: from PATH, else the MacTeX default
    /Library/TeX/texbin/xelatex when it exists, else None."""
    return shutil.which("xelatex") or next(
        (p for p in ("/Library/TeX/texbin/xelatex",) if Path(p).exists()), None)


def write_rich_formats(md_text: str, md_file: Path, formats: list[str]) -> list[str]:
    """Emit docx/pdf alongside the markdown one-pager, via pandoc. Returns the
    formats actually written. Degrades with a clear message, never raises."""
    want = [f.strip().lower() for f in formats if f.strip() and f.strip().lower() != "md"]
    if not want:
        return []
    if not shutil.which("pandoc"):
        print("  ⚠️ pandoc not found — skipping docx/pdf  (brew install pandoc)")
        return []
    done = []
    if "docx" in want:
        out = md_file.with_suffix(".docx")
        r = subprocess.run(["pandoc", str(md_file), "-o", str(out)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            done.append("docx")
        else:
            print(f"  ⚠️ docx failed: {r.stderr.strip()[:200]}")
    if "pdf" in want:
        xelatex = _find_xelatex()
        if not xelatex:
            print("  ⚠️ no xelatex engine — skipping pdf  (install BasicTeX/MacTeX)")
        else:
            clean = md_text
            for k, v in _PDF_GLYPHS.items():
                clean = clean.replace(k, v)
            # Stray backslashes (e.g. Windows paths like \ACME leaking from a brief)
            # are undefined LaTeX control sequences and abort the PDF. They're path
            # noise in a deliverable anyway — neutralise to forward slashes.
            clean = clean.replace("\\", "/")
            tmp = md_file.with_name(".brief_pdf_src.md")
            tmp.write_text(clean, encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = str(Path(xelatex).parent) + os.pathsep + env.get("PATH", "")
            out = md_file.with_suffix(".pdf")
            r = subprocess.run(
                ["pandoc", str(tmp), "-o", str(out),
                 "--pdf-engine=xelatex", "-V", "geometry:margin=2cm"],
                capture_output=True, text=True, env=env)
            tmp.unlink(missing_ok=True)
            if r.returncode == 0:
                done.append("pdf")
            else:
                print(f"  ⚠️ pdf failed: {r.stderr.strip()[:300]}")
    return done


# Names parse_brief re-exports and forwards assignments for (see parse_brief._ForwardingModule).
MOVED_NAMES = (
    'FIELD_TITLES',
    'STATUS_TAG',
    'VERDICT_ICON',
    '_MARKER_RE',
    '_PDF_GLYPHS',
    '_find_xelatex',
    '_fmt',
    '_scrub_markers',
    'render_client_brief',
    'render_golden_provenance',
    'render_loops37',
    'render_markdown',
    'write_rich_formats',
)
