# Running the brief engine: a runbook for the next person or agent

How to get from a client's documents to a graded brief, what to check, and the traps that have
cost us runs. `README.md` is the reference (every setting, the code map); this file is the order
to do things in. Last checked 2026-10-01 on branch `jev-hardening`.

## 0. Rules that are not optional

- **Never print or paste `engine/.env`.** It holds live keys. To see what is set, list names
  only: `grep -o '^[A-Z_]*=' engine/.env`.
- **Never leave anything from the input out** (Sai, 2026-10-01). Inputs range from one line to
  multi-document tenders. Any clip is a bug to design out (see section 7 for the ones left).
- **Every model call is Claude** (ADR 0006, "cannot fail silently"). If no Claude call can be made, `run()` stops with
  `NoClaudeAvailable`. It never writes a brief from another model or from heuristics.
- **Client documents never go in git.** Put runs under `engine/outputs/` (git-ignored).
- **Every test or trial reports cost and tokens** beside the scores, and every score change is
  read against the noise line (section 5).

## 1. Set up (once)

```bash
cd engine
python3 -m venv .venv && source .venv/bin/activate     # Python 3.11+
pip install -e ".[all]"
cp .env.example .env                                   # then add keys; names in README "Keys & config"
```

Keys that matter for a full run:

| What | Variable | Without it |
|---|---|---|
| Claude | none (the `cli` transport uses your Claude Code login) or `ANTHROPIC_API_KEY` | no brief (`NoClaudeAvailable`) |
| jev checks (figures, conflicts, category) | `TYPESAFE_API_KEY` | checks print "did not run" and the brief goes on unchecked |
| Embeddings for retrieval | `NVIDIA_API_KEY` (falls back to a local embedder) | slower queries |
| Remote vector store | `QDRANT_*` | use the local index (below) |

**The store.** Retrieval (Loops 3-7) reads the award corpora from a vector store:
- `engine/.env` sets `RAG_STORE=qdrant`, so any run without an explicit store goes over the
  network.
- The shared store is moving to AWS, which Shrey owns; ask him before changing store code or
  settings.
- For local work, use the local index: `RAG_STORE=local`. `RAG_INDEX` is no longer needed:
  without it the engine uses `rag/_index_v4`. The index (`engine/rag/_index_v4/`, about 340 MB) is git-ignored, so copy it
  from a teammate.

## 2. Check it works (no model cost)

```bash
cd engine && python3 -m pytest -q --ignore=outputs        # 1,013 tests, about 25 s, all offline
python3 -m pytest -q agent-server                         # the app backend's tests
```

`--ignore=outputs` matters: `outputs/` holds old audit scratch tests that fail by design.

## 3. Make a brief

### From one file (the CLI)

```bash
cd engine
BRIEF_CLAUDE_TRANSPORT=cli RAG_STORE=local napkin-brief path/to/brief.docx --format md,pdf
```

One file in gives the full brief (since 2026-10-03, Sai: "all the user has to do is upload the
handoff document. keep it that simple"). `--loops37 --golden` are no longer needed; `--quick`
gives the light brief (capture and Loop 2 only).

- Add `--attach FILE` (repeatable) for supporting documents such as brand guidelines, tender
  annexes or Q&A. Each is folded into the input under an `===== ATTACHMENT: … =====` header.
- `--text "…"` takes a one-line or pasted brief instead of a file. One line works: fields the
  line does not give become open questions.
- Outputs:
  - `client_brief.md` / `.pdf`: the deliverable;
  - `review.md`: the working file, with citations, rejected drafts and the reasons;
  - `brief_object.json`: everything, including `meta.llm_stats`, the call and token ledger.

### Long inputs (tenders, several documents)

The golden extraction reads only the first 40,000 characters by default
(`BRIEF_CLIP_EXTRACT_CHARS`). For anything longer, set it above the input's size or the brief is
built from the first pages only:

```bash
BRIEF_CLIP_EXTRACT_CHARS=300000 napkin-brief rft.docx --attach trd.docx --attach qa.pdf
```

What happened when this was clipped: the Samaritans tender (227,000 characters) read only to
39,000 characters scored health about 40, against 62-74 when read whole. Splitting and merging
instead of a raised limit is planned (see `project_plan.clan`, `brief_maker_2026_10_01`).

### With research from upstream

From the CLI (since 2026-10-03, ADR 0017), give the research CLAN itself:

```bash
napkin-brief brief.docx --attach rft.docx --research Brand.clan
```

- `--research` takes a `.clan` zip, an unzipped CLAN folder, or a research dev run's `clan.json`.
- The engine reads the facts, the decisions people made on the findings, and the brand, then prints
  what it loaded, e.g. `research: 94 facts, 5 decisions, brand Samaritans`. A wrong file shows at once.
- Only verified and rejected findings become decisions; proposed ones give none.
- The CLAN's category is not passed (it is a taxonomy code). jev picks the category from the brief.

From Python, `research_facts.load_clan(path)` returns the same upstream dict. The manual way below
still works. Facts are rows of the shape
`{id, version, status, entity, key, value, unit, as_of, sources}`. A research CLAN keeps them in
`shared/facts.yaml`, **wrapped in a top-level `facts:` key**: unwrap it, or only one item arrives
and the research is silently unused.

```python
import os, sys, zipfile, yaml
from pathlib import Path
ENGINE = Path("engine").resolve()
sys.path[:0] = [str(ENGINE), str(ENGINE / "rag")]
os.environ.update(RAG_STORE="local", RAG_INDEX="_index_v4", BRIEF_CLAUDE_TRANSPORT="cli",
                  BRIEF_CLIP_EXTRACT_CHARS="300000")
os.chdir(ENGINE / "rag")
import parse_brief as pb

raw = Path("direction.txt").read_text()                       # the main brief
for f in ["rft.docx", "qa.pdf"]:                              # every supporting document, whole
    text, _ = pb.ingest(Path(f))
    raw += f"\n\n===== ATTACHMENT: {f} (supporting context — e.g. brand guidelines) =====\n{text}"
facts = yaml.safe_load(zipfile.ZipFile("Brand.clan").read("shared/facts.yaml"))
facts = facts.get("facts", []) if isinstance(facts, dict) else facts
assert len(facts) > 1
brief = pb.run(None, project="my-run", loops37=True, golden=True, raw_text=raw,
               source_name="direction + tender", upstream={"brand": "Brand", "category": "<contract enum>",
                                                           "facts": facts})
md = pb.render_client_brief(brief)
Path("client_brief.md").write_text(md); pb.write_rich_formats(md, Path("client_brief.md"), ["pdf"])
```

A full worked example, with grading, is
`engine/outputs/samaritans_youth_brief/run_brief.py` (local only, git-ignored).
`meta.research_facts` records which facts were given, used and skipped. Each field's `fact_refs`
lists the facts it cites.

### Long runs on a Mac

Wrap them in `caffeinate -dimsu …`. A sleeping Mac stalls `claude -p` calls, which looks like a
hang.

## 4. Grade it

The grader is `golden_critic`, independent of the writers. Its default for comparisons is
Fable 5.1, 3 samples, majority verdict per check:

```python
import json, golden_critic as gc
schema = json.loads(gc.SCHEMA_PATH.read_text()); gb = gc.from_brief_object(brief)
v, _ = gc.run_critic_sampled(schema, gb, gc.validate(schema, gb), model="claude-fable-5-1", samples=3)
print(v["health"], gc.quality_split(schema, gb, v)["quality"])
```

- **Health** is how complete and sound the brief is; **quality** covers the creative fields.
- Read `failed_checks`, and each empty field's `reason` and `rejected_attempt` in
  `brief_object.json` (`loop2_golden.fields.<id>`). An empty field usually means a gate
  rejected every draft. The reason says which gate and why.

## 5. Compare two versions of the code (checkpoints)

```bash
git worktree add ../napkin-os-stable <baseline-commit>     # then symlink engine/.env and rag/_index_v4 into it
cd engine/rag
python3 checkpoint_run.py --before ../../../napkin-os-stable --set test-five --arms after
```

- `--set` names come from `rag/golden/labels/client/eval_sets.json` (git-ignored: client
  material).
- Reports go to `outputs/e2e/checkpoint_<label>/`.
- **Noise:** one brief's health moves about ±15 between two runs of unchanged code (±34 on a
  five-brief sum).
- The hero fields (insight, single-minded proposition) are written fresh each run, so one run
  is never proof. Repeat before deciding.
- A test-five run costs about $4.5.

## 6. What the engine does with models (where cost goes)

Each call names its job; the job picks the model (`brief_llm.ROUTES`, ADR 0011):

| Job | Model (fallback) | Effort |
|---|---|---|
| extract (capture, golden extraction) | Opus 5.5 (Opus 4.6) | model default |
| hero writer (insight, SMP) | Opus 5.5 (Opus 4.6) | high, plus a sharpen pass |
| grounded writer (RTB, desired response) | Opus 5.5 (Opus 4.6) | model default |
| hero judge | Sonnet 5.5 (a judge never runs on its writer's model, so Opus 5.5 is skipped) | low |
| judge, mechanical, synth | Sonnet 5.5 (Haiku 4.5) | low / medium |

- `BRIEF_ROUTE_<JOB>` overrides one job's models, and `BRIEF_EFFORT_<JOB>` one job's effort.
  Measure a change on whole runs before editing the tables.
- A judge never runs on its writer's model.
- Typical cost for a short brief: about 110-160 s and well under $1.
- For the 228,000-character Samaritans input with 94 facts: 130-150 s, about $1.3-2.6 for the
  brief, plus about $0.5 to grade. Most of it is the long input sent to each extraction call.
- **jev** answers yes/no and choice questions with a calibrated probability, for about $0.04
  per million input tokens. It checks things; it never writes. `BRIEF_JEV_CHECKS=0` turns it off.

## 7. Known limits and traps (read before trusting a result)

- **Clips still in the code:** the capture, golden extraction, scorecard, how-to-win and
  territory calls read the first `BRIEF_CLIP_EXTRACT_CHARS` (40,000 by default, read once at
  import, so set it before `import parse_brief`). Judges never receive the brief itself, so
  `BRIEF_CLIP_CHARS` no longer changes anything.
  - jev's category, scorecard and capture-fallback checks read the first 60,000 characters.
  - jev's figure, fact-conflict, claim and open-question checks read the whole brief in
    pieces (fixed 2026-10-01).
- **A research number must be cited.** If a writer uses a figure that exists only in the
  research without its `[F:id]` (even a phone number), the citation gate fails the draft,
  and the field can end up empty. Seen on the Samaritans desired response, 2026-10-01.
- **Fields left empty are open questions, not failures of the run.** The engine never
  invents to fill a field (ADR 0009).
- **Loop 1 never reads RAG or research.** The capture is a faithful record of the client's
  words.
- **The local index vs the shared store:** results differ if you switch stores mid-comparison.
  Keep both arms on the same one.
- **Replays in the research tool** (`mock-backend/`, branch `research-tool-optimisation`) need
  its `research_cache/` folder. Otherwise a "replay" makes live, paid calls.

## 8. Where decisions and history live

- `rag/docs/adr/` holds the decisions with their evidence:
  - 0006: Claude only, never fail silently;
  - 0009: no invention at the source;
  - 0011: model routes and jev;
  - 0013: noise;
  - 0014: research facts.
- `project_plan.clan` (repo root, untracked, never commit it) holds the plan and the state for
  the next session. It is a zip: read `shared/data.yaml`. Keys `brief_maker_2026_10_01` and
  `brief_maker_next` cover the current brief-maker work.
- Sai approves changes one at a time: explain simply, trial on a brief, then commit.
