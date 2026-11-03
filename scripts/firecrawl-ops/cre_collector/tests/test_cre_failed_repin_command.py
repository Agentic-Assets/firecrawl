"""A failed Repin must be retained as a failed, settled command."""
import json
from datetime import UTC, datetime

import pytest

from cre_code_handoff_evidence import HandoffError, sha
from cre_runtime_maintenance_transition import validate_maintenance_command


def test_explicit_failed_command_requires_exact_exit_and_closed_log():
    log = b"native observation expired/future\n"
    raw = json.dumps({"schema": "cre-code-command-v1", "producer": "checkpoint-run-command-v1",
                      "argv": ["pwsh", "-Mode", "Repin"], "exit_code": 1, "effective_cwd": None,
                      "local_settlement": "confirmed", "log_closed": True, "command_log_sha256": sha(log),
                      "started_at": datetime.now(UTC).isoformat(), "finished_at": datetime.now(UTC).isoformat()}).encode()
    data = {"/receipt": raw, "/log": log}
    def reference(name): return {"path": name, "sha256": sha(data[name]), "bytes": len(data[name])}
    row = {"receipt": reference("/receipt"), "log": reference("/log")}
    validate_maintenance_command(row, ["pwsh", "-Mode", "Repin"], None, None, data.__getitem__, failed_repin=True)
    with pytest.raises(HandoffError):
        validate_maintenance_command(row, ["pwsh", "-Mode", "Repin"], None, None, data.__getitem__)
    with pytest.raises(HandoffError, match="unsupported command outcome"):
        validate_maintenance_command(row, ["pwsh", "-Mode", "Hold"], None, None,
                                     data.__getitem__, failed_repin=True)
