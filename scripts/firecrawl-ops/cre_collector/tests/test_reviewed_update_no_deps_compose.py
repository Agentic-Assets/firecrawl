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


def test_real_compose_selected_then_rollback_preserves_dependency_and_exact_hash(tmp_path):
    if os.environ.get("CRE_TEST_DISPOSABLE_COMPOSE") != "1":
        pytest.skip("requires explicit disposable Docker Compose fixture")
    from cre_reviewed_update_images import _rollback_configuration_sha

    project = "codexrollback" + uuid.uuid4().hex[:12]
    selected_tag = project + ":selected"
    base = tmp_path / "compose.yaml"
    overlay = tmp_path / "selected.yaml"
    dockerfile = tmp_path / "Dockerfile"
    base.write_text(
        "services:\n"
        "  dependency:\n    image: golang:1.26\n    command: [sleep, infinity]\n"
        "  api:\n    image: golang:1.26\n    command: [sleep, infinity]\n"
        "    depends_on: [dependency]\n"
        "networks:\n  default:\n    internal: true\n"
    )
    overlay.write_text("services:\n  api:\n    image: " + selected_tag + "\n")
    dockerfile.write_text('FROM golang:1.26\nLABEL org.opencontainers.image.revision="' + "a" * 40 + '"\n')
    cmd = ["docker", "compose", "--project-name", project, "-f", str(base)]
    selected_cmd = cmd + ["-f", str(overlay)]
    assert not subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project], text=True
    ).strip()
    def inspect(name):
        return json.loads(subprocess.check_output(["docker", "inspect", project + "-" + name + "-1"]))[0]
    def image(name):
        return json.loads(subprocess.check_output(["docker", "image", "inspect", name]))[0]

    built = False
    try:
        subprocess.run(["docker", "build", "--network", "none", "--pull=false", "-t", selected_tag,
                        str(tmp_path)], check=True, capture_output=True)
        built = True
        previous_image, selected_image = image("golang:1.26"), image(selected_tag)
        assert previous_image["Id"] != selected_image["Id"]
        subprocess.run(cmd + ["up", "-d", "--pull", "never"], check=True, capture_output=True)
        before, dependency_before = inspect("api"), inspect("dependency")
        rollback_config = _rollback_configuration_sha(before, "api", [str(base)])
        projected = project_expected_configuration(
            before, previous_image, selected_image, set(), set(), set(), "a" * 40,
            expected_config_files=str(base) + "," + str(overlay), expected_replace="api-1", no_deps=True
        )["config_sha256"]
        subprocess.run(selected_cmd + ["up", "-d", "--no-deps", "--force-recreate", "--pull", "never", "api"],
                       check=True, capture_output=True)
        selected, dependency_selected = inspect("api"), inspect("dependency")
        assert selected["Image"] == selected_image["Id"]
        assert snapshot_container(selected)["config_sha256"] == projected
        subprocess.run(cmd + ["up", "-d", "--no-deps", "--force-recreate", "--pull", "never", "api"],
                       check=True, capture_output=True)
        rolled_back, dependency_after = inspect("api"), inspect("dependency")
        assert rolled_back["Image"] == previous_image["Id"]
        assert snapshot_container(rolled_back)["config_sha256"] == rollback_config
        assert dependency_before["Id"] == dependency_selected["Id"] == dependency_after["Id"]
        assert snapshot_container(dependency_before)["config_sha256"] == snapshot_container(dependency_after)["config_sha256"]
        altered = copy.deepcopy(rolled_back)
        altered["Config"]["Labels"]["com.docker.compose.depends_on"] = "unexpected"
        assert snapshot_container(altered)["config_sha256"] != rollback_config
    finally:
        subprocess.run(cmd + ["down", "--remove-orphans"], check=True, capture_output=True)
        if built:
            subprocess.run(["docker", "image", "rm", selected_tag], check=True, capture_output=True)
