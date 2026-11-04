"""Synthetic disabled clock compatibility; no claim about Windows next-run facts."""

from datetime import timedelta

import pytest
import test_cre_code_handoff as fixtures

from cre_code_handoff_evidence import (
    HandoffError,
    clock,
    json_bytes,
    sha,
    validate_lineage,
)

# Reuse the existing complete synthetic provenance fixture without duplicating it.
admitted = fixtures.admitted
lineage_case = fixtures.lineage_case


@pytest.mark.parametrize("offset", [-1, 0, None], ids=["backward", "equal", "missing-timezone"])
def test_disabled_historical_member_clocks_are_parsed_without_ordering(lineage_case, offset):
    release, _, inputs = lineage_case
    member = release["task_baseline"][0]
    member["enabled"] = False
    member["next_run_utc"] = ((clock(member["last_run_utc"]) + timedelta(seconds=offset)).isoformat()
                              if offset is not None else "2026-10-22T00:00:00")
    edges = release["task_baseline"][2]["failure"]["transitions"]

    def replace(role, raw):
        inputs.data[role] = raw
        next(row for row in inputs.rows if row["role"] == role).update(bytes=len(raw), sha256=sha(raw))

    roles = {member["xml"]}
    for edge in edges:
        row = next(row for row in edge["xml"] if row["key"] == member["key"])
        roles.update((row["before"], row["after"]))
    for role in roles:
        replace(role, inputs.data[role].replace(b"<Enabled>true</Enabled>", b"<Enabled>false</Enabled>"))
    for edge in edges:
        xml = next(row for row in edge["xml"] if row["key"] == member["key"])
        readback = json_bytes(inputs.data[edge["readback"]])
        comparison = next(row for row in readback["task_comparisons"] if row["key"] == member["key"])
        comparison.update(enabled_before=False, enabled_after=False, state_before="Disabled", state_after="Disabled",
                          prior_xml_sha256=sha(inputs.data[xml["before"]]), observed_xml_sha256=sha(inputs.data[xml["after"]]))
        task = next(row for row in readback["tasks"] if row["key"] == member["key"])
        task.update(enabled=False, state="Disabled", next_run_utc=member["next_run_utc"], xml_sha256=sha(inputs.data[xml["after"]]))
        replace(edge["readback"], fixtures.encoded(readback))
    evidence = inputs.evidence(release)
    if offset is None:
        with pytest.raises(HandoffError, match="clock must include timezone"):
            validate_lineage(release, evidence)
    else:
        validate_lineage(release, evidence)
