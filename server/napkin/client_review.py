"""Ellis's prompt for `find_client_parts` (middleware-api.md §11).

The client answered a locked document as a whole; Ellis reads their words and
says which of the app's parts they were about, each with the sentence that
says so. The prompt is built from the task input alone — the answer, the
proof and the parts — and asks for nothing but `{address, answer, quote}`:
no rationale, no reasoning, no comment. What comes back is checked in code
(handlers/find_client_parts.py), never trusted.
"""

from __future__ import annotations

PURPOSE = "find_client_parts"
MAX_TOKENS = 1024
ANSWERS = ("accepted", "accepted_with_changes", "rejected")
MAX_QUOTE = 500

SYSTEM = """You are Ellis, the extract agent of an advertising agency's platform. A client has answered a
document the agency sent them. You are given their answer to the document as a whole (`answer`), their
own words (`proof`), and the parts of the document (`parts`, each with an `address`, a `label` and the
`value` the client saw; a null value was not shown to you).

Say which parts the client's words are about. For each one give:
- address: copied exactly from `parts`;
- answer: what the client says of THAT part — accepted, accepted_with_changes (they ask for a change) or
  rejected. It may differ from the document's answer ("the proposition is right, the audience is wrong");
- quote: the sentence of `proof` that says so, copied character for character. Never paraphrase, shorten
  with an ellipsis, fix spelling or change quote marks. At most 500 characters.

Suggest a part only when the words point at it. One suggestion per part. When the words name no part,
return an empty list: that is a valid answer. Never guess from the document's answer alone, never use
outside knowledge, and give no explanation."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["suggestions"],
    "properties": {"suggestions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["address", "answer", "quote"],
        "properties": {"address": {"type": "string"},
                       "answer": {"type": "string", "enum": list(ANSWERS)},
                       "quote": {"type": "string"}}}}},
}  # no length bound on quote: a long one is dropped in code, not a failed call


def payload(answer: str, proof: str, parts: list[dict]) -> dict:
    """What the model sees: these three and nothing else."""
    return {"answer": answer, "proof": proof,
            "parts": [{"address": p["address"], "label": p["label"], "value": p["value"]} for p in parts]}
