"""Setup subprocess contracts with fake tools: no network, install or database.

Run only in a standalone --no-hardlinks clone, like the rest of this suite.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parents[1]
READY = {"message": "Firecrawl API", "documentation_url": "https://docs.firecrawl.dev"}


@pytest.fixture
def setup_clone(tmp_path: Path):
    """Run the real shell entrypoint against private, instrumented prerequisites."""
    repo = tmp_path / "repo"
    collector = repo / "scripts/firecrawl-ops/cre_collector"
    collector.mkdir(parents=True)
    for name in ("cre_setup.sh", "cre_platform.py"):
        shutil.copy2(COLLECTOR / name, collector / name)
    (collector / "node_modules").mkdir()
    (collector / "launchd").mkdir()
    (collector / "launchd/install_launchd.sh").touch()
    (collector / "cre_status.sh").touch()
    (collector / "cre_monitor.py").write_text("pass\n")
    (collector / "cre_gate.py").write_text("pass\n")
    (collector / "cre_ingest.py").write_text(
        "import json, os, sys\n"
        "def load_db_url(_):\n"
        "    return 'not-a-real-database', '/synthetic/equire.env'\n"
        "if __name__ == '__main__':\n"
        "    with open(os.environ['TOOL_LOG'], 'a') as log:\n"
        "        log.write(json.dumps(['ingest', *sys.argv[1:]]) + '\\n')\n"
        "    assert '--dry-run' in sys.argv\n"
    )
    # The old --check path invokes this POST-based healthcheck. Never execute it.
    (repo / "scripts/firecrawl-ops/firecrawl_healthcheck.sh").write_text(
        '#!/bin/bash\nprintf "full-healthcheck\\n" >> "$TOOL_LOG"\nexit 0\n'
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    stub = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['TOOL_LOG'], 'a') as log:\n"
        "    log.write(json.dumps([name, *args]) + '\\n')\n"
        "if args == ['--version']:\n"
        "    print('synthetic tool 1.0')\n"
        "elif name == 'docker':\n"
        "    print(os.environ.get('DOCKER_CONTEXT', 'default'))\n"
        "elif name == 'uname':\n"
        "    print(os.environ.get('SETUP_PLATFORM', 'Linux'))\n"
        "elif name == 'curl':\n"
        "    print(os.environ['READY_JSON'])\n"
        "    sys.exit(int(os.environ.get('CURL_EXIT', '0')))\n"
        "elif name == 'npx':\n"
        "    if os.environ.get('COLLECT_FAIL'):\n"
        "        sys.exit(7)\n"
        "    output = next(a.removeprefix('--out=') for a in args if a.startswith('--out='))\n"
        "    Path(output).write_text('[]')\n"
    )
    for name in ("node", "npm", "npx", "git", "docker", "curl", "uname", "psql"):
        path = tools / name
        path.write_text(stub)
        path.chmod(0o755)
    (tools / "python3").symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "tools.jsonl"
    env = {
        **os.environ,
        "PATH": f"{tools}:/usr/bin:/bin",
        "HOME": str(home),
        "FC_DIR": str(repo),
        "PSQL_BIN": str(tools / "psql"),
        "TOOL_LOG": str(log),
        "READY_JSON": json.dumps(READY),
    }
    # The operator's runtime settings must never steer this synthetic fixture.
    for name in ("FIRECRAWL_API_URL", "API_URL", "CRE_SMOKE_SOURCE", "CRE_ENV_FILE"):
        env.pop(name, None)

    def run(*args: str, **overrides: str):
        log.write_text("")
        result = subprocess.run(
            ["/bin/bash", str(collector / "cre_setup.sh"), *args],
            check=False,
            env={**env, **overrides},
            text=True,
            capture_output=True,
            timeout=20,
        )
        calls = []
        for line in log.read_text().splitlines():
            # Non-JSON is the deliberately forbidden healthcheck sentinel.
            calls.append(json.loads(line) if line.startswith("[") else [line])
        return result, calls

    return collector, run


@pytest.mark.parametrize("dependencies_present", [False, True])
def test_check_never_installs_collects_ingests_or_runs_full_healthcheck(
    setup_clone, dependencies_present: bool
):
    collector, run = setup_clone
    if not dependencies_present:
        (collector / "node_modules").rmdir()
    result, calls = run("--check", "--reinstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 FAIL" in result.stdout
    assert not any(call[0] in {"ingest", "full-healthcheck"} for call in calls)
    assert not any(call[0] == "npx" and call[1:] != ["--version"] for call in calls)
    assert not any(call[:2] in (["npm", "ci"], ["npm", "install"]) for call in calls)
    assert not (collector / "out").exists()
    assert not list(collector.rglob("__pycache__"))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "http://localhost:3002/"),
        ({"API_URL": "http://legacy.invalid:4000/"}, "http://legacy.invalid:4000/"),
        (
            {"FIRECRAWL_API_URL": "http://preferred.invalid:5000"},
            "http://preferred.invalid:5000/",
        ),
        (
            {
                "API_URL": "http://ignored.invalid",
                "FIRECRAWL_API_URL": "http://preferred.invalid/",
            },
            "http://preferred.invalid/",
        ),
    ],
)
def test_readiness_is_one_bounded_get_with_url_precedence(
    setup_clone, overrides, expected
):
    _, run = setup_clone
    result, calls = run("--check", **overrides)
    assert result.returncode == 0, result.stdout + result.stderr
    requests = [
        call[1:] for call in calls if call[0] == "curl" and call[1:] != ["--version"]
    ]
    assert requests == [
        [
            "--disable",
            "-fsS",
            "--request",
            "GET",
            "--connect-timeout",
            "2",
            "--max-time",
            "5",
            expected,
        ]
    ]
    assert "GET only; does not prove scrape/worker health" in result.stdout


@pytest.mark.parametrize(
    "overrides",
    [
        {"CURL_EXIT": "28"},
        {"READY_JSON": "not json"},
        {"READY_JSON": "[]"},
        {"READY_JSON": '{"message":"Other API"}'},
    ],
)
def test_unavailable_or_wrong_service_warns_without_claiming_stack_health(
    setup_clone, overrides
):
    _, run = setup_clone
    result, calls = run("--no-install", **overrides)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN Firecrawl API readiness failed" in result.stdout
    assert "full stack health unverified" in result.stdout
    assert "smoke skipped: Firecrawl API readiness failed" in result.stdout
    assert not any(call[0] in {"ingest", "full-healthcheck"} for call in calls)
    assert not any(call[0] == "npx" and call[1:] != ["--version"] for call in calls)


@pytest.mark.parametrize("source", [None, "avison-young"])
def test_ordinary_setup_smoke_uses_default_bounded_source_and_preserves_override(
    setup_clone, source
):
    collector, run = setup_clone
    result, calls = run(
        "--no-install", **({"CRE_SMOKE_SOURCE": source} if source else {})
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collect = [call for call in calls if call[:3] == ["npx", "tsx", "collect.ts"]]
    assert len(collect) == 1
    assert f"--source={source or 'interra-realty'}" in collect[0]
    assert "--transaction=sale" in collect[0] and "--max-items=3" in collect[0]
    assert ["ingest", "--in", "out/setup_smoke.json", "--dry-run"] in calls
    assert ["full-healthcheck"] not in calls
    assert "Network smoke test" in result.stdout
    assert "max-items limits output, not inventory fetches" in result.stdout
    if source is None:
        assert "one inventory GET, at most 32 MiB / 60 seconds" in result.stdout
    assert not (collector / "out/setup_smoke.json").exists()


def test_collect_failure_is_warning_and_does_not_ingest(setup_clone):
    _, run = setup_clone
    result, calls = run("--no-install", COLLECT_FAIL="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN collect smoke failed for 'interra-realty'" in result.stdout
    assert not any(call[0] == "ingest" for call in calls)


def test_no_smoke_checks_readiness_without_collecting(setup_clone):
    _, run = setup_clone
    result, calls = run("--no-smoke")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Firecrawl API ready" in result.stdout
    assert "skipped (--check / --no-smoke)" in result.stdout
    assert not any(call[0] in {"ingest", "full-healthcheck"} for call in calls)
    assert not any(call[0] == "npx" and call[1:] != ["--version"] for call in calls)


@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
def test_platform_context_and_scheduler_guidance_are_preserved(setup_clone, platform):
    _, run = setup_clone
    context = "orbstack" if platform == "Darwin" else "desktop-linux"
    result, _ = run("--check", SETUP_PLATFORM=platform, DOCKER_CONTEXT=context)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"OK   docker context = {context}" in result.stdout
    if platform == "Darwin":
        assert "Clone is outside ~/Documents" in result.stdout
        assert "launchd generator present" in result.stdout
        assert "Check scheduled-run health: bash cre_status.sh\n" in result.stdout
        assert "--scheduler external" not in result.stdout
    else:
        assert "cre_status.sh --scheduler external" in result.stdout
        assert "does not verify an external scheduler" in result.stdout
        assert "source ~/.agentic-assets/env" in result.stdout
