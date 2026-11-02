"""The native authorization check must reread its input files."""

import json

import pytest

import cre_code_handoff as operation
import cre_runtime_maintenance as maintenance
from cre_code_handoff_evidence import HandoffError


@pytest.mark.parametrize("changed", ["observation", "authorization"])
def test_native_authorization_check_refuses_input_changed_during_validation(monkeypatch, changed):
    packet = {"schema": "cre-runtime-maintenance-v1"}
    paths = {"/release": b"release", "/observation": b"original observation", "/authorization": b"{}"}
    monkeypatch.setattr(operation, "load_release", lambda _: (b"release", packet, object()))
    monkeypatch.setattr(operation, "read_file", lambda path: paths[str(path)])

    def validate(*args, **kwargs):
        paths["/" + changed] = json.dumps({"changed": True}).encode()

    monkeypatch.setattr(maintenance, "validate_maintenance_authorization", validate)
    with pytest.raises(HandoffError):
        operation.main(["--maintenance-packet", "/release", "--check-authorization", "--observation", "/observation",
                        "--authorization", "/authorization", "--output", "/output"])
