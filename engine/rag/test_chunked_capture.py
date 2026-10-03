"""P1 step A (2026-10-03): a long input is captured in chunks, in parallel, with global sentence numbers;
every sentence is in exactly one chunk; a short input is captured exactly as before."""
import re

import parse_brief as pb


def _segs(n, size=100, every=None):
    return [("===== ATTACHMENT: doc =====" if every and i and i % every == 0 else f"Sentence {i} " + "x" * size)
            for i in range(n)]


def test_every_sentence_is_in_exactly_one_chunk_and_chunks_stay_in_size():
    segs = _segs(900)
    ranges = pb._chunks(segs, size=20000)
    assert ranges[0][0] == 0 and ranges[-1][1] == len(segs)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))                 # contiguous, no gap, no overlap
    assert all(sum(len(f"[{i + 1}] {segs[i]}") + 1 for i in range(lo, hi)) <= 20000 for lo, hi in ranges)


def test_a_chunk_ends_at_a_document_boundary_when_one_is_near():
    segs = _segs(400, every=150)                                                # a boundary every 150 sentences
    ranges = pb._chunks(segs, size=20000)
    assert any(segs[hi].startswith("=====") for _lo, hi in ranges[:-1])


def test_numbered_range_keeps_the_global_numbers():
    segs = ["a", "b", "c", "d"]
    assert pb._numbered_range(segs, 2, 4) == "[3] c\n[4] d"


def test_a_long_input_is_captured_per_chunk_and_merged(monkeypatch):
    segs = _segs(1200)                                                           # ~130k chars
    monkeypatch.setattr(pb, "CHUNK_ABOVE", 60000)
    monkeypatch.setattr(pb, "CHUNK_CHARS", 40000)
    calls = []

    def fake(user, system=None, **kw):
        first = int(re.search(r"\[(\d+)\]", user).group(1))
        calls.append(first)
        if first > 600:
            return None if len(calls) == 99 else {"fields": {
                "deliverables": [{"value": "A landing page", "src": [first]}, {"value": f"Report {first}", "src": [first]}],
                "budget": {"value": "EUR 90k", "src": [first]}}}
        return {"fields": {"deliverables": [{"value": "A landing page", "src": [first]}],
                           "budget": {"value": "EUR 140k", "src": [first]}},
                "open_questions": ["Q from chunk"]}
    monkeypatch.setattr(pb, "_json_call", fake)
    cap = pb.capture_toon(segs)
    assert len(calls) == len(pb._chunks(segs)) >= 3 and cap["chunks"]["count"] == len(calls)
    deliverables = [d["value"] for d in cap["fields"]["deliverables"]]
    assert deliverables.count("A landing page") == 1 and any(d.startswith("Report") for d in deliverables)
    assert cap["fields"]["budget"]["value"] == "EUR 140k"                       # the first chunk's value
    assert cap["fields"]["budget"]["alternatives"][0]["value"] == "EUR 90k"     # the other kept...
    assert any("more than one budget" in (q.get("question") if isinstance(q, dict) else q) for q in cap["open_questions"])


def test_a_failed_chunk_is_named_and_the_rest_is_kept(monkeypatch):
    segs = _segs(1200)
    monkeypatch.setattr(pb, "CHUNK_ABOVE", 60000)
    monkeypatch.setattr(pb, "CHUNK_CHARS", 40000)

    def fake(user, system=None, **kw):
        first = int(re.search(r"\[(\d+)\]", user).group(1))
        return None if first == 1 else {"fields": {"deliverables": [{"value": f"D{first}", "src": [first]}]}}
    monkeypatch.setattr(pb, "_json_call", fake)
    cap = pb.capture_toon(segs)
    assert cap["chunks"]["failed"] and cap["chunks"]["failed"][0].startswith("sentences 1-")
    assert cap["fields"]["deliverables"]


def test_a_short_input_is_one_call_as_before(monkeypatch):
    calls = []
    monkeypatch.setattr(pb, "_json_call", lambda u, system=None, **k: calls.append(u) or {"fields": {"budget": {"value": "x", "src": [1]}}})
    cap = pb.capture_toon(["One short brief sentence."])
    assert len(calls) == 1 and "chunks" not in cap and calls[0].startswith("CLIENT BRIEF (numbered sentences):\n[1]")


def test_only_short_factual_differences_raise_a_which_holds_question():
    assert pb._real_conflict("EUR 140k including VAT", "EUR 90k including VAT")              # figures differ
    assert not pb._real_conflict("Raise awareness of the service among young adults",
                                 "Raise awareness among young adults of the service")       # same words
    assert not pb._real_conflict("x " * 100, "y " * 100)                                    # prose: never asked


def test_a_228k_input_is_read_whole_by_default():
    assert pb.CHUNK_ABOVE >= 600000 and not pb._long(["s" * 1000] * 228)
