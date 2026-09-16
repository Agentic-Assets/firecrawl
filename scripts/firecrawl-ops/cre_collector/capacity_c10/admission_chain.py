"""Offline, reviewable bridge from sealed C10 receipts to authority proposals.

This module intentionally imports no provider, cache, database, scheduler, or
runtime controller.  Source collection remains a separately reviewed,
source-owned request-card operation.  This surface can only provision private
roots, validate an existing receipt manifest, publish a no-write review bundle,
and render (never install) a repository authority proposal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cre_capacity_multisource_v1 as multisource

from . import admission
from .adapters import candidate_registry
from .authority import AUTHORITY_KIND, SCHEMA_VERSION, repository_implementation_sha256
from .contracts import C10Error, canonical_bytes, require_no_write, sha256
from .policy import load_policy

ROOT_MODE = 0o700
FILE_MODE = 0o600
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
BUNDLE_KIND = "cre_capacity_c10_v1_admission_bundle"
BUNDLE_SCHEMA_VERSION = 1
NO_WRITE = dict(admission.NO_WRITE)


class _PrivateRoot:
    """A live descriptor for one stable, owner-only directory."""

    def __init__(self, path: Path, fd: int, stat_result: os.stat_result) -> None:
        self.path = path
        self.fd = fd
        self.device = stat_result.st_dev
        self.inode = stat_result.st_ino

    def recheck(self) -> None:
        try:
            listed = self.path.lstat()
            opened = os.fstat(self.fd)
        except OSError as exc:
            raise C10Error("private admission root is unavailable") from exc
        if (
            not stat.S_ISDIR(listed.st_mode)
            or stat.S_ISLNK(listed.st_mode)
            or listed.st_uid != os.geteuid()
            or stat.S_IMODE(listed.st_mode) != ROOT_MODE
            or (listed.st_dev, listed.st_ino) != (self.device, self.inode)
            or (opened.st_dev, opened.st_ino) != (self.device, self.inode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != ROOT_MODE
        ):
            raise C10Error("private admission root changed or is not owner-0700")


def _absolute_leaf(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.name in {"", ".", ".."} or path.parent == path:
        raise C10Error(f"{label} must be an absolute leaf directory")
    return path


def _open_parent_for_provision(path: Path) -> tuple[Path, int]:
    parent = path.parent
    try:
        listed = parent.lstat()
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
    except OSError as exc:
        raise C10Error("private-root parent is unavailable") from exc
    if (
        not stat.S_ISDIR(listed.st_mode)
        or stat.S_ISLNK(listed.st_mode)
        or listed.st_uid != os.geteuid()
        or stat.S_IMODE(listed.st_mode) & 0o022
        or (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & 0o022
    ):
        os.close(fd)
        raise C10Error("private-root parent is not a stable owner-controlled directory")
    return parent, fd


@contextmanager
def _open_private_root(path: Path, label: str) -> Iterator[_PrivateRoot]:
    path = _absolute_leaf(path, label)
    try:
        listed = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
    except OSError as exc:
        raise C10Error(f"{label} is unavailable") from exc
    root = _PrivateRoot(path, fd, opened)
    try:
        if (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino):
            raise C10Error(f"{label} changed while opening")
        root.recheck()
        yield root
    finally:
        os.close(fd)


def _provision_one(path: Path, label: str) -> dict[str, str]:
    path = _absolute_leaf(path, label)
    _parent, parent_fd = _open_parent_for_provision(path)
    try:
        try:
            os.mkdir(path.name, ROOT_MODE, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise C10Error(f"{label} must be a fresh private root") from exc
        try:
            listed = path.lstat()
            opened = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise C10Error(f"{label} creation could not be verified") from exc
        if (
            not stat.S_ISDIR(listed.st_mode)
            or stat.S_ISLNK(listed.st_mode)
            or stat.S_IMODE(listed.st_mode) != ROOT_MODE
            or listed.st_uid != os.geteuid()
            or (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise C10Error(f"{label} was not created as an owner-0700 directory")
    finally:
        os.close(parent_fd)
    return {"path": str(path), "mode": "0700"}


def provision_roots(receipt_root: Path, admission_root: Path) -> list[dict[str, str]]:
    """Create two distinct fresh owner-only leaves below trusted parents."""
    if receipt_root == admission_root:
        raise C10Error("receipt and admission roots must be distinct")
    return [
        _provision_one(receipt_root, "receipt root"),
        _provision_one(admission_root, "admission root"),
    ]


def _direct_child(root: _PrivateRoot, path: Path, label: str) -> str:
    if (
        not path.is_absolute()
        or path.parent != root.path
        or path.name in {"", ".", ".."}
    ):
        raise C10Error(f"{label} must be directly inside its private root")
    return path.name


def _read_private_json(
    root: _PrivateRoot, path: Path, maximum: int, label: str
) -> tuple[dict[str, Any], bytes]:
    name = _direct_child(root, path, label)
    root.recheck()
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root.fd)
    except OSError as exc:
        raise C10Error(f"{label} is unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != FILE_MODE
            or opened.st_size < 1
            or opened.st_size > maximum
        ):
            raise C10Error(f"{label} is not an owner-0600 bounded regular file")
        raw = b""
        while len(raw) <= maximum:
            chunk = os.read(fd, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if not raw or len(raw) > maximum:
            raise C10Error(f"{label} size is invalid")
    finally:
        os.close(fd)
    root.recheck()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C10Error(f"{label} JSON is invalid") from exc
    if not isinstance(value, dict):
        raise C10Error(f"{label} JSON must be an object")
    return value, raw


def _write_private_json(
    root: _PrivateRoot, name: str, value: Mapping[str, Any]
) -> Path:
    if name in {"", ".", ".."} or "/" in name:
        raise C10Error("sealed admission artifact name is invalid")
    raw = canonical_bytes(value)
    root.recheck()
    created = False
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            FILE_MODE,
            dir_fd=root.fd,
        )
        created = True
    except FileExistsError as exc:
        raise C10Error("sealed admission artifact already exists") from exc
    except OSError as exc:
        raise C10Error("cannot create sealed admission artifact") from exc
    try:
        try:
            written = 0
            while written < len(raw):
                count = os.write(fd, raw[written:])
                if count < 1:
                    raise C10Error("cannot fully write sealed admission artifact")
                written += count
            os.fsync(fd)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != FILE_MODE
                or opened.st_size != len(raw)
            ):
                raise C10Error(
                    "sealed admission artifact did not retain owner-0600 mode"
                )
        finally:
            os.close(fd)
        root.recheck()
        os.fsync(root.fd)
    except Exception as exc:
        if created:
            try:
                root.recheck()
                os.unlink(name, dir_fd=root.fd)
                os.fsync(root.fd)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise C10Error("cannot durably publish sealed admission artifact") from exc
        raise
    return root.path / name


def _adapter_digests() -> dict[str, str]:
    policy = load_policy()
    registry = candidate_registry()
    expected = {source["key"] for source in policy["sources"]}
    if set(registry) != expected:
        raise C10Error("C10 candidate registry must exactly match the fixed policy")
    return {key: repository_implementation_sha256(key) for key in sorted(expected)}


def _checked_candidate_registry() -> tuple[dict[str, Any], set[str]]:
    """Return the fixed source descriptors without promoting any candidate."""
    policy = load_policy()
    registry = candidate_registry()
    expected = {source["key"] for source in policy["sources"]}
    if set(registry) != expected:
        raise C10Error("C10 candidate registry must exactly match the fixed policy")
    for key, descriptor in registry.items():
        if getattr(descriptor, "key", None) != key:
            raise C10Error(
                "C10 candidate descriptor key does not match its policy slot"
            )
        if getattr(descriptor, "fully_verified", None) not in {True, False}:
            raise C10Error("C10 candidate descriptor review state is invalid")
    return registry, expected


def collect_panel(*, receipt_root: Path) -> dict[str, str]:
    """Seal the fixed 20-source compatibility panel without provider transport.

    This is intentionally a receipt-generation *boundary*, not a generic
    fetcher.  A currently unreviewed descriptor yields a sealed blocked outcome
    rather than an invented HTTP response.  Once a separately reviewed
    coordinator exists, it may replace that one descriptor's blocked outcome
    through its own `ReceiptProducer`/one-shot transport, never through this
    command.  Therefore no panel outcome is eligible for admission here.
    """
    registry, expected = _checked_candidate_registry()
    digests = _adapter_digests()
    outcomes: list[dict[str, Any]] = []
    for key in sorted(expected):
        descriptor = registry[key]
        reviewed = descriptor.fully_verified is True
        outcomes.append(
            {
                "source_key": key,
                "state": "blocked" if not reviewed else "unavailable",
                "eligible": False,
                "reason": (
                    "candidate adapter has no independently reviewed live receipt coordinator"
                    if not reviewed
                    else "no collection transport is available from the admission command"
                ),
                "adapter_implementation_sha256": digests[key],
            }
        )
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "kind": "cre_capacity_c10_v1_compatibility_panel",
        "policy_sha256": load_policy()["policy_sha256"],
        "outcomes": outcomes,
        "no_write": NO_WRITE,
    }
    panel = {**unsigned, "panel_sha256": sha256(unsigned)}
    with _open_private_root(receipt_root, "receipt root") as root:
        path = _write_private_json(
            root, f"compatibility-{panel['panel_sha256']}.json", panel
        )
    return {"path": str(path), "panel_sha256": panel["panel_sha256"]}


def _validate_ready_cohort(cohort: Mapping[str, Any]) -> None:
    policy = load_policy()
    if cohort.get("aggregate", {}).get("state") != "ready_for_review":
        raise C10Error("receipt cohort is partial or not ready for review")
    admission._verified_cohort_sources(cohort, policy)


def build_bundle(
    *,
    receipt_root: Path,
    receipt_manifest: Path,
    admission_root: Path,
    now_utc: datetime | None = None,
) -> dict[str, str]:
    """Validate existing evidence and publish one immutable no-write review bundle."""
    with _open_private_root(receipt_root, "receipt root") as receipts:
        manifest, manifest_raw = _read_private_json(
            receipts, receipt_manifest, MAX_MANIFEST_BYTES, "receipt manifest"
        )
        if manifest.get("receipt_root") != str(receipts.path):
            raise C10Error(
                "receipt manifest root does not match the opened private root"
            )
        try:
            cohort = multisource.prevalidate_cohort(manifest, now_utc=now_utc)
        except multisource.MultisourceError as exc:
            raise C10Error(f"receipt prevalidation failed: {exc}") from exc
    _validate_ready_cohort(cohort)
    adapter_digests = _adapter_digests()
    policy = load_policy()
    unsigned: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "receipt_root": str(receipts.path),
        "receipt_manifest_name": receipt_manifest.name,
        "receipt_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "cohort": cohort,
        "policy_sha256": policy["policy_sha256"],
        "adapter_implementation_sha256": adapter_digests,
        "no_write": NO_WRITE,
    }
    bundle = {**unsigned, "bundle_sha256": sha256(unsigned)}
    name = f"admission-{cohort['cohort_sha256']}.json"
    with _open_private_root(admission_root, "admission root") as root:
        path = _write_private_json(root, name, bundle)
    return {
        "path": str(path),
        "bundle_sha256": bundle["bundle_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
    }


def _load_bundle(bundle_path: Path) -> dict[str, Any]:
    with _open_private_root(bundle_path.parent, "admission root") as root:
        bundle, _raw = _read_private_json(root, bundle_path, MAX_BUNDLE_BYTES, "bundle")
    required = {
        "schema_version",
        "kind",
        "receipt_root",
        "receipt_manifest_name",
        "receipt_manifest_sha256",
        "cohort",
        "policy_sha256",
        "adapter_implementation_sha256",
        "no_write",
        "bundle_sha256",
    }
    if (
        set(bundle) != required
        or bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION
        or bundle.get("kind") != BUNDLE_KIND
        or not isinstance(bundle.get("receipt_root"), str)
        or not isinstance(bundle.get("receipt_manifest_name"), str)
        or not isinstance(bundle.get("cohort"), dict)
        or not isinstance(bundle.get("adapter_implementation_sha256"), dict)
    ):
        raise C10Error("sealed admission bundle schema is invalid")
    if sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    ) != bundle.get("bundle_sha256"):
        raise C10Error("sealed admission bundle digest is invalid")
    require_no_write(bundle)
    return bundle


def _revalidate_bundle_receipts(bundle: Mapping[str, Any]) -> None:
    """Reopen the named private manifest and all referenced receipts at render."""
    receipt_root = Path(bundle["receipt_root"])
    manifest_name = bundle["receipt_manifest_name"]
    with _open_private_root(receipt_root, "receipt root") as root:
        manifest, manifest_raw = _read_private_json(
            root,
            root.path / manifest_name,
            MAX_MANIFEST_BYTES,
            "receipt manifest",
        )
        if manifest.get("receipt_root") != str(root.path):
            raise C10Error(
                "receipt manifest root does not match the opened private root"
            )
        if (
            hashlib.sha256(manifest_raw).hexdigest()
            != bundle["receipt_manifest_sha256"]
        ):
            raise C10Error("sealed bundle receipt manifest digest is stale or tampered")
        try:
            current_cohort = multisource.prevalidate_cohort(manifest)
        except multisource.MultisourceError as exc:
            raise C10Error(f"receipt provenance revalidation failed: {exc}") from exc
    if current_cohort != bundle["cohort"]:
        raise C10Error(
            "sealed bundle cohort no longer matches current receipt provenance"
        )


def render_authority(bundle_path: Path) -> dict[str, Any]:
    """Render, but never install, the exact repository-authority proposal.

    Rendering requires all candidate adapters to have been independently
    reviewed.  The present repository deliberately fails this condition.
    """
    bundle = _load_bundle(bundle_path)
    _revalidate_bundle_receipts(bundle)
    policy = load_policy()
    if bundle["policy_sha256"] != policy["policy_sha256"]:
        raise C10Error("sealed bundle policy is no longer current")
    cohort = bundle["cohort"]
    _validate_ready_cohort(cohort)
    registry = candidate_registry()
    expected = {source["key"] for source in policy["sources"]}
    if set(registry) != expected or any(
        registry[key].fully_verified is not True for key in expected
    ):
        raise C10Error("C10 source adapters are not independently reviewed")
    current = _adapter_digests()
    if bundle["adapter_implementation_sha256"] != current:
        raise C10Error("sealed bundle adapter implementation digests are stale")
    unsigned_plan = admission.unsigned_plan(cohort, policy, current)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": AUTHORITY_KIND,
        "approved_cohort_sha256": cohort["cohort_sha256"],
        "approved_plan_sha256": sha256(unsigned_plan),
        "approved_adapters": current,
    }


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 UTC time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("provision-roots")
    provision.add_argument("--receipt-root", type=Path, required=True)
    provision.add_argument("--admission-root", type=Path, required=True)
    panel = commands.add_parser("collect-panel")
    panel.add_argument("--receipt-root", type=Path, required=True)
    bundle = commands.add_parser("build-bundle")
    bundle.add_argument("--receipt-root", type=Path, required=True)
    bundle.add_argument("--receipt-manifest", type=Path, required=True)
    bundle.add_argument("--admission-root", type=Path, required=True)
    bundle.add_argument("--now-utc", type=_parse_utc)
    render = commands.add_parser("render-authority")
    render.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "provision-roots":
            result: Any = provision_roots(args.receipt_root, args.admission_root)
        elif args.command == "collect-panel":
            result = collect_panel(receipt_root=args.receipt_root)
        elif args.command == "build-bundle":
            result = build_bundle(
                receipt_root=args.receipt_root,
                receipt_manifest=args.receipt_manifest,
                admission_root=args.admission_root,
                now_utc=args.now_utc,
            )
        else:
            result = render_authority(args.bundle)
    except C10Error as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
