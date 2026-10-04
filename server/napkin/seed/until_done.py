"""Run the seeding job until every unit is done, resuming by itself.

    python -m napkin.seed.until_done <the same arguments as napkin.seed.run>

A long run can meet a usage window that outlasts the job's own retries: units
are then left for the next run and the job exits 3. This reruns it after a
wait (30 minutes by default), as many times as it takes, and stops when:

  - the job exits 0 (everything done);
  - <run-dir>/STOP exists (you asked it to);
  - three attempts in a row finish no new unit (something needs a person);
  - the job fails some other way (its exit code is passed on).

Each attempt resumes from the run directory's cache, so nothing is paid twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

WAIT = 1800
MAX_IDLE = 3


def _done(run_dir: Path) -> int:
    try:
        s = json.loads((run_dir / "status.json").read_text())
        return int(s.get("cells_written", 0)) + int(s.get("skipped_fresh", 0))
    except (OSError, ValueError):
        return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--run-dir" not in args:
        print("--run-dir is required", file=sys.stderr)
        return 2
    run_dir = Path(args[args.index("--run-dir") + 1])
    idle, attempt, best = 0, 0, -1
    while True:
        attempt += 1
        print(json.dumps({"attempt": attempt, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}), flush=True)
        code = subprocess.call([sys.executable, "-m", "napkin.seed.run", *args])
        if code == 0:
            print(json.dumps({"until_done": "finished", "attempts": attempt}), flush=True)
            return 0
        if code != 3:
            print(json.dumps({"until_done": "the job failed", "exit": code}), flush=True)
            return code
        if (run_dir / "STOP").exists():
            print(json.dumps({"until_done": "stopped by the STOP file"}), flush=True)
            return 3
        done = _done(run_dir)
        idle = 0 if done > best else idle + 1
        best = max(best, done)
        if idle >= MAX_IDLE:
            print(json.dumps({"until_done": f"{MAX_IDLE} attempts in a row made no progress; stopping"}), flush=True)
            return 3
        print(json.dumps({"until_done": f"units left; resuming in {WAIT // 60} minutes"}), flush=True)
        end = time.time() + WAIT
        while time.time() < end:
            if (run_dir / "STOP").exists():
                print(json.dumps({"until_done": "stopped by the STOP file"}), flush=True)
                return 3
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
