"""One maintenance boundary proves current authorization and actual lease owner."""

from types import SimpleNamespace

import pytest

import cre_code_handoff as operation
import cre_runtime_maintenance as maintenance
from cre_code_handoff_evidence import HandoffError


@pytest.mark.parametrize("failure", [None, "authorization", "owner", "input_race", "late_input_race"])
def test_combined_boundary_requires_both_and_checks_owner_last(monkeypatch, failure):
    packet = {"schema": "cre-runtime-maintenance-v1", "programs": {"native_git": "unused"},
              "roots": {"runtime": "/runtime"}}
    paths = {"/release": b"release", "/observation": b"original",
             "/authorization": b'{"expires_at":"2999-01-01T00:00:00Z"}'}
    events = []
    checkpoint = type("Checkpoint", (), {"canonical_shared_lock_dir": staticmethod(lambda _: "/lock")})
    monkeypatch.setattr(operation, "load_release", lambda _: (b"release", packet, SimpleNamespace(recheck=lambda: None)))
    monkeypatch.setattr(operation, "read_file", lambda path: paths[str(path)])
    def runtime_modules(*_):
        if failure == "late_input_race":
            paths["/authorization"] = b"changed"
        return checkpoint, None, None, None

    monkeypatch.setattr(operation, "runtime_modules", runtime_modules)

    def validate(*args, **kwargs):
        events.append("authorization")
        if failure == "authorization":
            raise HandoffError("authorization denied")
        if failure == "input_race":
            paths["/observation"] = b"changed"

    def owner(*args, **kwargs):
        events.append("owner")
        assert args[2] == 1234
        assert kwargs["maintenance_inputs"]["--authorization"] == "/authorization"
        if failure == "owner":
            raise HandoffError("owner denied")

    monkeypatch.setattr(maintenance, "validate_maintenance_authorization", validate)
    monkeypatch.setattr(operation, "assert_owned", owner)
    argv = ["--maintenance-packet", "/release", "--assert-maintenance-owner-pid", "1234",
            "--observation", "/observation", "--authorization", "/authorization", "--output", "/output"]
    if failure:
        with pytest.raises(HandoffError):
            operation.main(argv)
    else:
        assert operation.main(argv) == 0
        assert events == ["authorization", "owner"]
    if failure in ("authorization", "input_race", "late_input_race"):
        assert events == ["authorization"]
