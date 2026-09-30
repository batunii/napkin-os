-- 0009 shared scope: a fact that belongs to a vertical, or to every category.
--
-- The re-pilot stored Ireland's digital search spend (EUR 345m, 2025) under 19
-- leaves, and the alcohol age rule under every alcohol leaf: the same figure,
-- researched and stored once per leaf, with any contest repeated per leaf. A
-- fact now lives at the scope it describes:
--
--   category/<vertical>.<leaf>   one leaf (as before)
--   category/<vertical>          a whole vertical, e.g. category/alcohol
--   category/all                 every category, e.g. the Irish advertising market
--
-- layers.promote_shared finds the same figure (measure, qualifier, market,
-- period, value, unit) under two or more leaves and moves it up: to their
-- vertical when they share one, else to all. The broader fact carries every
-- copy's evidence; each leaf copy is retracted with the reason, so the history
-- shows the move. Cells may cite facts of their leaf, its vertical and all.

-- The category tree knows leaves, verticals, and all.
CREATE OR REPLACE FUNCTION layers.category_leaf_known() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.entity LIKE 'category/%'
     AND NEW.entity <> 'category/all'
     AND NOT EXISTS (SELECT 1 FROM layers.categories c WHERE 'category/' || c.code = NEW.entity)
     AND NOT EXISTS (SELECT 1 FROM layers.verticals v WHERE 'category/' || v.code = NEW.entity) THEN
    RAISE EXCEPTION 'unknown_leaf: % is not in the category tree', NEW.entity USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION layers.promote_shared(p_leaves text[], p_run text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  g         record;
  v_scope   text;
  v_fact    jsonb;
  v_ev      jsonb;
  v_out     jsonb;
  v_promoted integer := 0;
  v_retracted integer := 0;
  v_copy    text;
BEGIN
  FOR g IN
    SELECT f.key, f.qualifier_norm, f.market, f.period_start, f.period_end, f.value_type, f.value_num,
           f.value_text, f.value_bool, f.unit,
           array_agg(f.id ORDER BY f.id) AS ids,
           array_agg(DISTINCT split_part(split_part(f.entity, '/', 2), '.', 1)) AS verticals,
           count(DISTINCT f.entity) AS leaves,
           min(f.qualifier) AS qualifier,
           bool_or(f.market_basis = 'stated') AS market_stated,
           bool_or(f.period_basis = 'stated') AS period_stated
      FROM layers.facts f
     WHERE f.entity LIKE 'category/%.%'
       AND (p_leaves IS NULL OR substr(f.entity, 10) = ANY (p_leaves))
       AND f.status IN ('active', 'contested', 'history', 'undated')
     GROUP BY f.key, f.qualifier_norm, f.market, f.period_start, f.period_end, f.value_type, f.value_num,
              f.value_text, f.value_bool, f.unit
    HAVING count(DISTINCT f.entity) >= 2
  LOOP
    v_scope := CASE WHEN cardinality(g.verticals) = 1 THEN 'category/' || g.verticals[1] ELSE 'category/all' END;
    v_fact := jsonb_strip_nulls(jsonb_build_object(
      'id', 'f_' || left(md5(concat_ws('|', 'promote', v_scope, g.key, g.qualifier_norm, g.market, g.period_start,
                                      g.period_end, g.value_num, g.value_text, g.value_bool)), 24),
      'layer', 'category', 'entity', v_scope, 'key', g.key, 'qualifier', g.qualifier, 'market', g.market,
      'market_basis', CASE WHEN g.market = 'UNKNOWN' THEN 'unknown' WHEN g.market_stated THEN 'stated' ELSE 'inferred' END,
      'value', CASE g.value_type WHEN 'number' THEN to_jsonb(g.value_num) WHEN 'text' THEN to_jsonb(g.value_text)
                                 ELSE to_jsonb(g.value_bool) END,
      'unit', g.unit, 'licence', 'open', 'method', 'report',
      'period_start', g.period_start, 'period_end', g.period_end,
      'period_basis', CASE WHEN g.period_start IS NULL THEN NULL WHEN g.period_stated THEN 'stated' ELSE 'inferred' END));
    SELECT jsonb_agg(DISTINCT jsonb_build_object('excerpt_id', e.excerpt_id, 'quote', e.quote,
                                                 'quote_start', e.quote_start))
      INTO v_ev FROM layers.evidence e WHERE e.fact_id = ANY (g.ids);
    v_out := layers.append_fact(v_fact, v_ev, jsonb_build_object(
      'id', 'd_' || left(md5('promote|' || p_run || '|' || (v_fact ->> 'id')), 24), 'kind', 'classify',
      'handler', 'promote_shared@1', 'action', 'promote_scope',
      'rationale', format('The same figure appears under %s leaves; it describes %s, not one leaf.', g.leaves, v_scope)));
    v_promoted := v_promoted + 1;
    FOREACH v_copy IN ARRAY g.ids LOOP
      PERFORM layers.retract_fact(v_copy, format('moved to %s (%s): the same figure appears under %s leaves',
                                                 v_scope, v_out ->> 'fact', g.leaves),
        jsonb_build_object('id', 'd_' || left(md5('retract-moved|' || p_run || '|' || v_copy), 24),
                           'kind', 'classify', 'handler', 'promote_shared@1', 'action', 'retract_moved'));
      v_retracted := v_retracted + 1;
    END LOOP;
  END LOOP;
  RETURN jsonb_build_object('promoted', v_promoted, 'retracted', v_retracted);
END $$;

-- confirm_cell, as in 0005, citing the leaf's own facts, its vertical's and all
-- categories', never a superseded or retracted fact.
CREATE OR REPLACE FUNCTION layers.confirm_cell(p jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_org     text := layers.scope_org();
  v_cell    layers.cells;
  v_prefix  text;
  v_vertical text;
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
  SELECT vertical INTO v_vertical FROM layers.categories WHERE code = v_cell.leaf;

  -- every cited fact answers this cell
  SELECT string_agg(ci ->> 'fact_id', ', ') INTO v_bad
    FROM jsonb_array_elements(p -> 'citations') ci
   WHERE ci ? 'fact_id' AND NOT EXISTS (
     SELECT 1 FROM layers.facts f
      WHERE f.id = ci ->> 'fact_id'
        AND f.key LIKE v_prefix || '.%'
        AND (f.entity IN ('category/' || v_cell.leaf, 'category/' || v_vertical, 'category/all')
             OR f.entity LIKE 'brand/%')
        AND f.status NOT IN ('superseded', 'retracted')
        AND f.market IN (SELECT code FROM layers.applicable_markets(v_cell.market)));
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION 'invalid_input: fact(s) % do not answer % / % / % (lens prefix, leaf or its shared scope, market, superseded or retracted)',
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
