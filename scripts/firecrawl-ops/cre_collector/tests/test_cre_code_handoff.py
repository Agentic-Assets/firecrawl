"""Offline contracts: use real consumers, never import copied operational helpers."""

import copy
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import cre_code_handoff as operation
from cre_code_handoff_evidence import (
    TASK_PATH,
    TASKS,
    Evidence,
    HandoffError,
    json_bytes,
    role_paths,
    sha,
    unique,
    validate_authorization,
    validate_lineage,
    validate_observation,
    validate_owned_qa,
    validate_qa,
    validate_release,
    validate_xml_transition,
)
from cre_code_handoff_git import Git, local_path, read_file, windows_path
from cre_code_handoff_host import qualify_host, qualify_native
from cre_code_handoff_transition import make_transition, reference, validate_transition

FIXTURES = Path(__file__).parent / "fixtures/code_handoff"
NOW = datetime(2026, 10, 22, 9, tzinfo=UTC)
OLD = {"sha": "a" * 40, "tree": "b" * 40}
NEW = {"sha": "c" * 40, "tree": "d" * 40}


def encoded(value):
    return json.dumps(value).encode()


def host_observation():
    containers = [{"id": str(i), "name": "/playwright" if i == 0 else f"/service-{i}",
                   "image": f"sha256:{i}", "status": "running", "started_at": NOW.isoformat(),
                   "memory": 1024, "nano_cpus": 1, "cpu_quota": 1, "cpu_period": 1,
                   "environment_sha256": "0"*64} for i in range(5)]
    return {"containers": containers, "browser": {"status": "healthy", "activePages": 0, "maxConcurrentPages": 4},
            "queues": dict.fromkeys(("jobs_in_queue", "active_jobs_in_queue", "waiting_jobs_in_queue", "active_crawl_count"), 0),
            "linux_frontends": [], "linux_visibility": "synthetic current UID PID/role observation",
            "linux_resources": {"cpu_percent": 20, "memory_level": 1,
                                "memory": {"host": {"memory_available_bytes": 8*1024**3}, "causes": []}},
            "windows_resources": {"admitted": True, "cpuPercent": 20, "availablePhysicalGiB": 8}}


def attestation(subject, body_path, body, scope="code-only-release"):
    return {"schema": "cre-code-review-v1", "subject": subject, "scope": scope,
            "body": {"path": str(body_path), "sha256": sha(body), "bytes": len(body)},
            "verdict": "approved", "reviewer": "independent", "approval_ref": "AGENTIC-test"}


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
    inputs.add("runtime_snapshot", {"containers": host_observation()["containers"]})
    release = {"deployed": OLD, "selected": NEW, "tooling": NEW, "task_baseline": baseline}
    evidence = inputs.evidence(release)
    observed = {"schema": "cre-code-observation-v1", "observed_at": NOW.isoformat(), "qualified_at": NOW.isoformat(),
                "native": OLD, "runtime": OLD, "ownership": {"phase": "released-clear", "verified": True, "recovery_required": False},
                "resources_ok": True, "queues_idle": True,
                "tasks": [dict(row, state="Ready", xml_sha256=sha(inputs.data[row["xml"]]))
                          for row in baseline]}
    observed.update(host_observation())
    return release, evidence, observed, inputs


def test_real_xml_consumer_only_allows_two_exact_pin_changes():
    before = xml(OLD["sha"])
    after = before.replace(OLD["sha"].encode(), NEW["sha"].encode())
    validate_xml_transition(before, after, OLD["sha"], NEW["sha"])
    with pytest.raises(HandoffError):
        validate_xml_transition(before, after.replace(b"pwsh.exe", b"other.exe"), OLD["sha"], NEW["sha"])
    with pytest.raises(HandoffError):
        validate_xml_transition(before.replace(b"-ExpectedSha", b"-WrongPin"), after, OLD["sha"], NEW["sha"])


@pytest.mark.parametrize("field,value", [("native", NEW), ("runtime", NEW), ("ownership", {}),
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
    release_raw, observation_raw = encoded(release), encoded(observed)
    # The attestation is outside the immutable release, avoiding a circular hash.
    inputs.data["review-body"] = b"independent exact input review"
    inputs.data["review"] = encoded(attestation({"release_sha256": sha(release_raw), "tooling": NEW},
                                              "review-body", inputs.data["review-body"]))
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
    body_path=tmp_path/"review-body.md"; body_path.write_bytes(b"review")
    release_path.write_bytes(release_raw); observation_path.write_bytes(encoded(observed))
    review_path.write_bytes(encoded(attestation({"release_sha256": sha(release_raw), "tooling": NEW},
                                               body_path, b"review")))
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
    checkpoint = SimpleNamespace(run_command=command, safe_process_env=dict)
    runner = operation.Foreground({}, checkpoint, tmp_path)
    with pytest.raises(Uncertain, match="settlement unknown"):
        runner.run("fixture", ["fixture"])
    assert not list(tmp_path.glob("*.json"))


@pytest.fixture
def lineage_case(admitted):
    release, _, _, inputs = admitted
    original = {"sha": "1"*40, "tree": "2"*40}
    intermediate = {"sha": "3"*40, "tree": "4"*40}
    # Explicitly synthetic producer-shaped case; authentic fixture stays untouched.
    terminal = {"operation": {"task_instance_id": "fixture-instance", "audit": "fixture-audit",
                "series_id": "fixture-series", "checkpoint_run_id": "fixture-run",
                "parent_invocation_id": "fixture-parent", "child_invocation_id": "fixture-child"},
                "native": {"last_task_result": 1, "task_state": "Ready"}, "evidence_files": {},
                "linux": {"parent_local_settlement": "confirmed", "guards_stopped": True,
                          "nested_cleanup_confirmed": True, "canonical_lock_directory_exists": False,
                          "authority_state": "normal"}}
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
        readback = {"read_only": True, "all_checks_passed": True,
                    "checks": {"exact_three_task_lineage": True, "task_state_result_and_clocks_unchanged": True}}
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
    for field in ("last_run_utc","next_run_utc"):release["task_baseline"][0][field]=release["task_baseline"][0][field].replace("+00:00","Z");validate_lineage(release,evidence)


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
    inputs.add("qa-attestation", attestation({"qa_identity": NEW, "tooling": NEW},
               "review", inputs.data["review"], "offline fixtures"))
    release={"selected":NEW,"tooling":NEW,"qa":{"producer":"owned-gates-v1","scope":"offline fixtures",
             "final_executed":NEW,"gates":[gate],"independent_review":"review", "review_attestation":"qa-attestation"}}
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


@pytest.fixture
def complete_case(admitted):
    release, _, observed, inputs = admitted
    path="scripts/firecrawl-ops/cre_collector/fixture.py"
    inputs.add("allowed_paths_nul",path.encode()+b"\0")
    inputs.add("bundle",b"explicit synthetic bundle fixture; never fetched")
    inputs.add("qa-body",b"review of the complete synthetic gate scope")
    inputs.add("qa-attestation",attestation({"qa_identity":NEW,"tooling":NEW},"qa-body",inputs.data["qa-body"],"offline fixture scope"))
    inputs.add("gate-log",b"fixture command completed\n")
    inputs.add("gate",{"schema":"cre-owned-gate-v1","producer":"checkpoint-run-command-v1",
        "identity":NEW,"argv":["fixture"],"cwd":"/fixture","exit_code":0,"local_settlement":"confirmed",
        "unchanged":True,"log_closed":True,"started_at":NOW.isoformat(),"finished_at":NOW.isoformat(),
        "command_log_sha256":sha(inputs.data["gate-log"])})
    release.update(schema="cre-code-release-v1",development=NEW,
        roots={"native":"C:/native","runtime":"/fixture/runtime","development":"C:/dev","tooling":"/mnt/c/dev"},
        programs={"native_git":"C:/Git/git.exe","powershell":"C:/PowerShell/pwsh.exe"},
        wsl={"distro":"fixture-Ubuntu","user":"fixture"},allowed_paths=[path],
        objects={path:{"before":None,"after":{"mode":"100644","type":"blob","oid":"e"*40}}},
        runtime_env_file="/fixture/private.env",preserve_files=["/fixture/private.env"],
        qa={"producer":"owned-gates-v1","scope":"offline fixture scope","final_executed":NEW,
            "independent_review":"qa-body","review_attestation":"qa-attestation",
            "gates":[{"name":"fixture","receipt":"gate","executed":NEW,"argv":["fixture"],
                      "cwd":"/fixture","command_log":"gate-log"}]})
    return release, inputs.evidence(release), observed, inputs


def test_complete_positive_immutable_release_and_qa_consumer_join(complete_case):
    release,evidence,observed,_=complete_case
    validate_release(release,evidence)
    validate_observation(release,evidence,observed,NOW)
    roots=role_paths(release)
    assert roots["development"] == roots["tooling"] == "C:/dev"


@pytest.mark.parametrize("mutation",["release","tooling"])
def test_old_review_body_cannot_authorize_another_subject(admitted,mutation):
    release,evidence,observed,inputs=admitted
    raw=encoded(release);observation=encoded(observed)
    inputs.data["body"]=b"same perfectly intact prior review"
    inputs.data["attestation"]=encoded(attestation({"release_sha256":sha(raw),"tooling":NEW},"body",inputs.data["body"]))
    auth={"schema":"cre-code-authorization-v1","release_sha256":sha(raw),"observation_sha256":sha(observation),
          "authorized_at":NOW.isoformat(),"expires_at":(NOW+timedelta(seconds=299)).isoformat(),
          "review":{"path":"attestation","sha256":sha(inputs.data["attestation"]),"reviewer":"independent","approval_ref":"AGENTIC-test"}}
    validate_authorization(raw,release,evidence,observation,auth,NOW)
    if mutation == "release":
        raw+=b" ";auth["release_sha256"]=sha(raw)
    else:
        release["tooling"]=OLD
    with pytest.raises(HandoffError,match="review subject"):
        validate_authorization(raw,release,evidence,observation,auth,NOW)


@pytest.mark.parametrize("mutation",["memory_bool","memory_low","container_epoch","browser_cap","native_bool","native_low"])
def test_actual_host_qualifiers_reject_weakened_resources(mutation):
    observed=host_observation();expected=copy.deepcopy(observed)
    if mutation == "memory_bool":observed["linux_resources"]["memory_level"]=True
    elif mutation == "memory_low":observed["linux_resources"]["memory"]["host"]["memory_available_bytes"]-=1
    elif mutation == "container_epoch":observed["containers"][0]["started_at"]="another epoch"
    elif mutation == "browser_cap":observed["browser"]["maxConcurrentPages"]=5
    elif mutation == "native_bool":observed["windows_resources"]["admitted"]=1
    else:observed["windows_resources"]["availablePhysicalGiB"]=7.99
    with pytest.raises(HandoffError):
        qualify_host(observed,expected)
        qualify_native(observed["windows_resources"])


def git_command(repo,*arguments):
    return subprocess.run(["git","-C",str(repo),*arguments],check=True,capture_output=True).stdout


@pytest.fixture
def git_case(tmp_path,monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL",os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM","1")
    source=tmp_path/"source";source.mkdir()
    git_command(source,"init","-b","main")
    git_command(source,"config","user.email","fixture@example.invalid")
    git_command(source,"config","user.name","Offline Fixture")
    path="scripts/firecrawl-ops/cre_collector/fixture.py"
    leaf=source/path;leaf.parent.mkdir(parents=True);leaf.write_text("old\n")
    git_command(source,"add",".");git_command(source,"commit","-m","fixture old")
    old={"sha":git_command(source,"rev-parse","HEAD").decode().strip(),"tree":git_command(source,"rev-parse","HEAD^{tree}").decode().strip()}
    target=tmp_path/"target"
    subprocess.run(["git","clone","--no-hardlinks",str(source),str(target)],check=True,capture_output=True)
    leaf.write_text("new\n");git_command(source,"commit","-am","fixture new")
    new={"sha":git_command(source,"rev-parse","HEAD").decode().strip(),"tree":git_command(source,"rev-parse","HEAD^{tree}").decode().strip()}
    git_command(source,"update-ref","refs/remotes/origin/main",new["sha"])
    bundle=tmp_path/"fixture.bundle";git_command(source,"bundle","create",str(bundle),"refs/remotes/origin/main")
    git=Git("unused-native-git")
    release={"deployed":old,"selected":new,"allowed_paths":[path],
             "objects":{path:{"before":git.entries(source,old["sha"])[path],"after":git.entries(source,new["sha"])[path]}}}
    return git,target,release,bundle,path


def test_real_temporary_git_fast_forward_and_object_guards(git_case):
    git,target,release,bundle,_=git_case
    git.fast_forward(target,release,bundle)
    git.clean(target,release["selected"],tracking=release["selected"]["sha"])


@pytest.mark.parametrize("mutation",["hardlink","hook","object"])
def test_temporary_git_guards_refuse_before_checkout_change(git_case,tmp_path,mutation):
    git,target,release,bundle,path=git_case
    if mutation == "hardlink":(tmp_path/"alias").hardlink_to(target/path)
    elif mutation == "hook":(target/".git/hooks/post-merge").write_text("exit 0\n")
    else:release["objects"][path]["after"]["oid"]="0"*40
    with pytest.raises(HandoffError):git.fast_forward(target,release,bundle)
    assert git.text(target,"rev-parse","HEAD") == release["deployed"]["sha"]


@pytest.fixture
def apply_case(complete_case,tmp_path,monkeypatch):
    release,evidence,observed,inputs=complete_case
    release_file=tmp_path/"release.json";raw=encoded(release);release_file.write_bytes(raw)
    observation_file=tmp_path/"observation.json";observation_file.write_bytes(encoded(observed))
    body=tmp_path/"review.md";body.write_bytes(b"exact complete fixture review")
    review=tmp_path/"review.json";review.write_bytes(encoded(attestation({"release_sha256":sha(raw),"tooling":NEW},body,body.read_bytes())))
    output=tmp_path/"operation";auth_file=tmp_path/"authorization.json"
    auth={"schema":"cre-code-authorization-v1","release_sha256":sha(raw),"observation_sha256":sha(observation_file.read_bytes()),
          "authorized_at":NOW.isoformat(),"expires_at":(NOW+timedelta(seconds=299)).isoformat(),
          "operation_output":str(output),"review":{"path":str(review),"sha256":sha(review.read_bytes()),"reviewer":"independent","approval_ref":"AGENTIC-test"}}
    auth_file.write_bytes(encoded(auth))
    state={"fault":None,"calls":[],"enabled":[True]*3,"recovery":None,"acquisitions":0,"reached":[],"ownership_calls":0,"interrupt":None}
    def point(name):
        state["reached"].append(name)
        if state["fault"] == name:
            raise state["interrupt"] if state["interrupt"] is not None else HandoffError("injected "+name)
    class Lease:
        def __init__(self,path,*,recovery_required):
            assert path == tmp_path/"canonical-lock" and recovery_required is True
        def __enter__(self):state["acquisitions"]+=1;state["recovery"]=True;return self
        def clear_recovery_requirement(self):
            state["calls"].append("clear")
            point("clear_before")
            state["recovery"]=False
            point("clear_after")
        def __exit__(self,*args):
            state["calls"].append("lease_exit")
            point("lease_exit")
    def lock_path(root):
        assert root == local_path(release["roots"]["runtime"])
        return tmp_path/"canonical-lock"
    def command(argv,log_path,*,env):
        assert argv[:2] == ["bash","-c"] and argv[4].endswith("/scripts/firecrawl-ops/cre_collector")
        log_path.write_text("mocked dependency command; no process was dispatched\n")
        return 0
    checkpoint=SimpleNamespace(SharedLock=Lease,canonical_shared_lock_dir=lock_path,
        safe_process_env=dict,run_command=command,COLLECTOR_DIR=tmp_path/"tooling-collector")
    git=SimpleNamespace(development=lambda _:state["calls"].append("development"),
                        clean=lambda *args,**kwargs:None,
                        fast_forward=lambda root,_release,_bundle:state["calls"].append("ff:"+root))
    def readiness(*args,selected=False,**kwargs):
        assert kwargs["evidence"] is evidence
        if selected and "Verify" in state["calls"]:point("post_restore_readiness")
        return dict(host_observation(),native=NEW if selected else OLD,runtime=NEW if selected else OLD,
                    ownership={"phase":"owned-maintenance" if kwargs.get("owned") else "released-clear",
                               "verified":True,"recovery_required":kwargs.get("owned",False)},resources_ok=True,queues_idle=True)
    def native(self,mode,*args,**kwargs):
        state["calls"].append(mode)
        if mode == "Hold":
            point("before_hold")
            operation.write_new(output/"mutation-start.json",{"schema":"cre-code-mutation-v1",
                "release_sha256":sha(raw),"authorization_sha256":sha(auth_file.read_bytes()),
                "operation_output":str(output),"tooling":NEW,"fleet":release["task_baseline"]})
            state["enabled"]=[False]*3
            for row in release["task_baseline"]:(output/(row["key"]+"-before.xml")).write_bytes(inputs.data[row["xml"]])
        elif mode == "Restore":
            state["enabled"][0]=True
            point("first_enable")
            if state["fault"] in {"cleanup_receipt","failure_receipt"}:point(state["fault"])
            state["enabled"]=[True]*3
            for row in release["task_baseline"]:
                (output/(row["key"]+"-final.xml")).write_bytes(inputs.data[row["xml"]].replace(OLD["sha"].encode(),NEW["sha"].encode()))
            point("native_receipt")
        elif mode == "Verify":
            tasks=[]
            for row in release["task_baseline"]:
                tasks.append(dict(row,state="Ready",xml_sha256=sha((output/(row["key"]+"-final.xml")).read_bytes())))
            operation.write_new(output/"native-observation.json",{"observed_at":NOW.isoformat(),"tasks":tasks,
                                "windows_resources":host_observation()["windows_resources"]})
        elif mode == "FailureHold":
            assert operation.mutation_started(output,release,raw,auth_file.read_bytes())
            state["enabled"]=[False]*3
            if state["interrupt"] is not None:raise SystemExit("secondary cleanup interruption")
            if state["fault"] == "cleanup_receipt":raise OSError("injected failure-hold receipt write")
        elif mode != "Repin":raise AssertionError("unexpected external dispatcher")
    real_write=operation.write_new
    def write(path,value):
        if Path(path).name == "failure.json" and state["interrupt"] is not None:raise KeyboardInterrupt("secondary publication interruption")
        if state["fault"] == "failure_receipt" and Path(path).name == "failure.json":raise OSError("injected failure receipt write")
        point(Path(path).name)
        real_write(path,value)
    def owned(actual_checkpoint,path,pid):
        assert actual_checkpoint is checkpoint and path == tmp_path/"canonical-lock" and pid == os.getpid()
        assert state["acquisitions"] == 1 and state["recovery"] is True
        state["ownership_calls"]+=1
    def released(*args):
        assert args == (checkpoint,tmp_path/"canonical-lock")
        point("lock_clear");state["calls"].append("released")
    real_authorization=operation.validate_authorization
    def authorization(*args,**kwargs):
        real_authorization(*args,**kwargs)
        if (output/"commit-ready.json").exists():point("precommit")
    monkeypatch.setattr(operation,"now",lambda:NOW)
    monkeypatch.setattr(operation,"load_release",lambda _: (raw,release,evidence))
    monkeypatch.setattr(operation,"Git",lambda _:git)
    monkeypatch.setattr(operation,"runtime_modules",lambda *_:(checkpoint,None,None,None))
    monkeypatch.setattr(operation,"readiness",readiness)
    monkeypatch.setattr(operation,"preserved",lambda _: {"unchanged":"digest"})
    monkeypatch.setattr(operation,"installed_dependencies",lambda _: dict.fromkeys(("package.json","package-lock.json","node_modules/.package-lock.json"),"0"*64))
    monkeypatch.setattr(operation,"assert_owned",owned)
    monkeypatch.setattr(operation,"lock_clear",released)
    monkeypatch.setattr(operation,"validate_authorization",authorization)
    monkeypatch.setattr(operation.Foreground,"native",native)
    monkeypatch.setattr(operation,"write_new",write)
    argv=["--release",str(release_file),"--apply","--observation",str(observation_file),
          "--authorization",str(auth_file),"--output",str(output)]
    return state,output,argv


def test_actual_python_entrypoint_apply_positive_with_all_external_boundaries_mocked(apply_case):
    state,output,argv=apply_case
    assert operation.main(argv) == 0
    assert state["ownership_calls"] == 6
    assert state["enabled"] == [True]*3 and state["recovery"] is False
    assert [call for call in state["calls"] if call in {"Hold","Repin","Restore","Verify","clear","released"}] == ["Hold","Repin","Restore","Verify","clear","released"]
    assert json.loads((output/"terminal.json").read_text())["status"] == "committed"


@pytest.mark.parametrize("fault,classification,mutated",[
    ("before_hold","precommit-failure",False),("first_enable","precommit-failure",True),
    ("native_receipt","precommit-failure",True),("post_restore_readiness","precommit-failure",True),
    ("held-ready.json","precommit-failure",True),("commit-ready.json","precommit-failure",True),
    ("precommit","precommit-failure",True),
    ("clear_before","commit-uncertain",True),("clear_after","commit-uncertain",True),
    ("lease_exit","commit-uncertain",True),("lock_clear","commit-uncertain",True),("terminal.json","postcommit-confirmation-failure",True),
    ("cleanup_receipt","precommit-failure",True),
])
def test_apply_failure_boundaries_preserve_original_and_classify_actual_recovery(apply_case,fault,classification,mutated):
    state,output,argv=apply_case;state["fault"]=fault
    with pytest.raises(HandoffError,match="injected "+fault):operation.main(argv)
    assert fault in state["reached"]
    record=json.loads((output/"failure.json").read_text())
    assert record["classification"] == classification
    assert record["original_error"] == "injected "+fault
    assert state["acquisitions"] == 1
    assert ("FailureHold" in state["calls"]) is mutated
    assert state["enabled"] == ([False]*3 if mutated else [True]*3)
    assert record["recovery_retained"].startswith("not asserted")


def test_failure_receipt_error_does_not_mask_original(apply_case,capsys):
    state,_,argv=apply_case;state["fault"]="failure_receipt"
    with pytest.raises(HandoffError,match="injected failure_receipt"):operation.main(argv)
    assert "Original HandoffError: injected failure_receipt" in capsys.readouterr().err
    assert state["enabled"] == [False]*3


@pytest.mark.parametrize("exception_type",[KeyboardInterrupt,SystemExit])
def test_original_interrupt_survives_cleanup_and_publication_interruptions(apply_case,capsys,exception_type):
    state,_,argv=apply_case
    original=exception_type("original operation interruption")
    state.update(fault="first_enable",interrupt=original)
    with pytest.raises(exception_type) as caught:operation.main(argv)
    assert caught.value is original and "first_enable" in state["reached"]
    assert "FailureHold" in state["calls"] and state["enabled"] == [False]*3
    assert f"Original {exception_type.__name__}: original operation interruption" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "linux",reason="canonical owned command uses POSIX process groups")
def test_real_canonical_owned_command_producer_joins_qa_reader(git_case,tmp_path):
    import cre_checkpoint_refresh as checkpoint

    git,target,release,_,_=git_case
    selected=release["deployed"]
    collector=target/"scripts/firecrawl-ops/cre_collector"
    output=tmp_path/"gate-output";output.mkdir()
    command=["bash","-c",'cd -- "$1" && shift && exec "$@"',"offline-gate",str(collector),
             sys.executable,"-I","-c","print('owned offline fixture')"]
    runner=operation.Foreground({"roots":{"runtime":str(target)},"programs":{"native_git":"unused"},"selected":selected},checkpoint,output)
    receipt=runner.run("fixture",command,effective_cwd=collector)
    inputs=Inputs();inputs.add("receipt",(output/"01-fixture.json").read_bytes());inputs.add("log",(output/"01-fixture.log").read_bytes())
    inputs.add("body",b"review of actual offline subprocess")
    inputs.add("review",attestation({"qa_identity":selected,"tooling":selected},"body",inputs.data["body"],"owned fixture"))
    source={"selected":selected,"tooling":selected,"qa":{"producer":"owned-gates-v1","scope":"owned fixture",
        "final_executed":selected,"independent_review":"body","review_attestation":"review",
        "gates":[{"name":"fixture","receipt":"receipt","executed":selected,"argv":command,"cwd":str(collector),"command_log":"log"}]}}
    validate_owned_qa(source,inputs.evidence(source))
    assert receipt["local_settlement"] == "confirmed" and "stdout_eof" not in receipt
    git.clean(target,selected,tracking=selected["sha"])


def synthetic_transition_files(root,source,inputs):
    """A complete synthetic producer instance; no historical metadata is rewritten."""
    operation_root=root/"operation";operation_root.mkdir(parents=True)
    independent=root/"independent";independent.mkdir()
    roles={"runtime_snapshot",*(row["xml"] for row in source["task_baseline"])}
    source=copy.deepcopy(source)
    for index,row in enumerate(source["inputs"]):
        if row["role"] in roles:
            pinned=root/f"pinned-{index}.data"
            raw=inputs.data[row["path"]];pinned.write_bytes(raw)
            row["path"]=str(pinned);inputs.data[str(pinned)]=raw
    release_path=root/"release.json";release_raw=encoded(source);release_path.write_bytes(release_raw)
    digest=sha(release_raw);auth_digest="5"*64;output=str(operation_root)
    mutation={"schema":"cre-code-mutation-v1","release_sha256":digest,"authorization_sha256":auth_digest,
              "operation_output":output,"tooling":source["tooling"],"fleet":source["task_baseline"]}
    (operation_root/"mutation-start.json").write_bytes(encoded(mutation))
    stdout=b"synthetic native completion fixture";stderr=b""
    Path(str(operation_root)+".stdout.raw").write_bytes(stdout);Path(str(operation_root)+".stderr.raw").write_bytes(stderr)
    native={"schema":"cre-code-native-completion-v1","release_sha256":digest,"authorization_sha256":auth_digest,"operation_output":output,
            "capture":{"Started":True,"Failed":False,"NativeExitCode":0,"Stdout":{"Eof":True,"Truncated":False},"Stderr":{"Eof":True,"Truncated":False}},
            "stdout_sha256":sha(stdout),"stderr_sha256":sha(stderr)}
    native_raw=encoded(native);Path(str(operation_root)+".native.json").write_bytes(native_raw)
    rows=[]
    for baseline in source["task_baseline"]:
        key=baseline["key"];before=inputs.data[baseline["xml"]]
        final=before.replace(source["deployed"]["sha"].encode(),source["selected"]["sha"].encode())
        (operation_root/(key+"-before.xml")).write_bytes(before)
        (operation_root/(key+"-final.xml")).write_bytes(final)
        (independent/(key+"-observed.xml")).write_bytes(final)
        actions=[{field:value.replace(source["deployed"]["sha"],source["selected"]["sha"]) for field,value in action.items()} for action in baseline["actions"]]
        rows.append(dict(baseline,actions=actions,state="Ready",xml_sha256=sha(final)))
    readiness=dict(host_observation(),schema="cre-code-observation-v1",native=source["selected"],runtime=source["selected"],
                   observed_at=NOW.isoformat(),qualified_at=(NOW+timedelta(seconds=1)).isoformat(),tasks=rows,
                   ownership={"phase":"owned-maintenance","verified":True,"recovery_required":True},resources_ok=True,queues_idle=True)
    ready={"schema":"cre-code-commit-ready-v1","release_sha256":digest,"authorization_sha256":auth_digest,"operation_output":output,
           "selected":source["selected"],"recovery_required_at_publication":True,"readiness":readiness,
           "dependencies":dict.fromkeys(("package.json","package-lock.json","node_modules/.package-lock.json"),"0"*64)}
    ready_raw=encoded(ready);(operation_root/"commit-ready.json").write_bytes(ready_raw)
    terminal={"schema":"cre-code-terminal-v1","status":"committed","selected":source["selected"],
              "release_sha256":digest,"authorization_sha256":auth_digest,"operation_output":output,
              "finished_at":(NOW+timedelta(seconds=2)).isoformat(),
              "commit_ready_sha256":sha(ready_raw),"canonical_lease_released":True}
    terminal_raw=encoded(terminal);(operation_root/"terminal.json").write_bytes(terminal_raw)
    observation={**host_observation(),"schema":"cre-code-observation-v1","native":source["selected"],"runtime":source["selected"],
                 "observed_at":(NOW+timedelta(seconds=3)).isoformat(),"qualified_at":(NOW+timedelta(seconds=4)).isoformat(),
                 "ownership":{"phase":"released-clear","verified":True,"recovery_required":False},"resources_ok":True,"queues_idle":True,
                 "operation_terminal_sha256":sha(terminal_raw),"native_completion_sha256":sha(native_raw),
                 "release_sha256":digest,"authorization_sha256":auth_digest,"operation_output":output,"tasks":rows}
    (independent/"observation.json").write_bytes(encoded(observation))
    receipt=make_transition(source,release_path,operation_root,independent,read_file)
    validate_transition(receipt,source["deployed"],source["selected"],read_file)
    return receipt


def test_two_successive_maintained_producers_feed_same_failure_lineage(lineage_case,tmp_path):
    release,_,inputs=lineage_case
    for index,new in enumerate((NEW,{"sha":"e"*40,"tree":"f"*40})):
        release["selected"]=new
        source=copy.deepcopy(release)
        receipt=synthetic_transition_files(tmp_path/str(index),source,inputs)
        xml_roles=[]
        for row in receipt["tasks"]:
            before=read_file(row["before_xml"]["path"]);after=read_file(row["final_xml"]["path"])
            prior=inputs.add(f"maintained-{index}-{row['key']}-before",before)
            final=inputs.add(f"maintained-{index}-{row['key']}-final",after)
            xml_roles.append({"key":row["key"],"before":prior,"after":final})
            next(b for b in release["task_baseline"] if b["key"] == row["key"])["xml"]=final
        for ref in [receipt[name] for name in ("release","mutation","commit_ready","terminal","native_completion","native_stdout","native_stderr","independent_observation")]+[row[field] for row in receipt["tasks"] for field in ("before_xml","final_xml","independent_xml")]:
            inputs.data[ref["path"]]=read_file(ref["path"])
        role=inputs.add(f"maintained-{index}",receipt)
        release["task_baseline"][2]["failure"]["transitions"].append({"old":source["deployed"],"new":new,"xml":xml_roles,"readback":role})
        release["deployed"]=new
    validate_lineage(release,inputs.evidence(release))
    release["task_baseline"][2]["failure"]["transitions"].pop(-2)
    with pytest.raises(HandoffError,match="lineage edge"):
        validate_lineage(release,inputs.evidence(release))


def test_same_release_identical_xml_cannot_mix_two_operation_attempts(admitted,tmp_path):
    release,_,_,inputs=admitted
    receipt=synthetic_transition_files(tmp_path/"first",release,inputs)
    other=json_bytes(read_file(receipt["native_completion"]["path"]))
    other["operation_output"]=str(tmp_path/"other-operation")
    other["authorization_sha256"]="9"*64
    other_path=tmp_path/"other-native.json";other_path.write_bytes(encoded(other))
    receipt["native_completion"]=reference(other_path,read_file)
    with pytest.raises(HandoffError,match="mixes distinct operation"):
        validate_transition(receipt,OLD,NEW,read_file)


@pytest.mark.parametrize("field",[
    "selected","recovery_required_at_publication","dependencies","readiness","ownership",
    "queues_missing","queues_bool","queues_busy","linux_frontends","linux_visibility",
    "resources","native_resources","xml","qualified_at","stale","margin","chronology",
    "independent_ownership","independent_clock","independent_queues",
])
def test_maintained_transition_qualifies_complete_producer_facts(admitted,tmp_path,field):
    source,_,_,inputs=admitted
    receipt=synthetic_transition_files(tmp_path/field,source,inputs)
    ready_path=receipt["commit_ready"]["path"];ready=json_bytes(read_file(ready_path))
    terminal_path=receipt["terminal"]["path"];terminal=json_bytes(read_file(terminal_path))
    observed_path=receipt["independent_observation"]["path"];observed=json_bytes(read_file(observed_path))
    readiness=ready["readiness"]
    if field in {"selected","recovery_required_at_publication","dependencies","readiness"}:ready.pop(field)
    elif field == "ownership":readiness["ownership"]={"phase":"released-clear","verified":True,"recovery_required":False}
    elif field == "queues_missing":readiness.pop("queues")
    elif field == "queues_bool":readiness["queues"]["active_crawl_count"]=False
    elif field == "queues_busy":readiness["queues"]["active_crawl_count"]=1
    elif field == "linux_frontends":readiness["linux_frontends"]=[{"pid":123,"role":"apply"}]
    elif field == "linux_visibility":readiness.pop("linux_visibility")
    elif field == "resources":readiness["linux_resources"]["memory_level"]=True
    elif field == "native_resources":readiness["windows_resources"]["admitted"]=1
    elif field == "xml":readiness["tasks"][0]["xml_sha256"]="0"*64
    elif field == "qualified_at":readiness.pop("qualified_at")
    elif field == "stale":readiness["observed_at"]=(NOW-timedelta(seconds=300)).isoformat()
    elif field == "margin":
        readiness["observed_at"]=readiness["qualified_at"]=(NOW+timedelta(hours=2)).isoformat()
        terminal["finished_at"]=(NOW+timedelta(hours=2,seconds=1)).isoformat()
        observed["observed_at"]=(NOW+timedelta(hours=2,seconds=2)).isoformat()
        observed["qualified_at"]=(NOW+timedelta(hours=2,seconds=3)).isoformat()
    elif field == "chronology":terminal["finished_at"]=(NOW-timedelta(seconds=1)).isoformat()
    elif field == "independent_ownership":observed["ownership"]=readiness["ownership"]
    elif field == "independent_clock":observed.pop("qualified_at")
    elif field == "independent_queues":observed["queues"]={}
    Path(ready_path).write_bytes(encoded(ready));receipt["commit_ready"]=reference(ready_path,read_file)
    terminal["commit_ready_sha256"]=sha(read_file(ready_path))
    Path(terminal_path).write_bytes(encoded(terminal));receipt["terminal"]=reference(terminal_path,read_file)
    observed["operation_terminal_sha256"]=sha(read_file(terminal_path))
    Path(observed_path).write_bytes(encoded(observed));receipt["independent_observation"]=reference(observed_path,read_file)
    with pytest.raises(HandoffError):validate_transition(receipt,OLD,NEW,read_file)


def test_transition_retains_original_pinned_subset_for_outer_recheck(admitted,tmp_path):
    source,_,_,inputs=admitted
    receipt=synthetic_transition_files(tmp_path/"retained",source,inputs)
    retained={}
    validate_transition(receipt,OLD,NEW,read_file,retain=retained)
    release=json_bytes(read_file(receipt["release"]["path"]))
    pinned={row["path"] for row in release["inputs"] if row["role"] in {"runtime_snapshot",*TASKS}}
    assert pinned <= retained.keys()
    path=next(iter(pinned));Path(path).write_bytes(b"changed original pinned bytes")
    assert read_file(path) != retained[path]
    with pytest.raises(HandoffError,match="changed input"):
        validate_transition(receipt,OLD,NEW,read_file)


def add_natural_advancement(release,inputs,key="interra"):
    """Synthetic complete instances of the inspected native/Linux producers."""
    row=next(member for member in release["task_baseline"] if member["key"]==key);source={"interra":"interra-realty","newmark":"newmark"}[key]
    offset=0 if key=="interra" else 15;status="additive_scope_complete_current_inventory" if offset==0 else "supported_scope_complete"
    start=NOW+timedelta(hours=2,minutes=offset,seconds=1);finish=start+timedelta(seconds=120)
    row.update(last_run_utc=start.isoformat(),next_run_utc=(NOW+timedelta(hours=14,minutes=offset)).isoformat())
    release["roots"]={"runtime":"/home/fixture/runtime"}
    series=(start+timedelta(seconds=4)).strftime("%Y-%m-%dT%H%M%SZ")
    run=series+"-"+"f"*12;parent=("1" if offset==0 else "3")*32;child=("2" if offset==0 else "4")*32
    instance="{12345678-1234-1234-1234-123456789ab"+("c}" if offset==0 else "d}")
    directory=release["roots"]["runtime"]+"/scripts/firecrawl-ops/cre_collector/out/checkpoint-series/"+series
    stamp=int((start+timedelta(seconds=4)).timestamp()*1000)
    pointer={"key":key,"code":OLD}
    def put(name,value):pointer[name]=inputs.add("advance-"+key+"-"+name,value)
    put("stdout",b"synthetic original native stdout");put("stderr",b"")
    stream=lambda size:dict.fromkeys(("Truncated","ReadFailed","WriteFailed","FlushFailed","CloseFailed","DrainTimedOut","ReaderDidNotStop","RunReferenceConflict","Failed"),False)|{"Eof":True,"ObservedBytes":size,"CapturedBytes":size,"ObservedRunDirectory":None}
    capture={"Started":True,"StartAttempted":True,"StartFailed":False,"ReadOnlyDeadlineExpired":False,"Failed":False,"NativeExitCode":0,
             "Stdout":stream(len(inputs.data[pointer["stdout"]])),"Stderr":stream(0)}
    capture["Stdout"]["ObservedRunDirectory"]=directory
    summary={"schema_version":1,"verified":True,"source":source,"collector_git_sha":OLD["sha"],
             "windows_admission_unix_ms":stamp,"series_id":series,"invocation_id":parent,"run_id":run,
             "measurement":"generation_exact_current_inventory","quality_no_regression":True,
             "local_settlement":"confirmed","verified_listings":1,"transaction_scope":["sale","lease"]}
    put("audit",{"createdAt":(start+timedelta(seconds=1)).isoformat(),"returnedAt":(finish+timedelta(milliseconds=500)).isoformat(),
        "tier":"series","source":source,"pipeline":False,"expectedSha":OLD["sha"],"status":"returned","dispatch":"returned",
        "nativeExitCode":0,"wrapperExitCode":0,"exitCode":0,"linuxSettlement":"unverified","admitted":True,
        "cpuPercent":20,"availablePhysicalGiB":8,"windowsAdmissionUnixMs":stamp,"capture":capture,
        "notificationSummaryStatus":"verified","notificationSummary":summary})
    event_roles=[];record_ids=[]
    task=row["task_path"]+row["task_name"]
    for index,event_id in enumerate((107,129,100,200,201,102)):
        data={"TaskName":task}
        if event_id in {107,100,102}:data["InstanceId"]=instance
        if event_id == 129:data["ProcessID"]="1234"
        if event_id in {200,201}:data.update(TaskInstanceId=instance,ActionName=row["actions"][0]["execute"],EnginePID="1234")
        if event_id == 201:data["ResultCode"]="0"
        at=start+timedelta(milliseconds=100*(index+1)) if index<4 else finish+timedelta(seconds=1,milliseconds=100*index)
        record_ids.append(100+offset*10+index)
        event=(f'<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><Provider Name="Microsoft-Windows-TaskScheduler"/>'
               f'<EventID>{event_id}</EventID><EventRecordID>{100+offset*10+index}</EventRecordID><TimeCreated SystemTime="{at.isoformat()}"/>'
               '<Channel>Microsoft-Windows-TaskScheduler/Operational</Channel></System><EventData>'
               +''.join(f'<Data Name="{name}">{value}</Data>' for name,value in data.items())+'</EventData></Event>')
        event_roles.append(inputs.add("advance-"+key+"-event-"+str(event_id),event.encode()))
    pointer["events"]=event_roles
    put("bridge",{"observed_at":(start+timedelta(seconds=20)).isoformat(),"task_instance_guid":instance,"task_path":task,
        "scheduler_event_record_ids":record_ids[:4],"action_pid":1234,"action_creation_utc":(start+timedelta(milliseconds=150)).isoformat(),
        "direct_wsl_pid":5678,"direct_wsl_parent_pid":1234,"direct_wsl_creation_utc":(start+timedelta(seconds=4,milliseconds=200)).isoformat(),
        "scalar_argv":{"expected-sha":OLD["sha"],"sources":source,"windows-admission-unix-ms":str(stamp)}})
    put("series",{"schema_version":1,"collector_git_sha":OLD["sha"],"collector_git_dirty":False,"series_id":series,"status":"complete",
        "started_at":(start+timedelta(seconds=4,milliseconds=300)).isoformat(),"finished_at":finish.isoformat(),
        "latest_invocation_admission":{"expected_sha":OLD["sha"],"windows_admission_unix_ms":stamp},
        "latest_invocation_control":{"invocation_id":parent,"receipt":".series-control/"+parent+".json"},
        "sources":{source:{"state":"complete","checkpoint_status":status,"checkpoint_run":"runs/"+run,"attempts":[{"number":1,"rc":0,"series_control_child_id":child}]}}})
    put("parent_control",{"status":"complete","local_settlement":"confirmed","started_unix_ms":stamp+300,
        "finished_unix_ms":int(finish.timestamp()*1000),"last_checkpoint":{"child_invocation_id":child,"run_id":run,"pid":12},
        "stop_owner":{"invocation_id":parent,"owner_role":"checkpoint_series_one_shot_cli"}})
    put("run",{"schema_version":2,"collector_git_sha":OLD["sha"],"collector_git_dirty":False,"run_id":run,
        "status":status,"sources":{source:{"state":"ingested"}},
        "started_at":(start+timedelta(seconds=5)).isoformat(),"finished_at":(finish-timedelta(seconds=1)).isoformat()})
    put("child_control",{"schema_version":1,"invocation_id":parent,"child_invocation_id":child,"run_id":run,"collector_git_sha":OLD["sha"],
        "guards_stopped":True,"nested_cleanup_confirmed":True,"terminal_evidence_saved":True})
    put("xml",inputs.data[row["xml"]])
    put("observation",{"observed_at":(finish+timedelta(seconds=30)).isoformat(),"errors":[],"running_instances":[],
        "tasks":[{**{field:member[field] for field in ("key","task_path","task_name","enabled","last_run_utc","next_run_utc")},
                  "last_result":member["last_task_result"],"state":"Ready","missed_runs":0} for member in release["task_baseline"]]})
    release.setdefault("natural_advancements",[]).append(pointer)
    return pointer


@pytest.fixture
def natural_advancement_case(lineage_case):
    release,_,inputs=lineage_case
    pointer=add_natural_advancement(release,inputs)
    return release,inputs,pointer


def test_settled_same_code_natural_run_bridges_healthy_clock_advance(natural_advancement_case):
    release,inputs,_=natural_advancement_case;validate_lineage(release,inputs.evidence(release))


def test_two_healthy_runs_keep_their_independent_original_observation_clocks(natural_advancement_case):
    release,inputs,first=natural_advancement_case
    add_natural_advancement(release,inputs,"newmark")
    first_snapshot=json.loads(inputs.data[first["observation"]])
    assert first_snapshot["tasks"][1]["last_run_utc"] != release["task_baseline"][1]["last_run_utc"]
    validate_lineage(release,inputs.evidence(release))


def test_independent_members_cannot_reuse_a_scheduler_instance(natural_advancement_case):
    release,inputs,first=natural_advancement_case;second=add_natural_advancement(release,inputs,"newmark")
    role=second["bridge"];bridge=json.loads(inputs.data[role]);bridge["task_instance_guid"]=json.loads(inputs.data[first["bridge"]])["task_instance_guid"]
    raw=encoded(bridge);inputs.data[role]=raw;next(row for row in inputs.rows if row["role"]==role).update(bytes=len(raw),sha256=sha(raw))
    with pytest.raises(HandoffError,match="reused natural operation"):validate_lineage(release,inputs.evidence(release))


def test_maintained_old_edge_keeps_its_own_clocks_before_natural_advancement(lineage_case,tmp_path):
    release,_,inputs=lineage_case
    edge=release["task_baseline"][2]["failure"]["transitions"][-1]
    source=copy.deepcopy(release);source.update(deployed=edge["old"],selected=edge["new"])
    source["task_baseline"][2]["failure"]["transitions"].pop()
    for member in source["task_baseline"]:member["xml"]=next(row["before"] for row in edge["xml"] if row["key"]==member["key"])
    receipt=synthetic_transition_files(tmp_path/"maintained-before-natural",source,inputs)
    refs=[receipt[name] for name in ("release","mutation","commit_ready","terminal","native_completion","native_stdout","native_stderr","independent_observation")]
    refs.extend(row[field] for row in receipt["tasks"] for field in ("before_xml","final_xml","independent_xml"))
    for ref in refs:inputs.data[ref["path"]]=read_file(ref["path"])
    edge["readback"]=inputs.add("maintained-before-natural",receipt)
    add_natural_advancement(release,inputs)
    validate_lineage(release,inputs.evidence(release))


@pytest.mark.parametrize("mutation",["missing","duplicate","failed_key","wrong_source","wrong_sha","missing_bridge","unsettled_parent","running_child","nonzero","xml","wrong_parent","wrong_stamp","unfinished_task","missed","clock_regression","failed_clock","extra","wrong_run","event_result","event_guid","event_pid","capture_start","capture_failed","capture_deadline","series_bool","child_bool","checkpoint_status","run_float","run_bool"])
def test_natural_advancement_refuses_incomplete_or_cross_operation_proof(natural_advancement_case,mutation):
    release,inputs,pointer=natural_advancement_case
    if mutation == "missing":release["natural_advancements"]=[]
    elif mutation == "duplicate":release["natural_advancements"].append(copy.deepcopy(pointer))
    elif mutation == "failed_key":pointer["key"]="qualified"
    elif mutation == "extra":release["natural_advancements"].append(dict(pointer,key="newmark"))
    elif mutation == "failed_clock":release["task_baseline"][2]["last_run_utc"]=NOW.isoformat()
    elif mutation == "clock_regression":release["task_baseline"][0]["last_run_utc"]=(NOW-timedelta(days=2)).isoformat()
    elif mutation.startswith("event_"):
        role=pointer["events"][4]
        before,after={"event_result":(b'ResultCode">0',b'ResultCode">1'),"event_guid":(b'123456789abc',b'123456789abd'),"event_pid":(b'EnginePID">1234',b'EnginePID">9999')}[mutation]
        raw=inputs.data[role].replace(before,after);inputs.data[role]=raw
        next(row for row in inputs.rows if row["role"]==role).update(bytes=len(raw),sha256=sha(raw))
    else:
        role=pointer[{"wrong_source":"audit","wrong_sha":"audit","missing_bridge":"bridge","unsettled_parent":"parent_control",
            "running_child":"child_control","nonzero":"audit","xml":"xml","wrong_parent":"bridge","wrong_stamp":"bridge","unfinished_task":"observation","missed":"observation","wrong_run":"child_control","capture_start":"audit","capture_failed":"audit","capture_deadline":"audit","series_bool":"series","child_bool":"child_control","checkpoint_status":"series","run_float":"run","run_bool":"run"}[mutation]]
        value=json.loads(inputs.data[role]) if mutation != "xml" else None
        if mutation == "wrong_source":value["source"]="newmark"
        elif mutation == "wrong_sha":value["expectedSha"]=NEW["sha"]
        elif mutation == "missing_bridge":value.pop("direct_wsl_pid")
        elif mutation == "unsettled_parent":value["local_settlement"]="unverified"
        elif mutation == "running_child":value["guards_stopped"]=False
        elif mutation == "nonzero":value["exitCode"]=1
        elif mutation == "wrong_parent":value["direct_wsl_parent_pid"]=9999
        elif mutation == "wrong_stamp":value["scalar_argv"]["windows-admission-unix-ms"]="1"
        elif mutation == "unfinished_task":value["running_instances"]=[{"path":pointer["key"]}]
        elif mutation == "missed":value["tasks"][0]["missed_runs"]=1
        elif mutation == "wrong_run":value["run_id"]="different"
        elif mutation in {"capture_start","capture_failed","capture_deadline"}:value["capture"][{"capture_start":"StartAttempted","capture_failed":"StartFailed","capture_deadline":"ReadOnlyDeadlineExpired"}[mutation]]=mutation!="capture_start"
        elif mutation in {"series_bool","child_bool","run_float","run_bool"}:value["schema_version"]=2.0 if mutation=="run_float" else True
        elif mutation == "checkpoint_status":value["sources"]["interra-realty"]["checkpoint_status"]="different"
        raw=xml(NEW["sha"]) if mutation == "xml" else encoded(value)
        inputs.data[role]=raw
        next(row for row in inputs.rows if row["role"]==role).update(bytes=len(raw),sha256=sha(raw))
    with pytest.raises(HandoffError):validate_lineage(release,inputs.evidence(release))
