#!/usr/bin/env python3
"""One foreground Windows/Ubuntu code-only handoff. Default: offline preview.

No collection, SQL, installs, scheduler creation, automatic recovery or rollback.
Run maintained tooling from a separate reviewed clean checkout, never a capsule.
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cre_code_handoff_evidence import (
    DEPENDENCY_CHECKS,
    Evidence,
    HandoffError,
    canonical_path,
    clock,
    json_bytes,
    require,
    role_paths,
    sha,
    validate_authorization,
    validate_observation,
    validate_release,
)
from cre_code_handoff_git import (
    Git,
    installed_dependencies,
    local_path,
    read_file,
    windows_path,
)
from cre_code_handoff_host import observe_host, qualify_host
from cre_code_restoration_evidence import (
    validate_restoration,
    validate_restoration_authorization,
    validate_restoration_enable_boundary,
    validate_restoration_observation,
)


def now():
    return datetime.now(UTC)


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
    if release.get("schema") == "cre-code-restoration-v1":
        validate_restoration(release, evidence)
    elif release.get("schema") == "cre-runtime-maintenance-v1":
        from cre_runtime_maintenance import validate_maintenance_release

        validate_maintenance_release(release, evidence)
    else:
        validate_release(release, evidence)
    release["roots"] = role_paths(release)
    if release.get("schema") in {"cre-runtime-maintenance-v1", "cre-code-restoration-v1"}:
        from cre_maintenance_validation_context import LoadedMaintenance

        evidence._loaded_finite = LoadedMaintenance(raw, release, evidence)
        if release.get("schema") == "cre-runtime-maintenance-v1":
            evidence._loaded_maintenance = evidence._loaded_finite
    return raw, release, evidence


def runtime_modules(release, git):
    require(sys.platform == "linux" and os.geteuid() != 0, "nonroot Ubuntu required")
    tooling = local_path(release["roots"]["tooling"])
    windows_path(tooling)  # This first route invokes its native helper from a Windows-mapped checkout.
    runtime = local_path(release["roots"]["runtime"])
    native = local_path(release["roots"]["native"])
    require(not native.samefile(runtime), "native/runtime filesystem alias")
    require(not tooling.samefile(runtime) and not tooling.samefile(native),
            "tooling must remain stable outside updated checkouts")
    development = local_path(release["roots"]["development"])
    if development.samefile(tooling):
        require(release["development"] == release["tooling"], "co-located DEV/tooling pins differ")
    require(Path(__file__).resolve().parent == tooling / "scripts/firecrawl-ops/cre_collector",
            "invoked tooling path differs")
    git.clean(tooling, release["tooling"])
    import cre_checkpoint_refresh as checkpoint
    from cre_linux_telemetry import LinuxMemorySampler
    sys.path.insert(0, str(tooling / "scripts/firecrawl-ops"))
    from firecrawl_operator_handoff import ReadOnlyLoopbackClient, idle_snapshot
    return checkpoint, LinuxMemorySampler, ReadOnlyLoopbackClient, idle_snapshot


def released_authority(checkpoint, lease):
    """Bind a normal authority to this still-held, just-cleared operation."""
    lease._require_authority()
    require(lease.held and not lease.recovery_required and not lease.authority_recovery_required
            and lease._authority_fd_matches_path(), "released authority is not owned and normal")
    fields = checkpoint.SharedLock._authority_fields(lease.authority_fd)
    require(fields == (os.getpid(), lease.authority_token, lease.authority_generation, False)
            and lease.lease_token == fields[2], "released authority identity differs")
    identity = lease.authority_identity
    lease._require_authority()
    return fields, identity


def owned_authority(checkpoint, lease):
    """Record the still-owned recovery lease before its irreversible release."""
    assert_owned(checkpoint, lease.path, os.getpid())
    lease._require_authority()
    fields = checkpoint.SharedLock._authority_fields(lease.authority_fd)
    require(fields == (os.getpid(), lease.authority_token, lease.authority_generation, True)
            and lease.lease_token == fields[2] and lease._authority_fd_matches_path(),
            "release-start authority identity differs")
    return {"pid": fields[0], "token": fields[1], "generation": fields[2],
            "recovery_required": True, "authority_identity": list(lease.authority_identity),
            "directory_identity": list(lease.directory_identity)}


def lock_clear(checkpoint, lock, *, released=None):
    _clear_authority(checkpoint, lock, released=released)


def predecessor_clear(checkpoint, lock, released):
    """Observe the exact normal authority of an exited original operation."""
    require(released is not None and len(released) == 2
            and isinstance(released[0], (tuple, list)) and len(released[0]) == 4
            and isinstance(released[1], (tuple, list)) and len(released[1]) == 2
            and type(released[0][0]) is int and released[0][0] > 0
            and released[0][0] != os.getpid() and released[0][3] is False,
            "independent predecessor authority witness malformed")
    _clear_authority(checkpoint, lock, released=(tuple(released[0]), tuple(released[1])), independent=True)


def _clear_authority(checkpoint, lock, *, released=None, independent=False):
    from cre_posix import fcntl
    require(not lock.exists() and not lock.is_symlink(), "canonical owner/recovery directory present")
    authority = lock.with_name(lock.name + ".authority")
    fd = os.open(authority, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HandoffError("canonical authority is held") from error
        try:
            opened = os.fstat(fd)
            identity = opened.st_dev, opened.st_ino
            named = authority.lstat()
            require(identity == (named.st_dev, named.st_ino), "canonical authority pathname changed")
            fields = checkpoint.SharedLock._authority_fields(fd)
            require(fields is not None and not fields[3], "canonical owner/recovery not clear")
            if released is None:
                require(fields[0] == 0 or not checkpoint._pid_alive(fields[0]),
                        "canonical owner/recovery not clear")
            else:
                require((fields, identity) == released,
                        "released authority identity differs")
                if independent:
                    require(not checkpoint._pid_alive(fields[0]), "original predecessor is still live")
                else:
                    require(fields[0] == os.getpid(), "released authority owner differs")
            require(not lock.exists() and not lock.is_symlink(), "canonical owner/recovery directory present")
            if independent:
                require(not checkpoint._pid_alive(fields[0]), "original predecessor birth became ambiguous")
            named = authority.lstat()
            require(identity == (named.st_dev, named.st_ino)
                    and checkpoint.SharedLock._authority_fields(fd) == fields,
                    "canonical authority changed during clearance")
            require(not lock.exists() and not lock.is_symlink(), "canonical owner/recovery directory present")
            if independent:
                require(not checkpoint._pid_alive(fields[0]), "original predecessor birth became ambiguous")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def validate_owner_command(command, script, *, restoration_inputs=None, maintenance_inputs=None):
    require(str(script).encode() in command and b"--apply" in command, "foreign maintenance command")
    selectors = (command.count(b"--release"), command.count(b"--restoration-packet"),
                 command.count(b"--maintenance-packet"))
    require(selectors in {(1, 0, 0), (0, 1, 0), (0, 0, 1)}, "maintenance input selector differs")
    if b"--finite-reviewed" in command:
        require(command.count(b"--finite-reviewed") == 1 and command.count(b"--apply") == 1
                and selectors in {(0, 1, 0), (0, 0, 1)}, "finite owner mode differs")
        options = {b"--apply", b"--finite-reviewed", b"--observation-output", b"--observation",
                   b"--authorization", b"--output", b"--review", b"--reviewer", b"--approval-ref",
                   b"--restoration-packet" if selectors == (0, 1, 0) else b"--maintenance-packet"}
        supplied = [item for item in command[command.index(str(script).encode()) + 1:] if item.startswith(b"--")]
        require(set(supplied) == options and len(supplied) == len(options), "finite owner arguments differ")
        def value(flag):
            index = command.index(flag)
            require(index + 1 < len(command), "finite owner argument missing")
            return command[index + 1].decode()
        require(canonical_path(value(b"--observation")) ==
                canonical_path(value(b"--observation-output").rstrip("/") + "/observation.json"),
                "finite owner observation differs")
    if restoration_inputs is not None or maintenance_inputs is not None:
        require(not (restoration_inputs is not None and maintenance_inputs is not None), "ambiguous owner scope")
        expected = restoration_inputs if restoration_inputs is not None else maintenance_inputs
        require(selectors == ((0, 1, 0) if restoration_inputs is not None else (0, 0, 1)),
                "maintenance owner command scope differs")
        for flag, value in expected.items():
            encoded_flag = flag.encode()
            require(command.count(encoded_flag) == 1, "owner input missing/duplicate")
            index = command.index(encoded_flag)
            require(index + 1 < len(command) and canonical_path(command[index + 1].decode()) == canonical_path(value),
                    "owner belongs to another operation")


def assert_owned(checkpoint, lock, expected_pid, *, restoration_inputs=None, maintenance_inputs=None):
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
        validate_owner_command(command, Path(__file__).resolve(), restoration_inputs=restoration_inputs,
                               maintenance_inputs=maintenance_inputs)
        require(checkpoint.SharedLock._authority_fields(fd) == fields
                and checkpoint.SharedLock._owned_pid(directory) == expected_pid, "ownership changed")
    finally:
        os.close(fd)
        os.close(directory)


def verify_boundary_peer(pid, uid, request):
    """Bind a socket request to the exact supported local boundary client."""
    require(uid == os.geteuid() and pid > 0, "boundary client UID differs")
    require(Path("/proc", str(pid), "exe").resolve(strict=True) == Path("/proc/self/exe").resolve(strict=True),
            "boundary client interpreter differs")
    raw = Path("/proc", str(pid), "cmdline").read_bytes()
    require(len(raw) <= 16384, "boundary client command too large")
    command = [value.decode("utf-8") for value in raw.split(b"\0") if value]
    require(len(command) == 15 and Path(command[0]).name in {"python3", "python"}
            and command[1] == "-B" and canonical_path(command[2]) == canonical_path(str(Path(__file__).resolve()))
            and command[3] == request["selector"]
            and command[5] == "--assert-owner-boundary-pid" and command[6] == str(os.getpid())
            and command[7] == "--boundary-mode" and command[8] == request["mode"]
            and command[9] == "--observation" and command[11] == "--authorization"
            and command[13] == "--output", "boundary client command differs")
    for index, name in ((4, "release"), (10, "observation"), (12, "authorization"), (14, "output")):
        require(canonical_path(command[index]) == canonical_path(request[name]), "boundary client path differs")
    return True


def boundary_binding(args, release_raw, observation_raw, authorization_raw):
    return {"selector": "--maintenance-packet" if args.maintenance_packet else "--restoration-packet",
            "release": canonical_path(args.release), "output": canonical_path(args.output),
            "observation": canonical_path(args.observation), "authorization": canonical_path(args.authorization),
            "release_sha256": sha(release_raw), "observation_sha256": sha(observation_raw),
            "authorization_sha256": sha(authorization_raw)}


def check_boundary_times(observed_raw, authorization_raw):
    observation, authorization = json_bytes(observed_raw), json_bytes(authorization_raw)
    current = now()
    observed, qualified = clock(observation["observed_at"]), clock(observation["qualified_at"])
    start, end = clock(authorization["authorized_at"]), clock(authorization["expires_at"])
    require(observed <= qualified <= current and 0 <= (current - observed).total_seconds() < 300
            and start <= current < end and 0 < (end - start).total_seconds() <= 300,
            "owner boundary authorization or observation expired")


def boundary_executables(packet):
    """Recheck the executables already selected for this owned operation."""
    linux_git = shutil.which("git")
    require(linux_git is not None, "Linux Git executable missing")
    paths = [local_path(packet["programs"][key]) for key in ("native_git", "powershell")]
    paths.extend((Path(linux_git), Path(sys.executable)))
    return tuple((str(path.resolve(strict=True)), sha(read_file(path.resolve(strict=True)))) for path in paths)


def boundary_client(args):
    """Ask the live owner; never cold-load or trust a cached approval."""
    from cre_owned_handoff_boundary import endpoint_for, request_boundary

    require(bool(args.maintenance_packet) != bool(args.restoration_packet)
            and args.assert_owner_boundary_pid > 0 and args.boundary_mode in {"Hold", "Repin", "Verify", "Restore"}
            and args.release and args.output and args.observation and args.authorization,
            "exact owner boundary inputs required")
    raw, observed_raw, auth_raw = (read_file(path) for path in (args.release, args.observation, args.authorization))
    packet = json_bytes(raw)
    expected_schema = "cre-runtime-maintenance-v1" if args.maintenance_packet else "cre-code-restoration-v1"
    require(packet.get("schema") == expected_schema, "owner boundary selector differs")
    binding = boundary_binding(args, raw, observed_raw, auth_raw)
    modules = runtime_modules(packet, Git(packet["programs"]["native_git"]))
    lock = modules[0].canonical_shared_lock_dir(local_path(packet["roots"]["runtime"]))
    inputs = {binding["selector"]: args.release, "--output": args.output,
              "--observation": args.observation, "--authorization": args.authorization}

    def owner():
        assert_owned(modules[0], lock, args.assert_owner_boundary_pid,
                     maintenance_inputs=inputs if args.maintenance_packet else None,
                     restoration_inputs=inputs if args.restoration_packet else None)

    def fresh():
        require(all(read_file(path) == value for path, value in
                    ((args.release, raw), (args.observation, observed_raw), (args.authorization, auth_raw))),
                "owner boundary input changed")
        check_boundary_times(observed_raw, auth_raw)

    request_boundary(endpoint_for(args.assert_owner_boundary_pid), args.assert_owner_boundary_pid,
                     binding, args.boundary_mode, owner, fresh,
                     lambda: check_boundary_times(observed_raw, auth_raw))


def owned_boundary(args, raw, packet, evidence, git, checkpoint, lock, admitted, *, extra_check=None,
                   admitted_rechecks_evidence=False):
    """Keep the fully validated evidence in its lease owner for native checks."""
    from cre_owned_handoff_boundary import OwnerBoundary

    observed_raw, auth_raw = read_file(args.observation), read_file(args.authorization)
    binding = boundary_binding(args, raw, observed_raw, auth_raw)
    packet_bytes = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    executables = boundary_executables(packet)
    original = dict(evidence.original)
    payloads = dict(evidence.original_payloads)
    seen = {}
    for path, value in [(evidence.inputs[role]["path"], data) for role, data in original.items()] + list(payloads.items()):
        key = canonical_path(path)
        if len(key) >= 3 and key[1:3] == ":/":
            key = key.casefold()
        require(key not in seen or seen[key] == value, "conflicting evidence path aliases")
        seen[key] = value
    inputs = {binding["selector"]: args.release, "--output": args.output,
              "--observation": args.observation, "--authorization": args.authorization}

    def check(mode):
        require(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode() == packet_bytes
                and evidence.original == original and evidence.original_payloads == payloads,
                "validated owner evidence changed")
        if not admitted_rechecks_evidence:
            evidence.recheck()
        require(boundary_executables(packet) == executables, "owned executable changed")
        runtime_modules(packet, git)
        if extra_check is not None:
            extra_check(mode)
        admitted()
        require(evidence.original == original and evidence.original_payloads == payloads,
                "owner evidence closure changed")
        require(read_file(args.release) == raw and read_file(args.observation) == observed_raw
                and read_file(args.authorization) == auth_raw, "owner boundary input changed")
        assert_owned(checkpoint, lock, os.getpid(),
                     maintenance_inputs=inputs if args.maintenance_packet else None,
                     restoration_inputs=inputs if args.restoration_packet else None)
        check_boundary_times(observed_raw, auth_raw)
    return OwnerBoundary(binding, check, verify_boundary_peer)


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
    def __init__(self, release, checkpoint, output, lock=None, boundary=None):
        self.release, self.checkpoint, self.output, self.lock = release, checkpoint, output, lock
        self.boundary = boundary
        self.counter = 0

    def run(self, name, argv, seconds=180, *, effective_cwd=None):
        """Canonical merged regular log and group settlement, never invented EOF."""
        from cre_refresh_pipeline import command_poll_hook
        self.counter += 1
        identity = None
        command_git = None
        if effective_cwd is not None:
            root = local_path(self.release["roots"]["runtime"])
            require(local_path(effective_cwd) == root / "scripts/firecrawl-ops/cre_collector", "unexpected effective command cwd")
            command_git = Git(self.release["programs"]["native_git"])
            identity = (self.release["new"] if self.release.get("schema") == "cre-runtime-maintenance-v1"
                        else self.release["selected"])
            command_git.clean(root, identity, tracking=identity["sha"])
        log = self.output / f"{self.counter:02d}-{name}.log"
        require(not log.exists(), "command output replay")
        started, monotonic = now(), time.monotonic()
        if self.boundary is not None:
            self.boundary.command_deadline = monotonic + seconds

        def poll():
            require(time.monotonic() - monotonic < seconds, "owned command deadline expired")
            if self.lock is not None:
                assert_owned(self.checkpoint, self.lock, os.getpid())
            if self.boundary is not None:
                self.boundary.poll()

        poll()
        environment = self.checkpoint.safe_process_env()
        if self.release.get("runtime_env_file"):
            environment.update(CRE_ENV_FILE=str(local_path(self.release["runtime_env_file"])),
                               FIRECRAWL_API_URL="http://127.0.0.1:3002")
        try:
            with command_poll_hook(poll):
                # Maintenance logs are not collector performance evidence.
                rc = self.checkpoint._run_logged_command(argv, log, env=environment)
        finally:
            if self.boundary is not None:
                from cre_owned_handoff_boundary import deactivate_preserving_error

                try:
                    deactivate_preserving_error(self.boundary)
                finally:
                    self.boundary.command_deadline = None
        poll()
        if command_git is not None:
            command_git.clean(root, identity, tracking=identity["sha"])
        receipt = {
            "schema": "cre-owned-gate-v1" if identity else "cre-code-command-v1", "producer": "checkpoint-run-command-v1",
            "argv": argv, "dispatch_cwd": str(getattr(self.checkpoint, "COLLECTOR_DIR", Path.cwd())),
            "effective_cwd": str(effective_cwd) if effective_cwd else None, "started_at": started.isoformat(),
            "finished_at": now().isoformat(), "exit_code": rc,
            "local_settlement": "confirmed", "log_closed": True,
            "command_log_sha256": sha(read_file(log)),
        }
        if identity:
            receipt.update(identity=identity, unchanged=True, cwd=str(effective_cwd))
        write_new(log.with_suffix(".json"), receipt)
        require(rc == 0, f"{name} failed ({rc}); preserve held tasks and recovery")
        return receipt

    def native(self, mode, release_path, release_sha, observation=None, authorization=None, *, operation=None):
        script = Path(__file__).resolve().parent / "windows/Invoke-CreCodeHandoff.ps1"
        maintenance = self.release.get("schema") == "cre-runtime-maintenance-v1"
        restoration = self.release.get("schema") == "cre-code-restoration-v1"
        argv = [str(local_path(self.release["programs"]["powershell"])), "-NoProfile", "-NonInteractive",
                "-File", windows_path(script), "-Mode", mode,
                "-MaintenancePath" if maintenance else "-RestorationPath" if restoration else "-ReleasePath",
                windows_path(local_path(release_path)),
                "-MaintenanceSha" if maintenance else "-RestorationSha" if restoration else "-ReleaseSha", release_sha,
                "-Output", windows_path(self.output), "-OwnerPid", str(os.getpid())]
        if observation:
            argv += ["-Observation", windows_path(local_path(observation))]
        if authorization:
            argv += ["-Authorization", windows_path(local_path(authorization))]
        if operation:
            argv += ["-OperationOutput", windows_path(local_path(operation))]
        if self.boundary is not None:
            self.boundary.activate(mode)
        try:
            self.run("native-" + mode.lower(), argv)
        finally:
            if self.boundary is not None:
                from cre_owned_handoff_boundary import deactivate_preserving_error

                deactivate_preserving_error(self.boundary)


def readiness(release, checkpoint, memory_type, client, idle, git, *, selected=False, owned=False, evidence=None):
    expected = release["code"] if release.get("schema") == "cre-code-restoration-v1" else release["selected" if selected else "deployed"]
    for key in ("runtime", "native"):
        git.clean(release["roots"][key], expected)
    lock = checkpoint.canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
    if owned:
        assert_owned(checkpoint, lock, os.getpid())
    else:
        lock_clear(checkpoint, lock)
    frontends = visible_linux_frontends()
    require(not frontends, "visible competing Linux CRE frontend exists")
    host = observe_host(checkpoint, memory_type, client, idle)
    host.update(native=expected, runtime=expected,
                ownership={"phase": "owned-maintenance" if owned else "released-clear",
                           "verified": True, "recovery_required": owned}, resources_ok=True,
                queues_idle=True, linux_frontends=frontends,
                linux_visibility="current UID /proc roles; own ancestry excluded; PID/role only")
    qualify_host(host, evidence.obj("runtime_snapshot"))
    return host


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


def authorize(args, release_raw, release, evidence, *, finite=None):
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
    restoration = release.get("schema") == "cre-code-restoration-v1"
    validate = validate_restoration_authorization if restoration else validate_authorization
    if restoration:
        authorization.update(schema="cre-code-restoration-authorization-v1", scope="restore-held-task-states",
                             restoration_sha256=authorization.pop("release_sha256"))
        if finite is None:
            restoration_enable_boundary(release, evidence)
        else:
            from cre_code_restoration_evidence import validate_enable_context
            from cre_finite_handoff import FiniteProof

            require(type(finite) is FiniteProof, "finite qualification differs")
            finite.recheck(release_raw, release, evidence)
            validate_enable_context(release, evidence, finite.enable, now)
    require(not local_path(args.output).exists(), "operation output already exists")
    # Two distinct checks around publication; original bytes remain the oracle.
    validate(release_raw, release, evidence, observation_raw, authorization, now(), review_reader=read_file)
    temporary = Path(str(args.authorization) + ".pending")
    write_new(temporary, authorization)
    validate(release_raw, release, evidence, observation_raw, authorization, now(), review_reader=read_file)
    require(read_file(args.release) == release_raw and read_file(args.observation) == observation_raw,
            "publication inputs changed")
    if restoration:
        check_boundary_times(observation_raw, json.dumps(authorization).encode())
    # link is exclusive (rename could overwrite); failed pending publication is never resumed.
    os.link(temporary, args.authorization, follow_symlinks=False)
    temporary.unlink()


def mutation_started(output, release, release_raw, authorization_raw):
    path = output / "mutation-start.json"
    if not path.exists():
        return False
    marker = json_bytes(read_file(path))
    restoration = release.get("schema") == "cre-code-restoration-v1"
    require(marker.get("schema") == ("cre-code-restoration-mutation-v1" if restoration else "cre-code-mutation-v1")
            and marker.get("restoration_sha256" if restoration else "release_sha256") == sha(release_raw)
            and marker.get("authorization_sha256") == sha(authorization_raw)
            and canonical_path(marker.get("operation_output")) == canonical_path(str(output))
            and marker.get("tooling") == release["tooling"]
            and marker.get("fleet") == release["task_baseline"], "mutation-start binding differs")
    return True


def record_failure(args, release, release_raw, authorization_raw, checkpoint, output, classification, original, *, allow_hold=True):
    record = {"schema": "cre-code-restoration-failure-v1" if release.get("schema") == "cre-code-restoration-v1" else "cre-code-failure-v1", "classification": classification,
              "original_error_type": type(original).__name__, "original_error": str(original),
              "recovery_retained": "not asserted; inspect canonical state",
              "failure_hold": "not attempted before mutation", "cleanup_error": None}
    try:
        require(read_file(args.release) == release_raw and read_file(args.authorization) == authorization_raw,
                "cleanup immutable inputs changed")
        if allow_hold and mutation_started(output, release, release_raw, authorization_raw):
            cleanup = Foreground(release, checkpoint, output)
            cleanup.counter = 999
            cleanup.native("FailureHold", args.release, sha(release_raw), args.observation, args.authorization)
            record["failure_hold"] = "attempted; inspect per-task failure-hold.json"
    # Cleanup interruptions must not replace the original operation exception.
    except BaseException as cleanup_error:  # noqa: BLE001
        record["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
    try:
        write_new(output / "failure.json", record)
    # Publication interruptions likewise preserve the original BaseException.
    except BaseException as receipt_error:  # noqa: BLE001
        print(f"Original {type(original).__name__}: {original}; failure receipt unavailable: {type(receipt_error).__name__}; cleanup: {record['cleanup_error']}", file=sys.stderr)


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
        validate_authorization(release_raw, release, evidence, observation_raw, authorization, now(), review_reader=read_file)
    admitted()
    git.development(release)
    readiness(release, checkpoint, memory, client, idle, git, evidence=evidence)
    output = local_path(args.output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    lock = checkpoint.canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
    baseline = preserved(release)
    classification = "precommit-failure"
    try:
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
            for name, argv in DEPENDENCY_CHECKS.items():
                runner.run(name, ["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-handoff", str(collector), *argv],
                           effective_cwd=collector)
            require(installed_dependencies(collector) == parity and preserved(release) == baseline,
                    "read-only dependency checks changed preserved files")
            runner.native("Repin", args.release, sha(release_raw), args.observation, args.authorization)
            held = readiness(release, checkpoint, memory, client, idle, git, selected=True, owned=True, evidence=evidence)
            write_new(output / "held-ready.json", {"schema": "cre-code-held-ready-v1", "readiness": held,
                      "dependencies": parity, "release_sha256": sha(release_raw), "tasks_held": True})
            admitted()
            runner.native("Restore", args.release, sha(release_raw), args.observation, args.authorization)
            runner.native("Verify", args.release, sha(release_raw), args.observation, args.authorization)
            native = json_bytes(read_file(output / "native-observation.json"))
            final = readiness(release, checkpoint, memory, client, idle, git, selected=True, owned=True, evidence=evidence)
            final.update(schema="cre-code-observation-v1", observed_at=native["observed_at"],
                         tasks=native["tasks"], windows_resources=native["windows_resources"])
            final["qualified_at"] = now().isoformat()
            validate_observation(release, evidence, final, now(), selected=True, ownership_phase="owned-maintenance")
            evidence.recheck()
            write_new(output / "commit-ready.json", {"schema": "cre-code-commit-ready-v1", "selected": release["selected"],
                      "release_sha256": sha(release_raw), "authorization_sha256": sha(authorization_raw),
                      "operation_output": canonical_path(str(output)),
                      "readiness": final, "dependencies": parity, "recovery_required_at_publication": True})
            admitted()  # Real current trigger margin, review and hashes immediately before commit.
            classification = "commit-uncertain"
            lease.clear_recovery_requirement()
            released = released_authority(checkpoint, lease)
        lock_clear(checkpoint, lock, released=released)
        classification = "postcommit-confirmation-failure"
        write_new(output / "terminal.json", {"schema": "cre-code-terminal-v1", "status": "committed",
                  "selected": release["selected"], "finished_at": now().isoformat(),
                  "release_sha256": sha(release_raw), "authorization_sha256": sha(authorization_raw),
                  "operation_output": canonical_path(str(output)),
                  "commit_ready_sha256": sha(read_file(output / "commit-ready.json")),
                  "canonical_lease_released": True, "native_completion_and_independent_readback_required": True})
    except BaseException as original:
        record_failure(args, release, release_raw, authorization_raw, checkpoint, output, classification, original)
        raise


def restoration_enable_boundary(packet, evidence):
    validate_restoration_enable_boundary(packet, evidence, now())


def restoration_released_owner(packet, checkpoint, lock, released, native, evidence, *, independent=False):
    """Observe release once; an authenticated successor is never maintenance."""
    try:
        if independent:
            predecessor_clear(checkpoint, lock, released)
        else:
            lock_clear(checkpoint, lock, released=released)
    except HandoffError:
        from cre_code_restoration_evidence import (
            restoration_execution,
            retained_reference,
        )
        from cre_code_restoration_successor import observe_scheduled_successor

        restoration_execution(packet, evidence, native)
        record = json_bytes(retained_reference(native["execution"], evidence))
        return observe_scheduled_successor(packet, checkpoint, lock, released, record, evidence, read_file=read_file)
    return {"phase": "released-clear", "verified": True, "recovery_required": False}


def restoration_release_confirmation(args, packet_raw, packet, evidence, checkpoint, lock, released, output):
    """Confirm exact release or observe an actual scheduled successor, read-only."""
    try:
        lock_clear(checkpoint, lock, released=released)
        return {"phase": "released-clear", "verified": True, "recovery_required": False}
    except HandoffError:
        capture = output / "release-observation"
        capture.mkdir(mode=0o700)
        Foreground(packet, checkpoint, capture).native("Postreadback", args.release, sha(packet_raw), operation=output)
        native = json_bytes(read_file(capture / "native-observation.json"))
        # The native reader compares complete definitions; bind the exported bytes
        # as well before accepting any process/authority successor relationship.
        for row in native["tasks"]:
            baseline = next(item for item in packet["task_baseline"] if item["key"] == row["key"])
            raw = read_file(capture / (row["key"] + "-observed.xml"))
            from cre_code_handoff_evidence import enabled_xml_contract, xml_enabled

            require(sha(raw) == row["xml_sha256"] and xml_enabled(raw) is baseline["desired_enabled"]
                    and enabled_xml_contract(raw, baseline["desired_enabled"])
                    == enabled_xml_contract(evidence.raw(baseline["xml"]), baseline["desired_enabled"]),
                    "released successor task definition differs")
        return restoration_released_owner(packet, checkpoint, lock, released, native, evidence)


def restoration_capture(packet, evidence, args, git, modules, output, *, packet_raw, final=False, owned=False):
    native = json_bytes(read_file(output / "native-observation.json"))
    released = final and not owned and packet.get("enable_boundary") == "founder-approved-catchup-v1"
    if released:
        from cre_code_handoff_transition import reference

        operation = local_path(args.operation)
        terminal = json_bytes(read_file(operation / "restoration-terminal.json"))
        witness = terminal["released_authority"]
        for key in ("native", "runtime"):
            git.clean(packet["roots"][key], packet["code"])
        lock = modules[0].canonical_shared_lock_dir(local_path(packet["roots"]["runtime"]))
        ownership = restoration_released_owner(packet, modules[0], lock, (tuple(witness[0]), tuple(witness[1])), native, evidence,
                                              independent=True)
        observation = {"native": packet["code"], "runtime": packet["code"], "ownership": ownership,
                       "admission_reference": reference(operation / "restoration-ready.json", read_file)}
    else:
        observation = readiness(packet, *modules, git, owned=owned, evidence=evidence)
    observation.update(schema="cre-code-restoration-observation-v1", observed_at=native["observed_at"],
                       tasks=native["tasks"], windows_resources=native["windows_resources"],
                       restoration_sha256=sha(packet_raw), qualified_at=now().isoformat())
    if native.get("execution"):
        observation["execution"] = native["execution"]
    if final:
        from cre_code_handoff_transition import reference

        for task in observation["tasks"]:
            task["xml_reference"] = reference(output / (task["key"] + "-observed.xml"), read_file)
    validate_restoration_observation(packet, evidence, observation, now(), final=final, owned=owned)
    return observation


def apply_restoration(args, packet_raw, packet, evidence, git, modules, *, finite=None):
    checkpoint = modules[0]
    observed_raw, authorization_raw = read_file(args.observation), read_file(args.authorization)
    authorization = json_bytes(authorization_raw)
    output = local_path(args.output)
    require(canonical_path(authorization.get("operation_output")) == canonical_path(str(output)),
            "restoration authorization belongs to another output")

    def admitted():
        require(read_file(args.release) == packet_raw and read_file(args.observation) == observed_raw
                and read_file(args.authorization) == authorization_raw, "restoration immutable input changed")
        validate_restoration_authorization(packet_raw, packet, evidence, observed_raw, authorization,
                                           now(), review_reader=read_file)
        if finite is None:
            restoration_enable_boundary(packet, evidence)
        else:
            from cre_code_restoration_evidence import validate_enable_context

            validate_enable_context(packet, evidence, finite.enable, now)
        check_boundary_times(observed_raw, authorization_raw)

    if finite is not None:
        from cre_finite_handoff import FiniteProof

        require(type(finite) is FiniteProof, "finite qualification differs")
        finite.recheck(packet_raw, packet, evidence)
    admitted()  # Unsupported catch-up admission must refuse before any mutation/lease.
    from cre_code_restoration_evidence import (
        prepare_enable_context,
        validate_enable_context,
    )
    from cre_maintenance_validation_context import RecordingGit

    history_git = finite.replay if finite is not None else RecordingGit(packet["programs"]["native_git"])
    enable_context = finite.enable if finite is not None else prepare_enable_context(packet, evidence, now, git=history_git)

    def boundary_admitted():
        require(read_file(args.release) == packet_raw and read_file(args.observation) == observed_raw
                and read_file(args.authorization) == authorization_raw, "restoration immutable input changed")
        validate_restoration_authorization(packet_raw, packet, evidence, observed_raw, authorization,
                                           now(), review_reader=read_file)
        validate_enable_context(packet, evidence, enable_context, now)
        check_boundary_times(observed_raw, authorization_raw)
    git.clean(packet["roots"]["development"], packet["development"], tracking=packet["development"]["sha"])
    git.command(packet["roots"]["development"], "merge-base", "--is-ancestor", packet["code"]["sha"], packet["development"]["sha"])
    readiness(packet, *modules, git, evidence=evidence)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    lock = checkpoint.canonical_shared_lock_dir(local_path(packet["roots"]["runtime"]))
    baseline = preserved(packet)
    collector = local_path(packet["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    dependencies = installed_dependencies(collector)
    classification = "precommit-failure"
    failure_recorded = False
    try:
        with checkpoint.SharedLock(lock, recovery_required=True) as lease:
            try:
                admitted()
                readiness(packet, *modules, git, owned=True, evidence=evidence)
                def boundary_check(_mode):
                    history_git.recheck()
                    for key in ("native", "runtime"):
                        git.clean(packet["roots"][key], packet["code"])
                    git.clean(packet["roots"]["development"], packet["development"],
                              tracking=packet["development"]["sha"])
                    git.command(packet["roots"]["development"], "merge-base", "--is-ancestor",
                                packet["code"]["sha"], packet["development"]["sha"])

                with owned_boundary(args, packet_raw, packet, evidence, git, checkpoint, lock,
                                    boundary_admitted, extra_check=boundary_check,
                                    admitted_rechecks_evidence=True) as boundary:
                    runner = Foreground(packet, checkpoint, output, lock, boundary)
                    runner.native("Restore", args.release, sha(packet_raw), args.observation, args.authorization)
                    runner.native("Verify", args.release, sha(packet_raw), args.observation, args.authorization)
                final = restoration_capture(packet, evidence, args, git, modules, output, packet_raw=packet_raw, final=True, owned=True)
                require(preserved(packet) == baseline and installed_dependencies(collector) == dependencies,
                        "restoration changed preserved environment/dependencies")
                evidence.recheck()
                write_new(output / "restoration-ready.json", {"schema": "cre-code-restoration-ready-v1",
                          "restoration_sha256": sha(packet_raw), "authorization_sha256": sha(authorization_raw),
                          "operation_output": canonical_path(str(output)), "code": packet["code"],
                          "readiness": final, "dependencies": dependencies, "recovery_required_at_publication": True})
                admitted()
                authority = owned_authority(checkpoint, lease)
                classification = "release-started"
                write_new(output / "release-start.json", {"schema": "cre-code-restoration-release-start-v1",
                          "restoration_sha256": sha(packet_raw), "authorization_sha256": sha(authorization_raw),
                          "operation_output": canonical_path(str(output)),
                          "mutation_sha256": sha(read_file(output / "mutation-start.json")),
                          "owned_authority": authority,
                          "ready_sha256": sha(read_file(output / "restoration-ready.json")),
                          "started_at": now().isoformat()})
                lease.clear_recovery_requirement()
                released = released_authority(checkpoint, lease)
            except BaseException as original:
                # Cleanup belongs inside this lease. Once release starts, even
                # an uncertain clear/exit is observation-only forever.
                record_failure(args, packet, packet_raw, authorization_raw, checkpoint, output,
                               classification, original, allow_hold=classification == "precommit-failure")
                failure_recorded = True
                raise
        catchup = packet.get("enable_boundary") == "founder-approved-catchup-v1"
        ownership = (restoration_release_confirmation(args, packet_raw, packet, evidence, checkpoint, lock, released, output)
                     if catchup else None)
        if not catchup:
            lock_clear(checkpoint, lock, released=released)
        classification = "postcommit-confirmation-failure"
        terminal = {"schema": "cre-code-restoration-terminal-v1",
                  "status": "restored", "code": packet["code"], "finished_at": now().isoformat(),
                  "restoration_sha256": sha(packet_raw), "authorization_sha256": sha(authorization_raw),
                  "operation_output": canonical_path(str(output)), "canonical_lease_released": True,
                  "ready_sha256": sha(read_file(output / "restoration-ready.json")),
                  "failed_predecessor_release_sha256": sha(evidence.raw(packet["failed_handoff"]["release"]))}
        if catchup:
            write_new(output / "release-observation.json", {"schema": "cre-code-restoration-release-observation-v1",
                      "restoration_sha256": sha(packet_raw), "authorization_sha256": sha(authorization_raw),
                      "operation_output": canonical_path(str(output)), "ownership": ownership, "observed_at": now().isoformat(),
                      "release_start_sha256": sha(read_file(output / "release-start.json")),
                      "released_authority": released})
            terminal.update(released_authority=released,
                            release_start_sha256=sha(read_file(output / "release-start.json")),
                            release_observation_sha256=sha(read_file(output / "release-observation.json")))
            terminal["finished_at"] = now().isoformat()
        write_new(output / "restoration-terminal.json", terminal)
    except BaseException as original:
        if not failure_recorded:
            record_failure(args, packet, packet_raw, authorization_raw, checkpoint, output,
                           classification, original, allow_hold=False)
        raise


def observe_restoration(args, raw, packet, evidence, git, modules):
    operation = local_path(args.operation) if args.operation else None
    terminal_raw = None
    if args.postreadback:
        require(operation is not None, "restoration postreadback requires distinct original operation")
        terminal_raw = read_file(operation / "restoration-terminal.json")
        terminal = json_bytes(terminal_raw)
        require(terminal.get("schema") == "cre-code-restoration-terminal-v1" and terminal.get("status") == "restored"
                and terminal.get("restoration_sha256") == sha(raw) and terminal.get("code") == packet["code"]
                and canonical_path(terminal.get("operation_output")) == canonical_path(str(operation))
                and terminal.get("canonical_lease_released") is True, "restoration terminal binding differs")
        completion = json_bytes(read_file(str(operation) + ".native.json"))
        capture = completion.get("capture", {})
        require(completion.get("schema") == "cre-code-restoration-native-completion-v1"
                and completion.get("restoration_sha256") == sha(raw)
                and completion.get("authorization_sha256") == terminal.get("authorization_sha256")
                and canonical_path(completion.get("operation_output")) == canonical_path(str(operation))
                and capture.get("Started") is True and capture.get("Failed") is False
                and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 0
                and all(capture.get(stream, {}).get("Eof") is True
                        and capture[stream].get("Truncated") is False for stream in ("Stdout", "Stderr"))
                and completion.get("stdout_sha256") == sha(read_file(str(operation) + ".stdout.raw"))
                and completion.get("stderr_sha256") == sha(read_file(str(operation) + ".stderr.raw")),
                "restoration original native completion/drain unverified")
    output = local_path(args.output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    Foreground(packet, modules[0], output).native("Postreadback" if args.postreadback else "Observe", args.release, sha(raw), operation=operation)
    observation = restoration_capture(packet, evidence, args, git, modules, output, packet_raw=raw, final=args.postreadback)
    if args.postreadback:
        observation.update(operation_output=canonical_path(str(operation)),
                           operation_terminal_sha256=sha(terminal_raw),
                           authorization_sha256=terminal["authorization_sha256"],
                           native_completion_sha256=sha(read_file(str(operation) + ".native.json")))
    evidence.recheck()
    require(read_file(args.release) == raw, "restoration input changed before observation publication")
    write_new(output / "observation.json", observation)
    if args.postreadback:
        from cre_code_handoff_transition import (
            make_reconciliation,
            validate_reconciliation,
        )

        receipt = make_reconciliation(packet, args.release, operation, output, read_file)
        validate_reconciliation(receipt, receipt["old"], packet["code"], read_file)
        write_new(output / "reconciliation.json", receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    input_kind = parser.add_mutually_exclusive_group(required=True)
    input_kind.add_argument("--release")
    input_kind.add_argument("--restoration-packet")
    input_kind.add_argument("--maintenance-packet")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--authorize", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--postreadback", action="store_true")
    mode.add_argument("--assert-owned-pid", type=int)
    mode.add_argument("--assert-maintenance-owner-pid", type=int)
    mode.add_argument("--assert-owner-boundary-pid", type=int)
    mode.add_argument("--check-authorization", action="store_true")
    parser.add_argument("--boundary-mode")
    parser.add_argument("--observation")
    parser.add_argument("--authorization")
    parser.add_argument("--output")
    parser.add_argument("--review")
    parser.add_argument("--reviewer")
    parser.add_argument("--approval-ref")
    parser.add_argument("--operation")
    parser.add_argument("--finite-reviewed", action="store_true")
    parser.add_argument("--observation-output")
    args = parser.parse_args(argv)
    if args.restoration_packet:
        args.release = args.restoration_packet
    if args.maintenance_packet:
        args.release = args.maintenance_packet
    if args.assert_owner_boundary_pid is not None:
        boundary_client(args)
        return 0
    require(args.boundary_mode is None, "boundary mode requires owner boundary")
    require(not args.finite_reviewed or args.apply, "finite handoff requires apply")
    require(args.finite_reviewed or args.observation_output is None, "observation output requires finite handoff")
    raw, release, evidence = load_release(args.release)
    restoration = release.get("schema") == "cre-code-restoration-v1"
    maintenance = release.get("schema") == "cre-runtime-maintenance-v1"
    require(restoration is bool(args.restoration_packet), "explicit input selector/schema differs")
    require(maintenance is bool(args.maintenance_packet), "explicit maintenance selector/schema differs")
    if not any((args.observe, args.authorize, args.apply, args.postreadback, args.assert_owned_pid,
                args.assert_maintenance_owner_pid is not None, args.check_authorization)):
        print(json.dumps({"preview_only": True, "execution": False,
                          "code" if restoration else "selected": release["code"] if restoration else release["new"] if maintenance else release["selected"],
                          "restoration_sha256" if restoration else "release_sha256": sha(raw)}))
        return 0
    if args.check_authorization or args.assert_maintenance_owner_pid is not None:
        if args.assert_maintenance_owner_pid is not None:
            require(maintenance and args.assert_maintenance_owner_pid > 0,
                    "combined owner boundary requires maintenance and a positive PID")
        require(args.observation and args.authorization, "explicit admission inputs required")
        if maintenance:
            from cre_runtime_maintenance import validate_maintenance_authorization

            require(args.output, "maintenance operation output required")
            observed_raw = read_file(args.observation)
            authorization_raw = read_file(args.authorization)
            validate_maintenance_authorization(raw, release, evidence, observed_raw,
                                               json_bytes(authorization_raw), now(),
                                               output=args.output, review_reader=read_file)
            require(read_file(args.release) == raw and read_file(args.observation) == observed_raw
                    and read_file(args.authorization) == authorization_raw,
                    "maintenance authorization boundary inputs changed")
            if args.assert_maintenance_owner_pid is not None:
                git = Git(release["programs"]["native_git"])
                modules = runtime_modules(release, git)
                evidence.recheck()
                require(read_file(args.release) == raw and read_file(args.observation) == observed_raw
                        and read_file(args.authorization) == authorization_raw,
                        "maintenance authorization boundary inputs changed")
                require(now() < clock(json_bytes(authorization_raw)["expires_at"]),
                        "maintenance authorization expired before owner check")
                lock = modules[0].canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
                assert_owned(modules[0], lock, args.assert_maintenance_owner_pid, maintenance_inputs={
                    "--maintenance-packet": args.release, "--output": args.output,
                    "--authorization": args.authorization, "--observation": args.observation,
                })
            return 0
        validator = validate_restoration_authorization if restoration else validate_authorization
        validator(raw, release, evidence, read_file(args.observation),
                  json_bytes(read_file(args.authorization)), now(), review_reader=read_file)
        if restoration:
            require(args.output and canonical_path(json_bytes(read_file(args.authorization)).get("operation_output"))
                    == canonical_path(str(local_path(args.output))), "restoration native output differs from authorization")
            restoration_enable_boundary(release, evidence)
        return 0
    if args.authorize:
        require(all((args.observation, args.authorization, args.review, args.reviewer, args.approval_ref, args.output)),
                "explicit observation/output/review/reviewer/approval reference required")
        if maintenance:
            from cre_runtime_maintenance import authorize_maintenance

            authorize_maintenance(args, raw, release, evidence)
            return 0
        authorize(args, raw, release, evidence)
        return 0
    git = Git(release["programs"]["native_git"])
    modules = runtime_modules(release, git)
    if args.assert_owned_pid:
        lock = modules[0].canonical_shared_lock_dir(local_path(release["roots"]["runtime"]))
        if maintenance:
            require(args.output and args.authorization and args.observation, "maintenance owner operation inputs required")
            assert_owned(modules[0], lock, args.assert_owned_pid, maintenance_inputs={
                "--maintenance-packet": args.release, "--output": args.output,
                "--authorization": args.authorization, "--observation": args.observation,
            })
        elif restoration:
            require(args.output and args.authorization and args.observation, "restoration owner operation inputs required")
            assert_owned(modules[0], lock, args.assert_owned_pid, restoration_inputs={
                "--restoration-packet": args.release, "--output": args.output,
                "--authorization": args.authorization, "--observation": args.observation,
            })
        else:
            assert_owned(modules[0], lock, args.assert_owned_pid)
        return 0
    require(args.output, "exclusive output directory required")
    if args.finite_reviewed:
        from cre_finite_handoff import run_finite

        require((maintenance or restoration) and all((args.observation_output, args.observation,
                args.authorization, args.review, args.reviewer, args.approval_ref)),
                "finite handoff requires explicit reviewed admission inputs")
        finite_command = [sys.executable.encode(), str(Path(__file__).resolve()).encode(),
                          *(value.encode() for value in (sys.argv[1:] if argv is None else argv))]
        finite_inputs = {"--maintenance-packet" if maintenance else "--restoration-packet": args.release,
                         "--observation": args.observation, "--authorization": args.authorization,
                         "--output": args.output}
        validate_owner_command(finite_command, Path(__file__).resolve(),
                               maintenance_inputs=finite_inputs if maintenance else None,
                               restoration_inputs=finite_inputs if restoration else None)
        run_finite(args, raw, release, evidence, git, modules)
        return 0
    if maintenance and args.observe:
        from cre_runtime_maintenance import observe_maintenance

        observe_maintenance(args, raw, release, evidence, git, modules)
        return 0
    if maintenance and args.postreadback:
        from cre_runtime_maintenance import postreadback_maintenance

        postreadback_maintenance(args, raw, release, evidence, git, modules)
        return 0
    if maintenance and args.apply:
        from cre_runtime_maintenance import apply_maintenance

        require(args.observation and args.authorization, "explicit maintenance admission required")
        apply_maintenance(args, raw, release, evidence, git, modules)
        return 0
    require(not maintenance, "maintenance operation is not implemented")
    if args.apply:
        require(args.observation and args.authorization, "explicit admission required")
        if restoration:
            apply_restoration(args, raw, release, evidence, git, modules)
        else:
            apply(args, raw, release, evidence, git, modules)
    else:
        if restoration:
            observe_restoration(args, raw, release, evidence, git, modules)
            return 0
        if args.postreadback:
            require(args.operation, "independent postreadback requires the original operation")
            terminal = json_bytes(read_file(local_path(args.operation) / "terminal.json"))
            require(terminal.get("schema") == "cre-code-terminal-v1" and terminal.get("status") == "committed"
                    and terminal.get("canonical_lease_released") is True
                    and terminal.get("selected") == release["selected"]
                    and terminal.get("release_sha256") == sha(raw), "original operation terminal binding differs")
        output = local_path(args.output)
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        runner = Foreground(release, modules[0], output)
        runner.native("Postreadback" if args.postreadback else "Observe", args.release, sha(raw))
        native = json_bytes(read_file(output / "native-observation.json"))
        observation = readiness(release, *modules, git, selected=args.postreadback, evidence=evidence)
        observation.update(schema="cre-code-observation-v1", observed_at=native["observed_at"], tasks=native["tasks"])
        observation["windows_resources"] = native["windows_resources"]
        observation["release_sha256"] = sha(raw)
        if args.postreadback:
            observation["operation_terminal_sha256"] = sha(read_file(local_path(args.operation) / "terminal.json"))
            observation["operation_output"] = canonical_path(str(local_path(args.operation)))
            observation["authorization_sha256"] = terminal["authorization_sha256"]
            observation["native_completion_sha256"] = sha(read_file(str(local_path(args.operation)) + ".native.json"))
        observation["qualified_at"] = now().isoformat()
        validate_observation(release, evidence, observation, now(), selected=args.postreadback)
        evidence.recheck()
        write_new(output / "observation.json", observation)
        if args.postreadback:
            from cre_code_handoff_transition import make_transition, validate_transition

            transition = make_transition(release, args.release, local_path(args.operation), output, read_file)
            validate_transition(transition, release["deployed"], release["selected"], read_file)
            write_new(output / "transition.json", transition)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(78)
