"""The dogfood run log: everything a job did, in order, with what it cost.

One JSON-lines file per job, under NAPKIN_RUNLOG_DIR/<yyyy-mm-dd>/<task>-<job>.jsonl
(off when the setting is unset). Each line is one event:

  job_start   task, handler, doc, the stages it will run, the model configured
  stage_start / stage_end   a stage, its time, and what it spent (calls, tokens, cost, searches)
  model       one structured call: stage, purpose, model, attempts, seconds, tokens, cost, ok or the error
  research    one web search: stage, lens, market, query, sources found, seconds, cost (as reported)
  retrieval   one library lookup: stage, purpose, k, passages found, seconds
  decision    one decision the job wrote: agent, action, targets, what was decided, certainty, attention
  gap         a stage the job went on without, and why
  question / answer   what the job asked a person, and what came back
  note        anything else a stage wants on record (a plan, a research ask, a check's outcome)
  job_end     state, summary, every field's end state, and the totals per stage

Costs: a model call's cost is estimated from its tokens and the price table below
(NAPKIN_PRICES, a JSON object {model-prefix: [usd per M input, usd per M output]}, overrides
it); `cost_src` says "est". A web search's cost is the research service's own report when it
gives one ("reported"), else unknown (null).

What a call sent and got back is NOT written unless NAPKIN_RUNLOG_BODIES=1: client material is
confidential, so by default a call is logged by its sizes only. Turn bodies on for test prompts.

`python -m napkin.runlog [file | dir | --latest]` prints a run as tables.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import sys
import threading
import time
from pathlib import Path

# USD per million tokens (input, output). Estimates: check them against the current price list.
PRICES = {"claude-opus-4-1": (15.0, 75.0), "claude-opus-4-0": (15.0, 75.0), "claude-opus-4-2": (15.0, 75.0),
          "claude-opus": (5.0, 25.0), "claude-sonnet": (3.0, 15.0), "claude-haiku": (1.0, 5.0)}

_cfg = {"dir": None, "bodies": False}


def configure(directory: str | None, bodies: bool = False, prices: str | None = None):
    _cfg["dir"] = Path(directory) if directory else None
    _cfg["bodies"] = bool(bodies)
    if prices:
        try:
            PRICES.update({k: tuple(v) for k, v in json.loads(prices).items()})
        except (ValueError, TypeError):
            print("napkin.runlog: NAPKIN_PRICES is not a JSON object of [in, out]; ignored", file=sys.stderr)


def price(model: str | None, tin: int, tout: int) -> float | None:
    m = str(model or "")
    best = max((k for k in PRICES if m.startswith(k)), key=len, default=None)
    if best is None:
        return None
    pi, po = PRICES[best]
    return round(tin / 1e6 * pi + tout / 1e6 * po, 6)


def bodies() -> bool:
    return _cfg["bodies"]


class RunLog:
    """A job's log. Thread-safe; the stage is the job's (stages run one at a time), so a
    call made by a stage's worker threads is counted to that stage."""

    def __init__(self, path: Path | None, job: str, handler: str):
        self.path, self.job, self.handler = path, job, handler
        self.stage_name = None
        self.t0 = time.monotonic()
        self._lock = threading.Lock()
        self._fh = None
        self.totals: dict = {}
        self._stage_t0 = {}

    @property
    def on(self) -> bool:
        return self.path is not None

    def _write(self, ev: str, **kw):
        if self.path is None:
            return
        rec = {"t": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
               "s": round(time.monotonic() - self.t0, 3), "ev": ev, "job": self.job}
        rec.update({k: v for k, v in kw.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            try:
                if self._fh is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError as e:  # the log never stops a job
                print(f"napkin.runlog: cannot write {self.path}: {e}", file=sys.stderr)
                self.path = None

    def _tot(self, stage=None) -> dict:
        s = stage or self.stage_name or "-"
        return self.totals.setdefault(s, {"model_calls": 0, "attempts": 0, "in_tokens": 0, "out_tokens": 0,
                                          "model_cost": 0.0, "model_s": 0.0, "searches": 0, "search_cost": 0.0,
                                          "search_s": 0.0, "lookups": 0, "jev_calls": 0, "jev_cost": 0.0,
                                          "decisions": 0, "errors": 0})

    # -- the job --------------------------------------------------------------------
    def start(self, task, doc, stages, model=None, **kw):
        self._write("job_start", task=task, handler=self.handler, doc=doc, stages=list(stages), model=model,
                    bodies=_cfg["bodies"], **kw)

    @contextlib.contextmanager
    def stage(self, name):
        self.stage_name = name
        t = time.monotonic()
        self._write("stage_start", stage=name)
        err = None
        try:
            yield
        except BaseException as e:
            err = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            with self._lock:
                tot = dict(self._tot(name))
            self._write("stage_end", stage=name, secs=round(time.monotonic() - t, 2), error=err,
                        **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in tot.items()})

    def end(self, state, summary=None, fields=None, gaps=None, **kw):
        with self._lock:
            tots = json.loads(json.dumps(self.totals))
        allc = {k: round(sum(t[k] for t in tots.values()), 4) for k in
                ("model_calls", "in_tokens", "out_tokens", "model_cost", "searches", "search_cost", "jev_calls",
                 "jev_cost", "decisions", "errors")}
        allc["cost"] = round(allc["model_cost"] + allc["search_cost"] + allc["jev_cost"], 4)
        self._write("job_end", state=state, secs=round(time.monotonic() - self.t0, 2), summary=summary,
                    fields=fields, gaps=gaps, per_stage=tots, total=allc, **kw)
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    # -- what happened ----------------------------------------------------------------
    def model(self, *, purpose, model, secs, tin, tout, attempts, ok, error=None, payload=None, reply=None,
              payload_chars=None, reply_chars=None):
        cost = price(model, tin, tout)
        with self._lock:
            t = self._tot()
            t["model_calls"] += 1
            t["attempts"] += attempts
            t["in_tokens"] += tin
            t["out_tokens"] += tout
            t["model_cost"] += cost or 0.0
            t["model_s"] += secs
            t["errors"] += 0 if ok else 1
        self._write("model", stage=self.stage_name, purpose=purpose, model=model, secs=round(secs, 2),
                    attempts=attempts, in_tokens=tin, out_tokens=tout, cost=cost, cost_src="est" if cost is not None
                    else None, ok=ok, error=error, payload_chars=payload_chars, reply_chars=reply_chars,
                    payload=payload if _cfg["bodies"] else None, reply=reply if _cfg["bodies"] else None)

    def research(self, *, lens, market, query, secs, sources=None, cost=None, cached=None, error=None,
                 queries=None, urls=None):
        with self._lock:
            t = self._tot()
            t["searches"] += 1
            t["search_cost"] += cost or 0.0
            t["search_s"] += secs
            t["errors"] += 1 if error else 0
        self._write("research", stage=self.stage_name, lens=lens, market=market, query=query, secs=round(secs, 2),
                    sources=sources, urls=urls, cost=cost, cost_src="reported" if cost is not None else None,
                    cached=cached, error=error, queries=queries)

    def jev(self, *, check, questions, secs, state_chars, cost, error=None):
        with self._lock:
            t = self._tot()
            t["jev_calls"] += 1
            t["jev_cost"] += cost or 0.0
            t["errors"] += 1 if error else 0
        self._write("jev", stage=self.stage_name, check=check, questions=questions, secs=round(secs, 2),
                    state_chars=state_chars, cost=cost, cost_src="est", error=error)

    def retrieval(self, *, purpose, k, secs, found=None, error=None, query=None):
        with self._lock:
            t = self._tot()
            t["lookups"] += 1
            t["errors"] += 1 if error else 0
        self._write("retrieval", stage=self.stage_name, purpose=purpose, k=k, secs=round(secs, 2), found=found,
                    error=error, query=query if _cfg["bodies"] else None)

    def decisions(self, decs, stage=None):
        for d in decs or []:
            r = d.get("reasoning") or {}
            cert = r.get("certainty") or {}
            with self._lock:
                self._tot(stage)["decisions"] += 1
            self._write("decision", stage=stage or self.stage_name, id=d.get("id"), agent=d.get("agent"),
                        kind=d.get("kind"), action=d.get("action"),
                        targets=[str(t).split("#", 1)[-1] for t in d.get("targets") or []],
                        decided=r.get("decided"), certainty=cert.get("level"), why_certain=cert.get("why"),
                        because=[p.get("point") for p in r.get("because") or [] if isinstance(p, dict)][:8],
                        attention=r.get("attention"), polarity=d.get("polarity"), basis=d.get("basis"),
                        cites=len(d.get("cites") or []))

    def gap(self, stage, reason, detail):
        self._write("gap", stage=stage, reason=reason, detail=detail)

    def question(self, stage, text, options=None):
        self._write("question", stage=stage, text=text, options=options)

    def answer(self, stage, answer):
        self._write("answer", stage=stage, answer=answer if _cfg["bodies"] else "(answered)")

    def note(self, what, **kw):
        self._write("note", stage=self.stage_name, what=what, **kw)


NULL = RunLog(None, "-", "-")


def open_log(job: str, handler: str) -> RunLog:
    d = _cfg["dir"]
    if d is None:
        return RunLog(None, job, handler)
    task = handler.split("@", 1)[0]
    return RunLog(d / _dt.date.today().isoformat() / f"{task}-{job}.jsonl", job, handler)


# -- reading a run back ------------------------------------------------------------------
def _fmt_cost(c):
    return "-" if c in (None, 0, 0.0) else f"${c:.3f}"


def report(path: Path) -> str:
    evs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = []
    st = next((e for e in evs if e["ev"] == "job_start"), {})
    end = next((e for e in reversed(evs) if e["ev"] == "job_end"), None)
    out.append(f"# {st.get('task', '?')} {st.get('job', '?')}  doc {st.get('doc', '?')}  model {st.get('model', '?')}")
    if end:
        tot = end.get("total") or {}
        out.append(f"  {end.get('state')} in {end.get('secs')}s — {tot.get('model_calls')} model calls, "
                   f"{tot.get('in_tokens')}+{tot.get('out_tokens')} tokens, model {_fmt_cost(tot.get('model_cost'))}, "
                   f"{tot.get('searches')} searches {_fmt_cost(tot.get('search_cost'))}, total {_fmt_cost(tot.get('cost'))}")
        out.append(f"  {end.get('summary') or ''}")
    out.append("\n## Stages\n  stage        secs   calls  tokens(in+out)     model$   search  search$  decisions errors")
    for e in evs:
        if e["ev"] == "stage_end":
            out.append(f"  {e['stage']:<11} {e.get('secs', 0):>6}  {e.get('model_calls', 0):>5}  "
                       f"{e.get('in_tokens', 0):>7}+{e.get('out_tokens', 0):<7}  {_fmt_cost(e.get('model_cost')):>8}  "
                       f"{e.get('searches', 0):>6}  {_fmt_cost(e.get('search_cost')):>7}  {e.get('decisions', 0):>9} "
                       f"{e.get('errors', 0):>6}" + (f"  ERROR {e['error']}" if e.get("error") else ""))
    out.append("\n## Calls (in order)")
    for e in evs:
        if e["ev"] == "model":
            out.append(f"  {e['s']:>7.1f}s {e.get('stage') or '-':<9} model    {e['purpose']:<28} {e.get('model', ''):<18} "
                       f"{e['secs']:>6.1f}s x{e['attempts']} {e['in_tokens']:>6}+{e['out_tokens']:<6} "
                       f"{_fmt_cost(e.get('cost')):>8} " + ("ok" if e.get("ok") else f"FAILED {e.get('error', '')}"))
        elif e["ev"] == "research":
            out.append(f"  {e['s']:>7.1f}s {e.get('stage') or '-':<9} research {e['lens']}/{e['market']} "
                       f"“{str(e.get('query') or '')[:70]}” {e['secs']:.1f}s {e.get('sources', '?')} source(s) "
                       f"{_fmt_cost(e.get('cost'))}" + (" cached" if e.get("cached") else "")
                       + (f" FAILED {e['error']}" if e.get("error") else ""))
        elif e["ev"] == "jev":
            out.append(f"  {e['s']:>7.1f}s {e.get('stage') or '-':<9} jev      {e.get('check')} {e['questions']} question(s) "
                       f"{e['secs']:.2f}s {_fmt_cost(e.get('cost'))}" + (f" FAILED {e['error']}" if e.get("error") else ""))
        elif e["ev"] == "retrieval":
            out.append(f"  {e['s']:>7.1f}s {e.get('stage') or '-':<9} library  {e.get('purpose') or '-'} k={e['k']} "
                       f"{e.get('found', '?')} passage(s) {e['secs']:.1f}s" + (f" FAILED {e['error']}" if e.get("error") else ""))
        elif e["ev"] == "note":
            out.append(f"  {e['s']:>7.1f}s {e.get('stage') or '-':<9} note     {e['what']}: "
                       + json.dumps({k: v for k, v in e.items() if k not in ('t', 's', 'ev', 'job', 'stage', 'what')},
                                    ensure_ascii=False)[:400])
        elif e["ev"] in ("gap", "question", "answer"):
            out.append(f"  {e['s']:>7.1f}s {e.get('stage') or '-':<9} {e['ev']:<8} "
                       + json.dumps({k: v for k, v in e.items() if k not in ('t', 's', 'ev', 'job', 'stage')},
                                    ensure_ascii=False)[:400])
    out.append("\n## Decisions")
    for e in evs:
        if e["ev"] == "decision":
            out.append(f"  [{e.get('stage')}] {e.get('agent')} · {e.get('action')} → {', '.join(e.get('targets') or [])[:80]}"
                       f"  ({e.get('certainty') or '-'}{', ' + e['polarity'] if e.get('polarity') else ''})")
            out.append(f"      {e.get('decided') or ''}")
            if e.get("attention"):
                out.append(f"      ! {e['attention'][:300]}")
    if end and end.get("fields"):
        out.append("\n## Fields at the end")
        for k, f in end["fields"].items():
            out.append(f"  {k:<28} {f.get('state'):<9} by {f.get('by')}")
    return "\n".join(out)


def _latest(d: Path) -> Path | None:
    files = sorted(d.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--latest"
    if arg == "--latest":
        base = Path(os.environ.get("NAPKIN_RUNLOG_DIR") or ".")
        p = _latest(base)
    else:
        p = Path(arg)
        p = _latest(p) if p.is_dir() else p
    if not p:
        sys.exit("no run log found")
    print(f"({p})\n" + report(p))
