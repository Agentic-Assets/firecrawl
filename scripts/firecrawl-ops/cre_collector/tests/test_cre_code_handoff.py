"""Offline contracts: use real consumers, never import copied operational helpers."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cre_code_handoff as operation
from cre_code_handoff_evidence import (
    Evidence, HandoffError, TASKS, TASK_PATH, json_bytes, sha, unique,
    validate_authorization, validate_lineage, validate_observation, validate_owned_qa,
    validate_qa, validate_xml_transition,
)
from cre_code_handoff_git import read_file, local_path, windows_path

FIXTURES = Path(__file__).parent / "fixtures/code_handoff"
NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
OLD = {"sha": "a" * 40, "tree": "b" * 40}
NEW = {"sha": "c" * 40, "tree": "d" * 40}


def encoded(value):
    return json.dumps(value).encode()


def xml(commit, enabled=True):
    return (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f'<RegistrationInfo><Description>Reviewed {commit}</Description></RegistrationInfo>'
            f'<Actions><Exec><Command>pwsh.exe</Command><Arguments>-ExpectedSha {commit}</Arguments>'
            f'</Exec></Actions><Settings><Enabled>{str(enabled).lower()}</Enabled></Settings></Task>').encode()


class Inputs:
    def __init__(self):
        self.data, self.rows = {}, []

    def add(self, name, value):
        raw = value if isinstance(value, bytes) else encoded(value)
        self.data[name] = raw
        self.rows.append({"role": name, "path": name, "bytes": len(raw), "sha256": sha(raw)})
        return name

    def evidence(self, release):
        release["inputs"] = self.rows
        return Evidence(release, self.data.__getitem__)


@pytest.fixture
def admitted():
    inputs = Inputs()
    baseline = []
    for key, name in TASKS.items():
        role = inputs.add(key, xml(OLD["sha"]))
        baseline.append({"key": key, "task_path": TASK_PATH, "task_name": name,
                         "enabled": True, "last_task_result": 0,
                         "last_run_utc": (NOW-timedelta(days=1)).isoformat(),
                         "next_run_utc": (NOW+timedelta(hours=2)).isoformat(),
                         "actions": [{"execute": "pwsh.exe", "arguments": "original", "working_directory": "C:/runtime"}],
                         "xml": role})
    release = {"deployed": OLD, "selected": NEW, "task_baseline": baseline}
    evidence = inputs.evidence(release)
    observed = {"schema": "cre-code-observation-v1", "observed_at": NOW.isoformat(),
                "native": OLD, "runtime": OLD, "owner_clear": True,
                "resources_ok": True, "queues_idle": True,
                "tasks": [dict(row, state="Ready", xml_sha256=sha(inputs.data[row["xml"]]))
                          for row in baseline]}
    return release, evidence, observed, inputs


def test_real_xml_consumer_only_allows_two_exact_pin_changes():
    before = xml(OLD["sha"])
    after = before.replace(OLD["sha"].encode(), NEW["sha"].encode())
    validate_xml_transition(before, after, OLD["sha"], NEW["sha"])
    with pytest.raises(HandoffError):
        validate_xml_transition(before, after.replace(b"pwsh.exe", b"other.exe"), OLD["sha"], NEW["sha"])
    with pytest.raises(HandoffError):
        validate_xml_transition(before.replace(b"-ExpectedSha", b"-WrongPin"), after, OLD["sha"], NEW["sha"])


@pytest.mark.parametrize("field,value", [("native", NEW), ("runtime", NEW), ("owner_clear", False),
                                        ("resources_ok", False), ("queues_idle", False)])
def test_readiness_mismatches_refuse(admitted, field, value):
    release, evidence, observed, _ = admitted
    observed[field] = value
    with pytest.raises(HandoffError):
        validate_observation(release, evidence, observed, NOW)


@pytest.mark.parametrize("age,accepted", [(299.999, True), (300, False), (301, False), (-1, False)])
def test_strict_capture_age_boundary(admitted, age, accepted):
    release, evidence, observed, _ = admitted
    observed["observed_at"] = (NOW-timedelta(seconds=age)).isoformat()
    if accepted:
        validate_observation(release, evidence, observed, NOW)
    else:
        with pytest.raises(HandoffError):
            validate_observation(release, evidence, observed, NOW)


@pytest.mark.parametrize("margin,accepted", [(900, True), (899.999, False)])
def test_trigger_margin_uses_current_clock(admitted, margin, accepted):
    release, evidence, observed, _ = admitted
    value = (NOW+timedelta(seconds=margin)).isoformat()
    release["task_baseline"][0]["next_run_utc"] = value
    observed["tasks"][0]["next_run_utc"] = value
    if accepted:
        validate_observation(release, evidence, observed, NOW)
    else:
        with pytest.raises(HandoffError):
            validate_observation(release, evidence, observed, NOW)


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "extra", "result", "clock", "xml", "enabled"])
def test_exact_task_fleet_does_not_collapse_or_waive(admitted, mutation):
    release, evidence, observed, _ = admitted
    if mutation == "duplicate":
        observed["tasks"][1] = copy.deepcopy(observed["tasks"][0])
    elif mutation == "missing":
        observed["tasks"].pop()
    elif mutation == "extra":
        observed["tasks"].append(dict(observed["tasks"][0], key="unexpected"))
    else:
        field, value = {"result": ("last_task_result", 1), "clock": ("last_run_utc", NOW.isoformat()),
                        "xml": ("xml_sha256", "0"*64), "enabled": ("enabled", False)}[mutation]
        observed["tasks"][0][field] = value
    with pytest.raises(HandoffError):
        validate_observation(release, evidence, observed, NOW)


def test_duplicate_json_object_keys_are_refused():
    with pytest.raises(HandoffError):
        json_bytes(b'{"tasks":[],"tasks":[]}')


def test_current_nonzero_result_needs_original_proof(admitted):
    release, evidence, _, _ = admitted
    release["task_baseline"][2]["last_task_result"] = 1
    with pytest.raises(HandoffError, match="exact reviewed provenance"):
        validate_lineage(release, evidence)


def test_real_retained_readback_unique_sets_and_failed_terminal_shape():
    readback = json_bytes((FIXTURES / "readback.json").read_bytes())
    assert set(unique(readback["tasks"], "key", TASKS)) == set(TASKS)
    assert set(unique(readback["task_comparisons"], "key", TASKS)) == set(TASKS)
    readback["tasks"].append(readback["tasks"][0])
    with pytest.raises(HandoffError, match="duplicate"):
        unique(readback["tasks"], "key", TASKS)
    terminal = json_bytes((FIXTURES / "failed-terminal.json").read_bytes())
    assert terminal["native"]["last_task_result"] == 1
    assert terminal["linux"]["series_status"] == "failed"
    assert terminal["linux"]["source_state_counts"] == {"complete": 39, "failed_source": 1}


def test_changed_hash_is_detected_again_at_authorization_boundary(admitted):
    release, evidence, observed, inputs = admitted
    inputs.add("review", b"independent exact input review")
    evidence = inputs.evidence(release)
    release_raw, observation_raw = encoded(release), encoded(observed)
    auth = {"schema": "cre-code-authorization-v1", "release_sha256": sha(release_raw),
            "observation_sha256": sha(observation_raw), "authorized_at": NOW.isoformat(),
            "expires_at": (NOW+timedelta(seconds=299)).isoformat(),
            "review": {"path": "review", "sha256": sha(inputs.data["review"]),
                       "reviewer": "independent", "approval_ref": "AGENTIC-test"}}
    validate_authorization(release_raw, release, evidence, observation_raw, auth, NOW)
    inputs.data["interra"] += b"changed"
    with pytest.raises(HandoffError, match="changed input"):
        validate_authorization(release_raw, release, evidence, observation_raw, auth, NOW)


def test_authorization_publication_rechecks_before_final_commit(tmp_path, monkeypatch, admitted):
    release, evidence, observed, _ = admitted
    release_path, observation_path, review_path = [tmp_path/name for name in ("release.json", "observation.json", "review.txt")]
    release_raw = encoded(release)
    release_path.write_bytes(release_raw); observation_path.write_bytes(encoded(observed)); review_path.write_bytes(b"review")
    args = SimpleNamespace(release=release_path, observation=observation_path, review=review_path,
                           authorization=tmp_path/"authorization.json", output=tmp_path/"operation",
                           reviewer="independent", approval_ref="AGENTIC-test")
    monkeypatch.setattr(operation, "now", lambda: NOW)
    original = operation.write_new
    def changed_after_pending(path, value):
        original(path, value)
        observation_path.write_bytes(b"changed")
    monkeypatch.setattr(operation, "write_new", changed_after_pending)
    with pytest.raises(HandoffError, match="publication inputs changed"):
        operation.authorize(args, release_raw, release, evidence)
    assert not args.authorization.exists()


def test_auth_outputs_are_create_only(tmp_path):
    output = tmp_path/"receipt.json"
    operation.write_new(output, {"original": True})
    with pytest.raises(FileExistsError):
        operation.write_new(output, {"replacement": True})
    assert json.loads(output.read_text()) == {"original": True}


def test_alias_and_hardlink_evidence_refused(tmp_path):
    target = tmp_path/"target"
    target.write_bytes(b"original")
    linked = tmp_path/"hardlink"
    linked.hardlink_to(target)
    with pytest.raises(HandoffError):
        read_file(target)


def test_paths_accept_equivalent_separator_forms():
    assert windows_path("C:\\Users\\Owner\\source") == windows_path("/mnt/c/Users/Owner/source")
    assert str(local_path("C:\\Users\\Owner\\source")).replace("\\", "/").endswith("/Users/Owner/source")


def test_uncertain_owned_command_keeps_actual_error_and_no_success_receipt(tmp_path):
    class Uncertain(RuntimeError):
        pass
    def command(*args, **kwargs):
        raise Uncertain("group settlement unknown")
    checkpoint = SimpleNamespace(run_command=command, safe_process_env=lambda: {})
    runner = operation.Foreground({}, checkpoint, tmp_path)
    with pytest.raises(Uncertain, match="settlement unknown"):
        runner.run("fixture", ["fixture"])
    assert not list(tmp_path.glob("*.json"))


@pytest.fixture
def lineage_case(admitted):
    release, _, _, inputs = admitted
    original = {"sha": "1"*40, "tree": "2"*40}
    intermediate = {"sha": "3"*40, "tree": "4"*40}
    terminal = json_bytes((FIXTURES/"failed-terminal.json").read_bytes())
    key = "qualified"
    baseline = release["task_baseline"][2]
    baseline["last_task_result"] = 1
    original_xml = inputs.add("failed-xml", xml(original["sha"]))
    terminal["evidence_files"]["task_definition_sha256"] = sha(inputs.data[original_xml])
    terminal["linux"].update(git_head=original["sha"], git_tree=original["tree"])
    operation_tuple = terminal["operation"]
    settlement = {"operation": {
        "instance": operation_tuple["task_instance_id"], "audit": operation_tuple["audit"],
        "series": operation_tuple["series_id"], "run": operation_tuple["checkpoint_run_id"],
        "parent": operation_tuple["parent_invocation_id"], "child": operation_tuple["child_invocation_id"],
        "task": TASK_PATH+TASKS[key], "last_run_utc": baseline["last_run_utc"], "task_result": 1,
        "task_xml_sha256": sha(inputs.data[original_xml]), "code": original["sha"], "tree": original["tree"],
    }}
    qualification = {"operation": dict(operation_tuple, native_task_result=1,
                     task_definition_sha256=sha(inputs.data[original_xml]),
                     collector_git_sha=original["sha"], collector_git_tree=original["tree"])}
    edges = []
    for index, (before, after) in enumerate(((original, intermediate), (intermediate, OLD))):
        readback = json_bytes((FIXTURES/"readback.json").read_bytes())
        readback.update(deployment_sha=after["sha"], deployment_tree=after["tree"])
        definitions=[]
        comparisons, tasks=[], []
        for member in release["task_baseline"]:
            name = member["key"]
            prior = inputs.add(f"{index}-{name}-before", xml(before["sha"]))
            following = inputs.add(f"{index}-{name}-after", xml(after["sha"]))
            definitions.append({"key": name, "before": prior, "after": following})
            comparisons.append({"key": name, "prior_xml_sha256": sha(inputs.data[prior]),
                "observed_xml_sha256": sha(inputs.data[following]), "raw_xml_equal_after_only_sha_replacement": True,
                "enabled_before": True, "enabled_after": True, "state_before": "Ready", "state_after": "Ready",
                "result_before": member["last_task_result"], "result_after": member["last_task_result"],
                "last_run_unchanged": True, "next_run_unchanged": True})
            tasks.append({"key": name, "enabled": True, "state": "Ready", "last_result": member["last_task_result"],
                "last_run_utc": member["last_run_utc"], "next_run_utc": member["next_run_utc"],
                "xml_sha256": sha(inputs.data[following])})
        readback.update(task_comparisons=comparisons, tasks=tasks)
        edges.append({"old": before, "new": after, "xml": definitions,
                      "readback": inputs.add(f"readback-{index}", readback)})
    baseline["failure"]={"terminal": inputs.add("terminal", terminal),
        "settlement": inputs.add("settlement", settlement), "qualification": inputs.add("qualification", qualification),
        "xml": original_xml, "identity": original, "operation": operation_tuple, "transitions": edges}
    return release, inputs.evidence(release), inputs


def test_original_failure_reaches_current_baseline_through_every_real_shape_edge(lineage_case):
    release, evidence, _ = lineage_case
    validate_lineage(release, evidence)


@pytest.mark.parametrize("mutation", ["missing", "reversed", "cycle", "wrong_original", "result", "other_task"])
def test_lineage_gaps_and_result_waivers_refuse(lineage_case, mutation):
    release, evidence, _ = lineage_case
    failure=release["task_baseline"][2]["failure"]
    if mutation == "missing":
        failure["transitions"].pop(0)
    elif mutation == "reversed":
        failure["transitions"].reverse()
    elif mutation == "cycle":
        failure["transitions"][1]["new"] = failure["identity"]
    elif mutation == "wrong_original":
        failure["operation"] = dict(failure["operation"], child_invocation_id="other")
    elif mutation == "result":
        release["task_baseline"][2]["last_task_result"] = 2
    else:
        release["task_baseline"][0]["last_task_result"] = 1
    with pytest.raises(HandoffError):
        validate_lineage(release, evidence)


def test_historical_qa_reader_preserves_actual_final_and_reused_identities():
    summary=json_bytes((FIXTURES/"combined-qa.json").read_bytes())
    inputs=Inputs()
    role=inputs.add("summary", summary)
    policy={"producer":"combined-candidate-v1", "summary":role,
        "final_executed":summary["final_candidate"], "broad_executed":summary["broad_executed_candidate"],
        "expectations":{field:summary[field] for field in
            ("scope","full_python","node","ops","final_focused_passed","reuse_boundary","remaining_gates")}}
    release={"selected":{"tree":summary["final_candidate"]["tree"]},"qa":policy}
    # Identity mutation refuses before any unavailable historical payload is read.
    policy["broad_executed"]=dict(policy["broad_executed"], head=policy["final_executed"]["head"])
    with pytest.raises(HandoffError, match="QA execution identities"):
        validate_qa(release,inputs.evidence(release))


def test_new_owned_qa_uses_actual_merged_log_contract_without_invented_eof():
    inputs=Inputs();log=inputs.add("log",b"[time] command: fixture\n[time] exit: 0\n")
    inputs.add("review",b"independent fixture scope review")
    receipt={"schema":"cre-owned-gate-v1","producer":"checkpoint-run-command-v1",
        "identity":NEW,"argv":["fixture"],"cwd":"/fixture","exit_code":0,
        "local_settlement":"confirmed","unchanged":True,"log_closed":True,
        "started_at":NOW.isoformat(),"finished_at":NOW.isoformat(),"command_log_sha256":sha(inputs.data[log])}
    receipt_role=inputs.add("receipt",receipt)
    gate={"name":"offline fixture","receipt":receipt_role,"executed":NEW,"argv":["fixture"],
          "cwd":"/fixture","command_log":log}
    release={"selected":NEW,"qa":{"producer":"owned-gates-v1","scope":"offline fixtures",
             "final_executed":NEW,"gates":[gate],"independent_review":"review"}}
    evidence=inputs.evidence(release)
    validate_owned_qa(release,evidence)
    gate["executed"]=OLD
    with pytest.raises(HandoffError):
        validate_owned_qa(release,evidence)


def test_default_preview_does_not_construct_any_host_adapter(monkeypatch, capsys):
    release={"selected":NEW}
    monkeypatch.setattr(operation,"load_release",lambda _: (b"immutable",release,None))
    def forbidden(*args,**kwargs):
        raise AssertionError("preview attempted a host call")
    monkeypatch.setattr(operation,"Git",forbidden)
    monkeypatch.setattr(operation,"runtime_modules",forbidden)
    assert operation.main(["--release","fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["execution"] is False


def test_unknown_qa_schema_refuses_without_host_calls():
    release={"qa":{"producer":"arbitrary-jsonpath-plugin"}}
    with pytest.raises(HandoffError,match="unknown QA producer"):
        validate_qa(release,None)


def test_linux_process_observer_excludes_only_ancestry_and_emits_no_commands(tmp_path):
    own=tmp_path/"10";own.mkdir();(own/"stat").write_text("10 (fixture) S 0 0 0")
    (own/"cmdline").write_bytes(b"python3\0/tooling/cre_code_handoff.py\0--apply\0")
    foreign=tmp_path/"20";foreign.mkdir()
    (foreign/"cmdline").write_bytes(b"python3\0/other/cre_code_handoff.py\0--apply\0private-value\0")
    assert operation.visible_linux_frontends(tmp_path,pid=10) == [{"pid":20,"roles":["cre_code_handoff.py"]}]
