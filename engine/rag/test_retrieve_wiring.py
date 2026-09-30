"""The pipeline's retrieval surface: brief-safe defaults, the brief_context entry point,
and the grounding check. Run: python3 -m pytest test_retrieve_wiring.py -q
"""
from __future__ import annotations
import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store_local  # noqa: E402
import retrieve  # noqa: E402
import filters as F  # noqa: E402


def _row(i, doc, vec, text, **md):
    """Build a store row for doc with the given vector and text, defaulting scope=global
    and tenant=house as apply_defaults() would on a real write."""
    # scope=global mirrors what contract.apply_defaults() stamps on every real write
    # (chunking.py:261). Pass scope=... to override, or scope=None for the pathological
    # row that never went through the contract at all.
    base = {"doc_id": doc, "level": "parent", "source": "ipa",
            "scope": "global", "tenant": "house", **md}
    return {"id": f"c{i}", "source": f"{doc}.md", "section": "S", "chunk_index": i, "vector": vec,
            "text": text, "header": "", "retrieval_queries": "",
            "metadata": {k: v for k, v in base.items() if v is not None}}


def _local(monkeypatch):
    """engine/.env sets RAG_STORE=qdrant and rag.py loads it, so every store call would
    otherwise go over the network. Tests pin the local store explicitly."""
    monkeypatch.setenv("RAG_STORE", "local")
    monkeypatch.setattr(retrieve.rag, "embed", lambda t, i="query": ([[1.0, 0.0]], "stub"))


def _index(tmp_path):
    """Build a local index with an active row, a production-stage row, a superseded row
    and a legacy row with neither field, for the brief-safe default tests."""
    st = store_local.LocalStore(tmp_path); st.ensure(2)
    st.replace_all([
        _row(0, "keep", [1.0, 0.0], "a strategy case about challenger brands", status="active"),
        _row(1, "dandad_group", [1.0, 0.0], "a design craft reference", source="dandad", stage="production"),
        _row(2, "overruled", [1.0, 0.0], "a superseded finding", status="superseded"),
        _row(3, "legacy", [1.0, 0.0], "an older chunk written before these fields existed"),
    ])
    st.manifest_path.write_text(json.dumps({"chunks": 4, "dim": 2}))
    return tmp_path


def test_brief_safe_hides_production_and_superseded_but_keeps_legacy_chunks(tmp_path, monkeypatch):
    """The brief_safe default excludes production and superseded chunks but keeps a legacy
    chunk that predates both fields; turning it off returns everything."""
    _local(monkeypatch); idx = _index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("challenger", k=10, index_dir=idx)}
    assert got == {"keep", "legacy"}          # legacy has neither field and must survive
    allp = {h["doc_id"] for h in retrieve.retrieve("challenger", k=10, index_dir=idx, brief_safe=False)}
    assert allp == {"keep", "legacy", "dandad_group", "overruled"}


def test_an_explicit_filter_still_wins_over_the_safe_default(tmp_path, monkeypatch):
    """An explicit stage filter overrides the brief-safe default, so a production-only
    caller can still see its own material."""
    _local(monkeypatch); idx = _index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("x", k=10, index_dir=idx,
                                                  where={"stage": "production"})}
    assert got == {"dandad_group"}            # the production clan can ask for its own material


def test_brief_safe_narrows_nothing_that_predates_the_fields():
    """The BRIEF_SAFE filter still matches a chunk with neither stage nor status set."""
    assert F.matches({"source": "ipa"}, retrieve.BRIEF_SAFE)


# ---- scope: the confidentiality boundary -------------------------------------
def _scoped_index(tmp_path):
    """Build a local index with a global house row, two brand-scoped dossiers and one row
    with scope=None that never went through the contract, for scope-boundary tests."""
    st = store_local.LocalStore(tmp_path); st.ensure(2)
    st.replace_all([
        _row(0, "house", [1.0, 0.0], "a licensed craft lesson"),                 # scope=global
        _row(1, "bmw_dossier", [1.0, 0.0], "what we learned on BMW", scope="brand:bmw"),
        _row(2, "audi_dossier", [1.0, 0.0], "what we learned on Audi", scope="brand:audi"),
        _row(3, "unlabelled", [1.0, 0.0], "a row that never met the contract", scope=None),
    ])
    st.manifest_path.write_text(json.dumps({"chunks": 4, "dim": 2}))
    return tmp_path


def test_a_caller_that_says_nothing_gets_the_house_corpus_only(tmp_path, monkeypatch):
    """The fail-closed default. This is the whole point of the change: a caller that
    forgets to pass scopes must not be served another agency's dossier."""
    _local(monkeypatch); idx = _scoped_index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("learned", k=10, index_dir=idx)}
    assert got == {"house"}


def test_a_brand_scoped_caller_gets_house_plus_its_own_and_nothing_else(tmp_path, monkeypatch):
    """A caller authorised for global plus its own brand sees the house corpus and its own
    dossier, but never another brand's."""
    _local(monkeypatch); idx = _scoped_index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("learned", k=10, index_dir=idx,
                                                  scopes=["global", "brand:bmw"])}
    assert got == {"house", "bmw_dossier"}         # audi's dossier is the breach case


def test_an_explicit_where_cannot_widen_the_scope(tmp_path, monkeypatch):
    """Unlike stage/status, scope does NOT yield to an explicit filter. A handler that
    can widen its own scope turns any handler bug into a cross-brand leak."""
    _local(monkeypatch); idx = _scoped_index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("learned", k=10, index_dir=idx,
                                                  where={"scope": {"in": ["brand:audi"]}},
                                                  scopes=["global", "brand:bmw"])}
    assert "audi_dossier" not in got


def test_an_unlabelled_row_is_withheld_rather_than_assumed_public(tmp_path, monkeypatch):
    """Deliberately the opposite of how `ne` treats an absent field (filters.py). A row
    with no `status` is legacy and safe to show; a row with no `scope` has an UNKNOWN
    owner, and unknown must never be served. apply_defaults() stamps scope=global on
    every real write, so this only fires on a row that bypassed the contract."""
    _local(monkeypatch); idx = _scoped_index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("contract", k=10, index_dir=idx,
                                                  scopes=["global", "brand:bmw"])}
    assert "unlabelled" not in got
    allp = {h["doc_id"] for h in retrieve.retrieve("contract", k=10, index_dir=idx,
                                                   scopes=retrieve.ALL_SCOPES,
                                                   tenants=retrieve.ALL_TENANTS)}
    assert "unlabelled" in allp                    # still reachable for an audit


# ---- tenant: the agency boundary ---------------------------------------------
def _tenanted_index(tmp_path):
    """Build a local index with a house-tenant row and one row each for two different
    agency tenants, for tenant-boundary tests."""
    st = store_local.LocalStore(tmp_path); st.ensure(2)
    st.replace_all([
        _row(0, "licensed", [1.0, 0.0], "an IPA case everyone has licensed"),      # tenant=house
        _row(1, "acme_work", [1.0, 0.0], "what Acme learned", tenant="acme"),
        _row(2, "rival_work", [1.0, 0.0], "what the rival agency learned", tenant="rival"),
    ])
    st.manifest_path.write_text(json.dumps({"chunks": 3, "dim": 2}))
    return tmp_path


def test_a_caller_naming_no_agency_gets_the_licensed_corpus_only(tmp_path, monkeypatch):
    """A caller that names no tenant sees only the house-licensed corpus."""
    _local(monkeypatch); idx = _tenanted_index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("learned", k=10, index_dir=idx)}
    assert got == {"licensed"}


def test_an_agency_gets_the_licensed_corpus_plus_its_own(tmp_path, monkeypatch):
    """An agency named as a tenant sees the licensed corpus plus its own work, but never
    a rival agency's."""
    _local(monkeypatch); idx = _tenanted_index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("learned", k=10, index_dir=idx,
                                                  tenants=["house", "acme"])}
    assert got == {"licensed", "acme_work"}        # the rival's work is the breach case


def test_tenant_and_scope_are_independent_boundaries(tmp_path, monkeypatch):
    """A rival agency's brand:bmw lesson is not this agency's BMW. Being authorised for
    brand:bmw must not reach across the tenant boundary to fetch it."""
    _local(monkeypatch)
    st = store_local.LocalStore(tmp_path); st.ensure(2)
    st.replace_all([
        _row(0, "ours", [1.0, 0.0], "our own bmw lesson", tenant="acme", scope="brand:bmw"),
        _row(1, "theirs", [1.0, 0.0], "their bmw lesson", tenant="rival", scope="brand:bmw"),
    ])
    st.manifest_path.write_text(json.dumps({"chunks": 2, "dim": 2}))
    got = {h["doc_id"] for h in retrieve.retrieve("bmw", k=10, index_dir=tmp_path,
                                                  scopes=["global", "brand:bmw"],
                                                  tenants=["house", "acme"])}
    assert got == {"ours"}


# ---- grounding check ---------------------------------------------------------
def test_grounding_flags_invented_citations():
    """check_grounding() marks text grounded when every citation is allowed, and flags a
    citation not in the allowed set as invented while still listing all cited ids."""
    allowed = {"ipa_0409", "pb_fcb-grid#process"}
    good = retrieve.check_grounding("Pre-selling worked for [ipa_0409].", allowed)
    assert good["grounded"] and good["cited"] == ["ipa_0409"] and not good["invented"]

    bad = retrieve.check_grounding("As shown in [ipa_0999] and [ipa_0409].", allowed)
    assert not bad["grounded"] and bad["invented"] == ["ipa_0999"]
    assert bad["cited"] == ["ipa_0999", "ipa_0409"]


def test_grounding_notices_a_confident_paragraph_with_no_citation_at_all():
    """A confident claim with no citation at all is flagged uncited and not grounded."""
    r = retrieve.check_grounding("Award-winning launches always pre-sell.", {"ipa_0409"})
    assert r["uncited"] and not r["grounded"] and r["cited"] == []


def test_grounding_handles_section_suffixes_and_repeats():
    """A citation with a section suffix is recognised, and citing it twice still lists it
    once and counts as grounded."""
    allowed = {"pb_fcb-grid#process"}
    r = retrieve.check_grounding("See [pb_fcb-grid#process], and again [pb_fcb-grid#process].", allowed)
    assert r["cited"] == ["pb_fcb-grid#process"] and r["grounded"]


def test_grounding_accepts_a_context_object_citations_map():
    """check_grounding() accepts a BriefContext's own citations() as the allowed set."""
    import brief_context as bc
    hit = bc.Hit(cite="ipa_0409", doc_id="ipa_0409", source="ipa", bucket="exemplars",
                 title="t", section="s", header="h", text="x", score=1.0)
    ctx = bc.BriefContext(blocks={"exemplars": bc.Block("exemplars", [hit], 100)},
                          query="q", keywords=[], filters={})
    assert retrieve.check_grounding("[ipa_0409] supports this.", ctx.citations())["grounded"]
