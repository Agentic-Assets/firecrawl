"""Behavior tests for reusable finite trials; all inference and receipts are fake."""

import json
import urllib.parse

import native_pdf as trial
import pytest


def setup_trial(tmp_path, *, efforts=("high", "xhigh"), budget=10, reservation=1):
    pdf = trial.FIXTURE.read_bytes()
    (tmp_path / "source.pdf").write_bytes(pdf)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(
        trial.runner.canonical(
            {
                "schema": "document-input-benchmark-corpus/v1",
                "documents": [
                    {
                        "id": "sample",
                        "path": "source.pdf",
                        "sha256": trial.runner.digest(pdf),
                        "pages": 1,
                    }
                ],
            }
        )
    )
    config = tmp_path / "config.json"
    config.write_bytes(
        trial.runner.canonical(
            {
                "schema": "native-pdf-benchmark/v1",
                "routes": [
                    {
                        "model": "example/model",
                        "provider": "example",
                        "effort": e,
                        "max_tokens": 8000,
                    }
                    for e in efforts
                ],
                "budget_usd": budget,
                "reservation_usd": reservation,
                "workers": 1,
            }
        )
    )
    out = tmp_path / "private"
    trial.prepare(config, manifest, out)
    return config, manifest, out


def fake_gateway(monkeypatch, *, unknown_cost=False, byok=False, invalid=False):
    calls = []

    def request(url, key, *, data=None):
        calls.append((url, data))
        if data is None:
            generation = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["id"][
                0
            ]
            return (
                {"http_status": 404}
                if unknown_cost
                else {
                    "http_status": 200,
                    "body": {
                        "data": {
                            "id": generation,
                            "provider_name": "example",
                            "model": "example/model",
                            "total_cost": 0.02,
                            "is_byok": byok,
                        }
                    },
                }
            )
        probe = "marker and annual_rent" in data["messages"][0]["content"][0]["text"]
        answer = (
            {
                "marker": "CEDAR-9472",
                "annual_rent_usd_by_unit": {"ALPHA": 41327, "BETA": 86519},
            }
            if probe
            else {"transcription": "Synthetic text", "facts": [], "tables": []}
        )
        return {
            "http_status": 200,
            "body": {
                "id": "gen_" + str(len(calls)),
                "model": "example/model",
                "choices": [
                    {
                        "message": {
                            "content": "invalid"
                            if invalid and not probe
                            else json.dumps(answer)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        }

    monkeypatch.setattr(trial.runner, "request_json", request)
    return calls


def test_offline_actions_do_not_read_credentials_or_use_network(
    tmp_path, monkeypatch, capsys
):
    config, _manifest, out = setup_trial(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("offline action attempted credentials or network")

    monkeypatch.setattr(trial, "credential", forbidden)
    monkeypatch.setattr(trial.runner, "request_json", forbidden)
    for action in ("qualify", "run", "report"):
        trial.main([action, "--out", str(out)])
        value = json.loads(capsys.readouterr().out)
        assert value.get("network_calls", 0) == 0
    plan, ph = trial.read_plan(out)
    names = [
        trial.make_spec(plan, ph, plan["documents"][0], r)["name"]
        for r in plan["config"]["routes"]
    ]
    assert len(set(names)) == 2  # Same provider/model, different reasoning settings.
    config.write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        trial.read_plan(out)


def test_full_mock_workflow_qualification_resume_and_private_results(
    tmp_path, monkeypatch
):
    _, _, out = setup_trial(tmp_path)
    calls = fake_gateway(monkeypatch)
    plan, ph = trial.read_plan(out)
    with pytest.raises(ValueError, match="qualifications"):
        trial.dispatch(out, plan, ph, probe=False, key="fake")
    assert not calls
    assert trial.dispatch(out, plan, ph, probe=True, key="fake")["sent"] == 2
    assert trial.dispatch(out, plan, ph, probe=False, key="fake")["sent"] == 2
    assert len(calls) == 8
    before = {p.name: p.read_bytes() for p in (out / "results").glob("*.json")}
    assert trial.dispatch(out, plan, ph, probe=False, key="fake")["sent"] == 0
    assert len(calls) == 8
    report = trial.report(out, plan, ph)
    assert len(report["rows"]) == 2
    assert all(r["cost_usd"] == 0.02 and r["strict_usable"] for r in report["rows"])
    assert before == {p.name: p.read_bytes() for p in (out / "results").glob("*.json")}
    assert all(
        p.stat().st_mode & 0o777 == 0o600 for p in (out / "results").glob("*.json")
    )


def test_interrupted_intent_blocks_paid_dispatch(tmp_path, monkeypatch):
    _, _, out = setup_trial(tmp_path, efforts=("high",))
    plan, ph = trial.read_plan(out)
    spec = trial.make_spec(plan, ph, None, plan["config"]["routes"][0], probe=True)
    trial.runner.write_new(
        out / "qualification" / (spec["name"] + ".intent.json"),
        trial.runner.canonical({"identity": spec["identity"], "dispatch": "started"}),
    )
    calls = fake_gateway(monkeypatch)
    with pytest.raises(ValueError, match="unsettled"):
        trial.dispatch(out, plan, ph, probe=True, key="fake")
    assert not calls


@pytest.mark.parametrize("unknown,byok", [(True, False), (False, True)])
def test_uncertain_cost_never_releases_budget_or_qualifies(
    tmp_path, monkeypatch, unknown, byok
):
    _, _, out = setup_trial(tmp_path, budget=1, reservation=1)
    calls = fake_gateway(monkeypatch, unknown_cost=unknown, byok=byok)
    plan, ph = trial.read_plan(out)
    value = trial.dispatch(out, plan, ph, probe=True, key="fake")
    assert value["sent"] == 1 and value["remaining"] == 1
    assert trial.audit_existing(out / "qualification") == 1
    with pytest.raises(ValueError, match="qualifications"):
        trial.dispatch(out, plan, ph, probe=False, key="fake")
    assert len(calls) == 2


def test_failed_model_answer_is_preserved_and_not_retried(tmp_path, monkeypatch):
    _, _, out = setup_trial(tmp_path, efforts=("high",))
    calls = fake_gateway(monkeypatch, invalid=True)
    plan, ph = trial.read_plan(out)
    trial.dispatch(out, plan, ph, probe=True, key="fake")
    trial.dispatch(out, plan, ph, probe=False, key="fake")
    assert trial.report(out, plan, ph)["rows"][0]["strict_usable"] is False
    assert trial.dispatch(out, plan, ph, probe=False, key="fake")["sent"] == 0
    assert len(calls) == 4


def test_receipt_and_response_tampering_refused(tmp_path, monkeypatch):
    _, _, out = setup_trial(tmp_path, efforts=("high",))
    fake_gateway(monkeypatch)
    plan, ph = trial.read_plan(out)
    trial.dispatch(out, plan, ph, probe=True, key="fake")
    trial.dispatch(out, plan, ph, probe=False, key="fake")
    path = next((out / "results").glob("*.result.json"))
    original = json.loads(path.read_bytes())
    bad = {**original, "raw_output": "changed"}
    path.write_bytes(trial.runner.canonical(bad))
    with pytest.raises(ValueError, match="response content"):
        trial.report(out, plan, ph)
    bad = json.loads(trial.runner.canonical(original))
    bad["generation_lookup"]["body"]["data"]["provider_name"] = "different"
    path.write_bytes(trial.runner.canonical(bad))
    with pytest.raises(ValueError, match="provider receipt"):
        trial.report(out, plan, ph)
    for field, value in (
        ("usage", {"prompt_tokens": 9999999}),
        ("status", "incomplete"),
    ):
        bad = {**original, field: value}
        path.write_bytes(trial.runner.canonical(bad))
        with pytest.raises(ValueError, match="response " + field):
            trial.report(out, plan, ph)


def test_legacy_adoption_of_missing_source_is_read_only(tmp_path):
    missing = tmp_path / "missing-source"
    with pytest.raises(ValueError, match="existing private legacy source"):
        trial.adopt(
            missing,
            tmp_path / "unused",
            tmp_path / "unused",
            tmp_path / "unused",
            tmp_path / "unused",
        )
    assert not missing.exists()


def test_malformed_provider_or_receipt_body_retains_failed_unknown_outcome(tmp_path):
    trial.validate_response({"provider_response": [], "status": "incomplete"})
    result = {"generation_lookup": {"http_status": 200, "body": {"data": []}}}
    assert trial.verified_cost(tmp_path, tmp_path / "unused.result.json", result, {}) is None


def test_pdf_mutation_refuses_before_dispatch(tmp_path):
    _, _, out = setup_trial(tmp_path)
    (tmp_path / "source.pdf").write_bytes(b"%PDF-1.4 changed")
    with pytest.raises(ValueError, match="hash"):
        trial.read_plan(out)


def test_actual_page_count_and_optional_gold_hash_are_verified(tmp_path):
    _, manifest, _ = setup_trial(tmp_path)
    value = json.loads(manifest.read_bytes())
    value["documents"][0]["pages"] = 2
    manifest.write_bytes(trial.runner.canonical(value))
    with pytest.raises(ValueError, match="page count"):
        trial.validate_manifest(manifest)
    value["documents"][0]["pages"] = 1
    gold = tmp_path / "gold.json"
    gold.write_text("{}")
    value["documents"][0].update(
        {
            "gold_path": "gold.json",
            "gold_sha256": trial.runner.digest(gold.read_bytes()),
        }
    )
    manifest.write_bytes(trial.runner.canonical(value))
    assert len(trial.validate_manifest(manifest)[0]) == 1
    gold.write_text('{"changed": true}')
    with pytest.raises(ValueError, match="gold hash"):
        trial.validate_manifest(manifest)


def test_cost_reconciliation_uses_only_get_and_keeps_original_bytes(
    tmp_path, monkeypatch, capsys
):
    _, _, out = setup_trial(tmp_path, efforts=("high",))
    fake_gateway(monkeypatch, unknown_cost=True)
    plan, ph = trial.read_plan(out)
    trial.dispatch(out, plan, ph, probe=True, key="fake")
    original = {
        p.name: p.read_bytes() for p in (out / "qualification").glob("*.result.json")
    }
    calls = fake_gateway(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "fake")
    trial.main(["reconcile", "--out", str(out)])
    counts = json.loads(capsys.readouterr().out)
    assert counts["qualification"] == {"verified_costs": 1, "pending_costs": 0}
    assert calls and all(data is None and "/generation?" in url for url, data in calls)
    assert original == {
        p.name: p.read_bytes() for p in (out / "qualification").glob("*.result.json")
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("workers", True),
        ("workers", 99),
        ("budget_usd", float("nan")),
        ("reservation_usd", 100),
    ],
)
def test_invalid_planning_settings_refused(tmp_path, field, value):
    config, _, _ = setup_trial(tmp_path)
    settings = json.loads(config.read_bytes())
    settings[field] = value
    with pytest.raises(ValueError):
        trial.validate_config(settings)
