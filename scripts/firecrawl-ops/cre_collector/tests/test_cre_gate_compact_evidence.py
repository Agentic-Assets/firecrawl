"""Gate counting reads each artifact once without retaining listing bodies."""

import copy
import json
import sys
import weakref

import cre_gate as gate
import pytest


def full_savills():
    return {
        "runMeta": {
            "transactions": ["sale", "lease"],
            "mode": "full",
            "maxItemsPerSource": None,
            "finishedAt": "2026-10-14T00:00:00Z",
        },
        "sources": [
            {
                "sourceKey": "savills",
                "transaction": transaction,
                "supported": True,
                "truncated": False,
                "listingsCollected": 1,
                "totalAvailableOnSource": 1,
                "sourceUrl": f"https://example.invalid/{transaction}",
                "method": "synthetic",
            }
            for transaction in ("sale", "lease")
        ],
        "listings": [
            {
                "sourceKey": "savills",
                "transactionMode": transaction,
                "id": transaction,
                "url": f"https://example.invalid/{transaction}",
            }
            for transaction in ("sale", "lease")
        ],
        "brokers": [],
    }


def write_artifact(tmp_path, payload, name="artifact.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_main_reads_once_and_releases_full_payload_before_floor(
    tmp_path, monkeypatch, capsys
):
    class Payload(dict):
        pass

    payload = full_savills()
    payload["listings"][0]["rawDetail"] = "ignored-detail" * 10_000
    payload["brokers"] = [{"ignoredBody": "ignored-broker" * 10_000}]
    payload["runMeta"]["ignoredMetadata"] = "ignored-run" * 10_000
    payload["sources"][0]["ignoredMetadata"] = "ignored-source" * 10_000
    path = write_artifact(tmp_path, payload)
    real_load = gate.json.load
    real_floor = gate.effective_floor_for_source
    read_paths = []
    originals = []

    def read(handle, *args, **kwargs):
        data = Payload(real_load(handle, *args, **kwargs))
        read_paths.append(handle.name)
        originals.append(weakref.ref(data))
        return data

    def floor(source, global_floor, evidence, policy):
        assert all(ref() is None for ref in originals)
        assert "ignored-" not in json.dumps(evidence)
        assert len(json.dumps(evidence)) < 2_000
        return real_floor(source, global_floor, evidence, policy)

    monkeypatch.setattr(gate.json, "load", read)
    monkeypatch.setattr(gate, "effective_floor_for_source", floor)
    monkeypatch.setattr(
        sys, "argv", ["cre_gate.py", "--in", str(path), "--dry-run", "--quiet"]
    )
    gate.main()

    result = json.loads(capsys.readouterr().out)
    assert read_paths == [str(path)]
    assert result["per_source"]["savills"]["effective_floor"] == 1
    assert result["per_source"]["savills"]["current_active"] == 2


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing_max",
        "null_max",
        "zero_max",
        "finite_max",
        "reverse_transactions",
        "subset_transactions",
        "monitor_mode",
        "missing_run_meta",
        "null_run_meta",
        "empty_list_run_meta",
        "null_transactions",
        "list_mode",
        "duplicate_entry",
        "unsupported",
        "integer_supported",
        "null_supported",
        "truncated",
        "missing_truncated",
        "null_truncated",
        "source_error",
        "provider_mismatch",
        "raw_count_mismatch",
        "invalid_listing_transaction",
        "bool_count",
        "null_count",
        "missing_provider_total",
        "list_provider_total",
        "non_https_source",
        "missing_method",
        "list_method",
        "null_listing_transaction",
        "zero_complete",
        "to_row_rejected",
    ],
)
def test_compact_evidence_preserves_full_floor_decision(tmp_path, fault):
    payload = full_savills()
    meta = payload["runMeta"]
    entry = payload["sources"][0]
    if fault == "missing_max":
        del meta["maxItemsPerSource"]
    elif fault == "null_max":
        meta["maxItemsPerSource"] = None
    elif fault in {"zero_max", "finite_max"}:
        meta["maxItemsPerSource"] = 0 if fault == "zero_max" else 100
    elif fault == "reverse_transactions":
        meta["transactions"].reverse()
    elif fault == "subset_transactions":
        meta["transactions"] = ["sale"]
    elif fault == "monitor_mode":
        meta["mode"] = "monitor"
    elif fault == "missing_run_meta":
        del payload["runMeta"]
    elif fault in {"null_run_meta", "empty_list_run_meta"}:
        payload["runMeta"] = None if fault == "null_run_meta" else []
    elif fault == "null_transactions":
        meta["transactions"] = None
    elif fault == "list_mode":
        meta["mode"] = ["full"]
    elif fault == "duplicate_entry":
        payload["sources"][1] = copy.deepcopy(entry)
    elif fault in {"unsupported", "integer_supported"}:
        entry["supported"] = False if fault == "unsupported" else 1
    elif fault == "null_supported":
        entry["supported"] = None
    elif fault == "truncated":
        entry["truncated"] = True
    elif fault == "missing_truncated":
        del entry["truncated"]
    elif fault == "null_truncated":
        entry["truncated"] = None
    elif fault == "source_error":
        entry["error"] = "synthetic failure"
    elif fault == "provider_mismatch":
        entry["totalAvailableOnSource"] = 2
    elif fault == "raw_count_mismatch":
        payload["listings"].append(copy.deepcopy(payload["listings"][0]))
    elif fault == "invalid_listing_transaction":
        payload["listings"][0]["transactionMode"] = "unknown"
    elif fault == "bool_count":
        entry["listingsCollected"] = True
    elif fault == "null_count":
        entry["listingsCollected"] = None
    elif fault == "missing_provider_total":
        del entry["totalAvailableOnSource"]
    elif fault == "list_provider_total":
        entry["totalAvailableOnSource"] = [1]
    elif fault == "non_https_source":
        entry["sourceUrl"] = "http://example.invalid/sale"
    elif fault == "missing_method":
        del entry["method"]
    elif fault == "list_method":
        entry["method"] = ["synthetic"]
    elif fault == "null_listing_transaction":
        payload["listings"][0]["transactionMode"] = None
    elif fault == "zero_complete":
        payload["listings"] = []
        for source in payload["sources"]:
            source["listingsCollected"] = source["totalAvailableOnSource"] = 0
    elif fault == "to_row_rejected":
        del payload["listings"][0]["url"]

    original = copy.deepcopy(payload)
    path = write_artifact(tmp_path, payload)
    evidence = []
    counts = gate.count_artifacts([path], True, coverage_evidence=evidence)
    policy = gate.load_gate_coverage_policy()
    for global_floor in (gate.DEFAULT_FLOOR, 50):
        assert gate.effective_floor_for_source(
            "savills", global_floor, evidence, policy
        ) == gate.effective_floor_for_source("savills", global_floor, [payload], policy)
    assert payload == original
    if fault == "to_row_rejected":
        assert counts[0]["savills"] == 1
        assert gate.savills_full_scope_coverage_error(evidence) is None


def test_count_api_errors_order_and_multi_input_slots(tmp_path, monkeypatch):
    first = full_savills()
    first["sources"][0]["error"] = "first error"
    first["listings"][0]["raise"] = True
    first["listings"][1]["reject"] = True
    second = {
        "runMeta": {"finishedAt": "2026-10-15T00:00:00Z"},
        "sources": [
            {"sourceKey": "svn", "error": "second error"},
            {"sourceKey": "savills", "error": "later Savills error"},
        ],
        "listings": [{"sourceKey": "svn"}, {"sourceKey": "svn"}],
    }
    paths = [
        write_artifact(tmp_path, first, "first.json"),
        write_artifact(tmp_path, second, "second.json"),
    ]

    def to_row(listing, _brokers, _scraped_at):
        if listing.get("raise"):
            raise ValueError("synthetic rejected shape")
        return None if listing.get("reject") else {"accepted": True}

    monkeypatch.setattr(gate, "to_row", to_row)
    legacy = gate.count_artifacts(paths, True)
    evidence = []
    current = gate.count_artifacts(paths, True, coverage_evidence=evidence)
    assert (
        current
        == legacy
        == (
            {"svn": 2},
            {"savills": "first error", "svn": "second error"},
            {"savills", "svn"},
            "2026-10-14T00:00:00Z",
            1,
        )
    )
    assert len(evidence) == 2
    assert gate.savills_full_scope_coverage_error(evidence) == (
        "requires exactly one full Savills artifact"
    )
    assert gate.effective_floor_for_source(
        "svn", 100, evidence, gate.load_gate_coverage_policy()
    ) == (100, None, None)


def test_main_evidence_read_retains_utf8_validation(tmp_path, monkeypatch):
    path = tmp_path / "invalid-utf8.json"
    path.write_bytes(b'{"sources": [], "listings": [], "ignored": "\xff"}')
    monkeypatch.setattr(
        sys, "argv", ["cre_gate.py", "--in", str(path), "--apply", "--quiet"]
    )
    monkeypatch.setattr(
        gate,
        "read_baseline",
        lambda *_args, **_kwargs: pytest.fail("unexpected DB read"),
    )
    real_open = open
    encodings = []

    def locale_open(file, *args, **kwargs):
        # Emulate a non-UTF8 host: the legacy counting API can decode latin-1,
        # while main must retain its previous explicit UTF8 rejection.
        encodings.append(kwargs.get("encoding"))
        if not kwargs.get("encoding"):
            kwargs["encoding"] = "latin-1"
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(gate, "open", locale_open, raising=False)
    with pytest.raises(UnicodeDecodeError):
        gate.main()
    assert encodings == ["utf-8"]
    assert len(gate.count_artifacts([path], True)) == 5
    assert encodings[-1] is None


def test_main_decodes_valid_utf8_once_even_with_non_utf8_locale(
    tmp_path, monkeypatch, capsys
):
    payload = full_savills()
    payload["listings"][0]["name"] = "Étage"
    path = tmp_path / "utf8.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    real_open = open
    real_to_row = gate.to_row
    names = []

    def locale_open(file, *args, **kwargs):
        if not kwargs.get("encoding"):
            kwargs["encoding"] = "latin-1"
        return real_open(file, *args, **kwargs)

    def to_row(listing, brokers, scraped_at):
        names.append(listing.get("name"))
        return real_to_row(listing, brokers, scraped_at)

    monkeypatch.setattr(gate, "open", locale_open, raising=False)
    monkeypatch.setattr(gate, "to_row", to_row)
    monkeypatch.setattr(
        sys, "argv", ["cre_gate.py", "--in", str(path), "--dry-run", "--quiet"]
    )
    gate.main()
    assert names == ["Étage", None]
    assert (
        json.loads(capsys.readouterr().out)["per_source"]["savills"]["current_active"]
        == 2
    )
