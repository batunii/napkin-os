#!/usr/bin/env python3
"""
checkpoint_run.py — the paired before/after measurement Sai asked for on 2026-09-26: the
same three briefs through the OLD code (a git worktree at the baseline commit) and the
NEW code (this tree), one after another, same transport, same judge, and a summary.

    python3 checkpoint_run.py --before <worktree dir> --briefs mamaliga-engleza,employer-awareness-campaign-brief,friskies-engleza
    python3 checkpoint_run.py --before ... --arms before,after,after_novalidator

Each arm runs engine/rag/e2e_eval.py --trace <stem> --transport cli in its own tree with
RAG_STORE=local RAG_INDEX=_index_v4; the trace (calls, cost, seconds, health judged once
by Sonnet 5, per-check fails) is moved to outputs/e2e/checkpoint_<label>/<arm>/. The
before arm runs with RAG_VALIDATOR=none (the old code discarded validation anyway); the
after arm runs with the tree's default (jev on); `after_novalidator` isolates jev;
`after_hero55` writes the insight and SMP on Opus 5.5.

Brief lists (phase A item 5, Sai 2026-09-28): `--set test-three` runs a named list from
golden/labels/client/eval_sets.json (git-ignored, since brief names are client material);
`--briefs a,b` still works, and without either the three test briefs run.

Every report states the noise it can see (audit BW9): how far one brief's health moves
between two runs of code whose behaviour did not change, measured from the registry's
repeat pairs (eval_checkpoints.json `repeat_of`), and marks each difference from the
first arm as real or noise against twice that spread. It also carries each brief's
grounding count (grounding.py: reasons to believe jev finds not in the client's document),
because health alone rewards a filled field even when its facts are invented.

Before any brief runs, the runner checks that this process can reach Claude for the critic
(the arms get the CLI transport; so does the critic now), so a missing login stops the run
at once instead of after the briefs. A brief whose grading got no answer is graded once
more after CRITIC_RETRY_WAIT_S and is never stamped as graded; an ungraded brief reads
"not scored", never 0.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
E2E = ENGINE / "outputs" / "e2e"
BRIEFS_DIR = ENGINE.parent / "client_briefs"
SETS_FILE = HERE / "golden" / "labels" / "client" / "eval_sets.json"
REGISTRY = HERE / "eval_checkpoints.json"
DEFAULT_BRIEFS = "mamaliga-engleza,employer-awareness-campaign-brief,friskies-engleza"
CRITIC_RETRY_WAIT_S = 30


def load_sets(path: Path = SETS_FILE) -> dict:
    """{name: [stem, ...]} from the brief-list file; {} when it does not exist."""
    if not path.exists():
        return {}
    return {k: list(v) for k, v in (json.loads(path.read_text()).get("sets") or {}).items()}


def resolve_briefs(set_name: "str | None", briefs: "str | None", sets: "dict | None" = None) -> list:
    """The stems to run: the named set, else the comma list, else the three test briefs.
    An unknown set name is an error listing the known ones."""
    if set_name:
        sets = load_sets() if sets is None else sets
        if set_name not in sets:
            raise SystemExit(f"unknown brief set {set_name!r}; known: {', '.join(sorted(sets)) or 'none'} ({SETS_FILE})")
        return list(sets[set_name])
    return [b.strip() for b in (briefs or DEFAULT_BRIEFS).split(",") if b.strip()]


def noise_estimate(registry: Path = REGISTRY, e2e: Path = E2E) -> "dict | None":
    """Run-to-run noise of one brief's health, from the registry's repeat pairs: checkpoints
    marked `repeat_of` another whose behaviour did not change (9b06017 left the evidence
    identical; 2d9370c was a refactor). {"pairs": n, "sd": spread of a two-run difference,
    "brief": the per-brief line (2 x sd), "diffs": [...], "from": [ids]}; None without pairs."""
    reg = json.loads(registry.read_text())
    by_id = {c["id"]: c for c in reg["checkpoints"]}

    def health(c):
        f = e2e / c["dir"] / "rows.json"
        rows = (json.loads(f.read_text()).get(c["arm"]) or {}) if f.exists() else {}
        return {k: r.get("health") for k, r in rows.items() if r.get("health") is not None}

    diffs, used = [], []
    for c in reg["checkpoints"]:
        base = by_id.get(c.get("repeat_of") or "")
        if not base:
            continue
        a, b = health(base), health(c)
        d = [b[k] - a[k] for k in a if k in b]
        if d:
            diffs += d
            used.append(f"{base['id']}->{c['id']}")
    if not diffs:
        return None
    sd = (sum(x * x for x in diffs) / len(diffs)) ** 0.5
    return {"pairs": len(diffs), "sd": round(sd, 1), "brief": round(2 * sd), "diffs": diffs, "from": used}


_texts: dict = {}


def brief_text(stem: str) -> str:
    """The client brief's text as the pipeline reads it (cached per process); '' if absent."""
    if stem not in _texts:
        sys.path.insert(0, str(ENGINE))
        import parse_brief as pb
        src = next((f for f in BRIEFS_DIR.iterdir() if f.stem == stem), None) if BRIEFS_DIR.exists() else None
        _texts[stem] = pb.ingest(src)[0] if src else ""
    return _texts[stem]


def ground_row(out_dir: Path, stem: str, row: dict) -> dict:
    """Add the grounding count (grounding.check) of the arm's saved client_brief.md, once."""
    md = out_dir / f"trace_mix_{stem}" / "client_brief.md"
    if not md.exists() or row.get("grounding") is not None:
        return row
    import grounding
    bo = out_dir / f"trace_mix_{stem}" / "brief_object.json"
    used = (((json.loads(bo.read_text()).get("meta") or {}).get("research_facts") or {}).get("used") or []) if bo.exists() else []
    research = [u["line"] for u in used if isinstance(u, dict) and u.get("line")]
    g = grounding.check(brief_text(stem), md.read_text(), research=research)
    return {**row, "grounding": g if g is not None else {"error": "jev did not answer"}}


def preflight(critic: str) -> None:
    """Stop before any brief runs when the critic cannot reach Claude. The critic runs in this
    process on the same transport as the arms (CLI unless BRIEF_CLAUDE_TRANSPORT says otherwise)."""
    os.environ.setdefault("BRIEF_CLAUDE_TRANSPORT", "cli")
    if critic == "trace":
        return
    sys.path.insert(0, str(ENGINE))
    import parse_brief as pb
    pb._require_claude("anthropic")


# Which stage each routed job belongs to (brief_llm.ROUTES). Traces written before the route
# was recorded fall back to step names and call order (_stage_of).
ROUTE_STAGE = {"extract": "reading", "mechanical": "reading", "synth": "strategy notes",
               "hero": "insight + SMP", "hero_judge": "insight + SMP",
               "grounded_writer": "proof points", "judge": "proof points"}
READING_STEPS = {"capture_toon", "extract_golden_brief", "how_to_win_toon", "score_betterbriefs"}


def _stage_of(llm_events: list) -> list:
    """The stage of each model call, in order. By route when the trace has it; otherwise by
    step: the reading calls, the loop syntheses ('one'), and for the fill the first two
    '_one' calls are the insight and SMP writers and the later ones the RTB and desired
    response, so a judge or sharpen before the first proof writer belongs to insight + SMP."""
    ones = [e["start"] for e in llm_events if e.get("step") == "_one"]
    proof_from = ones[2] if len(ones) > 2 else float("inf")
    out = []
    for e in llm_events:
        st = e.get("step")
        if e.get("route") in ROUTE_STAGE and st not in READING_STEPS:
            out.append(ROUTE_STAGE[e["route"]])
        elif st in READING_STEPS:
            out.append("reading")
        elif st == "one":
            out.append("strategy notes")
        elif st in ("_one", "_judge_and_gate", "_refine_field", "_smp_territory"):
            out.append("proof points" if e.get("start", 0) >= proof_from else "insight + SMP")
        else:
            out.append("other")
    return out


def stage_breakdown(events: list, brief_secs: "float | None" = None) -> dict:
    """{stage: {"span_s", "calls", "usd"}} for one brief's trace: the stage's first start to
    last end (stages overlap, so spans do not add up to the brief time), its model calls and
    their list-price cost; "retrieval" counts the embedding and jev checks. The grader's call
    (_pinned_judge, after the brief) is left out."""
    llm = sorted([e for e in events if e.get("kind") == "llm" and e.get("step") != "_pinned_judge"
                  and (brief_secs is None or e.get("start", 0) < brief_secs)], key=lambda e: e.get("start", 0))
    rows = list(zip(_stage_of(llm), llm)) + [("retrieval", e) for e in events if e.get("kind") in ("embed", "validator")]
    out = {}
    for stage, e in rows:
        d = out.setdefault(stage, {"t0": e.get("start", 0), "t1": 0.0, "calls": 0, "usd": 0.0})
        d["t0"] = min(d["t0"], e.get("start", 0)); d["t1"] = max(d["t1"], e.get("start", 0) + e.get("secs", 0))
        d["calls"] += 1; d["usd"] += e.get("usd") or 0
    return {k: {"span_s": round(v["t1"] - v["t0"], 1), "calls": v["calls"], "usd": round(v["usd"], 3)}
            for k, v in out.items()}


JEV_USD_PER_M = 0.04        # jev list price per million input tokens (jev_checks docstring)


def trace_numbers(trace: dict) -> dict:
    """The per-brief cost and speed numbers from a trace (2026-09-29): the Claude calls' tokens
    and list-price cost, the jev requests (every relevance check and in-brief check) with their
    billed tokens and cost, embedding calls, a total, and the stage breakdown. The grader's
    call after the brief is left out. Traces from before the tracer recorded jev requests
    (kind "jev") have no jev tokens: jev_usd and total_usd are then None, jev_calls counts the
    relevance checks only."""
    ev = trace.get("events") or []
    end = trace.get("brief_secs")
    llm = [e for e in ev if e.get("kind") == "llm" and e.get("step") != "_pinned_judge"
           and (end is None or e.get("start", 0) < end)]
    jev = [e for e in ev if e.get("kind") == "jev"]
    tok = {"in": sum(e.get("in") or 0 for e in llm), "out": sum(e.get("out") or 0 for e in llm),
           "cache_read": sum(e.get("cache_read") or 0 for e in llm),
           "cache_write": sum(e.get("cache_creation") or 0 for e in llm)}
    # All input the models read: uncached + cache written + cache read. On the Claude Code login
    # most input arrives as cache writes, so "in" alone understated it (120k vs 36k, 2026-09-29).
    tok["in_total"] = tok["in"] + tok["cache_write"] + tok["cache_read"]
    claude_usd = round(sum(e.get("usd") or 0 for e in llm), 4)
    jev_tokens = sum(e.get("tokens") or 0 for e in jev) if jev else None
    jev_usd = round(jev_tokens / 1e6 * JEV_USD_PER_M, 5) if jev_tokens is not None else None
    return {"jev_calls": len(jev) if jev else sum(1 for e in ev if e.get("kind") == "validator"),
            "embed_calls": sum(1 for e in ev if e.get("kind") == "embed"),
            "tokens": tok, "jev_tokens": jev_tokens,
            "claude_usd": claude_usd, "jev_usd": jev_usd,
            "total_usd": round(claude_usd + jev_usd, 4) if jev_usd is not None else None,
            "stages": stage_breakdown(ev, end)}


def add_trace_numbers(out_dir: Path, stem: str, row: dict) -> dict:
    """Backfill trace_numbers on a row (a reused arm, an older checkpoint) from its saved trace."""
    f = out_dir / f"trace_mix_{stem}.json"
    if ("tokens" in row and "in_total" in (row.get("tokens") or {})) or not f.exists():
        return row
    return {**row, **trace_numbers(json.loads(f.read_text()))}


def run_one(tree: Path, stem: str, env_extra: dict, out_dir: Path) -> dict:
    """One brief through one tree's e2e_eval --trace; returns the trace's headline numbers."""
    env = {**os.environ, "RAG_STORE": "local", "RAG_INDEX": "_index_v4",
           "BRIEF_CLAUDE_TRANSPORT": "cli",
           # every judge call's inputs, for the paired judge test (judge_format_ab.py)
           "BRIEF_JUDGE_DUMP": str((out_dir / f"judge_calls_{stem}.jsonl").resolve()), **env_extra}
    (out_dir / f"judge_calls_{stem}.jsonl").unlink(missing_ok=True)
    rag = tree / "engine" / "rag"
    log = out_dir / f"{stem}.log"
    t0 = time.time()
    with open(log, "w") as f:
        proc = subprocess.run([sys.executable, "e2e_eval.py", "--trace", stem, "--transport", "cli"],
                              cwd=rag, env=env, stdout=f, stderr=subprocess.STDOUT)
    src = tree / "engine" / "outputs" / "e2e" / f"trace_mix_{stem}.json"
    src_dir = tree / "engine" / "outputs" / "e2e" / f"trace_mix_{stem}"
    row = {"stem": stem, "exit": proc.returncode, "wall": round(time.time() - t0, 1)}
    if src.exists():
        shutil.move(str(src), out_dir / f"trace_mix_{stem}.json")
        if src_dir.exists():
            shutil.rmtree(out_dir / f"trace_mix_{stem}", ignore_errors=True)
            shutil.move(str(src_dir), out_dir / f"trace_mix_{stem}")
        t = json.loads((out_dir / f"trace_mix_{stem}.json").read_text())
        llm = [e for e in t["events"] if e["kind"] == "llm"]
        s = t.get("score") or {}
        row.update({"brief_secs": t.get("brief_secs"), "calls": len(llm),
                    "usd": round(sum(e.get("usd", 0) for e in llm), 3),
                    "health": s.get("health"), "quality": s.get("quality"),
                    "judged": s.get("judged_checks"), "judge_model": s.get("judge_model"),
                    "failed_checks": s.get("failed_checks"), "signoff_fails": s.get("signoff_fails"),
                    "fallback_links": t.get("fallback_links"), "retrieval_fallback": t.get("retrieval_fallback"),
                    "validation_degraded": t.get("validation_degraded"), **trace_numbers(t)})
    else:
        row["error"] = "no trace written (see log)"
    return row


DEFAULT_CRITIC = "claude-fable-5-1x3"      # Sai, 2026-09-28: every checkpoint from now on


def score_row(out_dir: Path, stem: str, row: dict, critic: str) -> dict:
    """Score one arm's saved brief with THIS tree's critic (golden_critic.run_critic_sampled: the
    model, N samples, majority per check), whatever tree wrote it, so every arm of a
    checkpoint is graded by the same critic (an old worktree's own critic is Sonnet x1).
    The trace's own score is kept as health_trace / quality_trace. No-op without a brief."""
    bo = out_dir / f"trace_mix_{stem}" / "brief_object.json"
    if not bo.exists() or (row.get("critic") == critic and row.get("health") is not None):
        return row
    sys.path.insert(0, str(ENGINE))
    import golden_critic as gc
    model, _, n = critic.partition("x")
    schema = json.loads(gc.SCHEMA_PATH.read_text())
    gb = gc.from_brief_object(json.loads(bo.read_text()))
    import parse_brief as _pb
    t_grade = time.time()
    with _pb._stats_scope() as led:          # the grading's own calls, apart from the brief's
        v, judged = gc.run_critic_sampled(schema, gb, gc.validate(schema, gb), model=model, samples=int(n or 1))
    if not judged:                     # no sample answered (a rate limit): once more after a pause
        print(f"[critic] {stem}: no answer, retrying in {CRITIC_RETRY_WAIT_S} s", file=sys.stderr, flush=True)
        time.sleep(CRITIC_RETRY_WAIT_S)
        with _pb._stats_scope() as led:
            v, judged = gc.run_critic_sampled(schema, gb, gc.validate(schema, gb), model=model, samples=int(n or 1))
    q = gc.quality_split(schema, gb, v)
    import e2e_eval
    grading = {"secs": round(time.time() - t_grade, 1), "calls": led.get("calls", 0),
               "usd": e2e_eval._usd(f"anthropic:{model}", led.get("prompt_tokens", 0), led.get("completion_tokens", 0),
                                   led.get("cache_read_tokens", 0), led.get("cache_creation_tokens", 0))}
    row = {**row, "health_trace": row.get("health_trace", row.get("health")),
           "quality_trace": row.get("quality_trace", row.get("quality")),
           "critic": critic if judged else None, "grading": grading, "critic_samples": v.get("critic_samples"), "judged": judged,
           "judge_model": v.get("judge_model"),
           "health": v["health"] if judged else None, "quality": q["quality"] if judged else None,
           "failed_checks": [f"{fr['id']}.{c['id']}" for fr in v["fields"] for c in fr["checks"] if c["status"] == "fail"],
           "signoff_fails": [d["id"] for d in v["definition_of_done"] if d["status"] == "fail"]}
    print(f"[critic] {stem}: health {row['health']} (trace {row['health_trace']}), spread "
          f"{(row.get('critic_samples') or {}).get('spread')}", file=sys.stderr, flush=True)
    return row


def pairwise_arms(out: Path, briefs: list, a_arm: str, b_arm: str) -> dict:
    """Head-to-head per brief: arm `a_arm`'s finished client brief against `b_arm`'s, judged
    blind in both orders over PAIRWISE_SAMPLES rounds (pairwise.judge_pair). Reads each arm's
    saved client_brief.md, so it works on reused arms too. {stem: result or {"error"}}."""
    sys.path.insert(0, str(ENGINE))
    import pairwise
    import parse_brief as pb
    res = {}
    for stem in briefs:
        fa = out / a_arm / f"trace_mix_{stem}" / "client_brief.md"
        fb = out / b_arm / f"trace_mix_{stem}" / "client_brief.md"
        if not (fa.exists() and fb.exists()):
            res[stem] = {"error": f"missing client_brief.md for {a_arm if not fa.exists() else b_arm}"}
            continue
        src = next((f for f in (ENGINE.parent / "client_briefs").iterdir() if f.stem == stem), None)
        text = pb.ingest(src)[0] if src else ""
        r = pairwise.judge_pair(text, fa.read_text(), fb.read_text(), key=stem, pb=pb)
        res[stem] = r if r else {"error": "the judge gave no usable round"}
        print(f"[pairwise] {stem}: {res[stem].get('verdict', res[stem].get('error'))}", file=sys.stderr, flush=True)
    return res


def render_pairwise(pw: dict, a_arm: str, b_arm: str) -> str:
    """Markdown for the head-to-head: verdict, round wins, order consistency, mean scores."""
    L = [f"## Head to head: {a_arm} (A) against {b_arm} (B)", "",
         "Blind, both orders per round, a round the orders disagree on is a tie.", "",
         "| brief | verdict | A wins | B wins | ties | orders agreed | A score | B score |", "|---|---|---|---|---|---|---|---|"]
    for stem, r in pw.items():
        if "error" in r:
            L.append(f"| {stem} | not judged: {r['error']} | | | | | | |")
            continue
        L.append(f"| {stem} | {r['verdict']} | {r['wins']['A']} | {r['wins']['B']} | {r['wins']['tie']} | "
                 f"{int(r['consistency'] * 100)}% | {r['scores']['A']} | {r['scores']['B']} |")
    return "\n".join(L) + "\n"


def _score(v) -> str:
    """A health or quality cell: the number, or 'not scored' (never 0)."""
    return "not scored" if v is None else str(v)


def _grounding_cell(g) -> str:
    """'invented of claims (+n to confirm)', or why it is missing."""
    if not g:
        return ""
    if "error" in g:
        return g["error"]
    extra = [f"+{g['to_confirm']} to confirm"] if g.get("to_confirm") else []
    extra += [f"{g['from_research']} from research"] if g.get("from_research") else []
    return f"{g['invented']} of {g['of']}" + (f" ({', '.join(extra)})" if extra else "")


def _delta(h, base, line) -> str:
    """Difference from the first arm, marked real or noise against the per-brief line."""
    if h is None or base is None:
        return ""
    d = h - base
    if line is None:
        return f"{d:+d}"
    return f"{d:+d} ({'real' if abs(d) >= line else 'noise'})"


def render(label: str, arms: dict, rows: dict, noise: "dict | None" = None) -> str:
    """Markdown: the noise line, one table per brief across arms (health, difference from the
    first arm, grader spread, grounding), then totals over the briefs every arm scored."""
    critics = sorted({str(r.get("critic") or "not scored") for rs in rows.values() for r in rs.values()})
    line = noise["brief"] if noise else None
    base_arm = next(iter(arms))
    L = [f"# Checkpoint run {label}", "", f"Scored by: {', '.join(critics)}.", ""]
    if noise:
        L += [f"**Noise:** one brief's health moves by about ±{noise['brief']} between two runs of unchanged code "
              f"(2 × {noise['sd']}, from {noise['pairs']} repeat runs: {', '.join(noise['from'])}; scored by the old "
              f"one-sample critic, so an upper bound). A difference from {base_arm} under {noise['brief']} is marked "
              "noise. Read the per-check lists and the grounding count beside it.", ""]
    else:
        L += ["**Noise:** no repeat runs in the registry, so no line can be drawn; read the per-check lists.", ""]
    for stem in rows[base_arm]:
        base = rows[base_arm].get(stem, {}).get("health")
        L += [f"## {stem}", f"| arm | health | Δ vs {base_arm} | grader spread | invented proof points (RTB) | quality | judged | "
              "brief s | calls | input tokens (incl. cache) / output | Claude $ | jev $ | total $ | failed checks | sign-off fails |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for arm in arms:
            r = rows[arm].get(stem, {})
            if "error" in r:
                L.append(f"| {arm} | ERROR | | | | | | | | | {r['error']} | |")
                continue
            spread = (r.get("critic_samples") or {}).get("spread")
            tk = r.get("tokens") or {}
            tokens = f"{tk.get('in_total', tk.get('in', 0)):,} / {tk.get('out', 0):,}" if tk else ""
            money = lambda v, d=3: "" if v is None else f"{v:.{d}f}"
            L.append(f"| {arm} | {_score(r.get('health'))} | {'' if arm == base_arm else _delta(r.get('health'), base, line)} | "
                     f"{'' if spread is None else spread} | {_grounding_cell(r.get('grounding'))} | {_score(r.get('quality'))} | "
                     f"{r.get('judged')} | {r.get('brief_secs')} | {r.get('calls')} | "
                     f"{tokens} | "
                     f"{money(r.get('claude_usd', r.get('usd')))} | {money(r.get('jev_usd'), 4)} | {money(r.get('total_usd'))} | "
                     f"{', '.join(r.get('failed_checks') or [])} | {', '.join(r.get('signoff_fails') or [])} |")
        L.append("")
    stems = [s for s in rows[base_arm] if all(rows[a].get(s, {}).get("health") is not None for a in arms)]
    sum_line = f" A difference in the health sum under {round(noise['brief'] * len(stems) ** 0.5)} is noise." if noise and stems else ""
    L += ["## Totals", f"Over the {len(stems)} of {len(rows[base_arm])} briefs every arm scored.{sum_line}", "",
          "| arm | health sum | quality sum | invented proof points | brief s sum | input tokens (incl. cache) / output | Claude $ | jev $ | total $ |",
          "|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        ok = [rows[arm][s] for s in stems]
        inv = [(r.get("grounding") or {}).get("invented") for r in rows[arm].values()]
        L.append(f"| {arm} | {sum(r['health'] for r in ok) if ok else 'not scored'} | "
                 f"{sum(r.get('quality') or 0 for r in ok) if ok else 'not scored'} | "
                 f"{sum(i for i in inv if i is not None) if any(i is not None for i in inv) else ''} | "
                 f"{round(sum(r.get('brief_secs') or 0 for r in ok), 1)} | "
                 f"{sum((r.get('tokens') or {}).get('in_total', (r.get('tokens') or {}).get('in', 0)) for r in ok):,} / {sum((r.get('tokens') or {}).get('out', 0) for r in ok):,} | "
                 f"{round(sum(r.get('claude_usd', r.get('usd')) or 0 for r in ok), 3)} | "
                 f"{'' if any(r.get('jev_usd') is None for r in ok) else round(sum(r['jev_usd'] for r in ok), 4)} | "
                 f"{'' if any(r.get('total_usd') is None for r in ok) else round(sum(r['total_usd'] for r in ok), 3)} |")
    return "\n".join(L) + "\n"


def main() -> None:
    """Run every brief through every arm, sequentially, and write the report."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--before", required=True, help="worktree of the baseline commit")
    ap.add_argument("--after", default=str(ENGINE.parent), help="the tree under test (default: this one)")
    ap.add_argument("--briefs", default=None, help="comma-separated brief stems (default: the three test briefs)")
    ap.add_argument("--set", default=None, dest="brief_set",
                    help=f"a named brief list from {SETS_FILE.relative_to(ENGINE)} (overrides --briefs)")
    ap.add_argument("--arms", default="before,after")
    ap.add_argument("--label", default=None)
    ap.add_argument("--critic", default=DEFAULT_CRITIC, metavar="MODEL[xN]",
                    help=f"critic that scores every arm's saved brief, in this tree (default {DEFAULT_CRITIC}: "
                         "Fable 5.1, 3 samples, majority per check); reused arms are re-scored when "
                         "their critic differs; 'trace' keeps each tree's own score")
    ap.add_argument("--pairwise", default=None, metavar="A,B",
                    help="after the runs, judge arm A's brief against arm B's per brief, blind, both "
                         "orders (pairwise.py), e.g. --pairwise after,before")
    ap.add_argument("--reuse", default=None,
                    help="an earlier checkpoint dir whose arms (those not named in --arms) are copied "
                         "instead of re-run, e.g. the 'before' arm, which does not change between checkpoints")
    a = ap.parse_args()
    briefs = resolve_briefs(a.brief_set, a.briefs)
    preflight(a.critic)
    label = a.label or dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    out = ENGINE / "outputs" / "e2e" / f"checkpoint_{label}"
    all_arms = {"before": (Path(a.before), {"RAG_VALIDATOR": "none"}),
                "after": (Path(a.after), {}),
                "after_novalidator": (Path(a.after), {"RAG_VALIDATOR": "none"}),
                # insight/SMP drafts and sharpening on Opus 5.5 (their judges then fall to
                # Sonnet 5, since a judge never runs on its writer's model; ADR 0011)
                "after_hero55": (Path(a.after), {"BRIEF_ROUTE_HERO": "claude-opus-5-5,claude-opus-4-6"}),
                # evidence strongest at both ends of each field's list (RAG_MIX_ORDER=edge, A/B)
                "after_edge": (Path(a.after), {"RAG_MIX_ORDER": "edge"}),
                # the hero sharpen pass off (phase C change 1); the judges then still
                # answered one object per test
                "after_nosharpen": (Path(a.after), {"BRIEF_SHARPEN": "0", "BRIEF_JUDGE_FORMAT": "full"}),
                # compact judge answers: pass lists by test number, short reasons (phase C change 2)
                "after_compactjudge": (Path(a.after), {"BRIEF_SHARPEN": "0", "BRIEF_JUDGE_FORMAT": "compact"})}
    arms = {k: all_arms[k] for k in a.arms.split(",") if k in all_arms}
    rows: dict = {arm: {} for arm in arms}
    if a.reuse:
        prev = json.loads((Path(a.reuse) / "rows.json").read_text())
        reused = [arm for arm, prev_rows in prev.items() if arm not in arms and prev_rows and arm in all_arms]
        arms = {**{arm: all_arms[arm] for arm in reused}, **arms}   # reused arms first, in their recorded order
        for arm in reused:
            rows[arm] = prev[arm]
            out.mkdir(parents=True, exist_ok=True)
            if (Path(a.reuse) / arm).exists() and not (out / arm).exists():
                shutil.copytree(Path(a.reuse) / arm, out / arm)
            print(f"[{arm}] reused from {a.reuse}", file=sys.stderr, flush=True)
            if a.critic != "trace":
                # scored again with this run's critic, in THIS checkpoint's copy only: the
                # reused checkpoint's own records are never changed (Sai: leave the past as is)
                rows[arm] = {stem: score_row(out / arm, stem, r, a.critic) for stem, r in rows[arm].items()}
            rows[arm] = {stem: ground_row(out / arm, stem, r) for stem, r in rows[arm].items()}
            rows[arm] = {stem: add_trace_numbers(out / arm, stem, r) for stem, r in rows[arm].items()}
    for arm, (tree, env_extra) in arms.items():
        if rows.get(arm):
            continue                                       # reused
        (out / arm).mkdir(parents=True, exist_ok=True)
        for stem in briefs:
            print(f"[{arm}] {stem} …", file=sys.stderr, flush=True)
            rows[arm][stem] = run_one(tree, stem, env_extra, out / arm)
            if a.critic != "trace":
                rows[arm][stem] = score_row(out / arm, stem, rows[arm][stem], a.critic)
            rows[arm][stem] = ground_row(out / arm, stem, rows[arm][stem])
            print(f"[{arm}] {stem}: {rows[arm][stem]}", file=sys.stderr, flush=True)
            (out / "rows.json").write_text(json.dumps(rows, indent=1))
    out.mkdir(parents=True, exist_ok=True)
    for arm in rows:                                                  # tokens and costs on every row, reused or new
        rows[arm] = {stem: add_trace_numbers(out / arm, stem, r) for stem, r in rows[arm].items()}
    (out / "rows.json").write_text(json.dumps(rows, indent=1))      # also when every arm was reused
    report = render(label, arms, rows, noise_estimate())
    if a.pairwise:
        pa, pb_arm = [x.strip() for x in a.pairwise.split(",")][:2]
        pw = pairwise_arms(out, briefs, pa, pb_arm)
        (out / "pairwise.json").write_text(json.dumps(pw, indent=1))
        report += "\n" + render_pairwise(pw, pa, pb_arm)
    (out / "report.md").write_text(report)
    print((out / "report.md").read_text())
    print(f"-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
