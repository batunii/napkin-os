-- 0004 cells: the living dossier (Planner Research Taxonomy, panels 3 and 4).
--
-- A cell is one leaf x one lens x one market: "mobile networks, regulation,
-- IE". It is what a planner reads. Facts are its evidence underneath.
--
--   lenses         the eight standing questions, each with its own clock
--   cells          opened per leaf and market by the job that researches them
--   cell_versions  every confirmation of a cell, never overwritten: summary in
--                  our words, what changed since last time, confidence, the
--                  last-confirmed date. A year of versions = the category's history.
--   cell_citations what each version stands on: facts, or verbatim excerpts
--                  for claims that are not a single value (codes, culture)
--   cell_verifications  a person confirming an agent-written version
--   cell_status    fresh | stale | empty per cell, from the lens's clock; an
--                  amber leaf (its own regulator) takes the stricter clock
--
-- The guideline asks for 3 to 5 cited sources per cell. A version citing fewer
-- is kept and shown as thin, rather than refused: a thin cell that says so is
-- more useful than an empty one.

CREATE TABLE layers.lenses (
  code          text PRIMARY KEY,
  name          text NOT NULL,
  key_prefix    text NOT NULL UNIQUE CHECK (key_prefix ~ '^[a-z_]+$'),
  question      text NOT NULL,
  cadence       interval NOT NULL,
  amber_cadence interval,            -- the stricter clock on a regulated (amber) leaf
  ordinal       integer NOT NULL
);

INSERT INTO layers.lenses (code, name, key_prefix, question, cadence, amber_cadence, ordinal) VALUES
  ('market_structure', 'Market structure', 'market',
   'Who are the players, what share, what is growing, what is consolidating?', '3 months', NULL, 1),
  ('brands_positioning', 'Brands and positioning', 'positioning',
   'What does each competitor claim, which line, which asset, who changed agency?', '7 days', NULL, 2),
  ('consumer_culture', 'Consumer and culture', 'consumer',
   'Which segments, what attitudes, which cultural moments and tensions are live?', '7 days', NULL, 3),
  ('category_codes', 'Category codes', 'codes',
   'What does every ad in this category do? What is the accepted grammar and what is worn out?', '3 months', NULL, 4),
  ('rhythm_moments', 'Rhythm and moments', 'rhythm',
   'Seasonal peaks, contract cycles, launch windows, events the category plans around.', '1 month', NULL, 5),
  ('media_spend', 'Media and spend', 'media',
   'Which channels, what share of spend, what is working, what does attention cost here?', '1 month', NULL, 6),
  ('regulation_clearance', 'Regulation and clearance', 'regulation',
   'Which codes apply, what needs substantiation, what mandatory copy, who signs off?', '1 month', '1 month', 7),
  ('effectiveness_evidence', 'Effectiveness evidence', 'effectiveness',
   'Which cases in this leaf won and why? What does the evidence say about long vs short, share of voice, pricing power?',
   '3 months', NULL, 8);

CREATE TABLE layers.cells (
  id            text PRIMARY KEY CHECK (id ~ '^cell_'),
  leaf          text NOT NULL REFERENCES layers.categories (code),
  lens          text NOT NULL REFERENCES layers.lenses (code),
  market        text NOT NULL REFERENCES layers.geographies (code) CHECK (market <> 'UNKNOWN'),
  opened_at     timestamptz NOT NULL DEFAULT now(),
  opened_by_org text NOT NULL,
  UNIQUE (leaf, lens, market)
);

CREATE TABLE layers.cell_versions (
  id             text PRIMARY KEY CHECK (id ~ '^cv_'),
  cell_id        text NOT NULL REFERENCES layers.cells (id),
  version        integer NOT NULL CHECK (version >= 1),
  summary        text NOT NULL CHECK (length(summary) BETWEEN 1 AND 4000),
  change_note    text NOT NULL CHECK (length(change_note) BETWEEN 1 AND 2000),   -- what changed since last time
  confidence     text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
  confirmed_at   timestamptz NOT NULL DEFAULT now(),                           -- the last-confirmed stamp
  author_kind    text NOT NULL CHECK (author_kind IN ('agent', 'human')),
  author         text NOT NULL CHECK (author ~ '^(agent|human):'),
  decision_id    text NOT NULL REFERENCES layers.decisions (id),
  written_by_org text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (author_kind = split_part(author, ':', 1)),
  UNIQUE (cell_id, version)
);

CREATE TABLE layers.cell_citations (
  version_id text NOT NULL REFERENCES layers.cell_versions (id),
  fact_id    text REFERENCES layers.facts (id),
  excerpt_id text REFERENCES layers.excerpts (id),
  CHECK ((fact_id IS NULL) <> (excerpt_id IS NULL)),
  UNIQUE NULLS NOT DISTINCT (version_id, fact_id, excerpt_id)
);

CREATE TABLE layers.cell_verifications (
  version_id  text NOT NULL REFERENCES layers.cell_versions (id),
  verified_by text NOT NULL CHECK (verified_by ~ '^human:'),
  verified_at timestamptz NOT NULL DEFAULT now(),
  decision_id text NOT NULL REFERENCES layers.decisions (id),
  PRIMARY KEY (version_id, verified_by)
);

-- History is kept: a version, its citations and its verifications never change.
CREATE TRIGGER cells_frozen BEFORE UPDATE OR DELETE ON layers.cells
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER cell_versions_frozen BEFORE UPDATE OR DELETE ON layers.cell_versions
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER cell_citations_frozen BEFORE UPDATE OR DELETE ON layers.cell_citations
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER cell_verifications_frozen BEFORE UPDATE OR DELETE ON layers.cell_verifications
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();

-- Who wrote a row comes from the transaction's scope, never the caller.
CREATE FUNCTION layers.stamp_org() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'cells' THEN
    NEW.opened_by_org := layers.scope_org();
  ELSE
    NEW.written_by_org := layers.scope_org();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER cells_stamp BEFORE INSERT ON layers.cells
  FOR EACH ROW EXECUTE FUNCTION layers.stamp_org();
CREATE TRIGGER cell_versions_stamp BEFORE INSERT ON layers.cell_versions
  FOR EACH ROW EXECUTE FUNCTION layers.stamp_org();

-- ── opening a leaf for a market: its eight cells ─────────────────────────────

CREATE FUNCTION layers.open_cells(p_leaf text, p_market text)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM layers.categories WHERE code = p_leaf) THEN
    RAISE EXCEPTION 'unknown_leaf: % is not in the category tree', p_leaf USING ERRCODE = 'foreign_key_violation';
  END IF;
  INSERT INTO layers.cells (id, leaf, lens, market, opened_by_org)
  SELECT 'cell_' || md5(p_leaf || '|' || l.code || '|' || p_market), p_leaf, l.code, p_market, layers.scope_org()
    FROM layers.lenses l
  ON CONFLICT (leaf, lens, market) DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- ── confirming a cell: one new version, with what it stands on ──────────────
--
--   layers.confirm_cell({leaf, lens, market, summary, change_note, confidence,
--                        author, citations: [{fact_id} | {excerpt_id}], decision}) -> jsonb
--
-- A cited fact must answer this cell: its key is under the lens's prefix, it
-- is about this leaf (or a named competitor), it is not superseded, and its
-- market applies here (an IE cell may cite IE, EU and GLOBAL facts, never GB).

CREATE FUNCTION layers.confirm_cell(p jsonb)
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

  SELECT coalesce(max(version), 0) + 1 INTO v_version FROM layers.cell_versions WHERE cell_id = v_cell.id;
  v_vid := 'cv_' || md5(v_cell.id || '|' || v_version);
  INSERT INTO layers.cell_versions (id, cell_id, version, summary, change_note, confidence, author_kind, author,
                                    decision_id, written_by_org)
  VALUES (v_vid, v_cell.id, v_version, p ->> 'summary', p ->> 'change_note', p ->> 'confidence',
          split_part(p ->> 'author', ':', 1), p ->> 'author', v_did, v_org);

  FOR c IN SELECT * FROM jsonb_array_elements(p -> 'citations') LOOP
    INSERT INTO layers.cell_citations (version_id, fact_id, excerpt_id)
    VALUES (v_vid, c ->> 'fact_id', c ->> 'excerpt_id') ON CONFLICT DO NOTHING;
  END LOOP;

  SELECT count(*) INTO v_sources FROM layers.version_sources(v_vid);
  RETURN jsonb_build_object('cell', v_cell.id, 'version', v_version, 'id', v_vid, 'sources', v_sources);
END $$;

-- The distinct sources a version stands on, through its facts' evidence and
-- its directly cited excerpts.
CREATE FUNCTION layers.version_sources(p_version text)
RETURNS TABLE (source_id text) LANGUAGE sql STABLE AS $$
  SELECT DISTINCT c.source_id
    FROM layers.cell_citations ci
    LEFT JOIN layers.evidence e ON e.fact_id = ci.fact_id
    JOIN layers.excerpts x ON x.id = coalesce(ci.excerpt_id, e.excerpt_id)
    JOIN layers.captures c ON c.id = x.capture_id
   WHERE ci.version_id = p_version
$$;

-- ── freshness, as a planner sees it before the content ──────────────────────

CREATE VIEW layers.cell_status WITH (security_invoker = true) AS
  SELECT c.id AS cell, c.leaf, c.lens, c.market, cat.regulated AS amber,
         CASE WHEN cat.regulated AND l.amber_cadence IS NOT NULL THEN least(l.cadence, l.amber_cadence)
              ELSE l.cadence END AS cadence,
         v.version, v.confirmed_at, v.confidence, v.author_kind,
         v.confirmed_at + CASE WHEN cat.regulated AND l.amber_cadence IS NOT NULL
                               THEN least(l.cadence, l.amber_cadence) ELSE l.cadence END AS due_at,
         CASE WHEN v.id IS NULL THEN 'empty'
              WHEN now() >= v.confirmed_at + CASE WHEN cat.regulated AND l.amber_cadence IS NOT NULL
                                                  THEN least(l.cadence, l.amber_cadence) ELSE l.cadence END
                THEN 'stale'
              ELSE 'fresh' END AS state,
         (SELECT count(*) FROM layers.version_sources(v.id)) AS sources,
         coalesce((SELECT count(*) FROM layers.version_sources(v.id)) < 3, true) AS thin,
         coalesce(EXISTS (SELECT 1 FROM layers.cell_verifications cv WHERE cv.version_id = v.id)
                  OR v.author_kind = 'human', false) AS verified
    FROM layers.cells c
    JOIN layers.lenses l ON l.code = c.lens
    JOIN layers.categories cat ON cat.code = c.leaf
    LEFT JOIN LATERAL (SELECT * FROM layers.cell_versions cv WHERE cv.cell_id = c.id
                        ORDER BY cv.version DESC LIMIT 1) v ON true;

GRANT SELECT ON layers.lenses, layers.cells, layers.cell_versions, layers.cell_citations,
                layers.cell_verifications, layers.cell_status TO __APP_ROLE__;
GRANT INSERT ON layers.cells, layers.cell_versions, layers.cell_citations, layers.cell_verifications TO __APP_ROLE__;
