"""Savills-only native/series carrier; no provider or database access."""

import json
import hashlib
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest

from test_cre_checkpoint_series import config
from test_notification_summary import evidence as base_evidence, read, REFUSED


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    return base_evidence.__wrapped__(tmp_path, monkeypatch)


def carrier(**changes):
    # Reconstruct using the production config builder, not a hand-made option.
    args = dict(
        sources=("savills",),
        page_cap=400,
        concurrency=1,
        attempts_per_source=1,
        max_resume_age_hours=24,
        max_host_cpu_percent=90.0,
        cpu_sustain_seconds=30.0,
        cpu_sample_seconds=2.0,
        nice=15,
        admit_baseline_hold_additively=True,
    )
    args.update(changes)
    return series.series_config(**args)


def produce_gate_evidence(run, manifest, monkeypatch, *, held=True):
    source = "savills"
    artifact = run / "sources/savills.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("{}")
    manifest["sources"][source]["artifact"] = {"path": "sources/savills.json"}

    # Substitute only the database-backed gate subprocess. Both real gate
    # producers apply the existing admission transformation and persist it.
    def command(argv, log, **kwargs):
        output = Path(argv[argv.index("--out") + 1])
        refresh.atomic_write_json(
            output,
            {
                "per_source": {
                    source: {
                        "verdict": "hold" if held else "ok",
                        "reason": "current_active 3 below 70% of baseline median 102 (threshold 72)"
                        if held
                        else None,
                        "mark_missing_safe": False,
                    }
                },
                "summary": {
                    "torow_errors": 0,
                    "hold_sources": [source] if held else [],
                    "mark_missing_safe_brokerages": [],
                },
            },
        )
        return 2 if held else 0

    monkeypatch.setattr(refresh, "run_command", command)
    refresh.gate_source(run, manifest, source, artifact, None)
    refresh.run_aggregate_gate(run, manifest, None)
    return artifact


def child(run, monkeypatch, held=True):
    cfg = carrier()
    manifest = refresh.new_manifest(
        run,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("savills",),
        page_cap=400,
        concurrency=1,
        source_workers=1,
        admit_baseline_hold_additively=True,
    )
    produce_gate_evidence(run, manifest, monkeypatch, held=held)
    manifest["status"] = (
        "additive_scope_complete_coverage_hold" if held else "supported_scope_complete"
    )
    return cfg, manifest


def test_real_gate_producers_preserve_hold_and_bind_serial_child(tmp_path, monkeypatch):
    cfg, manifest = child(tmp_path, monkeypatch)
    assert manifest["config"] == series._expected_child_config("savills", cfg)
    assert series._child_succeeded(manifest, cfg)
    assert not series._child_succeeded(manifest, config(("savills",)))
    argv = series.build_checkpoint_argv(
        "savills", child_out_root=tmp_path, env_file=None, config=cfg
    )
    assert argv.count("--admit-baseline-hold-additively") == 1
    assert argv[argv.index("--transactions") + 1] == "both"
    assert "--max-items" not in argv and "--pipeline" not in argv
    gate = json.loads((tmp_path / "gates/savills.json").read_text())
    assert gate["per_source"]["savills"]["raw_verdict"] == "hold"
    assert gate["scope"]["whole_source_coverage"] is False
    assert manifest["aggregate_gate"]["baseline_advisory_holds"] == ["savills"]
    assert manifest["aggregate_gate"]["mark_missing_safe_brokerages"] == []


def test_fresh_naturally_passing_source_does_not_false_fail_after_commit(
    tmp_path, monkeypatch
):
    cfg, manifest = child(tmp_path, monkeypatch, held=False)
    assert series._child_succeeded(manifest, cfg)


@pytest.mark.parametrize(
    "changes",
    [
        dict(sources=("hanley",)),
        dict(sources=("savills", "hanley")),
        dict(pipeline=True),
        dict(source_workers=2),
        dict(admit_current_inventory_source="interra-realty"),
    ],
)
def test_carrier_selection_is_narrow(changes):
    with pytest.raises(ValueError):
        carrier(**changes)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["config"].update(max_items=1),
        lambda m: m["config"].update(transactions=["sale"]),
        lambda m: m["config"].update(allow_dirty=True),
        lambda m: m["config"].update(admit_baseline_hold_additively=False),
        lambda m: m["scope"].update(whole_source_coverage=True),
        lambda m: m["sources"]["savills"]["gate"].update(raw_verdict="first_seen"),
        lambda m: m["sources"]["savills"]["gate"].update(
            raw_reason="source pass error: timeout"
        ),
        lambda m: m["sources"]["savills"]["gate"].update(mark_missing_safe=True),
        lambda m: m["aggregate_gate"].update(baseline_advisory_holds=[]),
        lambda m: m["aggregate_gate"].update(non_ok_sources=["savills"]),
    ],
)
def test_terminal_cannot_launder_scope_or_binding(tmp_path, monkeypatch, mutate):
    cfg, manifest = child(tmp_path, monkeypatch)
    mutate(manifest)
    assert not series._child_succeeded(manifest, cfg)


def test_resume_requires_exact_explicit_option(tmp_path):
    cfg = carrier()
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=cfg)
    series.save_manifest(tmp_path, manifest)
    with pytest.raises(series.SeriesError, match="config"):
        series.load_resume_manifest(
            tmp_path / "manifest.json", git_sha="a" * 40, config=config(("savills",))
        )


def test_actual_gate_producer_to_private_summary(evidence, monkeypatch):
    # Start with the established private owner/control/readback fixture, retain
    # all ordinary receipt checks, then use production configs and gate writers.
    values = evidence["values"]
    cfg = carrier()
    parent, old_child = values["parent"], values["child"]
    source_entry = parent["sources"].pop("svn")
    parent["sources"]["savills"] = source_entry
    parent["config"] = cfg
    run = evidence["files"]["child"].parent
    produced_cfg, produced = child(run, monkeypatch)
    checkpoint = old_child["sources"].pop("svn")
    checkpoint["gate"] = produced["sources"]["savills"]["gate"]
    old_child["sources"]["savills"] = checkpoint
    old_child["config"] = produced["config"]
    old_child["scope"] = produced["scope"]
    old_child["aggregate_gate"] = produced["aggregate_gate"]
    old_child["status"] = produced["status"]
    source_entry["checkpoint_status"] = produced["status"]
    checkpoint["artifact"]["path"] = "sources/savills.json"
    evidence["files"]["artifact"] = run / "sources/savills.json"
    for row in evidence["artifact"]["listings"]:
        row["sourceKey"] = "savills"
    raw = json.dumps(evidence["artifact"], separators=(",", ":")).encode()
    checkpoint["artifact"].update(
        sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)
    )
    evidence["save"]()
    result = read(evidence, source="savills")
    assert result["verified_listings"] == 3
    assert result["transaction_breakdown"]["sale"] == 2
    assert series._reconciled_state(old_child, cfg) == "complete"
    assert result["baseline_hold_retained"] is True
    assert result["whole_source_coverage"] is False
    parent["config"].pop("admit_baseline_hold_additively")
    evidence["save"]()
    with pytest.raises(REFUSED):
        read(evidence, source="savills")


@pytest.mark.parametrize(
    "args",
    [
        ["--sources", "hanley"],
        ["--sources", "savills,hanley"],
        ["--sources", "savills", "--pipeline"],
        ["--sources", "savills", "--admit-current-inventory-source", "interra-realty"],
    ],
)
def test_invalid_cli_refuses_before_target_or_owner_admission(args, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid selection reached owner or database target inspection")

    monkeypatch.setattr(series, "admit_series_invocation", forbidden)
    monkeypatch.setattr(series, "database_target_fingerprint", forbidden)
    with pytest.raises(SystemExit) as exc:
        series.main(["--admit-baseline-hold-additively", *args])
    assert exc.value.code == 2
