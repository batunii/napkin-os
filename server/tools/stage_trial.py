#!/usr/bin/env python3
"""Re-run the later stages of a job from a saved checkpoint, with a chosen model per stage.

    python3 server/tools/stage_trial.py --clan runs/metrics/<run>/checkpoints/after_research.json \\
        --stages synthesise,report --model report=claude-sonnet-5-5 --model layout=claude-sonnet-5-5 --repeat 3

The checkpoint is the document as the host held it at a stage boundary (run_sample.py saves them). Only the
stages named run again, against the same facts, so a change to synthesise, report or layout is tested for
about a dollar and against identical input, and `--repeat` measures that stage's own noise. Stages after
research need no web and no database; only the model service is started.

--model PURPOSE=MODEL / --effort PURPOSE=LEVEL are matched on the model call's purpose (synthesise, report,
layout, ...). The default model is the middleware's own default (claude-opus-5), which the dev model service
maps to a Claude Code alias, so a per-stage id must be one the dev service knows.
Writes runs/metrics/trial-<label>-<utc>/{trial.json, middleware.jsonl, mock/metrics.jsonl}.
"""
import argparse
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def merge_patch(target, patch):
    """RFC 7396 merge patch: null deletes, objects merge, everything else (arrays included) replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def page_metrics(html: str, clan: dict) -> dict:
    """What a report page covers: sections, charts, quotes, and which lenses and how many of the facts and
    findings it references anywhere (a field, a cite, a chart or a quote)."""
    import re
    from napkin.doc import lens_of_key
    refs = set(re.findall(r"\b(?:f|fi)_[0-9A-Z]{6,}\b", html))
    pins = {f["id"]: f for f in clan.get("facts") or []}
    with_ev = {lens_of_key(f.get("key")) for f in pins.values()} - {None}
    on_page = {lens_of_key(pins[r]["key"]) for r in refs if r in pins} - {None}
    return {"sections": len(re.findall(r"<h2[\s>]", html)), "charts": html.count("<clan-chart"),
            "quotes": html.count("<clan-quote"), "facts_on_page": len(refs & set(pins)), "facts_in_doc": len(pins),
            "findings_on_page": len({r for r in refs if r.startswith("fi_")}),
            "lenses_with_evidence": len(with_ev), "lenses_on_page": len(on_page & with_ev)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clan", required=True, help="a checkpoint file: after_research.json or after_synthesise.json")
    ap.add_argument("--stages", default="synthesise,report")
    ap.add_argument("--model", action="append", default=[], metavar="PURPOSE=MODEL")
    ap.add_argument("--effort", action="append", default=[], metavar="PURPOSE=LEVEL")
    ap.add_argument("--default-model", default="claude-opus-5-5")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--label", default="trial")
    a = ap.parse_args()
    by_model = dict(x.split("=", 1) for x in a.model)
    by_effort = dict(x.split("=", 1) for x in a.effort)
    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    out = REPO / "runs" / "metrics" / f"trial-{a.label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    (out / "mock").mkdir(parents=True)
    os.environ["NAPKIN_METRICS_FILE"] = str(out / "middleware.jsonl")

    port = free_port()
    env = dict(os.environ, MOCK_BACKEND_PORT=str(port), MOCK_DATA=str(out / "mock"), MOCK_FAKES="model",
               MOCK_TIMEOUT_MODEL="600", MOCK_NO_CACHE="1")
    mock = subprocess.Popen([sys.executable, str(REPO / "mock-backend" / "server.py")], env=env,
                            stdout=open(out / "mock.log", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        from napkin.capabilities import Capabilities
        from napkin.config import Settings
        from napkin.model import ModelPort, build_wire
        from napkin.pipeline import report as report_stage
        from napkin.pipeline import synthesise as synth

        settings = Settings(model_api="anthropic", model_base_url=f"http://127.0.0.1:{port}", model_api_key="dummy",
                            model=a.default_model)

        class Routed(ModelPort):
            """The middleware's port, with a model and effort chosen by call purpose (what step 3 will do in config)."""
            def call(self, purpose, system, payload, schema, **kw):
                if kw.get("model") is None and purpose in by_model:
                    kw["model"] = by_model[purpose]
                if kw.get("effort") is None and purpose in by_effort:
                    kw["effort"] = by_effort[purpose]
                return super().call(purpose, system, payload, schema, **kw)

        class NoLayers:
            def open(self, scope, attribution=None):
                return None

        handler = "start_campaign@1.0"
        base = json.loads(Path(a.clan).read_text())
        results = []
        for i in range(1, a.repeat + 1):
            job = f"trial_{a.label}_{i}"
            caps = Capabilities(handler=handler, scope={"org": "org/trial", "brand": "brand/trial"},
                                model_port=Routed(build_wire(settings), a.default_model, 600),
                                research_port=None, layer_store=NoLayers(), research_semaphore=threading.Semaphore(1))
            caps.bind_job(job)
            clan = copy.deepcopy(base)
            row = {"repeat": i, "job": job, "secs": {}}
            if "synthesise" in stages:
                t0 = time.monotonic()
                result, change, _ = synth.run_synthesis(clan["id"], clan["version"], clan, {}, handler, caps, seed=job,
                                                        with_audience=True)
                row["secs"]["synthesise"] = round(time.monotonic() - t0, 1)
                clan["findings"] = list(clan.get("findings") or []) + list(change["findings_append"])
                clan["decision_chain"]["decisions"] = list(clan["decision_chain"]["decisions"]) + list(change["decisions"])
                clan["data"] = merge_patch(clan["data"], change["data_patch"])
                row["findings"] = len(change["findings_append"])
                (out / f"findings_{i}.json").write_text(json.dumps(change["findings_append"], indent=1))
            if "report" in stages:
                t0 = time.monotonic()
                rep, cites, _, _ = report_stage.compose(clan["id"], clan, handler, caps)
                row["secs"]["report"] = round(time.monotonic() - t0, 1)
                row["layout_by"] = rep.get("layout_by")
                row["report_claims"] = sum(1 for s in rep.get("sections") or [] for b in s.get("blocks") or []
                                           if b.get("kind") == "claim")
                row["summary_lines"] = len(rep.get("summary") or [])
                row["headline"] = (rep.get("headline") or {}).get("text")
                row["layout_chars"] = len(rep.get("layout") or "")
                row.update(page_metrics(rep.get("layout") or "", clan))
                (out / f"page_{i}.html").write_text(rep.get("layout") or "")
            results.append(row)
            print(f"repeat {i}: {row}", flush=True)
    finally:
        mock.terminate()
        time.sleep(1)

    # cost per repeat: the dev model service's ledger, keyed by the job id the middleware sent
    import run_report
    led = run_report.load(out / "mock" / "metrics.jsonl")
    mw = run_report.load(out / "middleware.jsonl")
    for r in results:
        mine = [e for e in led if e.get("job") == r["job"] and e.get("family") == "model"]
        r["cli_cost"] = round(sum(e.get("cost_usd") or 0 for e in mine), 3)
        r["calls"] = len(mine)
        r["tokens_out"] = sum(e.get("out") or 0 for e in mine)
        lc = 0.0
        for e in mw:
            if e.get("kind") == "model" and e.get("job") == r["job"] and e.get("stop") != "error":
                c = run_report.list_cost(e.get("model"), e.get("input_tokens") or 0, e.get("output_tokens") or 0,
                                         e.get("breakdown"))
                lc += c or 0
        r["list_cost"] = round(lc, 3)
    (out / "trial.json").write_text(json.dumps({"clan": str(a.clan), "stages": stages, "models": by_model,
                                                "efforts": by_effort, "default_model": a.default_model,
                                                "results": results}, indent=1))
    cols = ("report_claims", "layout_by", "sections", "charts", "quotes", "facts_on_page", "lenses_on_page",
            "layout_chars", "cli_cost", "calls")
    print("\n| repeat | secs | " + " | ".join(cols) + " |\n|---|---|" + "---|" * len(cols))
    for r in results:
        print(f"| {r['repeat']} | {sum(r['secs'].values()):.0f} | " + " | ".join(str(r.get(c, '')) for c in cols) + " |")
    print("run dir:", out)


if __name__ == "__main__":
    main()
