#!/usr/bin/env python3
"""Plan or explicitly apply evidence-backed CRE lifecycle reconciliation.

Dry-run is the default. Input is an offline JSON evidence bundle with ``rows``;
each row contains listing/index before-state plus ``source`` evidence. Source
evidence must name a collector artifact whose bytes match the supplied SHA-256,
whose successful source pass proves the requested identity/state, and whose
``runMeta.finishedAt`` is fresh against the applying host's current UTC clock,
and state ``present`` or ``absent``. A missing lifecycle event is repaired only
when ``event_repair`` explicitly identifies the absent event and acknowledges
that the source observation is not the historical transition time. A history
mismatch is repaired only when the source supplies every tracked value and a
named authority decision selects the resolution. Skipped rows remain visible
in the plan.

Apply requires all of: ``--apply``, a private mode-0600 operator/reference
contract bound to the plan hash, an exact plan-specific confirmation, and the
expected DB-target fingerprint.
Each bounded transaction locks rows and verifies the unchanged before-state hash
before mutation. This script never infers truth from inconsistent DB rows alone.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cre_ingest import (
    assert_expected_database_target,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
    sql_lit,
    to_row,
)

MAX_BATCH_SIZE = 250
ALLOWED_OPERATORS = {"cayman", "stace"}
APPROVAL_REF_RE = re.compile(r"^AGENTIC-[0-9]+$")
_UUID_NAMESPACE = uuid.UUID("c245ab8a-7397-5c20-920e-3bd852242c72")
_EVENT_TIME_SEMANTICS = "source_state_observed_at_not_transition_time"
_HISTORY_TIME_SEMANTICS = "source_evidence_observed_at"
_SOURCE_AUTHORITY = "source_values_are_authoritative"
_HISTORY_RESOLUTIONS = {
    "append_source_backed_history",
    "update_current_from_source_and_append_history",
}
_BEFORE_KEYS = (
    "listing_deleted", "listing_status", "index_soft_deleted",
    "observation_present", "presence_generation",
)
_TRACKED_FIELDS = (
    "sale_price_usd",
    "sale_price_per_sf",
    "lease_rate_min",
    "lease_rate_max",
    "status",
    "cap_rate",
)


def _parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def before_hash(row):
    values = []
    for key in _BEFORE_KEYS:
        value = row.get(key)
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif value is None:
            value = ""
        values.append(str(value))
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def _skip(raw, reason):
    return {
        "listing_id": raw.get("listing_id"),
        "brokerage_id": raw.get("brokerage_id"),
        "external_id": raw.get("external_id"),
        "reason": reason,
    }


def _valid_tracked_values(values):
    if not isinstance(values, dict) or set(values) != set(_TRACKED_FIELDS):
        return False
    for field in _TRACKED_FIELDS:
        value = values[field]
        if field == "status":
            if value is not None and not isinstance(value, str):
                return False
        elif value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return False
    return True


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bound_artifact(raw, source, *, evidence_base_dir, now, max_age_hours):
    """Bind a claim to collector bytes and return normalized proof metadata."""
    evidence_path = source.get("evidence_path")
    expected_sha = source.get("evidence_sha256")
    if not isinstance(evidence_path, str) or not evidence_path.strip():
        return None, "missing_source_evidence_path"
    candidate = Path(evidence_path)
    if not candidate.is_absolute():
        candidate = Path(evidence_base_dir) / candidate
    if candidate.is_symlink() or not candidate.is_file():
        return None, "missing_or_unsafe_source_evidence_artifact"
    actual_sha = _sha256_file(candidate)
    if actual_sha != expected_sha:
        return None, "source_evidence_hash_mismatch"
    try:
        with open(candidate, encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid_source_evidence_artifact"
    run_meta = artifact.get("runMeta") if isinstance(artifact, dict) else None
    if not isinstance(run_meta, dict) or run_meta.get("mode") != "full":
        return None, "source_evidence_not_full_crawl"
    try:
        finished = _parse_time(run_meta.get("finishedAt"))
        claimed = _parse_time(source.get("observed_at"))
    except (TypeError, ValueError):
        return None, "missing_or_invalid_observed_at"
    if finished != claimed:
        return None, "source_observation_does_not_match_artifact_completion"
    age = (now - finished).total_seconds() / 3600
    if age < 0 or age > max_age_hours:
        return None, "stale_source_evidence"

    source_key = source.get("source_key")
    passes = [
        entry for entry in artifact.get("sources") or []
        if isinstance(entry, dict) and entry.get("sourceKey") == source_key
    ]
    if not passes or any(entry.get("error") or entry.get("truncated") for entry in passes):
        return None, "source_evidence_pass_incomplete"
    brokers_by_idx = {i: broker for i, broker in enumerate(artifact.get("brokers") or [])}
    identities = {}
    for listing in artifact.get("listings") or []:
        if not isinstance(listing, dict) or listing.get("sourceKey") != source_key:
            continue
        row = to_row(listing, brokers_by_idx, run_meta["finishedAt"])
        if row is not None:
            identities.setdefault(row["external_id"], []).append(row)
    matches = identities.get(str(raw.get("external_id")), [])
    if source.get("state") == "present":
        if len(matches) != 1:
            return None, "source_identity_not_uniquely_present_in_artifact"
        if matches[0].get("source_url") != source.get("url"):
            return None, "source_url_does_not_match_artifact"
    elif matches:
        return None, "source_identity_present_in_claimed_absent_artifact"
    return {
        "path": str(candidate.resolve()),
        "sha256": actual_sha,
        "finished_at": run_meta["finishedAt"],
    }, None


def _history_request(raw, source, desired_present):
    request = raw.get("history_repair")
    if request is None:
        return None, None
    if not isinstance(request, dict):
        return None, "invalid_history_repair"
    resolution = request.get("resolution")
    authority = request.get("authority_decision")
    if resolution not in _HISTORY_RESOLUTIONS:
        return None, "unsupported_history_resolution"
    if not isinstance(authority, dict):
        return None, "missing_history_authority_decision"
    if authority.get("decision") != _SOURCE_AUTHORITY:
        return None, "history_authority_rejected"
    if not str(authority.get("decided_by") or "").strip() or not str(
        authority.get("reason") or ""
    ).strip():
        return None, "incomplete_history_authority_decision"

    source_values = source.get("tracked_values")
    current_values = raw.get("current_tracked_values")
    latest = raw.get("latest_history")
    if not _valid_tracked_values(source_values):
        return None, "incomplete_source_tracked_values"
    if not _valid_tracked_values(current_values):
        return None, "incomplete_current_tracked_values"
    if current_values["status"] != raw.get("listing_status"):
        return None, "current_status_snapshot_mismatch"
    if not isinstance(latest, dict) or not all(latest.get(key) for key in ("id", "observed_at")):
        return None, "incomplete_latest_history_snapshot"
    if not _valid_tracked_values(latest.get("tracked_values")):
        return None, "incomplete_latest_history_snapshot"
    try:
        _parse_time(latest["observed_at"])
    except (TypeError, ValueError):
        return None, "invalid_latest_history_observed_at"
    if desired_present and source_values["status"] == "inactive":
        return None, "source_values_conflict_with_presence"
    if not desired_present and source_values["status"] != "inactive":
        return None, "source_values_conflict_with_absence"
    if resolution == "append_source_backed_history" and current_values != source_values:
        return None, "append_history_requires_source_current_agreement"

    return {
        "resolution": resolution,
        "authority_decision": {
            "decision": _SOURCE_AUTHORITY,
            "decided_by": str(authority["decided_by"]).strip(),
            "reason": str(authority["reason"]).strip(),
        },
        "observed_at": source["observed_at"],
        "observed_at_semantics": _HISTORY_TIME_SEMANTICS,
        "source_values": {field: source_values[field] for field in _TRACKED_FIELDS},
        "current_values": {field: current_values[field] for field in _TRACKED_FIELDS},
        "latest_before": {
            "id": latest["id"],
            "observed_at": latest["observed_at"],
            "tracked_values": {
                field: latest["tracked_values"][field] for field in _TRACKED_FIELDS
            },
        },
    }, None


def _event_request(raw, desired_present, desired_generation):
    request = raw.get("event_repair")
    if request is None:
        return None, None
    if not isinstance(request, dict):
        return None, "invalid_event_repair"
    expected_type = "reappeared" if desired_present else "disappeared"
    if request.get("missing") is not True:
        return None, "event_not_declared_missing"
    if request.get("event_type") != expected_type:
        return None, "event_type_conflicts_with_source_state"
    if request.get("presence_generation") != desired_generation:
        return None, "event_generation_conflicts_with_source_index"
    if request.get("time_semantics") != _EVENT_TIME_SEMANTICS:
        return None, "event_time_semantics_not_acknowledged"
    return {
        "event_type": expected_type,
        "presence_generation": desired_generation,
        "provenance": "approved_missing_event_backfill",
        "evidence_observed_at_semantics": _EVENT_TIME_SEMANTICS,
        "require_reconciliation_provenance": True,
    }, None


def build_plan(bundle, *, evidence_base_dir=".", max_age_hours=48):
    """Deterministic planner whose freshness is anchored to the current clock."""
    now = datetime.now(timezone.utc)
    actions = []
    skipped = []
    rows = bundle.get("rows") or []
    identity_counts = {}
    for raw in rows:
        identity = (raw.get("brokerage_id"), raw.get("external_id"))
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    for raw in sorted(rows, key=lambda r: (
        str(r.get("brokerage_id", "")), str(r.get("external_id", "")),
    )):
        identity = (raw.get("brokerage_id"), raw.get("external_id"))
        reason = None
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        if not all(raw.get(k) for k in ("listing_id", "brokerage_id", "external_id")):
            reason = "incomplete_identity"
        elif identity_counts[identity] > 1 or raw.get("ambiguous") or source.get("ambiguous"):
            reason = "ambiguous_identity_or_evidence"
        elif source.get("complete") is not True:
            reason = "incomplete_source_evidence"
        elif source.get("state") not in {"present", "absent"}:
            reason = "unsupported_source_state"
        elif not source.get("source_key") or not source.get("url"):
            reason = "missing_source_provenance"
        elif not isinstance(source.get("evidence_sha256"), str) or not all((
            len(source["evidence_sha256"]) == 64,
            all(char in "0123456789abcdef" for char in source["evidence_sha256"]),
        )):
            reason = "missing_or_invalid_source_evidence_hash"
        else:
            bound_artifact, reason = _validate_bound_artifact(
                raw, source, evidence_base_dir=evidence_base_dir,
                now=now, max_age_hours=max_age_hours,
            )
        if reason:
            skipped.append(_skip(raw, reason))
            continue

        desired_present = source["state"] == "present"
        desired_status = raw.get("listing_status")
        if desired_present and raw.get("listing_deleted") and desired_status == "inactive":
            desired_status = "active"
        elif not desired_present:
            desired_status = "inactive"
        lifecycle_change = any((
            bool(raw.get("listing_deleted")) == desired_present,
            bool(raw.get("index_soft_deleted")) == desired_present,
            bool(raw.get("observation_present")) != desired_present,
            raw.get("listing_status") != desired_status,
        ))
        generation = int(raw.get("presence_generation") or 0)
        if bool(raw.get("observation_present")) != desired_present:
            generation += 1

        history, reason = _history_request(raw, source, desired_present)
        if reason:
            skipped.append(_skip(raw, reason))
            continue
        if history is not None:
            desired_status = history["source_values"]["status"]
        event, reason = _event_request(raw, desired_present, generation)
        if reason:
            skipped.append(_skip(raw, reason))
            continue
        if lifecycle_change and event is None:
            event = {
                "event_type": "reappeared" if desired_present else "disappeared",
                "presence_generation": generation,
                "provenance": "approved_lifecycle_state_reconciliation",
                "evidence_observed_at_semantics": _EVENT_TIME_SEMANTICS,
                "require_reconciliation_provenance": False,
            }
        history_needed = history is not None and any((
            history["current_values"] != history["source_values"],
            history["latest_before"]["tracked_values"] != history["source_values"],
        ))
        if not lifecycle_change and event is None and not history_needed:
            skipped.append(_skip(raw, "already_consistent"))
            continue
        if history is not None and not history_needed:
            history = None

        operations = []
        if lifecycle_change:
            operations.append("lifecycle_sync")
        if event is not None:
            operations.append("event_backfill" if raw.get("event_repair") else "lifecycle_event")
        if history is not None:
            operations.append("history_alignment")
        action = {
            "listing_id": raw["listing_id"],
            "brokerage_id": raw["brokerage_id"],
            "external_id": raw["external_id"],
            "source_key": source.get("source_key"),
            "source_url": source.get("url"),
            "source_evidence_sha256": source["evidence_sha256"],
            "source_evidence_path": bound_artifact["path"],
            "observed_at": source["observed_at"],
            "operations": operations,
            "lifecycle_change": lifecycle_change,
            "desired_present": desired_present,
            "desired_status": desired_status,
            "desired_generation": generation,
            "event": event,
            "history": history,
            "before": {key: raw.get(key) for key in _BEFORE_KEYS},
            "before_hash": before_hash(raw),
        }
        actions.append(action)
    core = {"schema_version": 3, "actions": actions, "skipped": skipped}
    core["plan_hash"] = hashlib.sha256(_canon(core).encode()).hexdigest()
    return core


def write_evidence(plan, prefix):
    """Write byte-stable JSON and CSV plan evidence."""
    json_path = prefix + ".json"
    csv_path = prefix + ".csv"
    os.makedirs(os.path.dirname(os.path.abspath(prefix)), exist_ok=True)
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(plan, sort_keys=True, indent=2) + "\n")
    fields = (
        "outcome", "reason", "listing_id", "brokerage_id", "external_id", "operations",
        "desired_present", "desired_status", "desired_generation", "observed_at",
        "source_evidence_path", "source_evidence_sha256",
        "event_type", "event_provenance", "event_time_semantics", "history_resolution",
        "history_authority", "history_decided_by", "history_time_semantics", "before_hash",
    )
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in plan["actions"]:
            event = row.get("event") or {}
            history = row.get("history") or {}
            authority = history.get("authority_decision") or {}
            writer.writerow({
                "outcome": "action",
                "reason": "",
                **row,
                "operations": "|".join(row["operations"]),
                "event_type": event.get("event_type"),
                "event_provenance": event.get("provenance"),
                "event_time_semantics": event.get("evidence_observed_at_semantics"),
                "history_resolution": history.get("resolution"),
                "history_authority": authority.get("decision"),
                "history_decided_by": authority.get("decided_by"),
                "history_time_semantics": history.get("observed_at_semantics"),
            })
        writer.writerows({"outcome": "skip", **row} for row in plan["skipped"])
    return json_path, csv_path


def _pg_bool(value):
    return "true" if value else "false"


def _sql_value(field, value):
    if value is None:
        return "NULL"
    if field == "status":
        return sql_lit(value)
    return f"{sql_lit(value)}::numeric"


def _tracked_predicate(alias, values):
    return " AND ".join(
        f"{alias}.{field} IS NOT DISTINCT FROM {_sql_value(field, values[field])}"
        for field in _TRACKED_FIELDS
    )


def _job_identity(plan_hash, brokerage_id):
    run_key = "reconcile:v3:" + plan_hash
    return run_key, str(uuid.uuid5(_UUID_NAMESPACE, f"{run_key}:{brokerage_id}"))


def build_apply_sql(actions, plan_hash, operator, approval_ref):
    """Build one bounded, lock-and-compare transaction."""
    if len(actions) > MAX_BATCH_SIZE:
        raise ValueError(f"batch exceeds {MAX_BATCH_SIZE} rows")
    lines = [
        "\\set ON_ERROR_STOP on", "BEGIN;", "SET LOCAL statement_timeout = '120s';",
        "CREATE TEMP TABLE _reconcile_apply (listing_id uuid PRIMARY KEY) ON COMMIT DROP;",
    ]
    for brokerage_id in sorted({action["brokerage_id"] for action in actions}):
        run_key, job_id = _job_identity(plan_hash, brokerage_id)
        lines.append(
            "INSERT INTO credeals.cre_scrape_jobs "
            "(id, brokerage_id, status, started_at, completed_at, notes, artifact_run_key) "
            f"VALUES ({sql_lit(job_id)}::uuid, {sql_lit(brokerage_id)}::uuid, "
            f"'running', now(), NULL, "
            f"{sql_lit('approved evidence-backed lifecycle reconciliation by ' + operator + '; ref=' + approval_ref + '; plan=' + plan_hash)}, "
            f"{sql_lit(run_key)}) ON CONFLICT DO NOTHING;"
        )
    for action in actions:
        run_key, job_id = _job_identity(plan_hash, action["brokerage_id"])
        listing_id = sql_lit(action["listing_id"])
        brokerage_id = sql_lit(action["brokerage_id"])
        external_id = sql_lit(action["external_id"])
        observed_at = sql_lit(action["observed_at"])
        digest_expr = (
            "encode(digest(convert_to(concat_ws(E'\\x1f', "
            "CASE WHEN (l.deleted_at IS NOT NULL) THEN 'true' ELSE 'false' END, "
            "COALESCE(l.status,''), CASE WHEN si.soft_deleted THEN 'true' ELSE 'false' END, "
            "CASE WHEN si.observation_present THEN 'true' ELSE 'false' END, "
            "COALESCE(si.presence_generation::text,'')), 'UTF8'), 'sha256'), 'hex')"
        )
        final_predicates = [
            f"(l.deleted_at IS NULL)={_pg_bool(action['desired_present'])}",
            f"l.status IS NOT DISTINCT FROM {sql_lit(action['desired_status'])}",
            f"si.soft_deleted={_pg_bool(not action['desired_present'])}",
            f"si.observation_present={_pg_bool(action['desired_present'])}",
            f"si.presence_generation={action['desired_generation']}",
        ]
        before_predicates = [f"{digest_expr}={sql_lit(action['before_hash'])}"]

        event = action.get("event")
        if event:
            event_predicates = [
                "ev.listing_id=l.id",
                f"ev.event_type={sql_lit(event['event_type'])}",
                f"ev.presence_generation={event['presence_generation']}",
            ]
            if event["require_reconciliation_provenance"]:
                event_predicates.extend([
                    f"ev.scrape_job_id={sql_lit(job_id)}::uuid",
                    f"ev.reconciliation_provenance={sql_lit(event['provenance'])}",
                    f"ev.evidence_observed_at={observed_at}::timestamptz",
                    "ev.evidence_time_semantics="
                    f"{sql_lit(event['evidence_observed_at_semantics'])}",
                    "ev.reconciliation_evidence_sha256="
                    f"{sql_lit(action['source_evidence_sha256'])}",
                ])
                before_predicates.append(
                    "NOT EXISTS (SELECT 1 FROM credeals.cre_listing_events ev WHERE "
                    + " AND ".join(event_predicates[:3]) + ")"
                )
            final_predicates.append(
                "EXISTS (SELECT 1 FROM credeals.cre_listing_events ev WHERE "
                + " AND ".join(event_predicates) + ")"
            )

        history = action.get("history")
        changed_fields = []
        if history:
            current_values = history["current_values"]
            source_values = history["source_values"]
            latest = history["latest_before"]
            changed_fields = [
                field for field in _TRACKED_FIELDS
                if current_values[field] != source_values[field]
            ]
            final_predicates.extend([
                _tracked_predicate("l", source_values),
                "EXISTS (SELECT 1 FROM credeals.cre_listing_price_history h "
                f"WHERE h.listing_id=l.id AND h.reconciliation_job_id={sql_lit(job_id)}::uuid "
                f"AND h.observed_at={observed_at}::timestamptz "
                f"AND h.reconciliation_provenance='approved_source_authority_history_alignment' "
                f"AND h.observed_at_semantics={sql_lit(history['observed_at_semantics'])} "
                "AND h.reconciliation_evidence_sha256="
                f"{sql_lit(action['source_evidence_sha256'])} "
                f"AND {_tracked_predicate('h', source_values)})",
            ])
            for field in changed_fields:
                event_type = "status_change" if field == "status" else "price_change"
                old_value = history["current_values"][field]
                new_value = source_values[field]
                final_predicates.append(
                    "EXISTS (SELECT 1 FROM credeals.cre_listing_events ev "
                    f"WHERE ev.listing_id=l.id AND ev.scrape_job_id={sql_lit(job_id)}::uuid "
                    f"AND ev.event_type={sql_lit(event_type)} AND ev.field={sql_lit(field)} "
                    f"AND ev.old_value IS NOT DISTINCT FROM "
                    f"{sql_lit(old_value) if old_value is not None else 'NULL'} "
                    f"AND ev.new_value IS NOT DISTINCT FROM "
                    f"{sql_lit(new_value) if new_value is not None else 'NULL'} "
                    "AND ev.reconciliation_provenance="
                    "'approved_source_authority_history_alignment' "
                    f"AND ev.evidence_observed_at={observed_at}::timestamptz "
                    "AND ev.reconciliation_evidence_sha256="
                    f"{sql_lit(action['source_evidence_sha256'])})"
                )
            before_predicates.extend([
                _tracked_predicate("l", current_values),
                "EXISTS (SELECT 1 FROM (SELECT h.id, h.observed_at, "
                + ", ".join(f"h.{field}" for field in _TRACKED_FIELDS)
                + " FROM credeals.cre_listing_price_history h "
                f"WHERE h.listing_id=l.id ORDER BY h.observed_at DESC, h.id DESC LIMIT 1) latest "
                f"WHERE latest.id={sql_lit(latest['id'])}::uuid "
                f"AND latest.observed_at={sql_lit(latest['observed_at'])}::timestamptz "
                f"AND {_tracked_predicate('latest', latest['tracked_values'])})",
            ])

        lines.extend([
            f"-- reconcile {action['listing_id']} operations={','.join(action['operations'])} "
            f"before={action['before_hash']}",
            "DO $$ BEGIN",
            "  PERFORM l.id FROM credeals.cre_listings l "
            "JOIN credeals.cre_source_index si "
            "ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id "
            f"WHERE l.id={listing_id}::uuid FOR UPDATE OF l, si;",
            "  IF NOT FOUND THEN RAISE EXCEPTION 'lifecycle target missing'; END IF;",
            "  IF EXISTS (",
            "    SELECT 1 FROM credeals.cre_listings l "
            "JOIN credeals.cre_source_index si "
            "ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id "
            f"WHERE l.id={listing_id}::uuid AND " + " AND ".join(final_predicates),
            "  ) THEN NULL; -- exact-plan replay: already applied, zero mutations",
            "  ELSE",
        ])
        if history:
            lines.extend([
                "    PERFORM h.id FROM credeals.cre_listing_price_history h "
                f"WHERE h.id={sql_lit(history['latest_before']['id'])}::uuid "
                f"AND h.listing_id={listing_id}::uuid FOR UPDATE;",
                "    IF NOT FOUND THEN RAISE EXCEPTION 'planned latest history row missing'; END IF;",
            ])
        lines.extend([
            "    IF EXISTS (",
            "      SELECT 1 FROM credeals.cre_listings l "
            "JOIN credeals.cre_source_index si "
            "ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id "
            f"WHERE l.id={listing_id}::uuid AND " + " AND ".join(before_predicates),
            f"    ) THEN INSERT INTO _reconcile_apply VALUES ({listing_id}::uuid);",
            "    ELSE RAISE EXCEPTION "
            "'reconciliation evidence drifted and desired state is not an exact replay'; END IF;",
            "  END IF;",
            "END $$;",
        ])
        if action["lifecycle_change"]:
            deleted_at = "NULL" if action["desired_present"] else f"{observed_at}::timestamptz"
            lines.extend([
                "UPDATE credeals.cre_listings SET "
                f"deleted_at={deleted_at}, status={sql_lit(action['desired_status'])}, "
                f"updated_at=GREATEST(COALESCE(updated_at, {observed_at}::timestamptz), "
                f"{observed_at}::timestamptz) WHERE id={listing_id}::uuid "
                "AND id IN (SELECT listing_id FROM _reconcile_apply);",
                "UPDATE credeals.cre_source_index SET "
                f"soft_deleted={_pg_bool(not action['desired_present'])}, "
                f"observation_present={_pg_bool(action['desired_present'])}, "
                f"presence_generation={action['desired_generation']}, "
                f"presence_changed_at={observed_at}::timestamptz, "
                f"last_enumerated_at={observed_at}::timestamptz "
                f"WHERE brokerage_id={brokerage_id}::uuid AND external_id={external_id} "
                f"AND {listing_id}::uuid IN (SELECT listing_id FROM _reconcile_apply);",
            ])
        if event:
            lines.append(
                "INSERT INTO credeals.cre_listing_events "
                "(listing_id, brokerage_id, scrape_job_id, event_type, source_value, source_url, "
                "presence_generation, detected_at, reconciliation_provenance, "
                "evidence_observed_at, evidence_time_semantics, "
                "reconciliation_evidence_sha256) "
                f"SELECT {listing_id}::uuid, {brokerage_id}::uuid, {sql_lit(job_id)}::uuid, "
                f"{sql_lit(event['event_type'])}, "
                f"{sql_lit('approved_lifecycle_reconciliation:' + event['provenance'])}, "
                f"{sql_lit(action.get('source_url')) if action.get('source_url') else 'NULL'}, "
                f"{event['presence_generation']}, now(), {sql_lit(event['provenance'])}, "
                f"{observed_at}::timestamptz, "
                f"{sql_lit(event['evidence_observed_at_semantics'])}, "
                f"{sql_lit(action['source_evidence_sha256'])} "
                f"WHERE {listing_id}::uuid IN (SELECT listing_id FROM _reconcile_apply) "
                "ON CONFLICT DO NOTHING;"
            )
        if history:
            source_values = history["source_values"]
            if history["resolution"] == "update_current_from_source_and_append_history":
                assignments = ", ".join(
                    f"{field}={_sql_value(field, source_values[field])}"
                    for field in _TRACKED_FIELDS
                )
                lines.append(
                    f"UPDATE credeals.cre_listings l SET {assignments}, "
                    f"updated_at=GREATEST(COALESCE(l.updated_at, {observed_at}::timestamptz), "
                    f"{observed_at}::timestamptz) WHERE l.id={listing_id}::uuid "
                    "AND l.id IN (SELECT listing_id FROM _reconcile_apply) AND NOT ("
                    f"{_tracked_predicate('l', source_values)});"
                )
            for field in changed_fields:
                old_value = history["current_values"][field]
                new_value = source_values[field]
                event_type = "status_change" if field == "status" else "price_change"
                lines.append(
                    "INSERT INTO credeals.cre_listing_events "
                    "(listing_id, brokerage_id, scrape_job_id, event_type, field, old_value, "
                    "new_value, source_value, source_url, detected_at, reconciliation_provenance, "
                    "evidence_observed_at, evidence_time_semantics, "
                    "reconciliation_evidence_sha256) "
                    f"SELECT {listing_id}::uuid, {brokerage_id}::uuid, {sql_lit(job_id)}::uuid, "
                    f"{sql_lit(event_type)}, {sql_lit(field)}, "
                    f"{sql_lit(old_value) if old_value is not None else 'NULL'}, "
                    f"{sql_lit(new_value) if new_value is not None else 'NULL'}, "
                    "'approved_history_reconciliation:source_authority', "
                    f"{sql_lit(action.get('source_url')) if action.get('source_url') else 'NULL'}, "
                    "now(), 'approved_source_authority_history_alignment', "
                    f"{observed_at}::timestamptz, {_sql_value('status', _HISTORY_TIME_SEMANTICS)}, "
                    f"{sql_lit(action['source_evidence_sha256'])} "
                    f"WHERE {listing_id}::uuid IN (SELECT listing_id FROM _reconcile_apply) "
                    "ON CONFLICT DO NOTHING;"
                )
            lines.append(
                "INSERT INTO credeals.cre_listing_price_history "
                "(listing_id, observed_at, sale_price_usd, sale_price_per_sf, lease_rate_min, "
                "lease_rate_max, status, cap_rate, source_lastmod, transaction_type, "
                "reconciliation_job_id, reconciliation_provenance, observed_at_semantics, "
                "reconciliation_evidence_sha256) "
                f"SELECT l.id, {observed_at}::timestamptz, "
                + ", ".join(_sql_value(field, source_values[field]) for field in _TRACKED_FIELDS[:4])
                + f", {_sql_value('status', source_values['status'])}, "
                f"{_sql_value('cap_rate', source_values['cap_rate'])}, l.source_lastmod, "
                f"l.transaction_type, {sql_lit(job_id)}::uuid, "
                "'approved_source_authority_history_alignment', "
                f"{sql_lit(history['observed_at_semantics'])}, "
                f"{sql_lit(action['source_evidence_sha256'])} "
                f"FROM credeals.cre_listings l WHERE l.id={listing_id}::uuid "
                "AND l.id IN (SELECT listing_id FROM _reconcile_apply) ON CONFLICT DO NOTHING;"
            )
    lines.extend([
        "COMMIT;",
        f"-- operator: {operator}",
        f"-- approval-reference: {approval_ref}",
    ])
    return "\n".join(lines) + "\n"


def build_finalize_sql(actions, plan_hash, operator, approval_ref):
    """Complete shared reconciliation jobs only after every batch succeeded."""
    jobs = [
        (*_job_identity(plan_hash, brokerage_id), brokerage_id)
        for brokerage_id in sorted({action["brokerage_id"] for action in actions})
    ]
    if not jobs:
        return "\\set ON_ERROR_STOP on\n"
    values = ",\n".join(
        f"({sql_lit(job_id)}::uuid, {sql_lit(run_key)}, {sql_lit(brokerage_id)}::uuid)"
        for run_key, job_id, brokerage_id in jobs
    )
    notes = (
        "approved evidence-backed lifecycle reconciliation by " + operator
        + "; ref=" + approval_ref + "; plan=" + plan_hash
    )
    return f"""\\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE _expected_reconcile_jobs
    (job_id uuid PRIMARY KEY, run_key text NOT NULL, brokerage_id uuid NOT NULL)
    ON COMMIT DROP;
INSERT INTO _expected_reconcile_jobs VALUES
{values};
SELECT j.id
FROM credeals.cre_scrape_jobs j
JOIN _expected_reconcile_jobs expected ON expected.job_id = j.id
ORDER BY j.id
FOR UPDATE OF j;
DO $$
BEGIN
  IF (SELECT count(*) FROM _expected_reconcile_jobs) <>
     (SELECT count(*) FROM credeals.cre_scrape_jobs j
      JOIN _expected_reconcile_jobs expected
        ON expected.job_id = j.id
       AND expected.run_key = j.artifact_run_key
       AND expected.brokerage_id = j.brokerage_id
      WHERE j.status IN ('running', 'completed')) THEN
    RAISE EXCEPTION 'not all reconciliation batches produced the expected jobs';
  END IF;
END $$;
UPDATE credeals.cre_scrape_jobs j
SET status = 'completed', completed_at = now(), notes = {sql_lit(notes)}
FROM _expected_reconcile_jobs expected
WHERE expected.job_id = j.id AND j.status = 'running';
COMMIT;
"""


def _revalidate_plan_evidence(plan, *, max_age_hours):
    now = datetime.now(timezone.utc)
    checked = set()
    for action in plan["actions"]:
        path = Path(action["source_evidence_path"])
        key = (str(path), action["source_evidence_sha256"], action["observed_at"])
        if key in checked:
            continue
        checked.add(key)
        if path.is_symlink() or not path.is_file():
            raise ValueError("planned source evidence artifact is missing or unsafe")
        if _sha256_file(path) != action["source_evidence_sha256"]:
            raise ValueError("planned source evidence artifact bytes changed")
        try:
            with open(path, encoding="utf-8") as handle:
                artifact = json.load(handle)
            finished = _parse_time((artifact.get("runMeta") or {}).get("finishedAt"))
            observed = _parse_time(action["observed_at"])
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("planned source evidence artifact is no longer valid") from exc
        age = (now - finished).total_seconds() / 3600
        if finished != observed or age < 0 or age > max_age_hours:
            raise ValueError("planned source evidence artifact is stale or timestamp-drifted")


def _run_psql_script(psql, db_url, sql):
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as handle:
        handle.write(sql)
        sql_path = handle.name
    try:
        subprocess.run(
            [psql, *psql_connection_args(db_url), "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
            env=psql_connection_env(db_url), check=True,
        )
    finally:
        os.unlink(sql_path)


def _load_approval_contract(path, plan_hash):
    if not path:
        raise ValueError("a private approval contract is required")
    contract_path = Path(path).expanduser()
    if contract_path.is_symlink() or not contract_path.is_file():
        raise ValueError("approval contract must be a regular non-symlink file")
    if contract_path.stat().st_mode & 0o077:
        raise ValueError("approval contract must be private (chmod 600)")
    try:
        with open(contract_path, encoding="utf-8") as handle:
            contract = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("approval contract is not valid JSON") from exc
    required = {
        "schema_version", "operation", "operator", "approval_ref", "plan_hash",
    }
    if not isinstance(contract, dict) or set(contract) != required:
        raise ValueError("approval contract has unexpected or missing fields")
    operator = str(contract.get("operator") or "").strip().lower()
    approval_ref = str(contract.get("approval_ref") or "").strip()
    if contract.get("schema_version") != 1:
        raise ValueError("unsupported approval contract schema")
    if contract.get("operation") != "cre-listing-lifecycle-reconciliation":
        raise ValueError("approval contract operation does not match")
    if operator not in ALLOWED_OPERATORS:
        raise ValueError("approval contract operator is not configured")
    if not APPROVAL_REF_RE.fullmatch(approval_ref):
        raise ValueError("approval contract reference must be an AGENTIC issue")
    if contract.get("plan_hash") != plan_hash:
        raise ValueError("approval contract is not bound to this plan hash")
    return operator, approval_ref


def apply_plan(plan, *, approval_contract_path, confirmation, supplied_hash, env_file,
               expected_db_target_sha256, batch_size, max_age_hours=48):
    plan_core = {key: value for key, value in plan.items() if key != "plan_hash"}
    computed_hash = hashlib.sha256(_canon(plan_core).encode()).hexdigest()
    if computed_hash != plan.get("plan_hash") or supplied_hash != plan.get("plan_hash"):
        raise ValueError("supplied plan hash does not match exact generated plan")
    operator, approval_ref = _load_approval_contract(
        approval_contract_path, plan["plan_hash"],
    )
    expected_confirmation = f"APPLY cre-listing-lifecycle-reconciliation {plan['plan_hash']}"
    if confirmation != expected_confirmation:
        raise ValueError("exact plan-specific confirmation is required")
    for action in plan["actions"]:
        history = action.get("history") or {}
        decision = history.get("authority_decision") or {}
        decided_by = str(decision.get("decided_by") or "").strip().lower()
        if history and decided_by != operator:
            raise ValueError("history authority decision must match the configured operator")
    if not expected_db_target_sha256:
        raise ValueError("--expected-db-target-sha256 is required for apply")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be 1..{MAX_BATCH_SIZE}")
    db_url, _ = load_db_url(env_file)
    assert_expected_database_target(db_url, expected_db_target_sha256)
    _revalidate_plan_evidence(plan, max_age_hours=max_age_hours)
    psql = find_psql()
    for offset in range(0, len(plan["actions"]), batch_size):
        batch = plan["actions"][offset:offset + batch_size]
        _run_psql_script(
            psql, db_url,
            build_apply_sql(batch, plan["plan_hash"], operator, approval_ref),
        )
    _run_psql_script(
        psql, db_url,
        build_finalize_sql(plan["actions"], plan["plan_hash"], operator, approval_ref),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--max-age-hours", type=float, default=48)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--approval-contract",
        help="private chmod-600 JSON contract bound to operator, AGENTIC reference, and plan hash",
    )
    parser.add_argument("--confirm")
    parser.add_argument("--plan-hash")
    parser.add_argument("--env-file")
    parser.add_argument("--expected-db-target-sha256")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    evidence_path = Path(args.evidence).resolve()
    with open(evidence_path, encoding="utf-8") as handle:
        bundle = json.load(handle)
    plan = build_plan(
        bundle, evidence_base_dir=evidence_path.parent,
        max_age_hours=args.max_age_hours,
    )
    json_path, csv_path = write_evidence(plan, args.out_prefix)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "plan_hash": plan["plan_hash"],
        "actions": len(plan["actions"]), "skipped": len(plan["skipped"]),
        "json": json_path, "csv": csv_path,
    }, sort_keys=True))
    if args.apply:
        apply_plan(
            plan, approval_contract_path=args.approval_contract,
            confirmation=args.confirm, supplied_hash=args.plan_hash,
            env_file=args.env_file,
            expected_db_target_sha256=args.expected_db_target_sha256,
            batch_size=args.batch_size,
            max_age_hours=args.max_age_hours,
        )


if __name__ == "__main__":
    main()
