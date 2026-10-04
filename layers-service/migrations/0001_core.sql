-- 0001 core: what every knowledge-layer database holds, category and agency alike.
--
-- A fact is only as good as what was said and when. So a fact never stores its
-- evidence as free text: it links to the exact span of a verbatim excerpt, the
-- excerpt belongs to one reading (capture) of one URL, and the capture carries
-- the dates: when the source was published and when we read it.
--
--   sources   one row per URL
--   captures  one row per time we read it: retrieved_at, published_at + basis, content hash
--   excerpts  the verbatim passages read in that capture (<= 1,500 chars)
--   facts     one row per fact version, typed value, the period it describes
--   evidence  fact <- the span of an excerpt that says it (checked here, not trusted)
--   decisions the decision behind every write, stored whole
--
-- Append-only: nothing is updated or deleted, except a fact's status and
-- superseded_by, which the append rule (0003) moves. Enforced by triggers, so
-- no caller can rewrite history, the owner included.
--
-- Scope never comes from a request body: the service sets it per transaction
-- with SET LOCAL napkin.org / napkin.brand, and rows read it from there.

CREATE SCHEMA layers;

CREATE TABLE layers.meta (
  key   text PRIMARY KEY,
  value text NOT NULL
);

-- Where a fact or cell applies. Every fact names one: a country (ISO 3166-1
-- alpha-2; the UK is GB), a region (EU), GLOBAL, or UNKNOWN when the place
-- could not be established. Seeded from data/geographies.json by the runner.
CREATE TABLE layers.geographies (
  code text PRIMARY KEY CHECK (code ~ '^[A-Z]{2}$' OR code IN ('EU', 'GLOBAL', 'UNKNOWN')),
  kind text NOT NULL CHECK (kind IN ('country', 'region', 'global', 'unknown')),
  name text,
  CHECK ((kind = 'country') = (code ~ '^[A-Z]{2}$' AND code <> 'EU'))
);

-- Which countries a region contains (IE is in the EU; GB is not).
CREATE TABLE layers.geography_members (
  region  text NOT NULL REFERENCES layers.geographies (code),
  country text NOT NULL REFERENCES layers.geographies (code),
  PRIMARY KEY (region, country)
);

CREATE TABLE layers.decisions (
  id         text PRIMARY KEY CHECK (id ~ '^d_' AND length(id) <= 128),
  kind       text NOT NULL,
  handler    text,
  action     text,
  rationale  text,
  cites      text[] NOT NULL DEFAULT '{}',
  org        text NOT NULL,                  -- the scope it was written under
  brand      text,
  body       jsonb NOT NULL,                 -- the decision whole, reasoning included
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE layers.sources (
  id           text PRIMARY KEY CHECK (id ~ '^src_'),
  uri          text NOT NULL UNIQUE,
  domain       text NOT NULL,
  publisher    text,
  tier         text NOT NULL CHECK (tier IN ('primary', 'secondary', 'tertiary', 'reviewer-verified')),
  licence      text NOT NULL CHECK (licence IN ('open', 'licensed-internal', 'client-confidential')),
  added_by_org text NOT NULL,
  added_at     timestamptz NOT NULL DEFAULT now()
);

-- One reading of a URL. Pages change, so the dates belong to the reading: a
-- page read in March and again in October is two captures, and March's quote
-- keeps March's dates.
CREATE TABLE layers.captures (
  id              text PRIMARY KEY CHECK (id ~ '^cap_'),
  source_id       text NOT NULL REFERENCES layers.sources (id),
  retrieved_at    timestamptz NOT NULL,
  published_at    date,
  published_basis text NOT NULL
                  CHECK (published_basis IN ('page_metadata', 'search_api', 'page_text', 'unknown')),
  title           text,
  content_sha256  text CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  decision_id     text REFERENCES layers.decisions (id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK ((published_at IS NULL) = (published_basis = 'unknown')),
  CHECK (published_at IS NULL OR published_at <= (retrieved_at AT TIME ZONE 'UTC')::date)
);
CREATE INDEX captures_source ON layers.captures (source_id, retrieved_at);

-- A verbatim passage as the capture read it. Short on purpose: enough context
-- to check what a quote meant, never a copy of the page.
CREATE TABLE layers.excerpts (
  id          text PRIMARY KEY CHECK (id ~ '^exc_'),
  capture_id  text NOT NULL REFERENCES layers.captures (id),
  ordinal     integer NOT NULL CHECK (ordinal >= 0),
  text        text NOT NULL CHECK (length(text) BETWEEN 1 AND 1500),
  text_sha256 text NOT NULL,
  UNIQUE (capture_id, ordinal)
);

CREATE TABLE layers.facts (
  id             text PRIMARY KEY CHECK (id ~ '^f_'),
  layer          text NOT NULL CHECK (layer IN ('brand', 'category')),
  org            text,                       -- brand layer only: the owning org
  brand          text,                       -- brand layer only
  entity         text NOT NULL CHECK (entity ~ '^(brand|org|category)/[a-z0-9][a-z0-9._-]*$'),
  key            text NOT NULL CHECK (key ~ '^[a-z0-9_]+(\.[a-z0-9_]+)*$'),
  -- The place the fact describes, always: a country, EU, GLOBAL, or UNKNOWN.
  -- Never empty: "no market" used to mean "applies everywhere", which served an
  -- EU or US figure to an Irish campaign as if it were Irish.
  market         text NOT NULL REFERENCES layers.geographies (code),
  market_basis   text NOT NULL CHECK (market_basis IN ('stated', 'inferred', 'unknown')),
  version        integer NOT NULL CHECK (version >= 1),
  value_type     text NOT NULL CHECK (value_type IN ('number', 'text', 'boolean')),
  value_num      numeric,
  value_text     text,
  value_bool     boolean,
  unit           text NOT NULL,
  -- The period the figure describes, as a range: "2025" is 2025-01-01..2025-12-31,
  -- "Q3 2025" is 2025-07-01..2025-09-30. Null both when the quote does not say.
  period_start   date,
  period_end     date,
  date_basis     text NOT NULL CHECK (date_basis IN ('period', 'unknown')),
  -- active | contested  current;      superseded  replaced by a later period;
  -- history  an earlier period, kept, never current;
  -- undated  no period, kept and flagged, never current while a dated row is.
  status         text NOT NULL
                 CHECK (status IN ('active', 'contested', 'superseded', 'history', 'undated')),
  supersedes     text REFERENCES layers.facts (id),
  superseded_by  text REFERENCES layers.facts (id),
  licence        text NOT NULL CHECK (licence IN ('open', 'licensed-internal', 'client-confidential')),
  method         text,
  decision_id    text NOT NULL REFERENCES layers.decisions (id),
  written_by_org text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK ((value_type = 'number')  = (value_num  IS NOT NULL)
     AND (value_type = 'text')    = (value_text IS NOT NULL)
     AND (value_type = 'boolean') = (value_bool IS NOT NULL)),
  CHECK ((date_basis = 'period') = (period_start IS NOT NULL AND period_end IS NOT NULL)),
  CHECK (date_basis = 'period' OR (period_start IS NULL AND period_end IS NULL)),
  CHECK (period_start <= period_end),
  CHECK (status <> 'undated' OR date_basis = 'unknown'),
  CHECK (status <> 'history' OR date_basis = 'period'),
  CHECK ((market_basis = 'unknown') = (market = 'UNKNOWN')),
  CHECK ((layer = 'brand') = (org IS NOT NULL AND brand IS NOT NULL)),
  CHECK (layer = 'brand' OR (org IS NULL AND brand IS NULL)),
  -- Versions count per entity + key across markets, so fact://…/<key>@<version>
  -- (which carries no market) names exactly one row.
  UNIQUE NULLS NOT DISTINCT (org, brand, entity, key, version)
);
CREATE INDEX facts_identity ON layers.facts (layer, org, brand, entity, key, market)
  WHERE status IN ('active', 'contested', 'undated');
CREATE INDEX facts_decision ON layers.facts (decision_id);

-- What was said: the span of an excerpt that states the fact.
CREATE TABLE layers.evidence (
  fact_id     text NOT NULL REFERENCES layers.facts (id),
  excerpt_id  text NOT NULL REFERENCES layers.excerpts (id),
  quote       text NOT NULL CHECK (length(quote) >= 1),
  quote_start integer NOT NULL CHECK (quote_start >= 0),   -- characters, 0-based
  quote_end   integer NOT NULL,
  decision_id text NOT NULL REFERENCES layers.decisions (id),
  added_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (quote_end = quote_start + length(quote)),
  PRIMARY KEY (fact_id, excerpt_id, quote_start)
);
CREATE INDEX evidence_excerpt ON layers.evidence (excerpt_id);

-- A write repeated with the same Idempotency-Key returns the first response.
-- The one table rows leave: entries older than 24 hours are pruned.
CREATE TABLE layers.idempotency (
  org         text NOT NULL,
  key         text NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
  body_sha256 text NOT NULL,
  status      integer NOT NULL,
  response    jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org, key)
);

-- ── integrity the database enforces itself ──────────────────────────────────

-- An excerpt's hash is computed here, never taken from the caller.
CREATE FUNCTION layers.excerpt_hash() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.text_sha256 := encode(sha256(convert_to(NEW.text, 'UTF8')), 'hex');
  RETURN NEW;
END $$;
CREATE TRIGGER excerpts_hash BEFORE INSERT ON layers.excerpts
  FOR EACH ROW EXECUTE FUNCTION layers.excerpt_hash();

-- A quote must be exactly the characters of its excerpt at the offsets given.
-- Checked on every insert, so no quote can enter paraphrased or misplaced.
CREATE FUNCTION layers.evidence_is_verbatim() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  passage text;
BEGIN
  SELECT e.text INTO passage FROM layers.excerpts e WHERE e.id = NEW.excerpt_id;
  IF passage IS NULL OR substr(passage, NEW.quote_start + 1, length(NEW.quote)) IS DISTINCT FROM NEW.quote THEN
    RAISE EXCEPTION 'quote is not verbatim at [%,%) of excerpt %', NEW.quote_start, NEW.quote_end, NEW.excerpt_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER evidence_verbatim BEFORE INSERT ON layers.evidence
  FOR EACH ROW EXECUTE FUNCTION layers.evidence_is_verbatim();

-- Append-only everywhere.
CREATE FUNCTION layers.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on layers.% is not allowed: the layers are append-only', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER decisions_frozen BEFORE UPDATE OR DELETE ON layers.decisions
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER sources_frozen BEFORE UPDATE OR DELETE ON layers.sources
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER captures_frozen BEFORE UPDATE OR DELETE ON layers.captures
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER excerpts_frozen BEFORE UPDATE OR DELETE ON layers.excerpts
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER evidence_frozen BEFORE UPDATE OR DELETE ON layers.evidence
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();
CREATE TRIGGER facts_no_delete BEFORE DELETE ON layers.facts
  FOR EACH ROW EXECUTE FUNCTION layers.refuse_change();

-- A fact's content never changes; only status and superseded_by move, and
-- superseded_by is set once.
CREATE FUNCTION layers.facts_only_status_moves() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.id, NEW.layer, NEW.org, NEW.brand, NEW.entity, NEW.key, NEW.market, NEW.market_basis, NEW.version,
      NEW.value_type, NEW.value_num, NEW.value_text, NEW.value_bool, NEW.unit,
      NEW.period_start, NEW.period_end, NEW.date_basis, NEW.supersedes, NEW.licence, NEW.method,
      NEW.decision_id, NEW.written_by_org, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.layer, OLD.org, OLD.brand, OLD.entity, OLD.key, OLD.market, OLD.market_basis, OLD.version,
      OLD.value_type, OLD.value_num, OLD.value_text, OLD.value_bool, OLD.unit,
      OLD.period_start, OLD.period_end, OLD.date_basis, OLD.supersedes, OLD.licence, OLD.method,
      OLD.decision_id, OLD.written_by_org, OLD.created_at) THEN
    RAISE EXCEPTION 'a fact''s content is immutable; only status and superseded_by may change'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
    RAISE EXCEPTION 'superseded_by is set once' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER facts_immutable BEFORE UPDATE ON layers.facts
  FOR EACH ROW EXECUTE FUNCTION layers.facts_only_status_moves();

CREATE FUNCTION layers.refuse_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'TRUNCATE on layers.% is not allowed', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER facts_no_truncate BEFORE TRUNCATE ON layers.facts
  FOR EACH STATEMENT EXECUTE FUNCTION layers.refuse_truncate();
CREATE TRIGGER evidence_no_truncate BEFORE TRUNCATE ON layers.evidence
  FOR EACH STATEMENT EXECUTE FUNCTION layers.refuse_truncate();
CREATE TRIGGER decisions_no_truncate BEFORE TRUNCATE ON layers.decisions
  FOR EACH STATEMENT EXECUTE FUNCTION layers.refuse_truncate();

-- The scope the service set for this transaction. Missing scope is an error,
-- never "everything".
CREATE FUNCTION layers.scope_org() RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
  v text := nullif(current_setting('napkin.org', true), '');
BEGIN
  IF v IS NULL THEN
    RAISE EXCEPTION 'napkin.org is not set for this transaction' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN v;
END $$;

-- The geographies whose facts apply to a campaign in p_market, nearest first:
-- the country itself (reach 0), the regions it belongs to (1), GLOBAL (2).
-- UNKNOWN applies to nothing. IE -> IE, EU, GLOBAL; GB -> GB, GLOBAL.
CREATE FUNCTION layers.applicable_markets(p_market text)
RETURNS TABLE (code text, reach integer) LANGUAGE sql STABLE AS $$
  SELECT p_market, 0 WHERE p_market <> 'UNKNOWN'
  UNION ALL
  SELECT m.region, 1 FROM layers.geography_members m WHERE m.country = p_market
  UNION ALL
  SELECT 'GLOBAL', 2 WHERE p_market NOT IN ('GLOBAL', 'UNKNOWN')
$$;

-- ── what the layers service's role may do ───────────────────────────────────
-- It owns nothing, so the row-level security of the kind migrations applies to it.

GRANT USAGE ON SCHEMA layers TO __APP_ROLE__;
GRANT SELECT ON ALL TABLES IN SCHEMA layers TO __APP_ROLE__;
GRANT INSERT ON layers.decisions, layers.sources, layers.captures, layers.excerpts,
               layers.facts, layers.evidence, layers.idempotency TO __APP_ROLE__;
GRANT UPDATE (status, superseded_by) ON layers.facts TO __APP_ROLE__;
GRANT DELETE ON layers.idempotency TO __APP_ROLE__;
