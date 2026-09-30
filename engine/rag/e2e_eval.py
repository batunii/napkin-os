#!/usr/bin/env python3
"""
e2e_eval.py — the whole brief pipeline on real client briefs: cost, tokens, calls, quality.

    python3 e2e_eval.py --n 3                  # RAG_PATH=mix and loops, 3 briefs each
    python3 e2e_eval.py --n 3 --paths mix

For each brief and path: parse_brief.run() end to end (Loops 1-2 capture, Loops 3-7
retrieval + synthesis, golden brief fill), measuring
  * model calls and tokens PER MODEL (hooked on parse_brief's own stats recorder), priced;
  * RAG calls: hosted embedding requests, Qdrant requests, validator calls;
  * wall time;
  * quality: golden_critic health score and definition-of-done, the brief's BetterBriefs
    scorecard, and a blind Sonnet comparison of the finished briefs across paths.
Outputs go to engine/outputs/e2e/ (git-ignored: the briefs are real client material).

Prices are USD per million tokens (input, output), for the Claude API models; NVIDIA,
Groq and Cerebras links run on free/trial tiers here and are priced at 0 — note that the
NVIDIA terms exclude production use, so those links are not a production cost model.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ENGINE))

OUT = ENGINE / "outputs" / "e2e"
BRIEFS = ENGINE.parent / "client_briefs"


def brief_files(folder: Path = BRIEFS) -> dict:
    """{stem: file} for the client briefs, one file per stem. When a stem has several files
    the client's own document wins over a converted .md copy (omv-btl-brief has both; the
    .md is a vault conversion with front matter), so every tool reads the same text: before
    2026-09-29 this module took whichever came last and checkpoint_run whichever came first."""
    rank = lambda f: (f.suffix.lower() == ".md", f.name)
    out: dict = {}
    for f in sorted((f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")), key=rank):
        out.setdefault(f.stem, f)
    return out


def _pick() -> list[str]:
    """The client briefs to run, in order, from golden/labels/client/pick.txt (git-ignored:
    the file names are client names). Empty when the file is absent."""
    f = HERE / "golden" / "labels" / "client" / "pick.txt"
    return [ln.strip() for ln in f.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")] if f.exists() else []


# Order matters: matching is by substring, so "claude-opus-5-5" must precede "claude-opus-5".
PRICES = {"claude-opus-4-6": (5, 25), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25), "claude-sonnet-5": (2, 10),
          "claude-haiku-4-5": (1, 5), "claude-fable-5-1": (10, 50)}
JUDGE_MODEL = "claude-sonnet-5"


CACHE_READ_FACTOR, CACHE_WRITE_FACTOR = 0.1, 1.25   # Claude API: cache reads 0.1x, writes 1.25x input


def _price(label: str) -> tuple[float, float]:
    """(input, output) USD per million tokens for a provider:model label; 0 if not Claude."""
    if ":" not in label:                        # the Claude call records a bare "anthropic"
        import parse_brief
        label = f"{label}:{parse_brief.model_for(label)}"
    model = label.split(":", 1)[-1].lower()
    for k, v in PRICES.items():
        if k in model:
            return v
    return (0.0, 0.0)


def _usd(label: str, tin: int, tout: int, cache_read: int = 0, cache_creation: int = 0) -> float:
    """List-price USD for one call's tokens: uncached input and output at the model's rates,
    cache reads at 0.1x input and cache writes at 1.25x input. Before 2026-09-25 cache
    tokens were folded into the input and priced in full, overstating CLI runs by ~35-40%
    (audit BW13). CLI and API costs are still not comparable: the CLI adds ~350 input
    tokens of its own per call (~507 before 2026-09-29, when it ran from ~; ADR 0005)."""
    pi, po = _price(label)
    return round((tin + cache_read * CACHE_READ_FACTOR + cache_creation * CACHE_WRITE_FACTOR) / 1e6 * pi
                 + tout / 1e6 * po, 5)


class Meter:
    """Per-model tokens and RAG call counts for one run, hooked into the pipeline."""

    def __init__(self, pb, rag, q):
        """Wrap parse_brief's stats recorder, the hosted embed call and Qdrant requests."""
        self.by_model: dict[str, dict] = {}
        self.embed = self.qdrant = 0
        self._tl = threading.local()
        self._lock = threading.Lock()
        orig_call, orig_usage, orig_embed, orig_req = pb._stats_call, pb._stats_usage, rag._nim_embed, q._req
        meter = self

        def stats_call(label, in_chars):
            """Remember which model this thread is calling."""
            meter._tl.label = label
            with meter._lock:
                meter.by_model.setdefault(label, {"calls": 0, "in": 0, "out": 0})["calls"] += 1
            return orig_call(label, in_chars)

        def stats_usage(usage, out_chars):
            """Attribute the call's token usage (input, output, cache read, cache write) to
            the model this thread called."""
            label = getattr(meter._tl, "label", "unknown")
            with meter._lock:
                m = meter.by_model.setdefault(label, {"calls": 0, "in": 0, "out": 0})
                m["in"] += int((usage or {}).get("prompt_tokens") or 0)
                m["out"] += int((usage or {}).get("completion_tokens") or 0)
                m["cache_read"] = m.get("cache_read", 0) + int((usage or {}).get("cache_read_tokens") or 0)
                m["cache_creation"] = m.get("cache_creation", 0) + int((usage or {}).get("cache_creation_tokens") or 0)
            return orig_usage(usage, out_chars)

        def nim_embed(*a, **k):
            """Count hosted embedding requests."""
            with meter._lock:
                meter.embed += 1
            return orig_embed(*a, **k)

        def req(*a, **k):
            """Count Qdrant requests."""
            with meter._lock:
                meter.qdrant += 1
            return orig_req(*a, **k)
        pb._stats_call, pb._stats_usage, rag._nim_embed, q._req = stats_call, stats_usage, nim_embed, req
        self._restore = lambda: setattr(pb, "_stats_call", orig_call) or setattr(pb, "_stats_usage", orig_usage) \
            or setattr(rag, "_nim_embed", orig_embed) or setattr(q, "_req", orig_req)

    def close(self) -> dict:
        """Unhook and return the tally with cost."""
        self._restore()
        cost = sum(_usd(k, v["in"], v["out"], v.get("cache_read", 0), v.get("cache_creation", 0))
                   for k, v in self.by_model.items())
        return {"by_model": self.by_model, "tokens_in": sum(v["in"] for v in self.by_model.values()),
                "tokens_out": sum(v["out"] for v in self.by_model.values()),
                "llm_calls": sum(v["calls"] for v in self.by_model.values()),
                "cost_usd": round(cost, 4), "embed_requests": self.embed, "qdrant_requests": self.qdrant}


def trace_one(stem: str, path: str = "mix") -> dict:
    """Every call of one full brief, timed: LLM calls by pipeline step (caller function),
    model, seconds and tokens; RAG calls (hosted embed, Qdrant, validator) with seconds.
    Writes outputs/e2e/trace_<path>_<stem>.json. The per-step view the call counts alone
    cannot give."""
    import inspect
    import rag
    import store_qdrant as q
    import parse_brief as pb
    import judge
    prev_path = os.environ.get("RAG_PATH")
    os.environ["RAG_PATH"] = path
    events, lock, tl = [], threading.Lock(), threading.local()
    t0 = time.time()
    orig_json, orig_usage, orig_call = pb._json_call, pb._stats_usage, pb._stats_call
    orig_embed, orig_req, orig_judge = rag._nim_embed, q._req, judge.Chain.judge
    # Every jev request with its billed input tokens (2026-09-29): the relevance checks
    # (JevBackend.score) and the in-brief checks (JevBackend.ask: figures, scorecard,
    # category, synthesis support, conflicts, the capture fallback), for jev cost per brief.
    import judge_jev
    orig_jscore, orig_jask = judge_jev.JevBackend.score, judge_jev.JevBackend.ask

    def jev_score(self, query, passages, *, deadline_s):
        """Time one relevance request and record the tokens jev billed (None if it failed)."""
        t = time.time(); ok = False
        try:
            res = orig_jscore(self, query, passages, deadline_s=deadline_s); ok = True
            return res
        finally:
            with lock:
                events.append({"kind": "jev", "what": "relevance", "start": round(t - t0, 2),
                               "secs": round(time.time() - t, 2),
                               "tokens": (getattr(self, "last_call", None) or {}).get("input_tokens") if ok else None})

    def jev_ask(self, state, questions, *, deadline_s=None):
        """Time one in-brief jev request and record its billed tokens (None if it failed)."""
        t = time.time(); res = None
        try:
            res = orig_jask(self, state, questions, deadline_s=deadline_s)
            return res
        finally:
            with lock:
                events.append({"kind": "jev", "what": "check", "start": round(t - t0, 2),
                               "secs": round(time.time() - t, 2),
                               "tokens": getattr(getattr(res, "usage", None), "input_tokens", None)})
    skip = {"traced_json", "_json_call", "wrapper", "<lambda>"}

    def step_name():
        """The pipeline function that made this call (first frame outside the plumbing)."""
        for fr in inspect.stack()[2:12]:
            if fr.function not in skip and fr.filename.endswith(("parse_brief.py", "golden_critic.py")):
                return fr.function
        return "?"

    def stats_call(label, in_chars):
        """Remember the model this thread is calling."""
        tl.label = label
        return orig_call(label, in_chars)

    def stats_usage(usage, out_chars):
        """Accumulate this thread's tokens (input, output, cache read/write) for the call in flight."""
        u = usage or {}
        tl.tin = getattr(tl, "tin", 0) + int(u.get("prompt_tokens") or 0)
        tl.tout = getattr(tl, "tout", 0) + int(u.get("completion_tokens") or 0)
        tl.cr = getattr(tl, "cr", 0) + int(u.get("cache_read_tokens") or 0)
        tl.cc = getattr(tl, "cc", 0) + int(u.get("cache_creation_tokens") or 0)
        return orig_usage(usage, out_chars)

    def traced_json(*a, **k):
        """Time one LLM call and attribute its tokens and the model that ANSWERED (the
        `info` link), not merely the last link tried."""
        tl.tin = tl.tout = tl.cr = tl.cc = 0; tl.label = "?"
        st = step_name(); t = time.time()
        info = k.setdefault("info", {}) if isinstance(k.get("info", {}), dict) else {}
        try:
            return orig_json(*a, **k)
        finally:
            with lock:
                events.append({"kind": "llm", "step": st, "model": getattr(tl, "label", "?"),
                               "answered_by": info.get("link"), "route": k.get("route"),
                               "start": round(t - t0, 2), "secs": round(time.time() - t, 2),
                               "in": tl.tin, "out": tl.tout, "cache_read": tl.cr, "cache_creation": tl.cc})

    def timed(kind, fn, describe=None):
        """Wrap a RAG call to log its duration (and, via `describe(result)`, what answered)."""
        def wrapper(*a, **k):
            """Call `fn` and append a timed event, even when it raises."""
            t = time.time(); res = None
            try:
                res = fn(*a, **k)
                return res
            finally:
                ev = {"kind": kind, "start": round(t - t0, 2), "secs": round(time.time() - t, 2)}
                if describe is not None:
                    try:
                        ev.update(describe(res) or {})
                    except Exception:
                        pass
                with lock:
                    events.append(ev)
        return wrapper

    def validator_info(res):
        """Which backend judged, and whether the chain fell back (audit C10)."""
        return {"backend": getattr(res, "backend_used", None), "fell_back": getattr(res, "fell_back", None)}
    pb._json_call, pb._stats_usage, pb._stats_call = traced_json, stats_usage, stats_call
    rag._nim_embed, q._req = timed("embed", orig_embed), timed("qdrant", orig_req)
    judge.Chain.judge = lambda self, *a, **k: timed("validator", orig_judge, validator_info)(self, *a, **k)
    judge_jev.JevBackend.score, judge_jev.JevBackend.ask = jev_score, jev_ask
    try:
        text = pb.ingest(brief_files()[stem])[0].strip()   # the production reader (audit D5)
        brief = pb.run(None, loops37=True, golden=True, raw_text=text, source_name=stem)
        t_brief = round(time.time() - t0, 1)
        # Score it in the same process so the independent judge call is traced too.
        import golden_critic as gc
        schema = json.loads(gc.SCHEMA_PATH.read_text())
        gb = gc.from_brief_object(brief)
        v = gc.validate(schema, gb)
        unjudged = v["health"]
        v, judged_n = gc.run_critic_sampled(schema, gb, v)     # CRITIC_MODEL x CRITIC_SAMPLES (default Sonnet x1)
        qsplit = gc.quality_split(schema, gb, v)
        score = {"health": v["health"] if judged_n else None, "health_unjudged": unjudged,
                 "critic_samples": v.get("critic_samples"),
                 "judged_checks": judged_n, "judge_model": v.get("judge_model"),
                 "quality": qsplit["quality"], "client_gaps": qsplit["client_gaps"],
                 "failed_checks": [f"{fr['id']}.{c['id']}" for fr in v["fields"] for c in fr["checks"]
                                   if c["status"] == "fail"],
                 "signoff_fails": [d["id"] for d in v["definition_of_done"] if d["status"] == "fail"],
                 "coverage_pct": brief["loop1_capture"]["no_loss_ledger"]["coverage_pct"],
                 "capture_format": brief["meta"].get("capture_format")}
        d = OUT / f"trace_{path}_{stem}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief_object.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False), encoding="utf-8")
        (d / "client_brief.md").write_text(pb.render_client_brief(brief), encoding="utf-8")
    finally:
        pb._json_call, pb._stats_usage, pb._stats_call = orig_json, orig_usage, orig_call
        rag._nim_embed, q._req, judge.Chain.judge = orig_embed, orig_req, orig_judge
        judge_jev.JevBackend.score, judge_jev.JevBackend.ask = orig_jscore, orig_jask
        if prev_path is None:
            os.environ.pop("RAG_PATH", None)
        else:
            os.environ["RAG_PATH"] = prev_path
    for e in events:
        if e["kind"] == "llm":
            e["usd"] = _usd(e.get("answered_by") or e["model"], e["in"], e["out"],
                            e.get("cache_read", 0), e.get("cache_creation", 0))
    out = {"brief": stem, "path": path, "brief_secs": t_brief, "wall_secs": round(time.time() - t0, 1),
           "claude_transport": pb.transport_used(),
           "extraction_mode": brief["meta"].get("extraction_mode"),
           "fallback_links": brief["meta"].get("fallback_links"),
           "retrieval_fallback": ((brief.get("loops3_7") or {}).get("fallback") or {}).get("reason"),
           "validation_degraded": (brief.get("loops3_7") or {}).get("validation_degraded"),
           "score": score,
           "events": sorted(events, key=lambda e: e["start"])}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"trace_{path}_{stem}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def _quality(brief_json: Path, brief: dict) -> dict:
    """golden_critic health and failures, plus the BetterBriefs scorecard tally."""
    import subprocess
    proc = subprocess.run([sys.executable, str(ENGINE / "golden_critic.py"), str(brief_json), "--judge"],
                          capture_output=True, text=True)
    out = proc.stdout
    hs = re.findall(r"health:\s*(\d+)/100", out)       # first = unjudged, last = judged
    jm = re.search(r"judged (\d+) checks", out)
    if proc.returncode != 0 or not jm or int(jm.group(1)) == 0:
        hs = hs[:1] + [None]                           # the judge did not run: no judged score
    qm = re.search(r"quality:\s*(\d+)/100 · client gaps: (.*?) ·", out)
    dims = (brief.get("betterbriefs_scorecard") or {}).get("dimensions") or []
    verdicts = [d.get("verdict") for d in dims]
    gf = (brief.get("loop2_golden") or {}).get("fields") or {}
    return {"health": int(hs[-1]) if hs and hs[-1] is not None else None,
            "health_unjudged": int(hs[0]) if hs and hs[0] is not None else None,
            "quality": int(qm.group(1)) if qm else None, "client_gaps": qm.group(2) if qm else None, "checks_pass": out.count(":PASS"),
            "checks_fail": out.count(":FAIL"),
            "betterbriefs": {v: verdicts.count(v) for v in ("pass", "vague", "missing")},
            "golden_fields_filled": sum(1 for v in gf.values() if isinstance(v, dict) and v.get("source") != "missing"),
            "golden_fields": len(gf)}


def _judge(pb, name: str, briefs: dict[str, str], brief_text: str = "") -> dict:
    """Blind head-to-head of the finished briefs of two paths (pairwise.judge_pair: both
    orders, PAIRWISE_SAMPLES rounds, a round the orders disagree on is a tie; audit BW8,
    D5, D6). `better` is the winning path or "same". A path whose brief is empty (its run
    failed) is not judged: {} with the reason (audit D5)."""
    import pairwise
    paths = sorted(briefs)
    if len(paths) < 2:
        return {}
    empty = [p for p in paths if not (briefs[p] or "").strip()]
    if empty:
        return {"skipped": f"empty brief from {', '.join(empty)} (the run failed)"}
    a, b = paths[:2]
    r = pairwise.judge_pair(brief_text, briefs[a], briefs[b], key=name, pb=pb)
    if r is None:                     # judged on the pinned model or not at all (audit D4)
        return {"scores": None, "better": None, "why": "", "judge_model": None}
    better = {"A": a, "B": b}.get(r["verdict"], "same")
    return {"scores": {a: r["scores"]["A"], b: r["scores"]["B"]}, "better": better,
            "wins": {a: r["wins"]["A"], b: r["wins"]["B"], "tie": r["wins"]["tie"]},
            "consistency": r["consistency"], "why": " | ".join(x["why"][0] for x in r["rounds"])[:600],
            "judge_model": r["judge_model"]}


def main() -> None:
    """Run the briefs through each path, measure, judge, and write the summary."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--paths", default="mix,loops")
    ap.add_argument("--pairwise", action="store_true",
                    help="also judge the paths' finished briefs head to head (pairwise.py: both orders, "
                         "3 rounds, about 6 extra judge calls per brief). Off by default")
    ap.add_argument("--trace", default=None, help="trace every call of ONE brief (a client_briefs stem)")
    ap.add_argument("--transport", choices=("api", "cli", "auto"), default=None,
                    help="how Claude links run: api (API key), cli (Claude Code login via `claude -p`), "
                         "auto (API, switching to the CLI on a credit/auth failure). "
                         "Default: BRIEF_CLAUDE_TRANSPORT, else api")
    a = ap.parse_args()
    if a.transport:
        os.environ["BRIEF_CLAUDE_TRANSPORT"] = a.transport
    if a.trace:
        t = trace_one(a.trace, a.paths.split(",")[0])
        print(json.dumps({"wall_secs": t["wall_secs"], "events": len(t["events"])}))
        return
    import rag
    import store_qdrant as q
    import parse_brief as pb
    files = brief_files()
    chosen = [s for s in _pick() if s in files][:a.n]
    rows, finished = [], {}
    for stem in chosen:
        text = pb.ingest(files[stem])[0].strip()   # the production reader (audit D5)
        finished[stem] = {}
        for path in a.paths.split(","):
            os.environ["RAG_PATH"] = path
            meter = Meter(pb, rag, q)
            t = time.time()
            try:
                brief = pb.run(None, loops37=True, golden=True, raw_text=text, source_name=stem)
                err = None
            except Exception as e:
                brief, err = {}, f"{type(e).__name__}: {e}"
            secs = round(time.time() - t, 1)
            tally = meter.close()
            d = OUT / path / stem
            d.mkdir(parents=True, exist_ok=True)
            (d / "brief_object.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")
            md = pb.render_client_brief(brief) if brief else ""
            (d / "client_brief.md").write_text(md, encoding="utf-8")
            finished[stem][path] = md
            l37 = brief.get("loops3_7") or {}
            row = {"brief": stem, "path": path, "seconds": secs, "error": err, **tally,
                   "claude_transport": pb.transport_used(),
                   "validator_calls": ((l37.get("retrieval_trace") or {}).get("calls") or {}).get("validator"),
                   "rag_path_used": l37.get("rag_path"),
                   "retrieval_fallback": (l37.get("fallback") or {}).get("reason"),
                   "validation_degraded": l37.get("validation_degraded"),
                   "extraction_mode": (brief.get("meta") or {}).get("extraction_mode"),
                   "fallback_links": (brief.get("meta") or {}).get("fallback_links"),
                   **(_quality(d / "brief_object.json", brief) if brief else {})}
            rows.append(row)
            print(f"  {stem} [{path}] {secs}s ${tally['cost_usd']} calls={tally['llm_calls']} "
                  f"tok={tally['tokens_in']}/{tally['tokens_out']} health={row.get('health')} err={err}", file=sys.stderr)
        if a.pairwise:                                 # opt-in (Sai, 2026-09-28)
            rows.append({"brief": stem, "judge": _judge(pb, stem, finished[stem], text)})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    runs = [r for r in rows if "path" in r]
    agg = {}
    for p in a.paths.split(","):
        rs = [r for r in runs if r["path"] == p and not r["error"]]
        if rs:
            n = len(rs)
            agg[p] = {"briefs": n, "avg_cost_usd": round(sum(r["cost_usd"] for r in rs) / n, 4),
                      "avg_tokens_in": round(sum(r["tokens_in"] for r in rs) / n),
                      "avg_tokens_out": round(sum(r["tokens_out"] for r in rs) / n),
                      "avg_llm_calls": round(sum(r["llm_calls"] for r in rs) / n, 1),
                      "avg_seconds": round(sum(r["seconds"] for r in rs) / n, 1),
                      # unjudged runs are left out, never counted as 0 (audit D5)
                      "avg_health": (round(sum(r["health"] for r in rs if r.get("health") is not None)
                                           / max(1, sum(1 for r in rs if r.get("health") is not None)), 1)
                                     if any(r.get("health") is not None for r in rs) else None),
                      "avg_embed_requests": round(sum(r["embed_requests"] for r in rs) / n, 1),
                      "avg_qdrant_requests": round(sum(r["qdrant_requests"] for r in rs) / n, 1)}
    judged = [r["judge"] for r in rows if "judge" in r and r["judge"]]
    agg["judge_better_counts"] = {p: sum(1 for j in judged if j.get("better") == p) for p in a.paths.split(",") + ["same"]}
    print(json.dumps({"aggregate": agg, "runs": runs, "judge": [r for r in rows if "judge" in r]}, indent=1))


if __name__ == "__main__":
    main()
