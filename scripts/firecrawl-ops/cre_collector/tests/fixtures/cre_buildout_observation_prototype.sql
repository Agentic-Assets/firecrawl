-- OFFLINE FIXTURE DRAFT ONLY. Not a production migration or approval to apply DDL.
-- The future production object, owner, permissions, retention, and rollout require
-- separate governance and review. This draft cannot run against a normal database.
BEGIN;
DO $$
BEGIN
  IF current_database() !~ '^cre_observation_fixture_[0-9a-f]{12}$'
     OR current_setting('cre.observation_fixture', true) IS DISTINCT FROM 'isolated-synthetic-only'
  THEN RAISE EXCEPTION 'observation prototype is restricted to the disposable fixture'; END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS cre_observation_fixture;
CREATE TABLE IF NOT EXISTS cre_observation_fixture.buildout_observations (
  source_key text NOT NULL,
  generation_id text NOT NULL,
  transaction_mode text NOT NULL CHECK (transaction_mode IN ('sale', 'lease')),
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  identity_state text NOT NULL CHECK (identity_state = 'unresolved'),
  source_native_id text,
  source_url text,
  observation jsonb NOT NULL,
  PRIMARY KEY (source_key, generation_id, transaction_mode, ordinal)
);
ALTER TABLE cre_observation_fixture.buildout_observations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON SCHEMA cre_observation_fixture FROM PUBLIC;
REVOKE ALL ON cre_observation_fixture.buildout_observations FROM PUBLIC;
COMMIT;
