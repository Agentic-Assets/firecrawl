"""Compare bounded container observations for a reviewed image transition.

This consumer does not observe Docker or authorize a rebuild. The owning
operation must produce both snapshots from Docker and bind the built images.
"""

import json
import re

from cre_code_handoff_evidence import require, sha, unique


def qualify_environment_transition(old_live, new_live, old_image, new_image, explicit_keys, allowed_changed):
    """Admit only reviewed image-default changes; never return environment values."""
    def parse(rows):
        require(isinstance(rows, list) and all(isinstance(row, str) and "=" in row for row in rows),
                "invalid environment")
        result = {}
        for row in rows:
            key, value = row.split("=", 1)
            require(key and key not in result, "duplicate or empty environment key")
            result[key] = value
        return result

    previous, current, previous_image, current_image = map(parse, (old_live, new_live, old_image, new_image))
    require(isinstance(explicit_keys, set) and isinstance(allowed_changed, set)
            and all(isinstance(key, str) and key for key in explicit_keys | allowed_changed),
            "environment scope must be explicit")
    require(set(previous) == set(current), "environment keys changed")
    changed = {key for key in previous if previous[key] != current[key]}
    require(changed == allowed_changed and not changed & explicit_keys,
            "unreviewed or explicit environment value changed")
    for key in changed:
        require(key in previous_image and key in current_image
                and previous[key] == previous_image[key] and current[key] == current_image[key],
                "changed environment is not the exact image default")
    return sorted(changed)


def observe_containers(docker):
    """Read the actual five-container fleet without emitting raw inspect data."""
    ids = docker(["ps", "--no-trunc", "-q"]).decode().split()
    require(len(ids) == 5 and len(set(ids)) == 5, "expected exact five running containers")
    inspected = json.loads(docker(["inspect", *ids]))
    require(isinstance(inspected, list) and len(inspected) == 5, "incomplete container inspection")
    result = [snapshot_container(value) for value in inspected]
    require({row["id"] for row in result} == set(ids), "container fleet changed during inspection")
    require(len(unique(result, "service")) == 5, "duplicate container service")
    return result


def snapshot_container(inspect):
    """Normalize Docker inspect in memory without publishing env or mount paths."""
    require(isinstance(inspect, dict), "Docker inspect object required")
    config, host = inspect.get("Config"), inspect.get("HostConfig")
    state, networks, mounts = inspect.get("State"), inspect.get("NetworkSettings"), inspect.get("Mounts")
    require(all(isinstance(value, dict) for value in (config, host, state, networks))
            and isinstance(mounts, list), "incomplete Docker inspect")
    config_fields = ("Env", "Labels", "User", "Entrypoint", "Cmd", "WorkingDir")
    host_fields = ("RestartPolicy", "Memory", "NanoCpus", "ShmSize", "ReadonlyRootfs", "Privileged",
                   "SecurityOpt", "CapAdd", "CapDrop", "PortBindings", "NetworkMode")
    require(all(field in config for field in config_fields) and all(field in host for field in host_fields)
            and isinstance(config["Env"], list) and all(isinstance(v, str) for v in config["Env"])
            and isinstance(config["Labels"], dict) and isinstance(networks.get("Networks"), dict)
            and isinstance(host["RestartPolicy"], dict)
            and set(host["RestartPolicy"]) == {"Name", "MaximumRetryCount"}, "Docker settings missing")
    labels = dict(config["Labels"])
    service = labels.get("com.docker.compose.service")
    require(isinstance(service, str) and service, "Compose service identity missing")
    # Compose changes these two build metadata labels when image bytes change.
    labels.pop("com.docker.compose.image", None)
    labels.pop("com.docker.compose.config-hash", None)
    mount_fields = ("Type", "Source", "Destination", "RW", "Propagation")
    require(all(isinstance(m, dict) and all(key in m for key in mount_fields) for m in mounts),
            "incomplete mount settings")
    stable_networks = {}
    volatile = {"EndpointID", "Gateway", "IPAddress", "IPPrefixLen", "IPv6Gateway",
                "GlobalIPv6Address", "GlobalIPv6PrefixLen", "MacAddress", "DNSNames"}
    for name, details in networks["Networks"].items():
        require(isinstance(name, str) and isinstance(details, dict), "invalid network settings")
        stable_network = {key: value for key, value in details.items() if key not in volatile}
        aliases = stable_network.get("Aliases")
        require(aliases is None or (isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)),
                "invalid network aliases")
        if aliases is not None:
            stable_network["Aliases"] = sorted(a for a in aliases if a not in {
                inspect.get("Id"), str(inspect.get("Id", ""))[:12]})
        stable_networks[name] = stable_network
    stable_config = dict(config)
    stable_config["Env"] = sorted(config["Env"])
    stable_config["Labels"] = labels
    # Image and the automatically generated hostname follow container identity.
    stable_config.pop("Image", None)
    if stable_config.get("Hostname") in {inspect.get("Id"), str(inspect.get("Id", ""))[:12]}:
        stable_config.pop("Hostname", None)
    stable = {"config": stable_config, "host": host,
              "mounts": sorted(({key: m[key] for key in mount_fields} for m in mounts),
                               key=lambda row: (row["Destination"], row["Source"])),
              "networks": stable_networks}
    require(all(isinstance(inspect.get(key), str) and inspect[key] for key in ("Id", "Image"))
            and isinstance(state.get("StartedAt"), str) and isinstance(state.get("Status"), str)
            and isinstance(host["RestartPolicy"].get("Name"), str), "container identity missing")
    return {"service": service, "id": inspect["Id"], "image": inspect["Image"],
            "started_at": state["StartedAt"], "status": state["Status"],
            "restart_policy": host["RestartPolicy"]["Name"],
            "config_sha256": sha(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode())}


def _transition_fleet(before, after):
    old, new = unique(before, "service"), unique(after, "service")
    require(len(old) == 5 and set(old) == set(new), "container service fleet differs")
    fields = {"service", "id", "image", "started_at", "config_sha256", "restart_policy", "status"}
    for row in [*old.values(), *new.values()]:
        require(set(row) == fields and row["status"] == "running"
                and all(isinstance(row[field], str) and row[field] for field in fields)
                and re.fullmatch(r"[0-9a-f]{64}", row["config_sha256"]),
                "container observation malformed")
    return old, new


def qualify_image_transition(before, after, expected_images, *, expected_after_config=None):
    """Compare to old settings or a separately qualified expected after-config."""
    old, new = _transition_fleet(before, after)
    _qualify_replaced_images(old, new, expected_images, expected_after_config)


def _qualify_replaced_images(old, new, expected_images, expected_after_config):
    require(isinstance(expected_images, dict) and expected_images
            and set(expected_images) <= {"api", "playwright-service", "nuq-postgres"}
            and set(expected_images) <= set(old), "unexpected image rebuild service")
    if expected_after_config is None:
        expected_after_config = {}
    require(isinstance(expected_after_config, dict) and set(expected_after_config) <= set(expected_images)
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in expected_after_config.values()), "invalid expected after-config bindings")
    for name in old:
        previous, current = old[name], new[name]
        if name not in expected_images:
            require(previous == current, "unrelated container changed")
            continue
        require(isinstance(expected_images[name], str) and expected_images[name]
                and current["image"] == expected_images[name], "built image identity differs")
        require(current["config_sha256"] == expected_after_config.get(name, previous["config_sha256"])
                and current["restart_policy"] == previous["restart_policy"],
                "container runtime configuration changed")
        require(current["id"] != previous["id"] and current["started_at"] != previous["started_at"],
                "rebuilt container was not replaced")


def qualify_current_image_transition(before, after, expected_images, *, expected_after_config=None):
    """Separate byte-identical no-ops from services that actually need replacement."""
    old, new = _transition_fleet(before, after)
    require(set(old) == {"api", "playwright-service", "nuq-postgres", "redis", "rabbitmq"}
            and isinstance(expected_images, dict) and expected_images
            and set(expected_images) <= {"api", "playwright-service", "nuq-postgres"},
            "image fleet or affected scope differs")
    expected_config = {} if expected_after_config is None else expected_after_config
    require(isinstance(expected_config, dict) and set(expected_config) <= set(expected_images),
            "invalid expected after-config scope")
    unchanged = set()
    for name, image in expected_images.items():
        require(isinstance(image, str) and image, "expected image identity missing")
        if (old[name]["image"] == image
                and expected_config.get(name, old[name]["config_sha256"]) == old[name]["config_sha256"]):
            require(old[name] == new[name], "unchanged image or configuration drifted")
            unchanged.add(name)
    replaced = set(expected_images) - unchanged
    if replaced:
        _qualify_replaced_images(old, new, {name: expected_images[name] for name in replaced},
                                 {name: expected_config[name] for name in replaced if name in expected_config})
    else:
        require(old == new and all(row.get("status") == "running" for row in old.values()),
                "unchanged fleet drifted")
    return {"unchanged": sorted(unchanged), "replaced": sorted(replaced)}
