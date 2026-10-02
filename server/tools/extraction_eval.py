#!/usr/bin/env python3
"""Compare models on the extraction step, on the exact source text a recorded run gave it.

    python3 server/tools/extraction_eval.py --run runs/metrics/<recorded run> --models claude-sonnet-5-5,claude-haiku-4-5 \\
        --repeat 2 --vocab lens_vocabulary.json

Reads the run's recordings/model_calls.jsonl, takes each extract_facts call (one per lens x market) and
sends the same system prompt, input and schema to each model named. Every reply is graded with the
middleware's own rules, so the numbers mean what the pipeline would have kept:
  returned      facts the model gave
  kept          facts whose evidence quote is verbatim in the source excerpt AND whose number is in that quote
  verbatim/fig  the two checks separately, per evidence quote
  wanted/vocab  share of keys that are one of the lens's asked-for names / a name in the lens document
It costs pennies per call, so it is the place to try a cheaper model on the largest model-cost line.
The recorded reply (the model the run used) is graded the same way as the baseline row.
Writes runs/metrics/exteval-<label>-<utc>/{exteval.json, mock/metrics.jsonl}.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_units(run: Path):
    """The last good extract_facts call per unit: (unit, system, payload, schema, model, reply)."""
    rec = run / "recordings" / "model_calls.jsonl"
    if not rec.is_file():
        raise SystemExit(f"{rec} not found: record a run first (run_sample.py records by default)")
    per = {}
    for line in rec.read_text().splitlines():
        r = json.loads(line)
        if r.get("purpose") == "extract_facts" and r.get("stop") == "ok" and r.get("reply"):
            per[r.get("unit")] = r
    out = []
    for unit, r in per.items():
        m = re.search(r"<input>\n(.*)\n</input>", r["user"], re.S)
        out.append((unit, r["system"], json.loads(m.group(1)), r["schema"], r["model"], r["reply"]))
    return out


def score(payload: dict, obj: dict, vocab: dict | None) -> dict:
    from napkin.rules.figures import quote_supports
    from napkin.rules.quotes import verbatim
    by_sid = {s["source_id"]: s for s in payload.get("sources") or []}
    wanted = set(payload.get("wanted") or [])
    canon, syn = (vocab or {}).get(payload.get("lens"), (set(), set()))
    n = kept = ev_n = ev_verb = ev_fig = in_wanted = in_vocab = 0
    for f in obj.get("facts") or []:
        n += 1
        unit = f.get("unit")
        if f.get("value_number") is not None:
            value = f["value_number"]
        elif f.get("value_boolean") is not None:
            value, unit = bool(f["value_boolean"]), "boolean"
        elif isinstance(f.get("value_text"), str) and f["value_text"].strip():
            value = f["value_text"].strip()
        else:
            value = None
        suffix = re.sub(r"[^a-z0-9_.]+", "_", str(f.get("key_suffix") or "").lower()).strip("._")
        head = suffix.split(".")[0]
        in_wanted += head in wanted
        in_vocab += head in canon
        ok = value is not None and not (unit == "proportion" and isinstance(value, (int, float)) and not 0 <= value <= 1)
        passed = False
        for ev in f.get("evidence") or []:
            ev_n += 1
            s = by_sid.get(ev.get("source_id"))
            q = next((verbatim(x, ev.get("quote", "")) for x in (s or {}).get("excerpts", []) if verbatim(x, ev.get("quote", ""))), None) if s else None
            ev_verb += bool(q)
            if q and value is not None and quote_supports(value, unit, q):
                ev_fig += 1
                passed = True
        kept += bool(ok and passed and suffix)
    return dict(returned=n, kept=kept, evidence=ev_n, verbatim=ev_verb, figure_ok=ev_fig, in_wanted=in_wanted,
                in_vocab=in_vocab, not_found=len(obj.get("not_found") or []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--models", required=True, help="comma-separated model ids the dev model service knows")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--vocab", default=None, help="lens_vocabulary.json, to score how many keys use the fixed names")
    ap.add_argument("--limit", type=int, default=0, help="only the first N units")
    ap.add_argument("--label", default="run")
    a = ap.parse_args()
    units = load_units(Path(a.run))
    if a.limit:
        units = units[:a.limit]
    vocab = None
    if a.vocab:
        vocab = {}
        for x in json.loads(Path(a.vocab).read_text()):
            c, s = vocab.setdefault(x["lens"], (set(), set()))
            c.add(x["key_suffix"])
            s.update(x.get("synonyms") or [])

    import socket
    import subprocess
    import threading
    import urllib.request
    s_ = socket.socket()
    s_.bind(("127.0.0.1", 0))
    port = s_.getsockname()[1]
    s_.close()
    out = REPO / "runs" / "metrics" / f"exteval-{a.label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
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

        base = {"model": "recorded", "units": len(units), "runs": 1, "secs": 0.0,
                **{k: 0 for k in ("returned", "kept", "evidence", "verbatim", "figure_ok", "in_wanted", "in_vocab", "errors")}}
        for unit, system, payload, schema, model, reply in units:
            sc = score(payload, json.loads(reply), vocab)
            for k, v in sc.items():
                base[k] = base.get(k, 0) + v
        base["model"] = f"recorded ({units[0][4]})" if units else "recorded"
        rows["recorded"] = base

        for model in [m.strip() for m in a.models.split(",") if m.strip()]:
            agg = {"model": model, "units": len(units), "runs": a.repeat, "secs": 0.0,
                   **{k: 0 for k in ("returned", "kept", "evidence", "verbatim", "figure_ok", "in_wanted", "in_vocab", "errors")}}
            for rep in range(a.repeat):
                for unit, system, payload, schema, _, _ in units:
                    job = f"exteval_{model}_{rep}_{re.sub(r'[^a-z0-9]', '', str(unit))}"
                    t0 = time.monotonic()
                    try:
                        obj = port_.call("extract_facts", system, payload, schema, usage=Usage(), attribution=job,
                                         max_tokens=8000, model=model, effort=a.effort, headers={"X-Napkin-Job": job})
                    except ModelError as e:
                        agg["errors"] += 1
                        print(f"  {model} {unit}: {e}", flush=True)
                        continue
                    agg["secs"] += time.monotonic() - t0
                    for k, v in score(payload, obj, vocab).items():
                        agg[k] = agg.get(k, 0) + v
                print(f"{model} repeat {rep + 1} done", flush=True)
            rows[model] = agg
    finally:
        mock.terminate()
        time.sleep(1)

    import run_report
    led = run_report.load(out / "mock" / "metrics.jsonl")
    for model, agg in rows.items():
        if model == "recorded":
            continue
        mine = [e for e in led if str(e.get("job", "")).startswith(f"exteval_{model}_")]
        agg["cli_cost"] = round(sum(e.get("cost_usd") or 0 for e in mine), 3)
        agg["calls"] = len(mine)
    (out / "exteval.json").write_text(json.dumps(rows, indent=1))
    print("\n| model | runs x units | returned | kept | kept % | verbatim % | number-in-quote % | wanted-name % | vocab-name % | secs/call | CLI $ |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for m, r in rows.items():
        n_calls = max(1, r["runs"] * r["units"])
        pct = lambda x, d: f"{100 * x / d:.0f}" if d else "-"
        print(f"| {r['model']} | {r['runs']} x {r['units']} | {r['returned']} | {r['kept']} | {pct(r['kept'], r['returned'])} | "
              f"{pct(r['verbatim'], r['evidence'])} | {pct(r['figure_ok'], r['evidence'])} | {pct(r['in_wanted'], r['returned'])} | "
              f"{pct(r['in_vocab'], r['returned'])} | {r['secs'] / n_calls:.1f} | {r.get('cli_cost', '-')} |")
    print("run dir:", out)


if __name__ == "__main__":
    main()
