-- 0006 period basis: was the period stated, or worked out?
--
-- `market_basis` already says whether the page named the place or we inferred
-- it. The period had no such flag: "unchanged compared to 2024, at 6.1%" was
-- stored as a 2025 figure exactly like "6.1% in 2025", though the first
-- passage never says 2025. Now every dated fact says which:
--
--   period_basis = stated    the passage names the period ("in 2025", "Q3 2025")
--                  inferred  worked out from context (a comparison, the
--                            publication date, the surrounding page)
--                  null      the fact is undated (date_basis = unknown)
--
-- Stated is checked, not trusted: the year the period ends in must appear in
-- one of the fact's quoted passages. Existing dated facts are backfilled by
-- that same rule.

ALTER TABLE layers.facts ADD COLUMN period_basis text CHECK (period_basis IN ('stated', 'inferred'));

-- The backfill runs as the owner. Forced row-level security would apply the
-- scope policies to it too, so it is lifted for this one statement and forced
-- again straight after.
ALTER TABLE layers.facts NO FORCE ROW LEVEL SECURITY;
UPDATE layers.facts f
   SET period_basis = CASE
         WHEN EXISTS (SELECT 1 FROM layers.evidence e JOIN layers.excerpts x ON x.id = e.excerpt_id
                       WHERE e.fact_id = f.id
                         AND x.text ~ ('(^|[^0-9])' || extract(year FROM f.period_end)::int::text || '([^0-9]|$)'))
         THEN 'stated' ELSE 'inferred' END
 WHERE f.date_basis = 'period';
ALTER TABLE layers.facts FORCE ROW LEVEL SECURITY;

ALTER TABLE layers.facts ADD CONSTRAINT facts_period_basis
  CHECK ((date_basis = 'period') = (period_basis IS NOT NULL));

-- A fact's content, now including its period basis, never changes.
CREATE OR REPLACE FUNCTION layers.facts_only_status_moves() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.id, NEW.layer, NEW.org, NEW.brand, NEW.entity, NEW.key, NEW.market, NEW.market_basis, NEW.version,
      NEW.value_type, NEW.value_num, NEW.value_text, NEW.value_bool, NEW.unit,
      NEW.period_start, NEW.period_end, NEW.date_basis, NEW.period_basis, NEW.supersedes, NEW.licence, NEW.method,
      NEW.decision_id, NEW.written_by_org, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.layer, OLD.org, OLD.brand, OLD.entity, OLD.key, OLD.market, OLD.market_basis, OLD.version,
      OLD.value_type, OLD.value_num, OLD.value_text, OLD.value_bool, OLD.unit,
      OLD.period_start, OLD.period_end, OLD.date_basis, OLD.period_basis, OLD.supersedes, OLD.licence, OLD.method,
      OLD.decision_id, OLD.written_by_org, OLD.created_at) THEN
    RAISE EXCEPTION 'a fact''s content is immutable; only status and superseded_by may change'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
    RAISE EXCEPTION 'superseded_by is set once' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

-- On insert, "stated" must be visible in the fact's own evidence. Evidence is
-- written after the fact row, so the check is deferred; append_fact makes it
-- run as soon as the evidence is in, and a direct insert meets it at commit.
CREATE FUNCTION layers.period_stated_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.period_basis = 'stated' AND NOT EXISTS (
       SELECT 1 FROM layers.evidence e JOIN layers.excerpts x ON x.id = e.excerpt_id
        WHERE e.fact_id = NEW.id
          AND x.text ~ ('(^|[^0-9])' || extract(year FROM NEW.period_end)::int::text || '([^0-9]|$)')) THEN
    RAISE EXCEPTION 'invalid_input: period_basis stated, but % appears in none of the fact''s passages',
      extract(year FROM NEW.period_end)::int USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER facts_period_stated AFTER INSERT ON layers.facts
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION layers.period_stated_check();

-- append_fact, as in 0003, now taking period_basis (required with a period,
-- refused without one); "stated" is checked before it returns.
CREATE OR REPLACE FUNCTION layers.append_fact(p_fact jsonb, p_evidence jsonb, p_decision jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_org      text := layers.scope_org();
  v_layer    text := p_fact ->> 'layer';
  v_brand    text;
  v_id       text := p_fact ->> 'id';
  v_entity   text := p_fact ->> 'entity';
  v_key      text := p_fact ->> 'key';
  v_market   text := p_fact ->> 'market';
  v_market_basis text := p_fact ->> 'market_basis';
  v_value    jsonb := p_fact -> 'value';
  v_type     text;
  v_num      numeric;
  v_text     text;
  v_bool     boolean;
  v_ps       date := (p_fact ->> 'period_start')::date;
  v_pe       date := (p_fact ->> 'period_end')::date;
  v_dated    boolean;
  v_period_basis text := p_fact ->> 'period_basis';
  v_asked_contest boolean := coalesce((p_fact ->> 'contested')::boolean, false);
  v_did      text := p_decision ->> 'id';
  v_existing jsonb;
  v_match    text;
  v_version  integer;
  v_status   text;
  v_outcome  text;
  v_supersedes text;
  v_max_end  date;
  v_same_period boolean;
  v_rank     integer;
  v_dated_ids   text[];
  v_undated_ids text[];
  e          jsonb;
BEGIN
  -- The "stated" check waits until this fact's evidence is written, then runs
  -- before we return, so a false claim fails this one fact, not the transaction.
  SET CONSTRAINTS layers.facts_period_stated DEFERRED;
  -- ── inputs ─────────────────────────────────────────────────────────────────
  IF v_layer = 'brand' THEN
    v_brand := nullif(current_setting('napkin.brand', true), '');
    IF v_brand IS NULL THEN
      RAISE EXCEPTION 'missing_scope: a brand-layer write needs napkin.brand' USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  IF v_id IS NULL OR v_did IS NULL OR p_decision ->> 'kind' IS NULL THEN
    RAISE EXCEPTION 'invalid_input: fact.id, decision.id and decision.kind are required' USING ERRCODE = 'check_violation';
  END IF;
  IF p_fact ? 'org' OR p_fact ? 'brand' OR p_fact ? 'scope' THEN
    RAISE EXCEPTION 'invalid_input: scope comes from the transaction, never the body' USING ERRCODE = 'check_violation';
  END IF;
  IF jsonb_typeof(p_evidence) IS DISTINCT FROM 'array' OR jsonb_array_length(p_evidence) = 0 THEN
    RAISE EXCEPTION 'invalid_input: a fact needs at least one piece of evidence' USING ERRCODE = 'check_violation';
  END IF;
  IF v_market IS NULL OR v_market_basis IS NULL THEN
    RAISE EXCEPTION 'invalid_input: every fact names its market and market_basis (UNKNOWN + unknown when the place is not established)'
      USING ERRCODE = 'check_violation';
  END IF;
  IF (v_ps IS NULL) <> (v_pe IS NULL) THEN
    RAISE EXCEPTION 'invalid_input: period_start and period_end come together' USING ERRCODE = 'check_violation';
  END IF;
  v_dated := v_ps IS NOT NULL;
  IF v_dated AND v_period_basis NOT IN ('stated', 'inferred') OR v_dated AND v_period_basis IS NULL THEN
    RAISE EXCEPTION 'invalid_input: a dated fact says whether its period was stated or inferred (period_basis)'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT v_dated AND v_period_basis IS NOT NULL THEN
    RAISE EXCEPTION 'invalid_input: period_basis without a period' USING ERRCODE = 'check_violation';
  END IF;

  CASE jsonb_typeof(v_value)
    WHEN 'number'  THEN v_type := 'number';  v_num  := (v_value #>> '{}')::numeric;
    WHEN 'string'  THEN v_type := 'text';    v_text := v_value #>> '{}';
    WHEN 'boolean' THEN v_type := 'boolean'; v_bool := (v_value #>> '{}')::boolean;
    ELSE RAISE EXCEPTION 'invalid_input: value must be a number, string or boolean' USING ERRCODE = 'check_violation';
  END CASE;

  -- A fact is never less restricted than what it was read from (C2: no silent
  -- declassification). Checked on every append, corroborations included, so a
  -- caller's wrong licence is refused rather than ignored.
  SELECT max(layers.licence_rank(src.licence)) INTO v_rank
    FROM jsonb_array_elements(p_evidence) ev
    JOIN layers.excerpts x   ON x.id = ev ->> 'excerpt_id'
    JOIN layers.captures c   ON c.id = x.capture_id
    JOIN layers.sources  src ON src.id = c.source_id;
  IF layers.licence_rank(p_fact ->> 'licence') IS NULL THEN
    RAISE EXCEPTION 'invalid_input: licence is required (open, licensed-internal, client-confidential)'
      USING ERRCODE = 'check_violation';
  END IF;
  IF v_rank > layers.licence_rank(p_fact ->> 'licence') THEN
    RAISE EXCEPTION 'invalid_input: licence % is less restricted than its sources', p_fact ->> 'licence'
      USING ERRCODE = 'check_violation';
  END IF;

  -- ── the decision (the same id may justify several facts; never two bodies) ──
  SELECT d.body INTO v_existing FROM layers.decisions d WHERE d.id = v_did;
  IF v_existing IS NULL THEN
    INSERT INTO layers.decisions (id, kind, handler, action, rationale, cites, org, brand, body)
    VALUES (v_did, p_decision ->> 'kind', p_decision ->> 'handler', p_decision ->> 'action',
            p_decision ->> 'rationale',
            coalesce(ARRAY(SELECT jsonb_array_elements_text(p_decision -> 'cites')), '{}'),
            v_org, nullif(current_setting('napkin.brand', true), ''), p_decision);
  ELSIF v_existing IS DISTINCT FROM p_decision THEN
    RAISE EXCEPTION 'idempotency_conflict: decision % exists with another body', v_did USING ERRCODE = 'unique_violation';
  END IF;

  -- ── one writer per entity + key at a time (covers identity and versions) ──
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|', v_layer, v_org, v_brand, v_entity, v_key), 7331));

  -- ── corroboration: the same thing said again ──────────────────────────────
  SELECT f.id INTO v_match
    FROM layers.facts f
   WHERE f.layer = v_layer AND f.org IS NOT DISTINCT FROM (CASE WHEN v_layer = 'brand' THEN v_org END)
     AND f.brand IS NOT DISTINCT FROM v_brand AND f.entity = v_entity AND f.key = v_key
     AND f.market IS NOT DISTINCT FROM v_market
     AND f.status IN ('active', 'contested', 'history', 'undated')
     AND f.value_type = v_type
     AND f.value_num IS NOT DISTINCT FROM v_num AND f.value_text IS NOT DISTINCT FROM v_text
     AND f.value_bool IS NOT DISTINCT FROM v_bool
     AND (NOT v_dated OR (f.date_basis = 'period' AND f.period_start = v_ps AND f.period_end = v_pe))
   ORDER BY CASE f.status WHEN 'active' THEN 0 WHEN 'contested' THEN 1 WHEN 'history' THEN 2 ELSE 3 END,
            f.version DESC
   LIMIT 1;

  IF v_match IS NOT NULL THEN
    FOR e IN SELECT * FROM jsonb_array_elements(p_evidence) LOOP
      INSERT INTO layers.evidence (fact_id, excerpt_id, quote, quote_start, quote_end, decision_id)
      VALUES (v_match, e ->> 'excerpt_id', e ->> 'quote', (e ->> 'quote_start')::int,
              (e ->> 'quote_start')::int + length(e ->> 'quote'), v_did)
      ON CONFLICT DO NOTHING;
    END LOOP;
    RETURN jsonb_build_object('fact', v_match, 'outcome', 'corroborated');
  END IF;

  -- ── a new row: which status ────────────────────────────────────────────────
  SELECT coalesce(max(f.version), 0) + 1 INTO v_version
    FROM layers.facts f
   WHERE f.org IS NOT DISTINCT FROM (CASE WHEN v_layer = 'brand' THEN v_org END)
     AND f.brand IS NOT DISTINCT FROM v_brand AND f.entity = v_entity AND f.key = v_key;

  -- The identity's current rows, locked until commit.
  SELECT coalesce(array_agg(f.id ORDER BY f.id) FILTER (WHERE f.date_basis = 'period'), '{}'),
         coalesce(array_agg(f.id ORDER BY f.id) FILTER (WHERE f.date_basis = 'unknown'), '{}')
    INTO v_dated_ids, v_undated_ids
    FROM (SELECT f.id, f.date_basis
            FROM layers.facts f
           WHERE f.layer = v_layer AND f.org IS NOT DISTINCT FROM (CASE WHEN v_layer = 'brand' THEN v_org END)
             AND f.brand IS NOT DISTINCT FROM v_brand AND f.entity = v_entity AND f.key = v_key
             AND f.market IS NOT DISTINCT FROM v_market
             AND f.status IN ('active', 'contested')
             FOR UPDATE) f;

  IF NOT v_dated THEN
    IF cardinality(v_dated_ids) > 0 THEN
      v_status := 'undated'; v_outcome := 'undated';
    ELSIF cardinality(v_undated_ids) > 0 OR v_asked_contest THEN
      v_status := 'contested'; v_outcome := 'contested';
    ELSE
      v_status := 'active'; v_outcome := 'created';
    END IF;
  ELSE
    SELECT max(f.period_end),
           bool_or(f.period_start = v_ps AND f.period_end = v_pe)
      INTO v_max_end, v_same_period
      FROM layers.facts f WHERE f.id = ANY (v_dated_ids);
    IF v_max_end IS NULL THEN
      v_status := CASE WHEN v_asked_contest THEN 'contested' ELSE 'active' END;
      v_outcome := CASE WHEN v_asked_contest THEN 'contested' ELSE 'created' END;
    ELSIF v_same_period OR (v_asked_contest AND v_pe >= v_max_end) THEN
      v_status := 'contested'; v_outcome := 'contested';
    ELSIF v_pe > v_max_end THEN
      v_status := 'active'; v_outcome := 'superseded';
      SELECT f.id INTO v_supersedes FROM layers.facts f WHERE f.id = ANY (v_dated_ids)
       ORDER BY f.period_end DESC, f.version DESC LIMIT 1;
    ELSE
      v_status := 'history'; v_outcome := 'history';
    END IF;
  END IF;

  INSERT INTO layers.facts (id, layer, org, brand, entity, key, market, market_basis, version, value_type, value_num,
                            value_text, value_bool, unit, period_start, period_end, date_basis, period_basis, status,
                            supersedes, licence, method, decision_id, written_by_org)
  VALUES (v_id, v_layer, CASE WHEN v_layer = 'brand' THEN v_org END, v_brand, v_entity, v_key, v_market,
          v_market_basis, v_version, v_type, v_num, v_text, v_bool, p_fact ->> 'unit', v_ps, v_pe,
          CASE WHEN v_dated THEN 'period' ELSE 'unknown' END, v_period_basis, v_status, v_supersedes,
          p_fact ->> 'licence', p_fact ->> 'method', v_did, v_org);

  FOR e IN SELECT * FROM jsonb_array_elements(p_evidence) LOOP
    INSERT INTO layers.evidence (fact_id, excerpt_id, quote, quote_start, quote_end, decision_id)
    VALUES (v_id, e ->> 'excerpt_id', e ->> 'quote', (e ->> 'quote_start')::int,
            (e ->> 'quote_start')::int + length(e ->> 'quote'), v_did);
  END LOOP;
  SET CONSTRAINTS layers.facts_period_stated IMMEDIATE;
  SET CONSTRAINTS layers.facts_period_stated DEFERRED;

  -- ── move the rows the new one displaces ────────────────────────────────────
  IF v_outcome = 'superseded' THEN
    UPDATE layers.facts SET status = 'superseded', superseded_by = v_id WHERE id = ANY (v_dated_ids);
  ELSIF v_outcome = 'contested' AND v_dated THEN
    UPDATE layers.facts SET status = 'contested' WHERE id = ANY (v_dated_ids) AND status = 'active';
  ELSIF v_outcome = 'contested' THEN
    UPDATE layers.facts SET status = 'contested' WHERE id = ANY (v_undated_ids) AND status = 'active';
  END IF;
  -- A dated row now leads: an undated row still current steps back, kept and flagged.
  IF v_dated AND v_status IN ('active', 'contested') THEN
    UPDATE layers.facts SET status = 'undated' WHERE id = ANY (v_undated_ids);
  END IF;

  RETURN jsonb_build_object('fact', v_id, 'outcome', v_outcome);
END $$;
