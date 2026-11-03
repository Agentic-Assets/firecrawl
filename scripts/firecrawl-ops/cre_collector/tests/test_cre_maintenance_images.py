"""A rebuild must preserve the unrelated services and reviewed settings."""

import copy

import pytest
from cre_code_handoff_evidence import HandoffError


def validator():
    try:
        from cre_runtime_maintenance_images import qualify_image_transition
    except ImportError:
        pytest.fail("container transition validator is missing")
    return qualify_image_transition


@pytest.fixture
def images():
    def row(service):
        return {"service": service, "id": "old-" + service, "image": "image-old-" + service,
                "started_at": "old-epoch", "config_sha256": "a" * 64,
                "restart_policy": "unless-stopped", "status": "running"}
    before = [row(name) for name in ("api", "playwright-service", "redis", "rabbitmq", "nuq-postgres")]
    after = copy.deepcopy(before)
    expected = {}
    for row in after[:2]:
        row.update(id="new-" + row["service"], image="image-new-" + row["service"], started_at="new-epoch")
        expected[row["service"]] = row["image"]
    return before, after, expected


def test_rebuilt_services_use_reviewed_images_and_preserve_configuration(images):
    before, after, expected = images
    validator()(before, after, expected)


def test_nuq_can_be_rebuilt_without_recreating_external_infrastructure(images):
    before, after, expected = images
    after[4].update(id="new-nuq", image="image-new-nuq", started_at="new-epoch")
    expected["nuq-postgres"] = "image-new-nuq"
    validator()(before, after, expected)


def test_image_default_change_requires_exact_reviewed_after_config(images):
    before, after, expected = images
    after[0]["config_sha256"] = "b" * 64
    with pytest.raises(HandoffError):
        validator()(before, after, expected)
    validator()(before, after, expected, expected_after_config={"api": "b" * 64})
    after[0]["config_sha256"] = "c" * 64
    with pytest.raises(HandoffError):
        validator()(before, after, expected, expected_after_config={"api": "b" * 64})


@pytest.mark.parametrize("change", ["restart", "configuration", "wrong-image", "unrelated-service", "duplicate"])
def test_rebuild_rejects_drift_or_unreviewed_images(images, change):
    before, after, expected = images
    if change == "restart":
        after[0]["restart_policy"] = "no"
    elif change == "configuration":
        after[1]["config_sha256"] = "b" * 64
    elif change == "wrong-image":
        after[0]["image"] = "other-image"
    elif change == "unrelated-service":
        after[2]["id"] = "changed"
    else:
        after.append(copy.deepcopy(after[0]))
    with pytest.raises(HandoffError):
        validator()(before, after, expected)


def test_inspect_snapshot_hashes_private_fields_and_detects_restart_drift():
    try:
        from cre_runtime_maintenance_images import snapshot_container
    except ImportError:
        pytest.fail("Docker inspect normalizer is missing")
    value = {"Id": "id", "Image": "sha256:image", "Name": "/fixture",
             "State": {"Status": "running", "StartedAt": "epoch"},
             "Config": {"Env": ["TOKEN=private-fixture"], "Labels": {"com.docker.compose.service": "api"},
                        "User": "1000", "Entrypoint": ["node"], "Cmd": ["app"], "WorkingDir": "/app"},
             "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                            "Memory": 1, "NanoCpus": 2, "ShmSize": 3, "ReadonlyRootfs": False,
                            "Privileged": False, "SecurityOpt": ["no-new-privileges:true"],
                            "CapAdd": None, "CapDrop": ["ALL"], "PortBindings": {}, "NetworkMode": "bridge"},
             "Mounts": [{"Type": "bind", "Source": "/private-fixture", "Destination": "/data",
                         "RW": False, "Propagation": "rprivate"}],
             "NetworkSettings": {"Networks": {"backend": {"Aliases": ["api", "id"]}}}}
    first = snapshot_container(value)
    assert "private-fixture" not in repr(first)
    changed = copy.deepcopy(value)
    changed["HostConfig"]["RestartPolicy"]["Name"] = "no"
    second = snapshot_container(changed)
    assert first["config_sha256"] != second["config_sha256"]
    changed = copy.deepcopy(value)
    changed["HostConfig"]["Dns"] = ["192.0.2.1"]
    assert first["config_sha256"] != snapshot_container(changed)["config_sha256"]
    changed = copy.deepcopy(value)
    changed["NetworkSettings"]["Networks"]["backend"]["Aliases"] = ["other", "id"]
    assert first["config_sha256"] != snapshot_container(changed)["config_sha256"]
    del changed["HostConfig"]["PortBindings"]
    with pytest.raises(HandoffError):
        snapshot_container(changed)


def test_actual_fleet_observer_refuses_incomplete_inspection():
    try:
        from cre_runtime_maintenance_images import observe_containers
    except ImportError:
        pytest.fail("container fleet observer is missing")
    def docker(argv):
        if argv == ["ps", "--no-trunc", "-q"]:
            return b"one\ntwo\nthree\nfour\nfive\n"
        assert argv == ["inspect", "one", "two", "three", "four", "five"]
        return b"[]"
    with pytest.raises(HandoffError):
        observe_containers(docker)


def test_actual_fleet_observer_reads_all_five_services_without_exposing_environment():
    import json
    try:
        from cre_runtime_maintenance_images import observe_containers
    except ImportError:
        pytest.fail("container fleet observer is missing")
    services = ["api", "playwright-service", "redis", "rabbitmq", "nuq-postgres"]
    def inspect(service):
        return {"Id": service, "Image": "image-" + service, "State": {"Status": "running", "StartedAt": "epoch"},
                "Config": {"Env": ["TOKEN=private-fixture"], "Labels": {"com.docker.compose.service": service},
                           "User": "", "Entrypoint": None, "Cmd": None, "WorkingDir": ""},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                               "Memory": 0, "NanoCpus": 0, "ShmSize": 0, "ReadonlyRootfs": False,
                               "Privileged": False, "SecurityOpt": None, "CapAdd": None, "CapDrop": None,
                               "PortBindings": {}, "NetworkMode": "backend"},
                "Mounts": [], "NetworkSettings": {"Networks": {"backend": {"Aliases": [service]}}}}
    def docker(argv):
        if argv == ["ps", "--no-trunc", "-q"]:
            return ("\n".join(services) + "\n").encode()
        assert argv == ["inspect", *services]
        return json.dumps([inspect(service) for service in services]).encode()
    observed = observe_containers(docker)
    assert {row["service"] for row in observed} == set(services)
    assert "private-fixture" not in repr(observed)


def test_only_reviewed_unoverridden_image_default_changes_are_admitted():
    try:
        from cre_runtime_maintenance_images import qualify_environment_transition
    except ImportError:
        pytest.fail("image default environment qualifier is missing")
    old_image = ["NODE_VERSION=old", "MODEL=old-image-default"]
    new_image = ["NODE_VERSION=new", "MODEL=new-image-default"]
    old_live = ["NODE_VERSION=old", "MODEL=private-explicit"]
    new_live = ["NODE_VERSION=new", "MODEL=private-explicit"]
    result = qualify_environment_transition(old_live, new_live, old_image, new_image, {"MODEL"}, {"NODE_VERSION"})
    assert result == ["NODE_VERSION"]
    assert "private-explicit" not in repr(result)
    with pytest.raises(HandoffError):
        qualify_environment_transition(old_live, ["NODE_VERSION=new", "MODEL=changed"], old_image, new_image,
                                       {"MODEL"}, {"NODE_VERSION"})
    with pytest.raises(HandoffError):
        qualify_environment_transition(old_live, new_live, old_image, new_image, {"NODE_VERSION", "MODEL"}, {"NODE_VERSION"})
    with pytest.raises(HandoffError):
        qualify_environment_transition(["NODE_VERSION=custom", "MODEL=private-explicit"], new_live,
                                       old_image, new_image, {"MODEL"}, {"NODE_VERSION"})


def test_current_transition_separates_true_noops_from_replacements(images):
    from cre_runtime_maintenance_images import qualify_current_image_transition

    before, after, expected = images
    after[0] = copy.deepcopy(before[0])
    expected["api"] = before[0]["image"]
    result = qualify_current_image_transition(before, after, expected)
    assert result == {"unchanged": ["api"], "replaced": ["playwright-service"]}
    with pytest.raises(HandoffError):
        qualify_current_image_transition(before, after, expected, expected_after_config={"api": "b" * 64})
    recreated = copy.deepcopy(after)
    recreated[0].update(id="recreated-api", started_at="new-epoch", config_sha256="b" * 64)
    result = qualify_current_image_transition(before, recreated, expected, expected_after_config={"api": "b" * 64})
    assert result == {"unchanged": [], "replaced": ["api", "playwright-service"]}
    after[0]["started_at"] = "changed"
    with pytest.raises(HandoffError, match="configuration drifted"):
        qualify_current_image_transition(before, after, expected)
