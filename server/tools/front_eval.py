#!/usr/bin/env python3
"""Compare models on the three front stages (extract, identify, select), on the exact input a recorded run gave them.

    python3 server/tools/front_eval.py --run runs/metrics/<recorded run> --models claude-sonnet-5-5,claude-haiku-4-5 \\
        --repeat 3 [--effort low]

Reads the run's recordings/model_calls.jsonl, takes the extract, identify and select calls and sends the same
system prompt, input and schema to each model named. There is no ground truth for these stages, so the score is
agreement with the recorded reply (the model the run used) on the fields the pipeline acts on:
  extract   the brand, the client organisation and the set of market codes
  identify  the brand names, the client organisation and the set of category leaves
  select    which lenses run, and the markets skipped per lens
"Same result" means the same decisions; the wording of reasons is not compared.
Writes runs/metrics/fronteval-<label>-<utc>/{fronteval.json, mock/metrics.jsonl}.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PURPOSES = ("extract", "identify", "select")


def decisions(purpose: str, obj: dict) -> dict:
    """The fields the pipeline acts on, as comparable values."""
    if purpose == "extract":
        name = lambda x: ((x or {}).get("name") or "").strip().lower()
        return {"brand": name(obj.get("brand")), "client_org": name(obj.get("client_org")),
                "markets": sorted((m.get("code") or "").upper() for m in obj.get("markets") or [])}
    if purpose == "identify":
        return {"brands": sorted((b.get("name") or "").strip().lower() for b in obj.get("brands") or []),
                "client_org": ((obj.get("client_org") or {}).get("name") or "").strip().lower(),
                "categories": sorted(c.get("leaf") or "" for c in obj.get("categories") or [])}
    if purpose == "select":
        return {"lenses": {x.get("lens"): [bool(x.get("run")), sorted(x.get("skip_markets") or [])]
                           for x in obj.get("lenses") or []}}
    raise ValueError(purpose)


def agreement(a: dict, b: dict) -> tuple[int, int]:
    """(fields that match, fields compared), a field being one decision; a lens counts as one field."""
    match = total = 0
    for k in a:
        if k == "lenses":
            for lens in set(a[k]) | set(b.get(k, {})):
                total += 1
                match += a[k].get(lens) == b.get(k, {}).get(lens)
        else:
            total += 1
            match += a[k] == b.get(k)
    return match, total


def load_calls(run: Path):
    rec = run / "recordings" / "model_calls.jsonl"
    if not rec.is_file():
        raise SystemExit(f"{rec} not found: record a run first (run_sample.py records by default)")
    per = {}
    for line in rec.read_text().splitlines():
        r = json.loads(line)
        if r.get("purpose") in PURPOSES and r.get("stop") == "ok" and r.get("reply"):
            per[r["purpose"]] = r
    out = []
    for p in PURPOSES:
        if p in per:
            r = per[p]
            m = re.search(r"<input>\n(.*)\n</input>", r["user"], re.S)
            out.append((p, r["system"], json.loads(m.group(1)), r["schema"], r["model"], r["reply"], r.get("max_tokens") or 4000))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--models", required=True, help="comma-separated model ids the dev model service knows")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--label", default="run")
    a = ap.parse_args()
    calls = load_calls(Path(a.run))

    s_ = socket.socket()
    s_.bind(("127.0.0.1", 0))
    port = s_.getsockname()[1]
    s_.close()
    out = REPO / "runs" / "metrics" / f"fronteval-{a.label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    (out / "mock").mkdir(parents=True)
    env = dict(os.environ, MOCK_BACKEND_PORT=str(port), MOCK_DATA=str(out / "mock"), MOCK_FAKES="model",
               MOCK_TIMEOUT_MODEL="600", MOCK_NO_CACHE="1")
    mock = subprocess.Popen([sys.executable, str(REPO / "mock-backend" / "server.py")], env=env,
                            stdout=open(out / "mock.log", "w"), stderr=subprocess.STDOUT)
    rows = {}
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        from napkin.config import Settings
        from napkin.model import ModelError, ModelPort, Usage, build_wire
        settings = Settings(model_api="anthropic", model_base_url=f"http://127.0.0.1:{port}", model_api_key="dummy")
        port_ = ModelPort(build_wire(settings), "claude-opus-5-5", 600)
        for model in [m.strip() for m in a.models.split(",") if m.strip()]:
            agg = {p: dict(match=0, total=0, errors=0, secs=0.0, calls=0) for p in PURPOSES}
            for rep in range(a.repeat):
                for purpose, system, payload, schema, _, reply, max_tokens in calls:
                    job = f"fronteval_{model}_{rep}_{purpose}"
                    t0 = time.monotonic()
                    try:
                        obj = port_.call(purpose, system, payload, schema, usage=Usage(), attribution=job,
                                         max_tokens=max_tokens, model=model, effort=a.effort, headers={"X-Napkin-Job": job})
                    except ModelError as e:
                        agg[purpose]["errors"] += 1
                        print(f"  {model} {purpose}: {e}", flush=True)
                        continue
                    agg[purpose]["secs"] += time.monotonic() - t0
                    agg[purpose]["calls"] += 1
                    m, t = agreement(decisions(purpose, json.loads(reply)), decisions(purpose, obj))
                    agg[purpose]["match"] += m
                    agg[purpose]["total"] += t
                print(f"{model} repeat {rep + 1} done", flush=True)
            rows[model] = agg
    finally:
        mock.terminate()
        time.sleep(1)

    import run_report
    led = run_report.load(out / "mock" / "metrics.jsonl")
    for model, agg in rows.items():
        for purpose in PURPOSES:
            mine = [e for e in led if str(e.get("job", "")) == f"fronteval_{model}_{purpose}"
                    or re.fullmatch(rf"fronteval_{re.escape(model)}_\d+_{purpose}", str(e.get("job", "")))]
            agg[purpose]["cli_cost"] = round(sum(e.get("cost_usd") or 0 for e in mine), 3)
    (out / "fronteval.json").write_text(json.dumps(rows, indent=1))
    print("\n| model | stage | decisions agreeing with the recorded reply | errors | secs/call | CLI $ |")
    print("|---|---|---|---|---|---|")
    for m, agg in rows.items():
        for p in PURPOSES:
            r = agg[p]
            pct = f"{100 * r['match'] / r['total']:.0f}%" if r["total"] else "-"
            print(f"| {m} | {p} | {r['match']}/{r['total']} ({pct}) | {r['errors']} | "
                  f"{r['secs'] / max(1, r['calls']):.1f} | {r['cli_cost']} |")
    print("run dir:", out)


if __name__ == "__main__":
    main()
