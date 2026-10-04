-- 0002 agency: one agency's own database (foundation-spec S3), napkin_agency_<slug>.
--
-- The database is the agency boundary: another agency's service role cannot
-- even connect (provision-databases.sh). Inside it, brands are the boundary:
-- two competing brands can share an agency, so every brand-layer row is
-- visible only under exactly its (org, brand) scope, by forced row-level
-- security. The org stamped on each row must also match the agency this
-- database was built for: a cross-check on the deployment, in the spirit of
-- S8's tenant field, that catches a service pointed at the wrong database.

INSERT INTO layers.meta (key, value) VALUES ('kind', 'agency'), ('agency', '__AGENCY_ORG__');

ALTER TABLE layers.facts ADD CONSTRAINT facts_brand_only CHECK (layer = 'brand');

CREATE FUNCTION layers.scope_brand() RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
  v text := nullif(current_setting('napkin.brand', true), '');
BEGIN
  IF v IS NULL THEN
    RAISE EXCEPTION 'napkin.brand is not set for this transaction' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN v;
END $$;

CREATE FUNCTION layers.agency_matches() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  mine text := (SELECT value FROM layers.meta WHERE key = 'agency');
BEGIN
  IF NEW.org IS DISTINCT FROM mine OR layers.scope_org() IS DISTINCT FROM mine THEN
    RAISE EXCEPTION 'this database belongs to %, not %', mine, coalesce(NEW.org, layers.scope_org())
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER facts_agency_matches BEFORE INSERT ON layers.facts
  FOR EACH ROW EXECUTE FUNCTION layers.agency_matches();
CREATE TRIGGER decisions_agency_matches BEFORE INSERT ON layers.decisions
  FOR EACH ROW EXECUTE FUNCTION layers.agency_matches();

-- ── the brand roster and the agency's view of the house RAG ─────────────────

-- The org's display names for its brands (find_brands / note_brand). The roster
-- facts themselves (roster.categories.*, roster.client_org) are brand-layer facts.
CREATE TABLE layers.brands (
  ref        text PRIMARY KEY CHECK (ref ~ '^brand/[a-z0-9][a-z0-9._-]*$'),
  name       text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- The agency's overrides of house-corpus passages: "never show us this case",
-- or lifting that later. Append-only; the latest row per passage wins.
CREATE TABLE layers.rag_overrides (
  id          text PRIMARY KEY CHECK (id ~ '^ro_'),
  passage_uri text NOT NULL CHECK (passage_uri ~ '^passage://'),
  action      text NOT NULL CHECK (action IN ('exclude', 'include')),
  reason      text NOT NULL,
  decision_id text NOT NULL REFERENCES layers.decisions (id),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_overrides_passage ON layers.rag_overrides (passage_uri, created_at DESC);
CREATE TRIGGER rag_overrides_frozen BEFORE UPDATE OR DELETE ON layers.rag_overrides
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();

-- Only the latest action per passage counts; filtered here so callers cannot forget to.
CREATE VIEW layers.rag_exclusions WITH (security_invoker = true) AS
  SELECT o.passage_uri, o.reason, o.decision_id, o.created_at
    FROM (SELECT DISTINCT ON (passage_uri) * FROM layers.rag_overrides
           ORDER BY passage_uri, created_at DESC, id DESC) o
   WHERE o.action = 'exclude';

-- ── row-level security ───────────────────────────────────────────────────────

ALTER TABLE layers.facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE layers.facts FORCE ROW LEVEL SECURITY;
CREATE POLICY facts_scoped ON layers.facts
  USING      (org = layers.scope_org() AND brand = layers.scope_brand())
  WITH CHECK (org = layers.scope_org() AND brand = layers.scope_brand());

-- Evidence is visible exactly when its fact is.
ALTER TABLE layers.evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE layers.evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY evidence_follows_fact ON layers.evidence
  USING      (EXISTS (SELECT 1 FROM layers.facts f WHERE f.id = fact_id))
  WITH CHECK (EXISTS (SELECT 1 FROM layers.facts f WHERE f.id = fact_id));

-- A decision is visible under the brand it was written for; an org-wide one
-- (brand null, e.g. a roster change) to the whole org.
ALTER TABLE layers.decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE layers.decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY decisions_scoped ON layers.decisions
  USING      (org = layers.scope_org() AND (brand IS NULL OR brand = layers.scope_brand()))
  WITH CHECK (org = layers.scope_org() AND (brand IS NULL OR brand = layers.scope_brand()));

GRANT SELECT, INSERT ON layers.brands, layers.rag_overrides TO __APP_ROLE__;
GRANT UPDATE (name, updated_at) ON layers.brands TO __APP_ROLE__;
GRANT SELECT ON layers.rag_exclusions TO __APP_ROLE__;
