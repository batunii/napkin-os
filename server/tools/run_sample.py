#!/usr/bin/env python3
"""Run one start_campaign job end to end against the dev stack and write its metrics.

    python3 server/tools/run_sample.py --name small --prompt "..." [--answers "Ireland and GB"]

Starts the mock backend (real `claude -p` behind every port, research cache OFF so
every research call is paid for and counted) and the middleware, drives the job
the way the host does (the contract suite's Host/Run), and writes into
runs/metrics/<name>-<utc>/: middleware.jsonl, mock/metrics.jsonl, report.md/json,
questions.json. Every model call costs real money; this is not a dry run.
"""
import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(url, secs=60):
    end = time.time() + secs
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise SystemExit(f"{url} did not come up")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--answers", default="{}", help='JSON {"markets": "...", "brand": "...", "categories": "..."}: '
                    "free text for a question that offers no option to pick")
    ap.add_argument("--job-timeout", type=int, default=2400)
    ap.add_argument("--no-record", action="store_true",
                    help="do not record this run (by default every model call is saved in full under recordings/, "
                         "the web answers under research_cache/, and the document at each stage under checkpoints/)")
    ap.add_argument("--replay-research", metavar="DIR",
                    help="answer research from an earlier run's research_cache/ (misses run live and are recorded); "
                         "the web then does not change between runs")
    a = ap.parse_args()
    out = REPO / "runs" / "metrics" / f"{a.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    (out / "mock").mkdir(parents=True)
    mport, wport = free_port(), free_port()
    env = dict(os.environ, MOCK_BACKEND_PORT=str(mport), MOCK_DATA=str(out / "mock"),
               MOCK_TIMEOUT_MODEL="600", MOCK_TIMEOUT_RESEARCH="900")
    if a.replay_research:
        env["MOCK_CACHE_ROOT"] = str(Path(a.replay_research).resolve())     # hits replay, misses go live
        env.pop("MOCK_NO_CACHE", None)
    elif not a.no_record:
        (out / "research_cache").mkdir()
        env["MOCK_CACHE_ROOT"] = str(out / "research_cache")               # empty: every unit goes live and is recorded
        env.pop("MOCK_NO_CACHE", None)
    else:
        env["MOCK_NO_CACHE"] = "1"
    mock = subprocess.Popen([sys.executable, str(REPO / "mock-backend" / "server.py")], env=env,
                            stdout=open(out / "mock.log", "w"), stderr=subprocess.STDOUT)
    mw_env = dict(os.environ, NAPKIN_MODEL_API="anthropic", NAPKIN_MODEL_BASE_URL=f"http://127.0.0.1:{mport}",
                  NAPKIN_MODEL_API_KEY="dummy", NAPKIN_RESEARCH_URL=f"http://127.0.0.1:{mport}",
                  NAPKIN_RETRIEVAL_URL=f"http://127.0.0.1:{mport}", NAPKIN_LAYERS_URL=f"http://127.0.0.1:{mport}",
                  NAPKIN_PORT=str(wport), NAPKIN_METRICS_FILE=str(out / "middleware.jsonl"))
    if not a.no_record:
        mw_env["NAPKIN_RECORD_DIR"] = str(out / "recordings")
    mw = subprocess.Popen([sys.executable, "-m", "napkin.app"], cwd=REPO / "server", env=mw_env,
                          stdout=open(out / "middleware.log", "w"), stderr=subprocess.STDOUT)
    try:
        wait(f"http://127.0.0.1:{mport}/healthz")
        wait(f"http://127.0.0.1:{wport}/healthz")
        spec = importlib.util.spec_from_file_location("contract_test", REPO / "mock-middleware" / "contract_test.py")
        ct = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ct)
        suite = ct.Suite(ct.Client(f"http://127.0.0.1:{wport}", None), REPO / "app" / "templates" / "campaign-research",
                         a.job_timeout)
        doc, data, inp, facts, chain = ct.start_doc(a.prompt)
        host = ct.Host(suite, doc, data, facts, chain)
        class Snapping(ct.Run):
            """Saves the document the first time each stage boundary is seen, so a later stage can be re-run from it."""
            seen: set = set()

            def reply(self, st, body, task):
                body = super().reply(st, body, task)
                if not a.no_record:
                    stage = (body.get("job") or {}).get("stage")
                    tag = None
                    if host.facts and not host.findings and stage in ("synthesise", "report"):
                        tag = "after_research"
                    elif host.findings and not (host.data.get("report") or {}).get("sections"):
                        tag = "after_synthesise"
                    if tag and tag not in self.seen:
                        self.seen.add(tag)
                        (out / "checkpoints").mkdir(exist_ok=True)
                        (out / "checkpoints" / f"{tag}.json").write_text(json.dumps(host.clan(), indent=1))
                return body

        run, asked = Snapping(suite, host, inp), []

        def answer(r, q):
            asked.append({"text": q.get("text"), "address": q.get("address"),
                          "options": [o.get("label") for o in q.get("options", [])]})
            cands = [o for o in q.get("options", []) if "value" in o]
            field = (q.get("address") or "").partition("#campaign.")[2]
            return (cands[0], None) if cands else (None, json.loads(a.answers).get(field, "Ireland"))

        t0 = time.time()
        run.start(answer)
        wall = time.time() - t0
        (out / "questions.json").write_text(json.dumps(asked, indent=1))
        # what the job produced, kept so run_report can grade quality
        (out / "facts.json").write_text(json.dumps(host.facts, indent=1))
        (out / "clan.json").write_text(json.dumps(host.clan(), indent=1))
        (out / "outcome.json").write_text(json.dumps({"selection": host.data.get("selection"), "report": host.data.get("report"),
                                                      "findings": host.findings}, indent=1))
        (out / "run.json").write_text(json.dumps({"name": a.name, "prompt": a.prompt, "wall_s": round(wall, 1),
                                                  "final_state": run.states[-1], "questions": len(asked), "recorded": not a.no_record,
                                                  "replay_research": a.replay_research,
                                                  "facts": len(host.facts), "findings": len(host.findings)}, indent=1))
        print(f"job {run.states[-1]} in {wall:.0f}s, {len(asked)} question(s), {len(host.facts)} facts")
    finally:
        for p in (mw, mock):
            p.terminate()
        time.sleep(1)
    subprocess.run([sys.executable, str(REPO / "server" / "tools" / "run_report.py"), str(out)], check=False)
    print("run dir:", out)


if __name__ == "__main__":
    main()
