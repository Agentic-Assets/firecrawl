"""Finite joins of retained successful scheduled single-source run evidence."""

import re
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from cre_code_handoff_evidence import TASK_PATH, TASKS, HandoffError, clock, identity, require, unique
from cre_code_handoff_host import qualify_native

SOURCES = {"interra": "interra-realty", "newmark": "newmark"}
EVENTS = (107, 129, 100, 200, 201, 102)
NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
FIELDS = ("enabled", "last_task_result", "last_run_utc", "next_run_utc")


def instance(value):
    require(isinstance(value, str) and re.fullmatch(r"\{[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}", value),
            "scheduled instance missing or malformed")
    try:
        return uuid.UUID(value.strip("{}"))
    except ValueError as error:
        raise HandoffError("invalid scheduled instance") from error


def milliseconds(value):
    require(type(value) is int and value > 0, "admission/control clock malformed")
    return datetime.fromtimestamp(value / 1000, UTC)


def successful_capture(capture, stdout, stderr):
    require(isinstance(capture, dict) and capture.get("StartAttempted") is True
            and capture.get("Started") is True and capture.get("StartFailed") is False
            and capture.get("ReadOnlyDeadlineExpired") is False and capture.get("Failed") is False
            and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 0,
            "natural native capture incomplete")
    for name, raw in (("Stdout", stdout), ("Stderr", stderr)):
        stream = capture.get(name, {})
        require(stream.get("Eof") is True and all(stream.get(flag) is False for flag in (
            "Truncated", "ReadFailed", "WriteFailed", "FlushFailed", "CloseFailed", "DrainTimedOut",
            "ReaderDidNotStop", "RunReferenceConflict", "Failed"))
            and type(stream.get("ObservedBytes")) is int and stream["ObservedBytes"] == len(raw)
            and type(stream.get("CapturedBytes")) is int and stream["CapturedBytes"] == len(raw),
            "natural native streams incomplete")


def scheduler_events(roles, evidence, bridge, task, execute):
    require(isinstance(roles, list) and len(roles) == len(set(roles)) == 6, "six distinct scheduler XML inputs required")
    records = {}
    for role in roles:
        raw = evidence.raw(role)
        require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), "scheduler XML declarations refused")
        root = ET.fromstring(raw)
        system = root.find("e:System", NS)
        require(system is not None and system.find("e:Provider", NS).get("Name") == "Microsoft-Windows-TaskScheduler"
                and system.findtext("e:Channel", namespaces=NS) == "Microsoft-Windows-TaskScheduler/Operational",
                "unexpected scheduler event producer")
        event_id = int(system.findtext("e:EventID", namespaces=NS))
        require(event_id in EVENTS and event_id not in records, "duplicate/unknown scheduler event")
        data = {}
        for node in root.findall("e:EventData/e:Data", NS):
            require(node.get("Name") not in data, "duplicate scheduler data key")
            data[node.get("Name")] = node.text
        require(data.get("TaskName") == task, "scheduler event belongs to another task")
        records[event_id] = (int(system.findtext("e:EventRecordID", namespaces=NS)),
                             clock(system.find("e:TimeCreated", NS).get("SystemTime")), data)
    require(set(records) == set(EVENTS), "scheduler terminal/start event missing")
    ordered = [records[key] for key in EVENTS]
    require(all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(ordered, ordered[1:], strict=False))
            and bridge.get("scheduler_event_record_ids") == [row[0] for row in ordered[:4]],
            "scheduler event chronology/bridge differs")
    guid = instance(bridge.get("task_instance_guid"))
    for event_id in (107, 100, 102):
        require(instance(records[event_id][2].get("InstanceId")) == guid, "scheduled instance differs")
    for event_id in (200, 201):
        data = records[event_id][2]
        require(instance(data.get("TaskInstanceId")) == guid and data.get("ActionName") == execute
                and data.get("EnginePID") == str(bridge["action_pid"]), "scheduler action identity differs")
    require(records[129][2].get("ProcessID") == str(bridge["action_pid"])
            and records[201][2].get("ResultCode") == "0", "scheduled process/result differs")
    return records


def validate_advancements(release, evidence, previous):
    baseline = unique(release["task_baseline"], "key", TASKS)
    proofs = unique(release.get("natural_advancements", []), "key")
    changed = {key for key in baseline if previous is not None
               and (any(previous[key][field] != baseline[key][field] for field in FIELDS[:2])
                    or any(clock(previous[key][field]) != clock(baseline[key][field]) for field in FIELDS[2:]))}
    require(set(proofs) == changed, "natural advancement proof set differs")
    seen = set()
    for key, proof in proofs.items():
        prior, current = previous[key], baseline[key]
        require(key in SOURCES and prior["enabled"] is current["enabled"]
                and type(prior["last_task_result"]) is int and prior["last_task_result"] == 0
                and type(current["last_task_result"]) is int and current["last_task_result"] == 0
                and clock(prior["last_run_utc"]) < clock(current["last_run_utc"]) < clock(current["next_run_utc"])
                and clock(prior["next_run_utc"]) < clock(current["next_run_utc"])
                and identity(proof["code"]) == release["deployed"], "unsupported natural task advancement")
        audit, bridge, observation, series, parent, run, child = (
            evidence.obj(proof[name]) for name in ("audit", "bridge", "observation", "series", "parent_control", "run", "child_control"))
        source, code, task = SOURCES[key], release["deployed"]["sha"], TASK_PATH + TASKS[key]
        require(bridge.get("task_path") == task and all(type(bridge.get(field)) is int and bridge[field] > 0
                for field in ("action_pid", "direct_wsl_pid", "direct_wsl_parent_pid"))
                and bridge["action_pid"] == bridge["direct_wsl_parent_pid"] != bridge["direct_wsl_pid"],
                "original direct process bridge missing")
        stamp = audit.get("windowsAdmissionUnixMs")
        admitted = milliseconds(stamp)
        require(bridge.get("scalar_argv") == {"expected-sha": code, "sources": source,
                                              "windows-admission-unix-ms": str(stamp)}, "natural admission scalars differ")
        summary = audit.get("notificationSummary", {})
        qualify_native(audit)
        operation = (str(instance(bridge.get("task_instance_guid"))), stamp, summary.get("invocation_id"), summary.get("run_id"))
        require(all(value not in seen for value in operation), "reused natural operation")
        seen.update(operation)
        require(audit.get("tier") == "series" and audit.get("source") == source and audit.get("pipeline") is False
                and audit.get("expectedSha") == code and audit.get("admitted") is True
                and audit.get("status") == audit.get("dispatch") == "returned"
                and all(type(audit.get(field)) is int and audit[field] == 0 for field in ("nativeExitCode", "wrapperExitCode", "exitCode"))
                and audit.get("notificationSummaryStatus") == "verified"
                and type(summary.get("schema_version")) is int and summary["schema_version"] == 1 and summary.get("verified") is True
                and summary.get("source") == source and summary.get("collector_git_sha") == code
                and type(summary.get("windows_admission_unix_ms")) is int and summary["windows_admission_unix_ms"] == stamp
                and summary.get("local_settlement") == "confirmed" and summary.get("quality_no_regression") is True
                and summary.get("measurement") == "generation_exact_current_inventory"
                and summary.get("transaction_scope") == ["sale", "lease"], "natural native audit/summary differs")
        successful_capture(audit.get("capture"), evidence.raw(proof["stdout"]), evidence.raw(proof["stderr"]))
        series_id, run_id, invocation = summary["series_id"], summary["run_id"], summary["invocation_id"]
        require(isinstance(series_id, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z", series_id)
                and isinstance(run_id, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z-[0-9a-f]{12}", run_id)
                and isinstance(invocation, str) and re.fullmatch(r"[0-9a-f]{32}", invocation), "natural operation IDs malformed")
        directory = release["roots"]["runtime"].rstrip("/") + "/scripts/firecrawl-ops/cre_collector/out/checkpoint-series/" + series_id
        require(audit["capture"]["Stdout"].get("ObservedRunDirectory") == directory
                and audit["capture"]["Stderr"].get("ObservedRunDirectory") in (None, directory)
                and type(series.get("schema_version")) is int and series["schema_version"] == 1 and series.get("series_id") == series_id
                and series.get("collector_git_sha") == code and series.get("collector_git_dirty") is False
                and series.get("status") == "complete" and set(series.get("sources", {})) == {source}
                and series.get("latest_invocation_admission", {}).get("expected_sha") == code
                and series["latest_invocation_admission"].get("windows_admission_unix_ms") == stamp
                and series.get("latest_invocation_control", {}).get("invocation_id") == invocation
                and series["latest_invocation_control"].get("receipt") == ".series-control/" + invocation + ".json",
                "natural series admission/identity differs")
        selected = series["sources"][source]
        attempts = selected.get("attempts")
        require(selected.get("state") == "complete" and selected.get("checkpoint_run") == "runs/" + run_id
                and isinstance(attempts, list) and attempts and type(attempts[-1].get("rc")) is int and attempts[-1]["rc"] == 0,
                "successful natural source attempt missing")
        child_id = attempts[-1].get("series_control_child_id")
        require(isinstance(child_id, str) and re.fullmatch(r"[0-9a-f]{32}", child_id)
                and ("child", child_id) not in seen
                and parent.get("status") == "complete" and parent.get("local_settlement") == "confirmed"
                and parent.get("stop_owner", {}).get("invocation_id") == invocation
                and parent["stop_owner"].get("owner_role") == "checkpoint_series_one_shot_cli"
                and parent.get("last_checkpoint", {}).get("run_id") == run_id
                and parent["last_checkpoint"].get("child_invocation_id") == child_id
                and run.get("schema_version") == 2 and run.get("run_id") == run_id
                and run.get("collector_git_sha") == code and run.get("collector_git_dirty") is False
                and run.get("status") in {"supported_scope_complete", "additive_scope_complete_current_inventory"}
                and selected.get("checkpoint_status") == run["status"]
                and set(run.get("sources", {})) == {source} and run["sources"][source].get("state") == "ingested"
                and type(child.get("schema_version")) is int and child["schema_version"] == 1 and child.get("invocation_id") == invocation
                and child.get("child_invocation_id") == child_id and child.get("run_id") == run_id
                and child.get("collector_git_sha") == code and all(child.get(field) is True for field in
                    ("guards_stopped", "nested_cleanup_confirmed", "terminal_evidence_saved")), "natural parent/child settlement differs")
        seen.add(("child", child_id))
        observed = unique(observation.get("tasks"), "key", TASKS)[key]
        require(observation.get("errors") == observation.get("running_instances") == []
                and observed.get("task_path") == current["task_path"] and observed.get("task_name") == current["task_name"]
                and observed.get("enabled") is current["enabled"] and observed.get("state") == "Ready"
                and type(observed.get("last_result")) is int and observed["last_result"] == 0
                and type(observed.get("missed_runs")) is int and observed["missed_runs"] == 0
                and all(clock(observed.get(field)) == clock(current[field]) for field in ("last_run_utc", "next_run_utc"))
                and evidence.raw(proof["xml"]) == evidence.raw(current["xml"]), "natural terminal task/XML differs")
        events = scheduler_events(proof["events"], evidence, bridge, task, current["actions"][0]["execute"])
        originals = [("event", record[0]) for record in events.values()] + [
            ("action", bridge["action_pid"], bridge["action_creation_utc"]),
            ("wsl", bridge["direct_wsl_pid"], bridge["direct_wsl_creation_utc"])]
        require(all(value not in seen for value in originals), "reused natural event/process identity")
        seen.update(originals)
        require(clock(current["last_run_utc"]) <= events[107][1] <= clock(bridge["action_creation_utc"])
                <= events[129][1] <= events[200][1] <= clock(audit["createdAt"]) <= admitted
                <= clock(bridge["direct_wsl_creation_utc"]) <= clock(bridge["observed_at"]) <= events[201][1]
                and admitted <= clock(series["started_at"]) <= milliseconds(parent.get("started_unix_ms"))
                <= clock(run["started_at"]) <= clock(run["finished_at"]) <= clock(series["finished_at"])
                <= milliseconds(parent.get("finished_unix_ms")) <= clock(audit["returnedAt"])
                <= events[201][1] <= events[102][1] <= clock(observation["observed_at"])
                < clock(current["next_run_utc"]), "natural operation chronology differs")
