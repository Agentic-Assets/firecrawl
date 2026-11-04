-- DRAFT ONLY. Part of AGENTIC-2895. Separate owner approval and production DDL
-- approval required. No collector integration. This marker is a tripwire, not authorization.
BEGIN;
DO $gate$
BEGIN
  IF current_database() !~ '^cre_store_fixture_[0-9a-f]{12}$' OR
     current_setting('cre_observation_store.approved_draft', true) IS DISTINCT FROM 'fixture-only' THEN
    RAISE EXCEPTION 'draft disabled; ownership and production approval required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cre_buildout_observation_writer'
                 AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                 AND NOT rolreplication AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'restricted writer group must be provisioned separately';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cre_buildout_observation_owner'
                 AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                 AND NOT rolreplication AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'restricted owner must be provisioned separately';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
             WHERE r.rolname IN ('cre_buildout_observation_writer', 'cre_buildout_observation_owner')) THEN
    RAISE EXCEPTION 'fixture roles must not inherit other roles';
  END IF;
END
$gate$;
-- Never take over or modify an existing schema, table, function, or ACL.
CREATE SCHEMA cre_buildout_observation AUTHORIZATION cre_buildout_observation_owner;
REVOKE ALL ON SCHEMA cre_buildout_observation FROM PUBLIC;
CREATE TABLE cre_buildout_observation.capture_pages (
  capture_id text NOT NULL CHECK (capture_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  capture_page integer NOT NULL CHECK (capture_page BETWEEN 0 AND 1000000),
  assertions jsonb NOT NULL CHECK (jsonb_typeof(assertions) = 'object' AND octet_length(assertions::text) <= 16384),
  provenance jsonb NOT NULL CHECK (jsonb_typeof(provenance) = 'object' AND octet_length(provenance::text) <= 16384
    AND provenance->>'sourceAuthorization' IS NOT DISTINCT FROM 'not_evaluated'
    AND provenance->>'sourceCompleteness' IS NOT DISTINCT FROM 'unverified'),
  original_sha256 text NOT NULL CHECK (original_sha256 ~ '^[0-9a-f]{64}'),
  original_bytes_length bigint NOT NULL CHECK (original_bytes_length BETWEEN 0 AND 16777216),
  artifact_bytes bytea NOT NULL CHECK (octet_length(artifact_bytes) <= 16777216),
  artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}' AND artifact_sha256 = encode(sha256(artifact_bytes), 'hex')),
  observations jsonb NOT NULL CHECK (jsonb_typeof(observations) = 'array' AND jsonb_array_length(observations) <= 1000 AND octet_length(observations::text) <= 16777216),
  received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (capture_id, capture_page)
);
ALTER TABLE cre_buildout_observation.capture_pages OWNER TO cre_buildout_observation_owner;
REVOKE ALL ON TABLE cre_buildout_observation.capture_pages FROM PUBLIC, cre_buildout_observation_writer;
CREATE FUNCTION cre_buildout_observation.append_page(
  p_capture_id text, p_capture_page integer, p_assertions jsonb, p_provenance jsonb,
  p_original_sha256 text, p_original_bytes_length bigint, p_artifact_bytes bytea, p_artifact_sha256 text, p_observations jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $body$
DECLARE
  existing cre_buildout_observation.capture_pages%ROWTYPE;
BEGIN
  -- Refuse bounded inputs before UTF-8 decoding and JSON parsing.
  IF p_artifact_bytes IS NULL OR octet_length(p_artifact_bytes) > 16777216
     OR p_observations IS NULL OR pg_column_size(p_observations) > 16777216
     OR p_assertions IS NULL OR pg_column_size(p_assertions) > 16384
     OR p_provenance IS NULL OR pg_column_size(p_provenance) > 16384 THEN
    RAISE EXCEPTION 'observation input exceeds bounds or is null';
  END IF;
  IF (convert_from(p_artifact_bytes, 'UTF8')::jsonb -> 'records') IS DISTINCT FROM p_observations THEN
    RAISE EXCEPTION 'observation records do not match artifact';
  END IF;
  INSERT INTO cre_buildout_observation.capture_pages
    (capture_id, capture_page, assertions, provenance, original_sha256, original_bytes_length, artifact_bytes, artifact_sha256, observations)
  VALUES (p_capture_id, p_capture_page, p_assertions, p_provenance, p_original_sha256, p_original_bytes_length, p_artifact_bytes, p_artifact_sha256, p_observations)
  ON CONFLICT (capture_id, capture_page) DO NOTHING;
  IF FOUND THEN RETURN 'inserted'; END IF;
  SELECT * INTO STRICT existing FROM cre_buildout_observation.capture_pages
    WHERE capture_id = p_capture_id AND capture_page = p_capture_page;
  IF existing.assertions IS DISTINCT FROM p_assertions OR existing.provenance IS DISTINCT FROM p_provenance
     OR existing.original_sha256 IS DISTINCT FROM p_original_sha256 OR existing.original_bytes_length IS DISTINCT FROM p_original_bytes_length
     OR existing.artifact_bytes IS DISTINCT FROM p_artifact_bytes OR existing.artifact_sha256 IS DISTINCT FROM p_artifact_sha256
     OR existing.observations IS DISTINCT FROM p_observations THEN
    RAISE EXCEPTION 'divergent capture page replay';
  END IF;
  RETURN 'unchanged';
END
$body$;
ALTER FUNCTION cre_buildout_observation.append_page(text, integer, jsonb, jsonb, text, bigint, bytea, text, jsonb) OWNER TO cre_buildout_observation_owner;
REVOKE ALL ON FUNCTION cre_buildout_observation.append_page(text, integer, jsonb, jsonb, text, bigint, bytea, text, jsonb) FROM PUBLIC;
GRANT USAGE ON SCHEMA cre_buildout_observation TO cre_buildout_observation_writer;
GRANT EXECUTE ON FUNCTION cre_buildout_observation.append_page(text, integer, jsonb, jsonb, text, bigint, bytea, text, jsonb) TO cre_buildout_observation_writer;
COMMIT;
