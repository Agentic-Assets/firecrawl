"""Ready output retains the actual release and mutation inputs."""

import test_cre_maintenance_steps as fixtures

from cre_code_handoff_evidence import sha

steps_case = fixtures.steps_case


def test_ready_references_exact_original_release_and_mutation(steps_case):
    case = steps_case
    ready = fixtures.invoke(case)
    for field, path in (("release_reference", case.args.release), ("mutation", case.out / "mutation-start.json")):
        raw = case.steps.read_file(path)
        assert ready[field]["sha256"] == sha(raw) and ready[field]["bytes"] == len(raw)
        assert str(ready[field]["path"]) == str(path)
