-- 0008 retraction: taking a wrong fact out of use without deleting it.
--
-- Append-only means a fact's content never changes. It does not mean a wrong
-- fact must stay in use. A person (or a reviewed corrections file) retracts it:
--
--   status = retracted     the fact drops out of every current read, cannot be
--                          cited by a new cell version, and never corroborates
--   layers.retractions     who retracted it, why, and the decision, kept forever
--
-- When a retraction leaves a contest with a single value, that value is no
-- longer disputed and becomes active again.

ALTER TABLE layers.facts DROP CONSTRAINT facts_status_check;
ALTER TABLE layers.facts ADD CONSTRAINT facts_status_check
  CHECK (status IN ('active', 'contested', 'superseded', 'history', 'undated', 'retracted'));

CREATE TABLE layers.retractions (
  fact_id          text PRIMARY KEY REFERENCES layers.facts (id),
  reason           text NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
  decision_id      text NOT NULL REFERENCES layers.decisions (id),
  retracted_by_org text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER retractions_frozen BEFORE UPDATE OR DELETE ON layers.retractions
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();

-- A retraction is visible exactly when its fact is.
ALTER TABLE layers.retractions ENABLE ROW LEVEL SECURITY;
ALTER TABLE layers.retractions FORCE ROW LEVEL SECURITY;
CREATE POLICY retractions_follow_fact ON layers.retractions
  USING      (EXISTS (SELECT 1 FROM layers.facts f WHERE f.id = fact_id))
  WITH CHECK (EXISTS (SELECT 1 FROM layers.facts f WHERE f.id = fact_id));

CREATE FUNCTION layers.retract_fact(p_fact text, p_reason text, p_decision jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_org   text := layers.scope_org();
  v_f     layers.facts;
  v_did   text := p_decision ->> 'id';
  v_body  jsonb;
  v_left  text[];
BEGIN
  SELECT * INTO v_f FROM layers.facts WHERE id = p_fact;
  IF v_f.id IS NULL THEN
    RAISE EXCEPTION 'unknown_fact: %', p_fact USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF v_f.status = 'retracted' THEN
    RETURN jsonb_build_object('fact', p_fact, 'outcome', 'already_retracted');
  END IF;
  IF v_did IS NULL OR p_decision ->> 'kind' IS NULL OR nullif(btrim(p_reason), '') IS NULL THEN
    RAISE EXCEPTION 'invalid_input: a retraction needs a reason and a decision (id, kind)' USING ERRCODE = 'check_violation';
  END IF;
  SELECT body INTO v_body FROM layers.decisions WHERE id = v_did;
  IF v_body IS NULL THEN
    INSERT INTO layers.decisions (id, kind, handler, action, rationale, cites, org, brand, body)
    VALUES (v_did, p_decision ->> 'kind', p_decision ->> 'handler', p_decision ->> 'action', p_reason,
            ARRAY[p_fact], v_org, nullif(current_setting('napkin.brand', true), ''), p_decision);
  ELSIF v_body IS DISTINCT FROM p_decision THEN
    RAISE EXCEPTION 'idempotency_conflict: decision % exists with another body', v_did USING ERRCODE = 'unique_violation';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|', v_f.layer, v_f.org, v_f.brand, v_f.entity, v_f.key), 7331));
  INSERT INTO layers.retractions (fact_id, reason, decision_id, retracted_by_org) VALUES (p_fact, p_reason, v_did, v_org);
  UPDATE layers.facts SET status = 'retracted' WHERE id = p_fact;

  -- A contest left with one value is no longer a contest.
  SELECT array_agg(id) INTO v_left FROM layers.facts
   WHERE layer = v_f.layer AND org IS NOT DISTINCT FROM v_f.org AND brand IS NOT DISTINCT FROM v_f.brand
     AND entity = v_f.entity AND key = v_f.key AND market = v_f.market
     AND qualifier_norm IS NOT DISTINCT FROM v_f.qualifier_norm AND status = 'contested';
  IF cardinality(v_left) = 1 AND NOT EXISTS (
       SELECT 1 FROM layers.facts WHERE layer = v_f.layer AND org IS NOT DISTINCT FROM v_f.org
          AND brand IS NOT DISTINCT FROM v_f.brand AND entity = v_f.entity AND key = v_f.key
          AND market = v_f.market AND qualifier_norm IS NOT DISTINCT FROM v_f.qualifier_norm
          AND status = 'active') THEN
    UPDATE layers.facts SET status = 'active' WHERE id = v_left[1];
  END IF;
  RETURN jsonb_build_object('fact', p_fact, 'outcome', 'retracted');
END $$;

CREATE FUNCTION layers.stamp_retraction() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.retracted_by_org := layers.scope_org();
  RETURN NEW;
END $$;
CREATE TRIGGER retractions_stamp BEFORE INSERT ON layers.retractions
  FOR EACH ROW EXECUTE FUNCTION layers.stamp_retraction();

GRANT SELECT, INSERT ON layers.retractions TO __APP_ROLE__;
