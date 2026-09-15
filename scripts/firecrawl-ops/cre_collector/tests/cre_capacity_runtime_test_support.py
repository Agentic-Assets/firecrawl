"""Shared synthetic captures for CRE capacity controller contract tests."""

from __future__ import annotations

import cre_capacity_experiment as experiment
import cre_capacity_runtime as runtime


def profile() -> tuple[dict[str, object], str]:
    return experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")


def public_state(
    *, state: str = "baseline", docker_memory: int | None = None
) -> dict[str, object]:
    selected, _ = profile()
    baseline = selected["runtime_baseline"]
    requested = selected["requested"]
    browser_pages = (
        baseline["global_pages"] if state == "baseline" else requested["global_pages"]
    )
    browser_cpu = (
        baseline["browser_cpus"] if state == "baseline" else requested["browser_cpus"]
    )
    browser_pids = (
        baseline["browser_pids"] if state == "baseline" else requested["browser_pids"]
    )
    api_cpu = baseline["api_cpus"] if state == "baseline" else requested["api_cpus"]
    browser_env = {
        "count": 2,
        "keys_sha256": "k",
        "values_sha256": "v",
        "excluding_pages_sha256": "same",
    }
    api_env = {
        "count": 1,
        "keys_sha256": "a",
        "values_sha256": "b",
        "excluding_pages_sha256": "b",
    }
    value: dict[str, object] = {
        "repo": {
            "git_sha": "a" * 40,
            "dirty": False,
            "compose_sha256": "c" * 64,
            "override_sha256": "d" * 64,
            "execution_inputs_sha256": {
                key: str(index) * 64
                for index, key in enumerate(runtime.EXECUTION_INPUTS, 1)
            },
        },
        "host": {
            "orb_status": "Running",
            "orbstack_memory_mib": 32768,
            "docker_context": "orbstack",
            "docker_memtotal_bytes": docker_memory
            if docker_memory is not None
            else 32768 * 1024 * 1024 - 32768,
        },
        "endpoints": {
            "api": "http://127.0.0.1:3002",
            "browser": "http://127.0.0.1:3003",
        },
        "browser": {
            "id": "browser-before" if state == "baseline" else "browser-after",
            "image": "sha256:" + "b" * 64,
            "env": browser_env,
            "page_slots": str(browser_pages),
            "nano_cpus": browser_cpu * 1_000_000_000,
            "memory_bytes": baseline["browser_memory_bytes"],
            "swap_bytes": 0,
            "pids_limit": browser_pids,
            "shm_bytes": baseline["browser_shm_bytes"],
            "port_bindings": {
                "3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3003"}]
            },
            "network_mode": "firecrawl_backend",
            "mounts_sha256": "empty",
            "mount_count": 0,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "cgroup_memory_max": baseline["browser_memory_bytes"],
            "cgroup_swap_max": 0,
            "cgroup_memory_current": 512 * 1024 * 1024,
        },
        "api": {
            "id": "api-same",
            "image": "sha256:" + "a" * 64,
            "env": api_env,
            "page_slots": None,
            "nano_cpus": api_cpu * 1_000_000_000,
            "memory_bytes": baseline["api_memory_bytes"],
            "swap_bytes": 0,
            "pids_limit": None,
            "shm_bytes": 64 * 1024 * 1024,
            "port_bindings": {"3002/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3002"}]},
            "network_mode": "firecrawl_backend",
            "mounts_sha256": "api-mount",
            "mount_count": 1,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "cgroup_memory_max": baseline["api_memory_bytes"],
            "cgroup_swap_max": 0,
            "cgroup_memory_current": 3 * 1024 * 1024 * 1024,
        },
        "settlement": {
            "api_root_status": 200,
            "browser_root_status": 404,
            "api": {"active": 0, "waiting": 0, "total": 0},
            "active_crawls": 0,
            "rabbitmq_queue_count": 2,
            "rabbitmq_ready": 0,
            "rabbitmq_unacknowledged": 0,
            "nuq": {
                "queue_scrape_total": 0,
                "queue_scrape_backlog_total": 0,
                "queue_crawl_finished_total": 0,
            },
            "cre_process_active": False,
        },
    }
    value["transition_sha256"] = runtime.transition_fingerprint(value)
    value["snapshot_sha256"] = runtime.snapshot_fingerprint(value)
    return value


def capture(state: str = "baseline") -> runtime.RuntimeCapture:
    return runtime.RuntimeCapture(
        public=public_state(state=state),
        browser_env={
            "MAX_CONCURRENT_PAGES": "4" if state == "baseline" else "10",
            "PROXY_SERVER": "private",
        },
        api_env={"MODEL_NAME": "private"},
    )
