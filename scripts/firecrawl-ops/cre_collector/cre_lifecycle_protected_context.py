"""Finite four-parent JLL Investor context for canonical lifecycle reconciliation.

This module reads evidence and renders a guard. It never connects to a database.
Opaque PostgreSQL JSON is retained verbatim so numeric precision survives SQL
comparison; Python's parsed representation is used only for scope validation.
"""

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

SCHEMA = "jll-investor-protected-context-v1"
SNAPSHOT_SCHEMA = "investor-four-parent-current-context-v1"
TABLES = tuple(sorted((
    "cre_listings", "cre_brokerages", "cre_source_index", "cre_source_baseline",
    "cre_scrape_jobs", "cre_listing_contacts", "cre_listing_documents",
    "cre_listing_images", "cre_listing_media", "cre_listing_links",
    "cre_listing_om_facts", "cre_listing_price_history", "cre_listing_events",
    "cre_listing_contacts_archive", "cre_listing_documents_archive",
)))
_POINTER_KEYS = {"snapshot_path", "snapshot_sha256", "database_target_sha256"}
_SNAPSHOT_KEYS = {
    "schema", "payload", "isolation", "read_only", "observed_at", "matched_rows",
    "expected_rows", "source_acceptance", "server_version_num",
    "evidence_time_semantics", "complete_exact_identity_scope",
}
_CATALOG_KEYS = {
    "schema", "table", "relkind", "row_security", "force_row_security",
    "columns", "constraints", "indexes", "triggers", "policies",
}
_CATALOG_PARTS = {
    "columns": {"position", "name", "type", "not_null", "identity", "generated", "default"},
    "constraints": {"name", "type", "validated", "definition"},
    "indexes": {"name", "valid", "ready", "definition"},
    "triggers": {"name", "enabled", "definition", "function_schema", "function_name", "function_definition"},
    "policies": {"name", "command", "permissive", "roles", "using", "check"},
}
_BEFORE_KEYS = {
    "listing_deleted", "listing_status", "index_soft_deleted",
    "observation_present", "presence_generation",
}


def _require(condition, message):
    if not condition:
        raise ValueError("protected context: " + message)


def _uuid(value):
    _require(isinstance(value, str), "UUID must be a string")
    try:
        _require(str(UUID(value)) == value, "UUID must be canonical")
    except (ValueError, AttributeError) as exc:
        raise ValueError("protected context: invalid UUID") from exc
    return value


def _url(value):
    _require(isinstance(value, str), "source URL must be a string")
    parsed = urlsplit(value)
    _require(parsed.scheme == "https" and parsed.netloc == "invest.jll.com"
             and parsed.path.startswith("/us/en/listings/")
             and not parsed.query and not parsed.fragment, "source URL scope differs")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("protected context: nonfinite JSON number")


def _file_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read_snapshot(path, expected_sha):
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink evidence path")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "unsafe evidence file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read()
        after = os.fstat(fd)
        _require(_file_identity(before) == _file_identity(after)
                 == _file_identity(path.stat(follow_symlinks=False)), "evidence file changed")
        _require(hashlib.sha256(data).hexdigest() == expected_sha, "snapshot digest differs")
    finally:
        os.close(fd)
    text = data.decode("utf-8")
    _require("\x00" not in text, "NUL in JSON text")
    value = json.loads(text, object_pairs_hook=_unique_object, parse_float=Decimal,
                       parse_constant=_nonfinite)
    return text, value


def _catalog_columns(catalog):
    _require(isinstance(catalog, list) and len(catalog) == len(TABLES), "catalog scope differs")
    columns = {}
    for table in catalog:
        _require(isinstance(table, dict) and set(table) == _CATALOG_KEYS, "catalog shape differs")
        name = table["table"]
        _require(isinstance(name, str) and name in TABLES and name not in columns
                 and table["schema"] == "credeals" and table["relkind"] in ("r", "p"),
                 "catalog relation differs")
        _require(type(table["row_security"]) is bool
                 and type(table["force_row_security"]) is bool, "catalog boolean differs")
        names, positions = set(), set()
        for part, keys in _CATALOG_PARTS.items():
            rows = table[part]
            _require(isinstance(rows, list), "catalog member is not an array")
            seen = set()
            for row in rows:
                _require(isinstance(row, dict) and set(row) == keys, "catalog member shape differs")
                row_name = row["name"]
                _require(isinstance(row_name, str) and row_name and row_name not in seen,
                         "duplicate or invalid catalog name")
                seen.add(row_name)
                for key in keys - {"position", "not_null", "validated", "valid", "ready",
                                   "permissive", "roles", "default", "using", "check"}:
                    _require(isinstance(row[key], str), "catalog text differs")
                for key in keys & {"not_null", "validated", "valid", "ready", "permissive"}:
                    _require(type(row[key]) is bool, "catalog boolean differs")
                for key in keys & {"default", "using", "check"}:
                    _require(row[key] is None or isinstance(row[key], str), "catalog nullable text differs")
                if part == "columns":
                    _require(type(row["position"]) is int and row["position"] > 0
                             and row["position"] not in positions, "catalog position differs")
                    positions.add(row["position"])
                    names.add(row_name)
                if part == "policies":
                    _require(isinstance(row["roles"], list)
                             and all(type(v) is int and v >= 0 for v in row["roles"]),
                             "catalog policy roles differ")
        _require(names, "catalog columns missing")
        columns[name] = names
    return columns


def _objects(payload):
    _require(isinstance(payload, dict) and set(payload) == {"objects", "catalog"}, "payload shape differs")
    objects = payload["objects"]
    _require(isinstance(objects, dict) and set(objects) == set(TABLES), "object scope differs")
    columns = _catalog_columns(payload["catalog"])
    for name, rows in objects.items():
        _require(isinstance(rows, list), "protected object is not an array")
        seen = set()
        for row in rows:
            _require(isinstance(row, dict) and set(row) == columns[name], "protected row columns differ")
            identity = _uuid(row.get("id")) if name != "cre_source_baseline" else row.get("source_key")
            _require(isinstance(identity, str) and identity not in seen, "duplicate protected row")
            seen.add(identity)
    return objects


def _identities(objects):
    brokerages, parents, indexes = (objects[k] for k in ("cre_brokerages", "cre_listings", "cre_source_index"))
    _require(len(brokerages) == 1 and brokerages[0].get("slug") == "jll", "brokerage scope differs")
    brokerage = _uuid(brokerages[0]["id"])
    _require(len(parents) == 4 and len(indexes) == 4, "four parents and indexes required")
    by_external = {}
    for row in parents:
        external = row.get("external_id")
        _require(isinstance(external, str) and re.fullmatch(r"investor:[A-Za-z0-9]+", external)
                 and external not in by_external, "Investor identity differs")
        _require(row.get("brokerage_id") == brokerage and row.get("status") == "active"
                 and row.get("deleted_at") is None and row.get("transaction_type") == "sale",
                 "parent scope differs")
        _require(_url(row.get("source_url")) == row.get("canonical_url"), "canonical URL differs")
        by_external[external] = row
    index_by_external = {}
    for row in indexes:
        external = row.get("external_id")
        _require(isinstance(external, str) and external in by_external
                 and external not in index_by_external, "index identity differs")
        _require(row.get("brokerage_id") == brokerage and row.get("source_key") == "jll-investor"
                 and row.get("url") == by_external[external]["source_url"], "index scope differs")
        _require(row.get("soft_deleted") is False and row.get("observation_present") is True
                 and type(row.get("presence_generation")) is int
                 and row["presence_generation"] >= 0, "index lifecycle differs")
        index_by_external[external] = row
    parent_ids = {row["id"] for row in parents}
    jobs = {row["id"] for row in objects["cre_scrape_jobs"]}
    for name, rows in objects.items():
        for row in rows:
            if name.startswith("cre_listing_"):
                fk = "source_listing_id" if name.endswith("_archive") else "listing_id"
                _require(_uuid(row.get(fk)) in parent_ids, "child escaped protected parents")
            if "brokerage_id" in row:
                _require(row["brokerage_id"] == brokerage, "row escaped protected brokerage")
            for key in ("scrape_job_id", "reconciliation_job_id"):
                if row.get(key) is not None:
                    _require(_uuid(row[key]) in jobs, "row references unprotected job")
            for key in ("is_primary", "active", "soft_deleted", "observation_present"):
                if key in row:
                    _require(type(row[key]) is bool, "protected boolean differs")
            if name == "cre_source_baseline":
                _require(row.get("source_key") in ("jll", "jll-investor")
                         and row.get("brokerage_slug") == "jll", "baseline scope differs")
    return brokerage, by_external, index_by_external


def _validate_actions(actions, parents, indexes, brokerage):
    _require(isinstance(actions, list) and len(actions) == 2, "two actions required")
    selected = set()
    evidence = None
    for action in actions:
        _require(isinstance(action, dict), "invalid action")
        current_evidence = tuple(action.get(key) for key in (
            "source_evidence_path", "source_evidence_sha256", "observed_at"))
        _require(all(isinstance(value, str) and value for value in current_evidence),
                 "source evidence binding missing")
        _require(evidence is None or evidence == current_evidence,
                 "actions must share one exact source artifact and observation")
        evidence = current_evidence
        external = action.get("external_id")
        _require(isinstance(external, str) and external in parents and external not in selected,
                 "action identity differs")
        selected.add(external)
        parent, index = parents[external], indexes[external]
        before = {"listing_deleted": False, "listing_status": "active",
                  "index_soft_deleted": False, "observation_present": True,
                  "presence_generation": index["presence_generation"]}
        supplied = action.get("before")
        _require(isinstance(supplied, dict) and set(supplied) == _BEFORE_KEYS
                 and supplied == before
                 and all(type(supplied[k]) is bool for k in (
                     "listing_deleted", "index_soft_deleted", "observation_present"))
                 and type(supplied["presence_generation"]) is int, "action before-state differs")
        digest = hashlib.sha256("\x1f".join(("false", "active", "false", "true",
                                           str(index["presence_generation"]))).encode()).hexdigest()
        _require(action.get("before_hash") == digest and action.get("listing_id") == parent["id"]
                 and action.get("brokerage_id") == brokerage
                 and action.get("source_key") == "jll-investor"
                 and action.get("source_url") == parent["source_url"], "action binding differs")
        generation = index["presence_generation"] + 1
        _require(action.get("desired_present") is False and action.get("desired_status") == "inactive"
                 and type(action.get("desired_generation")) is int
                 and action["desired_generation"] == generation
                 and action.get("lifecycle_change") is True and action.get("history") is None
                 and action.get("operations") == ["lifecycle_sync", "lifecycle_event"]
                 and not action.get("index_absent_retirement") and not action.get("event_repair"),
                 "only indexed disappearance actions permitted")
        event = action.get("event")
        _require(isinstance(event, dict) and event == {
            "event_type": "disappeared", "presence_generation": generation,
            "provenance": "approved_lifecycle_state_reconciliation",
            "evidence_observed_at_semantics": "source_state_observed_at_not_transition_time",
            "require_reconciliation_provenance": False,
        } and type(event["presence_generation"]) is int
            and event["require_reconciliation_provenance"] is False, "event repair not permitted")
    remaining = set(parents) - selected
    for url in {row["source_url"] for row in parents.values()}:
        group = {key for key, row in parents.items() if row["source_url"] == url}
        _require(len(group) == 2 and len(group & selected) == len(group & remaining) == 1,
                 "one protected current parent per shared URL required")
    return remaining


def _literal(value):
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _projection_sql(identities):
    """Fixed private capture seam, also used by the disposable SQL contract."""
    _require(len(identities) == 4, "projection requires four identities")
    values = []
    for identity in identities:
        _require(isinstance(identity, (tuple, list)) and len(identity) == 4,
                 "projection identity shape differs")
        listing, brokerage, external, url = identity
        _uuid(listing)
        _uuid(brokerage)
        _url(url)
        _require(isinstance(external, str) and re.fullmatch(r"investor:[A-Za-z0-9]+", external),
                 "projection source identity differs")
        values.append(f"({_literal(listing)}::uuid,{_literal(brokerage)}::uuid,{_literal(external)})")
    selectors = {
        "cre_listings": "t.id IN (SELECT id FROM expected)",
        "cre_brokerages": "t.id IN (SELECT brokerage_id FROM expected)",
        "cre_source_index": "EXISTS (SELECT 1 FROM expected e WHERE t.brokerage_id=e.brokerage_id AND t.external_id=e.external_id)",
        "cre_source_baseline": "t.source_key IN ('jll','jll-investor')",
        "cre_scrape_jobs": "t.brokerage_id IN (SELECT brokerage_id FROM expected)",
    }
    objects = []
    for name in TABLES:
        selector = selectors.get(name)
        if selector is None:
            fk = "source_listing_id" if name.endswith("_archive") else "listing_id"
            selector = f"t.{fk} IN (SELECT id FROM expected)"
        objects.append(f"'{name}',(SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM credeals.{name} t WHERE {selector})")
    return ("WITH expected(id,brokerage_id,external_id) AS (VALUES " + ",".join(values)
            + ") SELECT jsonb_build_object('objects',jsonb_build_object(" + ",".join(objects)
            + "),'catalog'," + _CATALOG_SQL + ")")


_CATALOG_SQL = """(SELECT jsonb_agg(jsonb_build_object(
 'schema',n.nspname,'table',c.relname,'relkind',c.relkind,'row_security',c.relrowsecurity,'force_row_security',c.relforcerowsecurity,
 'columns',(SELECT coalesce(jsonb_agg(jsonb_build_object('position',a.attnum,'name',a.attname,'type',format_type(a.atttypid,a.atttypmod),'not_null',a.attnotnull,'identity',a.attidentity,'generated',a.attgenerated,'default',pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum),'[]'::jsonb) FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),
 'constraints',(SELECT coalesce(jsonb_agg(jsonb_build_object('name',x.conname,'type',x.contype,'validated',x.convalidated,'definition',pg_get_constraintdef(x.oid,true)) ORDER BY x.conname),'[]'::jsonb) FROM pg_constraint x WHERE x.conrelid=c.oid),
 'indexes',(SELECT coalesce(jsonb_agg(jsonb_build_object('name',ic.relname,'valid',i.indisvalid,'ready',i.indisready,'definition',pg_get_indexdef(i.indexrelid)) ORDER BY ic.relname),'[]'::jsonb) FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid WHERE i.indrelid=c.oid),
 'triggers',(SELECT coalesce(jsonb_agg(jsonb_build_object('name',g.tgname,'enabled',g.tgenabled,'definition',pg_get_triggerdef(g.oid,true),'function_schema',pn.nspname,'function_name',pr.proname,'function_definition',pg_get_functiondef(pr.oid)) ORDER BY g.tgname),'[]'::jsonb) FROM pg_trigger g JOIN pg_proc pr ON pr.oid=g.tgfoid JOIN pg_namespace pn ON pn.oid=pr.pronamespace WHERE g.tgrelid=c.oid AND NOT g.tgisinternal),
 'policies',(SELECT coalesce(jsonb_agg(jsonb_build_object('name',y.polname,'command',y.polcmd,'permissive',y.polpermissive,'roles',y.polroles,'using',pg_get_expr(y.polqual,y.polrelid),'check',pg_get_expr(y.polwithcheck,y.polrelid)) ORDER BY y.polname),'[]'::jsonb) FROM pg_policy y WHERE y.polrelid=c.oid)
 ) ORDER BY c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='credeals' AND c.relname IN (""" + ",".join(f"'{name}'" for name in TABLES) + ") AND c.relkind IN ('r','p'))"


@dataclass(frozen=True)
class ProtectedContext:
    _path: str
    _sha256: str
    _target: str
    _original_json: str
    _identities: tuple
    _current: tuple

    @property
    def binding(self):
        return {"schema": SCHEMA, "snapshot_path": self._path,
                "snapshot_sha256": self._sha256, "database_target_sha256": self._target}

    @property
    def non_action_parents(self):
        return tuple(dict(zip(("listing_id", "brokerage_id", "external_id", "source_url"), row))
                     for row in self._current)

    def guard_sql(self):
        relations = ", ".join("credeals." + name for name in TABLES)
        body = ("BEGIN IF (" + _projection_sql(self._identities) + ") IS DISTINCT FROM ("
                + _literal(self._original_json) + "::jsonb -> 'payload') THEN "
                "RAISE EXCEPTION 'protected lifecycle context changed'; END IF; END;")
        return ("SET LOCAL standard_conforming_strings = on;\n"
                "SET LOCAL lock_timeout = '2s';\nSET LOCAL timezone = 'UTC';\n"
                + "LOCK TABLE " + relations + " IN SHARE ROW EXCLUSIVE MODE;\nDO "
                + _literal(body) + ";\n")


def load_context(value, actions, evidence_base_dir="."):
    """Validate exactly this indexed two-retirement/four-parent contract."""
    _require(isinstance(value, dict) and set(value) in (_POINTER_KEYS, _POINTER_KEYS | {"schema"}),
             "pointer keys differ")
    _require("schema" not in value or value["schema"] == SCHEMA, "binding schema differs")
    for key in ("snapshot_sha256", "database_target_sha256"):
        _require(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key]),
                 "invalid digest")
    _require(isinstance(value["snapshot_path"], str) and value["snapshot_path"], "missing snapshot path")
    path = Path(value["snapshot_path"]).expanduser()
    if not path.is_absolute():
        path = Path(evidence_base_dir) / path
    path = Path(os.path.abspath(path))
    original, snapshot = _read_snapshot(path, value["snapshot_sha256"])
    _require(isinstance(snapshot, dict) and set(snapshot) == _SNAPSHOT_KEYS, "snapshot shape differs")
    _require(snapshot["schema"] == SNAPSHOT_SCHEMA and snapshot["read_only"] == "on"
             and snapshot["isolation"] == "repeatable read"
             and snapshot["source_acceptance"] is False
             and snapshot["complete_exact_identity_scope"] is True
             and type(snapshot["expected_rows"]) is int and snapshot["expected_rows"] == 4
             and type(snapshot["matched_rows"]) is int and snapshot["matched_rows"] == 4
             and snapshot["evidence_time_semantics"] == "retained_database_evidence_observed_at",
             "snapshot observation scope differs")
    _require(isinstance(snapshot["observed_at"], str)
             and datetime.fromisoformat(snapshot["observed_at"].replace("Z", "+00:00")).tzinfo is not None,
             "invalid observation clock")
    _require(isinstance(snapshot["server_version_num"], str)
             and snapshot["server_version_num"].isdigit(), "invalid server version")
    objects = _objects(snapshot["payload"])
    brokerage, parents, indexes = _identities(objects)
    remaining = _validate_actions(actions, parents, indexes, brokerage)
    identities = tuple(sorted((row["id"], brokerage, key, row["source_url"])
                              for key, row in parents.items()))
    current = tuple(row for row in identities if row[2] in remaining)
    return ProtectedContext(str(path), value["snapshot_sha256"], value["database_target_sha256"],
                            original, identities, current)
