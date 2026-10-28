"""Opt-in, source-scoped automatic retention of settled collector artifacts.

The policy is an operator assertion, not publisher authentication or a
per-capture review. This module never installs database roles or schema.
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from buildout_observation_artifact import (
    credential_label,
    invalid_number,
    sensitive_string,
    unique_object,
)
from buildout_observation_io import read_consistent
from cre_ingest import (
    SOURCE_TO_BROKERAGE,
    _postgres_uri_hostname,
    assert_expected_database_target,
    load_db_url,
    parse_postgres_uri_query,
)

from source_evidence import acquisition, cli, collector, events

SCHEMA = "cre-automatic-source-evidence-policy/v1"
SENSITIVE_KEYS = {"xamzsecuritytoken", "xgoogsecuritytoken", "securitytoken"}


def _transport_binding(url):
    parsed = urlsplit(url)
    host = _postgres_uri_hostname(parsed, url)
    pairs = parse_postgres_uri_query(parsed.query)
    transport_keys = {"sslmode", "sslrootcert", "gssencmode", "sslnegotiation", "sslsni",
                      "sslcertmode", "ssl_min_protocol_version", "ssl_max_protocol_version",
                      "channel_binding", "require_auth", "requiressl"}
    selected = [(k.lower(), v) for k, v in pairs if k.lower() in transport_keys]
    if len({k for k, _ in selected}) != len(selected):
        raise ValueError("duplicate database transport setting")
    settings = dict(selected)
    if host.startswith(("/", "@")):
        kind, ca_path, ca_sha = "unix_socket", None, None
    else:
        ca_path = settings.get("sslrootcert")
        if (settings.get("sslmode") != "verify-full" or settings.get("gssencmode") != "disable"
                or not ca_path or not Path(ca_path).is_absolute()):
            raise ValueError("remote automatic evidence requires verify-full, explicit CA and disabled GSS")
        ca = read_consistent(ca_path, 2 * 1024 * 1024)
        if not ca or len(ca) > 2 * 1024 * 1024:
            raise ValueError("database CA is missing or exceeds limit")
        kind, ca_sha = "tls_verify_full", hashlib.sha256(ca).hexdigest()
    binding = {"kind": kind, "ca_path": ca_path, "ca_sha256": ca_sha,
               "settings_sha256": hashlib.sha256(events.encoded(selected)).hexdigest()}
    return binding


def _refuse_credential_fields(value):
    pending = [value]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 500000:
            raise ValueError("automatic evidence inspection exceeds limit")
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and (credential_label(key) or sensitive_string(key) or re.sub(r"[^a-z0-9]", "", key.lower()) in SENSITIVE_KEYS):
                    raise ValueError("credential-labelled content requires separate handling")
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and sensitive_string(item):
            raise ValueError("credential-bearing content requires separate handling")


def configuration(policy_path, expected_sha, env_file, target, git_sha, sources):
    """Load and bind an explicit operator policy; no database connection occurs."""
    if not all((policy_path, expected_sha, env_file, target)):
        raise ValueError("automatic evidence requires policy, digest, env file and target")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or not re.fullmatch(r"[0-9a-f]{64}", target):
        raise ValueError("invalid evidence policy or target digest")
    policy_path, env_file = str(Path(policy_path).expanduser().absolute()), str(Path(env_file).expanduser().absolute())
    raw = _read_private(Path(policy_path), 16384)
    if len(raw) > 16384:
        raise ValueError("automatic evidence policy exceeds limit")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("automatic evidence policy digest mismatch")
    policy = json.loads(raw.decode(), object_pairs_hook=unique_object, parse_constant=invalid_number)
    expected_keys = {"schema_version", "purpose", "data_scope", "format", "collector_git_sha",
                     "target_sha256", "source_keys", "max_artifact_bytes", "max_records"}
    allowed = policy.get("source_keys") if isinstance(policy, dict) else None
    if (not isinstance(policy, dict) or set(policy) != expected_keys or policy["schema_version"] != SCHEMA
            or policy["purpose"] != "private_unverified_evidence" or policy["data_scope"] != "public_listing_source"
            or policy["format"] != "collector_artifact_v1" or policy["collector_git_sha"] != git_sha
            or policy["target_sha256"] != target or not isinstance(allowed, list) or not allowed
            or any(not isinstance(k, str) or k not in SOURCE_TO_BROKERAGE for k in allowed)
            or len(set(allowed)) != len(allowed)
            or any(k in {"jll", "jll-main", "jll-investor"} for k in allowed)
            or type(policy["max_artifact_bytes"]) is not int or not 1 <= policy["max_artifact_bytes"] <= events.MAX_ARTIFACT_BYTES
            or type(policy["max_records"]) is not int or not 0 <= policy["max_records"] <= 10000):
        raise ValueError("invalid automatic evidence policy")
    try:
        if len(_read_private(Path(env_file), 65536)) > 65536:
            raise ValueError("automatic evidence env file exceeds limit")
        url, _ = load_db_url(env_file)
        assert_expected_database_target(url, target)
        transport = _transport_binding(url)
    except SystemExit as exc:
        raise ValueError("automatic evidence database target unavailable or mismatched") from exc
    return {"policy_path": policy_path, "policy_sha256": expected_sha, "env_file": env_file,
            "target_sha256": target, "collector_git_sha": git_sha, "source_keys": allowed,
            "max_artifact_bytes": policy["max_artifact_bytes"], "max_records": policy["max_records"],
            "transport": transport}


def _verify_config(config, sources):
    checked = configuration(config["policy_path"], config["policy_sha256"], config["env_file"],
                            config["target_sha256"], config["collector_git_sha"], sources)
    if checked != config:
        raise ValueError("automatic evidence configuration changed")


def _read_private(path, limit):
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise RuntimeError("safe private file reading is unavailable")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600):
            raise ValueError("sealed evidence file is not private")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                    info.st_mode, info.st_uid, info.st_nlink)
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise ValueError("private evidence file changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_exact(path, payload, limit):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        existing = _read_private(path, limit)
        if existing != payload:
            raise ValueError("existing sealed evidence differs")
        return
    with os.fdopen(fd, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def mark_attempt(manifest, source, attempt, temporary_rel):
    """Mark a scoped collection before its worker can publish output."""
    config = manifest["config"].get("automatic_source_evidence")
    if not config or source not in config["source_keys"]:
        return
    number = attempt["number"]
    expected = Path("sources") / f"{source}.attempt-{number}.json.tmp"
    if type(number) is not int or number < 1 or Path(temporary_rel) != expected:
        raise ValueError("invalid automatic evidence attempt path")
    attempt["source_evidence"] = {
        "state": "awaiting_artifact", "temporary_path": str(expected),
        "rejected_path": str(Path("rejected") / f"{source}-attempt-{number}.json"),
    }


def recover_unsealed(run_dir, manifest, save, *, make_private):
    """Reconcile settled pre-seal attempts under the caller's owned lock.

    This must never run from a polling callback or while workers are active.
    Ownership and process settlement are established by the caller.
    """
    config = manifest["config"].get("automatic_source_evidence")
    if not config:
        return
    for source, checkpoint in manifest["sources"].items():
        if source not in config["source_keys"]:
            continue
        for attempt in checkpoint.get("attempts", []):
            state = attempt.get("source_evidence")
            if state is None:
                attempt["source_evidence"] = {"state": "refused", "reason": "missing_attempt_marker"}
                manifest["automatic_source_evidence_paused"] = True
                manifest["automatic_source_evidence_status"] = "incomplete"
                save(run_dir, manifest)
                continue
            if state.get("state") != "awaiting_artifact":
                continue
            number = attempt.get("number")
            temporary = str(Path("sources") / f"{source}.attempt-{number}.json.tmp")
            rejected = str(Path("rejected") / f"{source}-attempt-{number}.json")
            if (type(number) is not int or number < 1
                    or state.get("temporary_path") != temporary or state.get("rejected_path") != rejected):
                attempt["source_evidence"] = {"state": "refused", "reason": "attempt_marker_mismatch"}
                manifest["automatic_source_evidence_paused"] = True
                manifest["automatic_source_evidence_status"] = "incomplete"
                save(run_dir, manifest)
                continue
            paths = [run_dir / temporary, run_dir / rejected]
            existing = [path for path in paths if os.path.lexists(path)]
            if len(existing) > 1:
                attempt["source_evidence"] = {"state": "refused", "reason": "conflicting_attempt_artifacts"}
                manifest["automatic_source_evidence_paused"] = True
                manifest["automatic_source_evidence_status"] = "incomplete"
            elif not existing:
                attempt["source_evidence"] = {"state": "no_artifact"}
                manifest["automatic_source_evidence_status"] = "incomplete"
            else:
                try:
                    make_private(existing[0])
                    seal(run_dir, manifest, source, attempt, existing[0])
                except (OSError, ValueError, RuntimeError) as exc:
                    attempt["source_evidence"] = {"state": "refused", "reason": type(exc).__name__}
                    manifest["automatic_source_evidence_paused"] = True
                    manifest["automatic_source_evidence_status"] = "incomplete"
            save(run_dir, manifest)


def seal(run_dir, manifest, source, attempt, artifact):
    """Seal a private plan before an artifact can be moved or rejected; no DB."""
    config = manifest["config"].get("automatic_source_evidence")
    if not config or source not in config["source_keys"]:
        return
    if not artifact.exists():
        attempt["source_evidence"] = {"state": "no_artifact"}
        manifest["automatic_source_evidence_status"] = "incomplete"
        return
    try:
        _verify_config(config, manifest["config"]["sources"])
        if manifest.get("collector_git_dirty") is not False or manifest.get("collector_git_sha") != config["collector_git_sha"]:
            raise ValueError("collector identity mismatch")
        raw = _read_private(artifact, config["max_artifact_bytes"])
        if len(raw) > config["max_artifact_bytes"]:
            raise ValueError("automatic evidence artifact exceeds limit")
        original_sha = hashlib.sha256(raw).hexdigest()
        obj, numeric_literals = acquisition.decode_json(raw)
        _refuse_credential_fields(obj)
        wrapped = collector.convert(obj, source_key=source, numeric_literals=numeric_literals or None, policy_binding={
            "policySha256": config["policy_sha256"], "collectorGitSha": config["collector_git_sha"],
            "targetSha256": config["target_sha256"], "artifactFileSha256": original_sha,
            "transportSha256": events.sha256(events.encoded(config["transport"])),
            "generation": manifest["run_id"], "attempt": attempt["number"]})
        if len(wrapped["records"]) > config["max_records"]:
            raise ValueError("automatic evidence record limit exceeded")
        plan = events.plan(wrapped, source_key=source)
        events.validate_content_policy(plan)
        if len(events.encoded(plan)) + 1 > cli.MAX_PLAN_BYTES:
            raise ValueError("automatic evidence plan exceeds limit")
        directory = run_dir / "source-evidence"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("automatic evidence directory is not private")
        path = directory / f"{source}-attempt-{attempt['number']}.json"
        original = directory / f"{source}-attempt-{attempt['number']}-input.json"
        _write_exact(original, raw, config["max_artifact_bytes"])
        _write_exact(path, events.encoded(plan) + b"\n", cli.MAX_PLAN_BYTES)
        attempt["source_evidence"] = {"state": "pending", "path": str(path.relative_to(run_dir)),
                                      "input_path": str(original.relative_to(run_dir)),
                                      "plan_sha256": plan["plan_sha256"],
                                      "plan_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                      "artifact_sha256": original_sha, "capture_id": plan["events"][0]["capture_id"],
                                      "events": len(plan["events"]), "policy_sha256": config["policy_sha256"]}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        attempt["source_evidence"] = {"state": "refused", "reason": type(exc).__name__}
        manifest["automatic_source_evidence_paused"] = True
        manifest["automatic_source_evidence_status"] = "incomplete"


def prepare_resume(manifest, *, replay_unknown):
    """Only explicit exact-plan replay can rearm an uncertain transaction."""
    states = [attempt.get("source_evidence") for checkpoint in manifest["sources"].values()
              for attempt in checkpoint.get("attempts", []) if attempt.get("source_evidence")]
    uncertain = [state for state in states if state.get("state") in {"appending", "outcome_unknown"}]
    if uncertain and replay_unknown:
        if any(state.get("state") == "refused" for state in states):
            raise ValueError("refused evidence needs separate correction")
        for state in uncertain:
            state["state"] = "pending"
            state["exact_replay_required"] = True
            state.pop("reason", None)
        manifest["automatic_source_evidence_paused"] = False
    elif uncertain:
        manifest["automatic_source_evidence_paused"] = True
    elif replay_unknown:
        raise ValueError("no uncertain evidence transaction to replay")


def append_sql(plan):
    """Require a dedicated unprivileged login before assuming the writer role."""
    guard = """DO $evidence$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = session_user
   AND r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolcreaterole
   AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit)
   OR (SELECT count(*) FROM pg_catalog.pg_auth_members m
     JOIN pg_catalog.pg_roles member ON member.oid = m.member
     WHERE member.rolname = session_user) <> 1
   OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
     JOIN pg_catalog.pg_roles member ON member.oid = m.member
     JOIN pg_catalog.pg_roles granted ON granted.oid = m.roleid
     WHERE member.rolname = session_user AND granted.rolname = 'cre_source_evidence_writer'
       AND m.set_option AND NOT m.admin_option AND NOT m.inherit_option)
   OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = 'cre_source_evidence_writer'
     AND NOT r.rolcanlogin AND NOT r.rolinherit AND NOT r.rolsuper AND NOT r.rolcreaterole
     AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication)
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles writer ON writer.oid = m.member
     WHERE writer.rolname = 'cre_source_evidence_writer')
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d JOIN pg_catalog.pg_roles member ON member.oid = d.refobjid
     WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass AND member.rolname = session_user
       AND d.deptype IN ('a', 'o', 'i'))
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d JOIN pg_catalog.pg_roles writer ON writer.oid = d.refobjid
     WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
       AND writer.rolname = 'cre_source_evidence_writer' AND d.deptype IN ('a', 'o', 'i')
       AND NOT (d.deptype = 'a' AND d.dbid = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())
         AND ((d.classid = 'pg_catalog.pg_namespace'::pg_catalog.regclass
               AND d.objid = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'cre_source_evidence'))
           OR (d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
               AND d.objid = (SELECT p2.oid FROM pg_catalog.pg_proc p2 JOIN pg_catalog.pg_namespace n2 ON n2.oid = p2.pronamespace
                 WHERE n2.nspname = 'cre_source_evidence' AND p2.proname = 'append_event'
                   AND pg_catalog.oidvectortypes(p2.proargtypes) = 'text, integer, text, text, timestamp with time zone, text, jsonb, jsonb, text, text, text, text')))))
   OR (SELECT count(*) FROM pg_catalog.pg_namespace n,
       LATERAL pg_catalog.aclexplode(n.nspacl) a JOIN pg_catalog.pg_roles writer ON writer.oid = a.grantee
       WHERE n.nspname = 'cre_source_evidence' AND writer.rolname = 'cre_source_evidence_writer'
         AND a.privilege_type = 'USAGE' AND NOT a.is_grantable) <> 1
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n,
       LATERAL pg_catalog.aclexplode(n.nspacl) a JOIN pg_catalog.pg_roles writer ON writer.oid = a.grantee
       WHERE n.nspname = 'cre_source_evidence' AND writer.rolname = 'cre_source_evidence_writer'
         AND (a.privilege_type <> 'USAGE' OR a.is_grantable))
   OR (SELECT count(*) FROM pg_catalog.pg_proc p,
       LATERAL pg_catalog.aclexplode(p.proacl) a JOIN pg_catalog.pg_roles writer ON writer.oid = a.grantee
       WHERE p.pronamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'cre_source_evidence')
         AND p.proname = 'append_event' AND pg_catalog.oidvectortypes(p.proargtypes) = 'text, integer, text, text, timestamp with time zone, text, jsonb, jsonb, text, text, text, text'
         AND writer.rolname = 'cre_source_evidence_writer' AND a.privilege_type = 'EXECUTE' AND NOT a.is_grantable) <> 1
   OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p,
       LATERAL pg_catalog.aclexplode(p.proacl) a JOIN pg_catalog.pg_roles writer ON writer.oid = a.grantee
       WHERE p.pronamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'cre_source_evidence')
         AND p.proname = 'append_event' AND pg_catalog.oidvectortypes(p.proargtypes) = 'text, integer, text, text, timestamp with time zone, text, jsonb, jsonb, text, text, text, text'
         AND writer.rolname = 'cre_source_evidence_writer' AND (a.privilege_type <> 'EXECUTE' OR a.is_grantable))
   OR NOT pg_catalog.pg_has_role(session_user, 'cre_source_evidence_writer', 'SET') THEN
   RAISE EXCEPTION 'automatic evidence requires a dedicated restricted writer login';
 END IF;
END $evidence$;
"""
    return cli.append_sql(plan).replace("BEGIN;\n", "BEGIN;\n" + guard, 1)


def pump(run_dir, manifest, save):
    """Run pending writes on the caller's foreground lane, never a poll callback."""
    config = manifest["config"].get("automatic_source_evidence")
    if not config or manifest.get("automatic_source_evidence_paused"):
        return
    candidates = [(source, attempt, attempt.get("source_evidence") or {})
                  for source, checkpoint in manifest["sources"].items()
                  for attempt in checkpoint.get("attempts", [])]
    candidates.sort(key=lambda item: item[2].get("exact_replay_required") is not True)
    for source, attempt, state in candidates:
        if state.get("state") != "pending":
            continue
        try:
            _verify_config(config, manifest["config"]["sources"])
            if (source not in config["source_keys"] or state.get("policy_sha256") != config["policy_sha256"]
                    or state.get("path") != f"source-evidence/{source}-attempt-{attempt['number']}.json"
                    or state.get("input_path") != f"source-evidence/{source}-attempt-{attempt['number']}-input.json"):
                raise ValueError("sealed evidence path or policy mismatch")
            raw = _read_private(run_dir / state["path"], cli.MAX_PLAN_BYTES)
            if len(raw) > cli.MAX_PLAN_BYTES or hashlib.sha256(raw).hexdigest() != state["plan_file_sha256"]:
                raise ValueError("sealed evidence plan changed")
            plan = json.loads(raw.decode(), object_pairs_hook=unique_object, parse_constant=invalid_number)
            events.validate_content_policy(plan)
            if plan["source_key"] != source:
                raise ValueError("sealed evidence source mismatch")
            original = _read_private(run_dir / state["input_path"], config["max_artifact_bytes"])
            if len(original) > config["max_artifact_bytes"] or hashlib.sha256(original).hexdigest() != state["artifact_sha256"]:
                raise ValueError("sealed evidence original changed")
            obj, numeric_literals = acquisition.decode_json(original)
            _refuse_credential_fields(obj)
            wrapped = collector.convert(obj, source_key=source, numeric_literals=numeric_literals or None, policy_binding={
                "policySha256": config["policy_sha256"], "collectorGitSha": config["collector_git_sha"],
                "targetSha256": config["target_sha256"], "artifactFileSha256": state["artifact_sha256"],
                "transportSha256": events.sha256(events.encoded(config["transport"])),
                "generation": manifest["run_id"], "attempt": attempt["number"]})
            expected = events.plan(wrapped, source_key=source)
            if (source not in config["source_keys"] or len(wrapped["records"]) > config["max_records"]
                    or plan != expected or plan["plan_sha256"] != state["plan_sha256"]
                    or plan["events"][0]["capture_id"] != state["capture_id"]):
                raise ValueError("sealed evidence plan binding mismatch")
            state["state"] = "appending"
            save(run_dir, manifest)
            def validate_transport(url):
                if _transport_binding(url) != config["transport"]:
                    raise ValueError("automatic evidence transport changed")
            cli.run_psql(append_sql(plan), config["env_file"], config["target_sha256"],
                         validate_transport=validate_transport)
            _verify_config(config, manifest["config"]["sources"])
            state["state"] = "appended"
            state.pop("exact_replay_required", None)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit) as exc:
            state["state"] = "outcome_unknown" if state.get("state") == "appending" else "refused"
            state["reason"] = type(exc).__name__
            manifest["automatic_source_evidence_paused"] = True
            manifest["automatic_source_evidence_status"] = "incomplete"
        save(run_dir, manifest)
        if manifest.get("automatic_source_evidence_paused"):
            return
