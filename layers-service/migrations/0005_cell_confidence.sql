-- 0005 cell confidence: derived by the database, never sent.
--
-- A cell's confidence was computed by the caller, and the first live run
-- showed why that is wrong: 44 of 156 cells disagreed with their own sources,
-- in both directions. Confidence is now derived here, from the distinct
-- publishers behind the version's citations and whether any is primary, in
-- the same transaction that stores the version. A payload that sends a
-- confidence is refused, so no caller can self-report one.

CREATE OR REPLACE FUNCTION layers.confirm_cell(p jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_org     text := layers.scope_org();
  v_cell    layers.cells;
  v_prefix  text;
  v_did     text := p -> 'decision' ->> 'id';
  v_existing jsonb;
  v_version integer;
  v_vid     text;
  v_bad     text;
  v_sources integer;
  v_pubs    integer;
  v_primary boolean;
  v_conf    text;
  c         jsonb;
BEGIN
  SELECT * INTO v_cell FROM layers.cells
   WHERE leaf = p ->> 'leaf' AND lens = p ->> 'lens' AND market = p ->> 'market';
  IF v_cell.id IS NOT NULL THEN
    -- one confirmation of a cell at a time, so versions never collide (cells
    -- are never updated, so a row lock is not available to the app role)
    PERFORM pg_advisory_xact_lock(hashtextextended(v_cell.id, 7332));
  END IF;
  IF v_cell.id IS NULL THEN
    RAISE EXCEPTION 'unknown_cell: % / % / % is not open; open the leaf for the market first (layers.open_cells)',
      p ->> 'leaf', p ->> 'lens', p ->> 'market' USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF jsonb_typeof(p -> 'citations') IS DISTINCT FROM 'array' OR jsonb_array_length(p -> 'citations') = 0 THEN
    RAISE EXCEPTION 'invalid_input: a cell version cites at least one fact or excerpt' USING ERRCODE = 'check_violation';
  END IF;
  IF p ? 'confidence' THEN
    RAISE EXCEPTION 'invalid_input: confidence is derived from the sources, never sent' USING ERRCODE = 'check_violation';
  END IF;
  IF v_did IS NULL OR p -> 'decision' ->> 'kind' IS NULL THEN
    RAISE EXCEPTION 'invalid_input: decision.id and decision.kind are required' USING ERRCODE = 'check_violation';
  END IF;
  SELECT key_prefix INTO v_prefix FROM layers.lenses WHERE code = v_cell.lens;

  -- every cited fact answers this cell
  SELECT string_agg(ci ->> 'fact_id', ', ') INTO v_bad
    FROM jsonb_array_elements(p -> 'citations') ci
   WHERE ci ? 'fact_id' AND NOT EXISTS (
     SELECT 1 FROM layers.facts f
      WHERE f.id = ci ->> 'fact_id'
        AND f.key LIKE v_prefix || '.%'
        AND (f.entity = 'category/' || v_cell.leaf OR f.entity LIKE 'brand/%')
        AND f.status <> 'superseded'
        AND f.market IN (SELECT code FROM layers.applicable_markets(v_cell.market)));
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION 'invalid_input: fact(s) % do not answer % / % / % (lens prefix, leaf, market or superseded)',
      v_bad, v_cell.leaf, v_cell.lens, v_cell.market USING ERRCODE = 'check_violation';
  END IF;

  SELECT d.body INTO v_existing FROM layers.decisions d WHERE d.id = v_did;
  IF v_existing IS NULL THEN
    INSERT INTO layers.decisions (id, kind, handler, action, rationale, cites, org, brand, body)
    VALUES (v_did, p -> 'decision' ->> 'kind', p -> 'decision' ->> 'handler', p -> 'decision' ->> 'action',
            p -> 'decision' ->> 'rationale',
            coalesce(ARRAY(SELECT jsonb_array_elements_text(p -> 'decision' -> 'cites')), '{}'),
            v_org, NULL, p -> 'decision');
  ELSIF v_existing IS DISTINCT FROM p -> 'decision' THEN
    RAISE EXCEPTION 'idempotency_conflict: decision % exists with another body', v_did USING ERRCODE = 'unique_violation';
  END IF;

  -- Confidence, derived from what the citations stand on: independent publishers
  -- and whether any source is primary. high = 3+ publishers incl. a primary;
  -- medium = 2+; low = otherwise.
  SELECT count(DISTINCT s.publisher), coalesce(bool_or(s.tier = 'primary'), false) INTO v_pubs, v_primary
    FROM jsonb_array_elements(p -> 'citations') ci
    LEFT JOIN layers.evidence e ON e.fact_id = ci ->> 'fact_id'
    JOIN layers.excerpts x ON x.id = coalesce(ci ->> 'excerpt_id', e.excerpt_id)
    JOIN layers.captures cp ON cp.id = x.capture_id
    JOIN layers.sources s ON s.id = cp.source_id;
  v_conf := CASE WHEN v_pubs >= 3 AND v_primary THEN 'high' WHEN v_pubs >= 2 THEN 'medium' ELSE 'low' END;

  SELECT coalesce(max(version), 0) + 1 INTO v_version FROM layers.cell_versions WHERE cell_id = v_cell.id;
  v_vid := 'cv_' || md5(v_cell.id || '|' || v_version);
  INSERT INTO layers.cell_versions (id, cell_id, version, summary, change_note, confidence, author_kind, author,
                                    decision_id, written_by_org)
  VALUES (v_vid, v_cell.id, v_version, p ->> 'summary', p ->> 'change_note', v_conf,
          split_part(p ->> 'author', ':', 1), p ->> 'author', v_did, v_org);

  FOR c IN SELECT * FROM jsonb_array_elements(p -> 'citations') LOOP
    INSERT INTO layers.cell_citations (version_id, fact_id, excerpt_id)
    VALUES (v_vid, c ->> 'fact_id', c ->> 'excerpt_id') ON CONFLICT DO NOTHING;
  END LOOP;

  SELECT count(*) INTO v_sources FROM layers.version_sources(v_vid);
  RETURN jsonb_build_object('cell', v_cell.id, 'version', v_version, 'id', v_vid, 'sources', v_sources,
                            'publishers', v_pubs, 'confidence', v_conf);
END $$;
