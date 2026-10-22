#!/usr/bin/env python3
"""One foreground Windows/Ubuntu code-only handoff. Default: offline preview.

No collection, SQL, installs, scheduler creation, automatic recovery or rollback.
Run maintained tooling from a separate reviewed clean checkout, never a capsule.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

from cre_code_handoff_evidence import (
    Evidence, HandoffError, json_bytes, require, sha, validate_authorization,
    validate_observation, validate_release,
)
from cre_code_handoff_git import Git, installed_dependencies, local_path, read_file, windows_path


def now():
    return datetime.now(timezone.utc)


def write_new(path, value):
    raw = (json.dumps(value, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def load_release(path):
    raw = read_file(path)
    release = json_bytes(raw)
    evidence = Evidence(release, read_file)
    validate_release(release, evidence)
    return raw, release, evidence


def runtime_modules(release, git):
    require(sys.platform == "linux" and os.geteuid() != 0, "nonroot Ubuntu required")
    tooling = local_path(release["roots"]["tooling"])
    windows_path(tooling)  # This first route invokes its native helper from a Windows-mapped checkout.
    require(tooling not in {local_path(release["roots"]["runtime"]), local_path(release["roots"]["native"])},
            "tooling must remain stable outside updated checkouts")
    require(Path(__file__).resolve().parent == tooling / "scripts/firecrawl-ops/cre_collector",
            "invoked tooling path differs")
    git.clean(tooling, release["tooling"])
    import cre_checkpoint_refresh as checkpoint
    from cre_linux_telemetry import LinuxMemorySampler
    sys.path.insert(0, str(tooling / "scripts/firecrawl-ops"))
    from firecrawl_operator_handoff import ReadOnlyLoopbackClient, idle_snapshot
    return checkpoint, LinuxMemorySampler, ReadOnlyLoopbackClient, idle_snapshot


def lock_clear(checkpoint, lock):
    from cre_posix import fcntl
    require(not lock.exists() and not lock.is_symlink(), "canonical owner/recovery directory present")
    authority = lock.with_name(lock.name + ".authority")
    fd = os.open(authority, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fields = checkpoint.SharedLock._authority_fields(fd)
        require(fields is not None and not fields[3]
                and (fields[0] == 0 or not Path("/proc", str(fields[0])).exists()),
                "canonical owner/recovery not clear")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HandoffError("canonical authority is held") from error
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def assert_owned(checkpoint, lock, expected_pid):
    """Narrow foreign observer using the existing owner/authority readers."""
    from cre_posix import fcntl
    directory = os.open(lock, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    authority_path = lock.with_name(lock.name + ".authority")
    fd = os.open(authority_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(directory)
        require(checkpoint._lock_directory_identity(lock) == (opened.st_dev, opened.st_ino)
                and checkpoint._lock_owner(lock) == expected_pid
                and checkpoint.SharedLock._owned_pid(directory) == expected_pid, "maintenance owner differs")
        fields = checkpoint.SharedLock._authority_fields(fd)
        require(fields is not None and fields[0] == expected_pid and fields[3]
                and fields[2] == checkpoint.SharedLock._owned_lease(directory), "authority lease differs")
        original = os.fstat(fd)
        require((original.st_dev, original.st_ino) == (authority_path.stat().st_dev, authority_path.stat().st_ino),
                "authority pathname changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            raise HandoffError("authority is not held")
        command = Path("/proc", str(expected_pid), "cmdline").read_bytes().split(b"\0")
        require(str(Path(__file__).resolve()).encode() in command and b"--apply" in command,
                "foreign maintenance command")
        require(checkpoint.SharedLock._authority_fields(fd) == fields
                and checkpoint.SharedLock._owned_pid(directory) == expected_pid, "ownership changed")
    finally:
        os.close(fd)
        os.close(directory)


def visible_linux_frontends(proc=Path("/proc"), *, pid=None):
    """Current-UID /proc role scan; return PID/role only, never command lines."""
    ancestors = set()
    cursor = os.getpid() if pid is None else pid
    for _ in range(64):
        if cursor <= 0:
            break
        require(cursor not in ancestors, "Linux ancestry cycle")
        ancestors.add(cursor)
        try:
            fields = (proc / str(cursor) / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            break
        cursor = int(fields[1])
    else:
        raise HandoffError("Linux ancestry visibility bound exceeded")
    roles = {"cre_checkpoint_refresh.py", "cre_checkpoint_series.py", "cre_guarded_tier.py",
             "cre_owned_runtime.py", "code_handoff.py", "cre_code_handoff.py"}
    rows = []
    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) in ancestors:
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            with (process / "cmdline").open("rb") as stream:
                raw = stream.read(65537)
            require(len(raw) <= 65536, "same-user process command exceeds visibility bound")
            arguments = raw.split(b"\0")
            matched = {Path(arg.decode("utf-8", "replace")).name for arg in arguments} & roles
            if matched:
                # Read-only handoff observers are also competing observations;
                # only this exact ancestry is excluded, never unknown ownership.
                rows.append({"pid": int(process.name), "roles": sorted(matched)})
        except FileNotFoundError:
            continue
        except PermissionError as error:
            raise HandoffError("same-user Linux process visibility unavailable") from error
    return rows


class Foreground:
    def __init__(self, release, checkpoint, output, lock=None):
        self.release, self.checkpoint, self.output, self.lock = release, checkpoint, output, lock
        self.counter = 0

    def run(self, name, argv, seconds=180, *, effective_cwd=None):
        """Canonical merged regular log and group settlement, never invented EOF."""
        from cre_refresh_pipeline import command_poll_hook
        self.counter += 1
        log = self.output / f"{self.counter:02d}-{name}.log"
        require(not log.exists(), "command output replay")
        started, monotonic = now(), time.monotonic()

        def poll():
            require(time.monotonic() - monotonic < seconds, "owned command deadline expired")
            if self.lock is not None:
                assert_owned(self.checkpoint, self.lock, os.getpid())

        poll()
        environment = self.checkpoint.safe_process_env()
        if self.release.get("runtime_env_file"):
            environment.update(CRE_ENV_FILE=str(local_path(self.release["runtime_env_file"])),
                               FIRECRAWL_API_URL="http://127.0.0.1:3002")
        with command_poll_hook(poll):
            rc = self.checkpoint.run_command(argv, log, env=environment)
        poll()
        write_new(log.with_suffix(".json"), {
            "schema": "cre-code-command-v1", "producer": "checkpoint-run-command-v1",
            "argv": argv, "dispatch_cwd": str(getattr(self.checkpoint, "COLLECTOR_DIR", Path.cwd())),
            "effective_cwd": str(effective_cwd) if effective_cwd else None, "started_at": started.isoformat(),
            "finished_at": now().isoformat(), "exit_code": rc,
            "local_settlement": "confirmed", "log_closed": True,
            "command_log_sha256": sha(read_file(log)),
        })
        require(rc == 0, f"{name} failed ({rc}); preserve held tasks and recovery")

    def native(self, mode, release_path, release_sha, observation=None, authorization=None):
        script = Path(__file__).resolve().parent / "windows/Invoke-CreCodeHandoff.ps1"
        argv = [str(local_path(self.release["programs"]["powershell"])), "-NoProfile", "-NonInteractive",
                "-File", windows_path(script), "-Mode", mode,
                "-ReleasePath", windows_path(local_path(release_path)), "-ReleaseSha", release_sha,
                "-Output", windows_path(self.output), "-OwnerPid", str(os.getpid())]
        if observation:
            argv += ["-Observation", windows_path(local_path(observation))]
        if authorization:
            argv += ["-Authorization", windows_path(local_path(authorization))]
        self.run("native-" + mode.lower(), argv)


def readiness(release, checkpoint, memory_type, client, idle, git, *, selected=False, owned=False):
    expected = release["selected" if selected else "deployed"]
    for key in ("runtime", "native"):
        git.clean(release["roots"][key], expected)
    lock = checkpoint.canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
    if owned:
        assert_owned(checkpoint, lock, os.getpid())
    else:
        lock_clear(checkpoint, lock)
    frontends = visible_linux_frontends()
    require(not frontends, "visible competing Linux CRE frontend exists")
    cpu = checkpoint.read_host_cpu_percent()
    memory = memory_type()
    level = memory()
    require(cpu < 90 and level == 1, "Linux resource admission refused")
    queues = idle(client("http://127.0.0.1:3002", timeout_seconds=5))
    return {"native": expected, "runtime": expected, "owner_clear": True,
            "resources_ok": True, "linux_resources": {"cpu_percent": cpu, "memory": memory.last_sample},
            "queues_idle": True, "queues": queues, "linux_frontends": frontends,
            "linux_visibility": "current UID /proc roles; own ancestry excluded; PID/role only"}


def preserved(release):
    result = {}
    for path in release["preserve_files"]:
        result[str(local_path(path))] = sha(read_file(path))
    for key in ("native", "runtime"):
        root = local_path(release["roots"][key])
        for path in [root / ".env", root / "scripts/firecrawl-ops/cre_collector/node_modules/.package-lock.json"]:
            result[str(path)] = sha(read_file(path)) if path.is_file() else None
        require(not (root / "apps/api/native/index.js").exists()
                and not list((root / "apps/api/native").glob("*.node")), "optional native addon present")
        require((root / ".claude/skills").is_symlink(), "canonical skill directory link missing")
        for path in (root / ".agents/skills").glob("*/SKILL.md"):
            raw = read_file(path)
            # This one reviewed directory link is intentional; other input reads refuse aliases.
            linked = root / ".claude/skills" / path.parent.name / "SKILL.md"
            require(linked.read_bytes() == raw, "skill link bytes differ")
            result[str(path)] = sha(raw)
    return result


def authorize(args, release_raw, release, evidence):
    observation_raw = read_file(args.observation)
    review_raw = read_file(args.review)
    current = now()
    authorization = {
        "schema": "cre-code-authorization-v1", "release_sha256": sha(release_raw),
        "observation_sha256": sha(observation_raw), "authorized_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=299)).isoformat(),
        "review": {"path": str(args.review), "sha256": sha(review_raw),
                   "reviewer": args.reviewer, "approval_ref": args.approval_ref},
        "operation_output": str(local_path(args.output).absolute()),
    }
    require(not local_path(args.output).exists(), "operation output already exists")
    # Two distinct checks around publication; original bytes remain the oracle.
    validate_authorization(release_raw, release, evidence, observation_raw, authorization, now())
    temporary = Path(str(args.authorization) + ".pending")
    write_new(temporary, authorization)
    validate_authorization(release_raw, release, evidence, observation_raw, authorization, now())
    require(read_file(args.release) == release_raw and read_file(args.observation) == observation_raw,
            "publication inputs changed")
    # link is exclusive (rename could overwrite); failed pending publication is never resumed.
    os.link(temporary, args.authorization, follow_symlinks=False)
    temporary.unlink()


def apply(args, release_raw, release, evidence, git, modules):
    checkpoint, memory, client, idle = modules
    observation_raw = read_file(args.observation)
    authorization_raw = read_file(args.authorization)
    authorization = json_bytes(authorization_raw)
    require(authorization.get("operation_output") == str(local_path(args.output).absolute()),
            "authorization belongs to another operation output")
    def admitted():
        require(read_file(args.release) == release_raw and read_file(args.observation) == observation_raw
                and read_file(args.authorization) == authorization_raw, "apply input changed")
        validate_authorization(release_raw, release, evidence, observation_raw, authorization, now())
    admitted()
    git.development(release)
    readiness(release, checkpoint, memory, client, idle, git)
    output = local_path(args.output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    lock = checkpoint.canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
    baseline = preserved(release)
    with checkpoint.SharedLock(lock, recovery_required=True) as lease:
        runner = Foreground(release, checkpoint, output, lock)
        admitted()
        runner.native("Hold", args.release, sha(release_raw), args.observation, args.authorization)
        admitted()
        bundle = evidence.inputs["bundle"]["path"]
        for key in ("native", "runtime"):
            evidence.recheck()
            git.fast_forward(release["roots"][key], release, bundle)
        require(preserved(release) == baseline, "environment/dependency/skill preservation changed")
        collector = local_path(release["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
        parity = installed_dependencies(collector)
        for name, argv in (
            ("npm-ls", ["npm", "ls", "--all", "--json"]),
            ("doctor", ["bash", "cre_setup.sh", "--check"]),
            ("gfm", ["node", "--import", "tsx", "--input-type=module", "--eval",
             'const {createRequire}=await import("node:module");const r=createRequire(process.cwd()+"/package.json");if(!r.resolve("joplin-turndown-plugin-gfm").startsWith(process.cwd()+"/node_modules/"))throw Error("foreign GFM");if(typeof r("joplin-turndown-plugin-gfm").gfm!=="function")throw Error("GFM export");await import("./sources/jll-html-doc.ts");']),
        ):
            runner.run(name, ["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-handoff", str(collector), *argv],
                       effective_cwd=collector)
        require(installed_dependencies(collector) == parity and preserved(release) == baseline,
                "read-only dependency checks changed preserved files")
        readiness(release, checkpoint, memory, client, idle, git, selected=True, owned=True)
        admitted()
        runner.native("Publish", args.release, sha(release_raw), args.observation, args.authorization)
        readiness(release, checkpoint, memory, client, idle, git, selected=True, owned=True)
        evidence.recheck()
        write_new(output / "code-verified.json", {"status": "passed", "selected": release["selected"],
                  "release_sha256": sha(release_raw), "authorization_sha256": sha(authorization_raw),
                  "dependencies": parity, "database_or_provider_writes": False})
        lease.clear_recovery_requirement()
    lock_clear(checkpoint, lock)
    write_new(output / "terminal.json", {"schema": "cre-code-terminal-v1", "status": "passed",
              "selected": release["selected"], "finished_at": now().isoformat(),
              "release_sha256": sha(release_raw), "authorization_sha256": sha(authorization_raw),
              "canonical_lease_released": True, "independent_postreadback_required": True})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--authorize", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--postreadback", action="store_true")
    mode.add_argument("--assert-owned-pid", type=int)
    mode.add_argument("--check-authorization", action="store_true")
    parser.add_argument("--observation")
    parser.add_argument("--authorization")
    parser.add_argument("--output")
    parser.add_argument("--review")
    parser.add_argument("--reviewer")
    parser.add_argument("--approval-ref")
    parser.add_argument("--operation")
    args = parser.parse_args(argv)
    raw, release, evidence = load_release(args.release)
    if not any((args.observe, args.authorize, args.apply, args.postreadback, args.assert_owned_pid, args.check_authorization)):
        print(json.dumps({"preview_only": True, "execution": False, "selected": release["selected"],
                          "release_sha256": sha(raw)}))
        return 0
    if args.check_authorization:
        require(args.observation and args.authorization, "explicit admission inputs required")
        validate_authorization(raw, release, evidence, read_file(args.observation),
                               json_bytes(read_file(args.authorization)), now())
        return 0
    if args.authorize:
        require(all((args.observation, args.authorization, args.review, args.reviewer, args.approval_ref, args.output)),
                "explicit observation/output/review/reviewer/approval reference required")
        authorize(args, raw, release, evidence)
        return 0
    git = Git(release["programs"]["native_git"])
    modules = runtime_modules(release, git)
    if args.assert_owned_pid:
        lock = modules[0].canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
        assert_owned(modules[0], lock, args.assert_owned_pid)
        return 0
    require(args.output, "exclusive output directory required")
    if args.apply:
        require(args.observation and args.authorization, "explicit admission required")
        apply(args, raw, release, evidence, git, modules)
    else:
        if args.postreadback:
            require(args.operation, "independent postreadback requires the original operation")
            terminal = json_bytes(read_file(local_path(args.operation) / "terminal.json"))
            require(terminal.get("schema") == "cre-code-terminal-v1" and terminal.get("status") == "passed"
                    and terminal.get("canonical_lease_released") is True
                    and terminal.get("selected") == release["selected"]
                    and terminal.get("release_sha256") == sha(raw), "original operation terminal binding differs")
        output = local_path(args.output)
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        runner = Foreground(release, modules[0], output)
        runner.native("Postreadback" if args.postreadback else "Observe", args.release, sha(raw))
        native = json_bytes(read_file(output / "native-observation.json"))
        observation = readiness(release, *modules, git, selected=args.postreadback)
        observation.update(schema="cre-code-observation-v1", observed_at=native["observed_at"], tasks=native["tasks"])
        validate_observation(release, evidence, observation, now(), selected=args.postreadback)
        evidence.recheck()
        write_new(output / "observation.json", observation)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(78)
