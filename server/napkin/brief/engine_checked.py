"""draft_brief through Sai's engine, then the check loop (checkloop.py): the engine writes the
brief; jev flags the parts that need rewriting; Jude says why; Dara rewrites them and fills
what the engine left empty. The owner's flow, 2026-10-01."""
from __future__ import annotations

from .. import reasoning as rsn
from ..util import iso
from . import capture as cap_stage
from . import checkloop
from . import fill as fill_stage
from .engine import EngineBriefJob
from .fields import KEYS, LABELS, filled, get, put
from .planned import DIMS, _short


class EngineCheckedBriefJob(EngineBriefJob):
    def stage_judge(self):
        if self.engine_state != "used":
            return super().stage_judge()  # the engine was not used: the middleware's own judge
        values = self._values()
        open_keys = [k for k in KEYS if k not in self.locked]
        said = "\n\n".join(m["text"] for m in cap_stage.material_payload(self.mats))[:12000]
        view = fill_stage.research_view(self.clan, self.data0)
        written = [k for k in open_keys if filled(values.get(k))]
        self.set_state(written, "judging", "judge")
        res = checkloop.run(self, values, said, open_keys, {"facts": view["facts"], "findings": view["findings"]}, [])
        patch, decisions = {}, []
        mat_ids = [m.id for m in self.mats if m.readable]
        for k, v in res["rewritten"].items():
            prior = self.writer.get(k)
            cites = list(v["cites"]) + ([prior] if prior else [])
            pts = [rsn.point(v["why"], cites or mat_ids)]
            if prior is None:
                pts.append(rsn.point("The brief engine left it empty", mat_ids))
            r = rsn.make(f"Rewrote the {LABELS.get(k, k)} on Jude's note." if prior else f"Wrote the {LABELS.get(k, k)}.",
                         pts, rsn.certainty("medium" if v["basis"] in ("research", "library") else "low",
                                            "rests on what it cites" if v["basis"] in ("research", "library")
                                            else "a planner's proposal, to confirm with the client"),
                         "the client says otherwise, or a person edits it", only_option="Jude named it for a rewrite")
            if self.held.get(k):
                d = self.dec(("propose", k, "check"), "edit", "propose", [k], cites, r, "drafter",
                             proposed_value=v["value"], basis=v["basis"])
                self.proposals.append({"field": k, "value": v["value"], "decision": d["id"]})
                self.set_state([k], "proposed", "judge")
            else:
                d = self.dec(("rework", k), "edit", "regenerate", [k], cites, r, "drafter", basis=v["basis"])
                put(patch, k, v["value"])
                self.set_state([k], "done", "judge")
            self.writer[k] = d["id"]
            decisions.append(d)
        read = res["after"] or res["before"]
        if read is not None:
            vals = {**values, **{k: v["value"] for k, v in res["rewritten"].items()}}
            health = round(100 * read["score"] / 14)
            fields = {k: {"outcome": "kept" if k in self.locked else "revised" if k in res["rewritten"] else
                          "passed" if filled(vals.get(k)) else "absent", "checks":
                          [{"check": "needs_rewrite", "method": "auto", "status": "pass" if read["p"].get(k, 0) < 0.5
                            else "review", "note": f"jev: {round(read['p'][k], 2)} that it needs rewriting"}]
                          if k in read["p"] else []} for k in KEYS}
            summary = (f"jev flagged {sum(1 for x in res['before']['p'].values() if x >= 0.5)} part(s); Jude sent "
                       f"{len(res['todo'])} to Dara, who rewrote {len(res['rewritten'])}.") if res["before"] else "Checked."
            patch["review"] = {
                "built_at": iso(), "handler": self.handler, "based_on": {"version": self.base},
                "scorecard": {"dimensions": [{"dimension": d, "verdict": read["dims"][d]} for d in DIMS],
                              "single_mindedness": {"verdict": "single", "split_into": []}, "summary": summary},
                "judge": {"reason_codes_version": "1", "fields": fields,
                          "dependencies": [{"id": "chain", "status": "pass" if read["chain"] >= 0.5 else "fail",
                                            "note": _short((res["jude"].get("chain") or {}).get("issue")
                                                           or f"jev: {round(read['chain'], 2)} that it holds", 300)}],
                          "definition_of_done": [{"id": "all_required_filled", "status": "pass" if all(
                              filled(vals.get(k)) for k in KEYS if k != "open_questions") else "fail"}],
                          "health": health}}
            vids = [d["id"] for d in decisions]
            disagreed = [k for k, x in res["before"]["p"].items() if x >= 0.5 and k not in res["todo"]] \
                if res["before"] else []
            pts = [rsn.point(summary, vids or [f"{self.doc}#review"]),
                   rsn.point(f"BetterBriefs as jev reads it: {sum(1 for d in DIMS if read['dims'][d] == 'pass')} pass, "
                             f"{sum(1 for d in DIMS if read['dims'][d] == 'vague')} vague, "
                             f"{sum(1 for d in DIMS if read['dims'][d] == 'missing')} missing", [f"{self.doc}#review"])]
            if disagreed:
                pts.append(rsn.point("Jude kept what jev flagged in: " + ", ".join(LABELS.get(k, k) for k in disagreed),
                                     [f"{self.doc}#review"]))
            r = rsn.make("Checked the engine's brief: jev flagged, Jude explained, Dara rewrote.", pts,
                         rsn.certainty("medium", "jev's line is provisional; Jude read every flag"),
                         "a part changes and the brief is checked again", only_option="one check of the brief")
            decisions.append(self.dec(("review", "check"), "edit", "review", ["review"], vids, r, "judge"))
        qs = [q for q in res["questions"] if q]
        if qs and "open_questions" not in self.locked and not self.held.get("open_questions"):
            have = list(get(self.W, "open_questions") or [])
            new = [q for q in qs if q not in have]
            if new:
                r = rsn.make(f"Added {len(new)} question(s) only the client can answer.",
                             [rsn.point("From Jude's check", [d["id"] for d in decisions] or mat_ids)],
                             rsn.certainty("high", "each needs the client"), "the client answers",
                             only_option="the questions follow from what is missing")
                put(patch, "open_questions", (have + new)[:12])
                decisions.append(self.dec(("questions", "check"), "edit", "questions", ["open_questions"],
                                          [d["id"] for d in decisions], r, "judge"))
        if decisions:
            self.add_chunk("judge", patch, decisions)
        with self.lock:
            for k, f in self.fields.items():
                if f["state"] in ("waiting", "judging", "revising", "drafting"):
                    self.fields[k] = {"state": "absent" if not filled(get(self.W, k)) else "done", "by": "judge"}
