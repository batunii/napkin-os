# ADR 0020: Reasons to believe hold proof only; missing proof is asked for

- **Status:** accepted (Sai, 2026-10-03: "finish it").
- **Code:**
  - `engine/parse_brief.py` (`_one`: requests moved to `_proof_needed` before judging; `proof_needed` on
    the field and in REQ-01's shipped draft);
  - `engine/brief_render.py` ("Proof still needed").
- **Tests:**
  - `engine/rag/test_cannot_fail_silently.py` (the judge never sees a request);
  - `engine/rag/test_fill_all.py` (rendering).

## Context

The proof writer's rule (since 2026-09-29) is to select proof only from the allowed facts and, where
the proposition needs proof that is not there, to write `TO CONFIRM: <the proof that is needed>`
instead of inventing it.

The reasons-to-believe judge then failed those drafts. Its verdict was "Items are 'TO CONFIRM'
placeholders, not proof" (`supports_smp`). On 6 of 7 briefs (2026-10-03) the field shipped flagged
even when its real items were good.

The two rules contradicted each other. The writer was right not to invent, and the judge was right that
a request is not proof.

## Decision

1. **Before the judge, every `TO CONFIRM` item leaves the reasons to believe.** The judge ranks and
   tests the real proof only.
2. **The requests are kept as `proof_needed` on the field.** Each also becomes an open question,
   "Proof needed: …". The client brief shows them under the reasons to believe as "Proof still
   needed", so nothing the writer asked for is lost.
3. **A draft with no real item left keeps its requests,** so the field is never blank (REQ-01, ADR
   0018). It is then judged and flagged as before.
4. **The writer's rule is unchanged.** It still never invents proof. The gap-filler (ADR 0019) is what
   turns "proof needed" into proof when stored facts or the web have it.

## Measured

Results are recorded below, from the live check before the commit.
