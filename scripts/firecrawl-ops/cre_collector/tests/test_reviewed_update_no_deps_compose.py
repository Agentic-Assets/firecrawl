"""Projection for the reviewed updater's docker compose up --no-deps."""
import copy
import json
import os
import subprocess
import uuid

import pytest

from cre_code_handoff_evidence import HandoffError
from cre_runtime_maintenance_build import project_expected_configuration
from cre_runtime_maintenance_images import snapshot_container


def test_reviewed_no_deps_projection_matches_disposable_compose(tmp_path):
    if os.environ.get("CRE_TEST_DISPOSABLE_COMPOSE") != "1":
        pytest.skip("requires explicit disposable Docker Compose fixture")
    project = "codexnodeps" + uuid.uuid4().hex[:12]
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  dependency:\n    image: golang:1.26\n    command: [sleep, infinity]\n"
        "  api:\n    image: golang:1.26\n    command: [sleep, infinity]\n"
        "    depends_on: [dependency]\n"
        "networks:\n  default:\n    internal: true\n"
    )
    cmd = ["docker", "compose", "--project-name", project, "-f", str(compose_file)]
    assert not subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project], text=True
    ).strip()

    def inspect():
        return json.loads(subprocess.check_output(["docker", "inspect", project + "-api-1"]))[0]

    try:
        subprocess.run(cmd + ["up", "-d", "--pull", "never"], check=True, capture_output=True)
        before = inspect()
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", "golang:1.26"]))[0]
        subprocess.run(cmd + ["up", "-d", "--no-deps", "--force-recreate", "--pull", "never", "api"],
                       check=True, capture_output=True)
        after = inspect()
        assert before["Config"]["Labels"]["com.docker.compose.depends_on"]
        assert after["Config"]["Labels"]["com.docker.compose.depends_on"] == ""

        old = copy.deepcopy(image)
        new = copy.deepcopy(image)
        new["Config"]["Labels"] = dict(new["Config"].get("Labels") or {})
        new["Config"]["Labels"]["org.opencontainers.image.revision"] = "a" * 40
        after["Config"]["Labels"]["org.opencontainers.image.revision"] = "a" * 40
        args = (before, old, new, set(), set(), set(), "a" * 40)
        kwargs = {"expected_config_files": str(compose_file), "expected_replace": "api-1"}
        expected = project_expected_configuration(*args, **kwargs, no_deps=True)["config_sha256"]
        assert expected == snapshot_container(after)["config_sha256"]
        assert project_expected_configuration(*args, **kwargs)["config_sha256"] != expected

        for key, value in (("com.docker.compose.depends_on", "unexpected:service_started:false"),
                           ("com.docker.compose.project", "other"),
                           ("com.docker.compose.replace", "other")):
            changed = copy.deepcopy(after)
            changed["Config"]["Labels"][key] = value
            assert snapshot_container(changed)["config_sha256"] != expected
        spoofed = copy.deepcopy(new)
        spoofed["Config"]["Labels"]["com.docker.compose.depends_on"] = ""
        with pytest.raises(HandoffError):
            project_expected_configuration(before, old, spoofed, set(), set(), set(), "a" * 40,
                                           **kwargs, no_deps=True)
    finally:
        subprocess.run(cmd + ["down", "--remove-orphans"], check=True, capture_output=True)
