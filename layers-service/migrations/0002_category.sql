-- 0002 category: the shared category layer (foundation-spec S3), napkin_category.
--
-- Facts here are shared by every agency. What stays private is WHY each was
-- written: a research decision can name the brand a campaign was for, and which
-- brands an agency works on is that agency's business. So facts are readable by
-- everyone, decisions only by the org that wrote them.

INSERT INTO layers.meta (key, value) VALUES ('kind', 'category');

-- Only category-layer facts, and never client material: a client-confidential
-- source belongs in that agency's own database.
ALTER TABLE layers.facts   ADD CONSTRAINT facts_category_only   CHECK (layer = 'category');
ALTER TABLE layers.sources ADD CONSTRAINT sources_not_confidential CHECK (licence <> 'client-confidential');
ALTER TABLE layers.facts   ADD CONSTRAINT facts_not_confidential   CHECK (licence <> 'client-confidential');

-- ── the category tree (Planner Research Taxonomy; seeded from taxonomy.json) ─

CREATE TABLE layers.verticals (
  code    text PRIMARY KEY CHECK (code ~ '^[a-z0-9_]+$'),
  name    text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}',
  ordinal integer NOT NULL
);

CREATE TABLE layers.categories (
  code        text PRIMARY KEY CHECK (code ~ '^[a-z0-9_]+\.[a-z0-9_]+$'),   -- <vertical>.<leaf>
  vertical    text NOT NULL REFERENCES layers.verticals (code),
  name        text NOT NULL,
  aliases     text[] NOT NULL DEFAULT '{}',
  regulated   boolean NOT NULL,
  provisional boolean NOT NULL,
  ordinal     integer NOT NULL
);

-- "The category layer rejects an unknown leaf" (Contract 3 §2.3).
CREATE FUNCTION layers.category_leaf_known() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.entity LIKE 'category/%'
     AND NOT EXISTS (SELECT 1 FROM layers.categories c WHERE 'category/' || c.code = NEW.entity) THEN
    RAISE EXCEPTION 'unknown_leaf: % is not in the category tree', NEW.entity USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER facts_leaf_known BEFORE INSERT ON layers.facts
  FOR EACH ROW EXECUTE FUNCTION layers.category_leaf_known();

-- ── row-level security ───────────────────────────────────────────────────────

ALTER TABLE layers.facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE layers.facts FORCE ROW LEVEL SECURITY;
CREATE POLICY facts_read   ON layers.facts FOR SELECT USING (true);
CREATE POLICY facts_write  ON layers.facts FOR INSERT WITH CHECK (written_by_org = layers.scope_org());
CREATE POLICY facts_status ON layers.facts FOR UPDATE USING (true) WITH CHECK (true);

ALTER TABLE layers.decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE layers.decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY decisions_own_org ON layers.decisions
  USING (org = layers.scope_org()) WITH CHECK (org = layers.scope_org());

GRANT SELECT ON layers.verticals, layers.categories TO __APP_ROLE__;
