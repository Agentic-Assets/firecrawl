"""Bounded read-only host observations for the existing Windows/Ubuntu route."""

import json
import math
import subprocess

from cre_code_handoff_evidence import require, sha, unique

CONTAINER_FIELDS = (
    "id", "name", "image", "started_at", "memory", "nano_cpus",
    "cpu_quota", "cpu_period", "environment_sha256",
)


def docker_output(arguments):
    result = subprocess.run(["docker", *arguments], check=True, capture_output=True, timeout=15)
    require(len(result.stdout) <= 4 * 1024 * 1024, "Docker observation exceeds bound")
    return result.stdout


def observe_host(checkpoint, memory_type, client, idle, docker=docker_output):
    ids = docker(["ps", "-q"]).decode().split()
    require(len(ids) == 5 and len(set(ids)) == 5, "expected exact five running containers")
    inspected = json.loads(docker(["inspect", *ids]))
    containers = []
    for value in inspected:
        containers.append({
            "id": value["Id"], "name": value["Name"], "image": value["Image"],
            "status": value["State"]["Status"], "started_at": value["State"]["StartedAt"],
            "memory": value["HostConfig"]["Memory"], "nano_cpus": value["HostConfig"]["NanoCpus"],
            "cpu_quota": value["HostConfig"]["CpuQuota"], "cpu_period": value["HostConfig"]["CpuPeriod"],
            "environment_sha256": sha(json.dumps(sorted(value["Config"].get("Env") or []), separators=(",", ":")).encode()),
        })
    browsers = [row for row in containers if "playwright" in row["name"]]
    require(len(browsers) == 1, "exact browser container missing")
    script = "fetch('http://127.0.0.1:'+(process.env.PORT||'3000')+'/health',{signal:AbortSignal.timeout(3000)}).then(async r=>{if(!r.ok)throw Error('health');process.stdout.write(await r.text())}).catch(()=>process.exit(1))"
    browser = json.loads(docker(["exec", browsers[0]["id"], "node", "-e", script]))
    memory = memory_type()
    level = memory()
    return {"containers": containers, "browser": browser,
            "linux_resources": {"cpu_percent": checkpoint.read_host_cpu_percent(),
                                "memory_level": level, "memory": memory.last_sample},
            "queues": idle(client("http://127.0.0.1:3002", timeout_seconds=5))}


def qualify_host(observation, expected):
    containers = unique(observation.get("containers"), "id")
    pinned = unique(expected.get("containers"), "id")
    require(len(containers) == len(pinned) == 5 and set(containers) == set(pinned), "container fleet identity differs")
    for key, row in containers.items():
        require(all(field in row and field in pinned[key] for field in CONTAINER_FIELDS)
                and all(isinstance(row[field], str) and row[field] for field in
                        ("id", "name", "image", "started_at", "environment_sha256"))
                and all(type(row[field]) is int for field in ("memory", "nano_cpus", "cpu_quota", "cpu_period"))
                and row.get("status") == "running"
                and all(row.get(field) == pinned[key].get(field) for field in CONTAINER_FIELDS),
                "container image/epoch/caps/environment drift")
    browser = observation.get("browser", {})
    require(browser.get("status") == "healthy" and type(browser.get("activePages")) is int
            and browser["activePages"] == 0 and type(browser.get("maxConcurrentPages")) is int
            and browser["maxConcurrentPages"] == 4, "browser health/idle/page-cap admission refused")
    resources = observation.get("linux_resources", {})
    cpu = resources.get("cpu_percent")
    memory = resources.get("memory", {})
    available = memory.get("host", {}).get("memory_available_bytes")
    require(type(cpu) in {int, float} and math.isfinite(cpu) and 0 <= cpu < 90
            and type(resources.get("memory_level")) is int and resources["memory_level"] == 1 and type(available) is int
            and available >= 8 * 1024**3 and memory.get("causes") == [],
            "Linux normal/8GiB/CPU admission refused")


def qualify_native(value):
    require(isinstance(value, dict) and value.get("admitted") is True,
            "native admission must be strict true")
    cpu, available = value.get("cpuPercent"), value.get("availablePhysicalGiB")
    require(type(cpu) in {int, float} and math.isfinite(cpu) and 0 <= cpu < 90
            and type(available) in {int, float} and math.isfinite(available) and available >= 8,
            "Windows CPU/8GiB measurements refused")
