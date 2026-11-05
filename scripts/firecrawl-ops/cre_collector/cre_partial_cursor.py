"""Owner-bound local cursor for partial-source windows; no database authority."""
import json
import os
import re
import secrets
from pathlib import Path

from cre_stop_guarded_tier import _read_at, _safe_stat

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ATTEMPT = re.compile(r"^[a-f0-9]{32}$")


def _name(source, target):
    if not isinstance(source, str) or not _KEY.fullmatch(source) or not isinstance(target, str) or not _HASH.fullmatch(target):
        raise ValueError("partial cursor source or target invalid")
    return f".partial-cursor-{source}-{target}.json"


def read_cursor(lock_path, source, target):
    """Read only this source/target cursor; an absent file means initial position 1."""
    name = _name(source, target)
    directory = os.open(Path(lock_path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True)
        try:
            raw, _ = _read_at(directory, name, limit=16384)
        except FileNotFoundError:
            return 1, None
        data = json.loads(raw)
    finally:
        os.close(directory)
    keys = {"schema", "sourceKey", "targetSha256", "position", "generation", "artifactSha256", "attemptId", "outcome"}
    if (not isinstance(data, dict) or set(data) != keys or data["schema"] != "cre-partial-cursor/v1"
            or data["sourceKey"] != source or data["targetSha256"] != target
            or type(data["position"]) is not int or data["position"] < 1
            or not isinstance(data["generation"], str) or not data["generation"]
            or not isinstance(data["artifactSha256"], str) or not _HASH.fullmatch(data["artifactSha256"])
            or not isinstance(data["attemptId"], str) or not _ATTEMPT.fullmatch(data["attemptId"])
            or data["outcome"] not in {"exact_commit", "settled_no_write"}):
        raise ValueError("partial cursor evidence invalid")
    return data["position"], data


def advance_cursor(lock_path, source, target, expected, position, *, generation, artifact_sha256,
                   attempt_id, outcome, proof, owner_check):
    """Publish only after owner-bound exact commit or verified no-write settlement."""
    name = _name(source, target)
    if (type(expected) is not int or expected < 1 or type(position) is not int or position < 1
            or not isinstance(generation, str) or not generation or not isinstance(artifact_sha256, str)
            or not _HASH.fullmatch(artifact_sha256) or not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id)
            or outcome not in {"exact_commit", "settled_no_write"} or not isinstance(proof, dict)
            or proof.get("ok") is not True or proof.get("artifact_sha256") != artifact_sha256
            or proof.get("generation_id") != generation or proof.get("attempt_id") != attempt_id
            or proof.get("database_target_sha256") != target or proof.get("outcome") != outcome
            or not callable(owner_check)):
        raise ValueError("partial cursor settlement invalid")
    owner_check()
    current, prior = read_cursor(lock_path, source, target)
    desired = {"schema": "cre-partial-cursor/v1", "sourceKey": source, "targetSha256": target,
               "position": position, "generation": generation, "artifactSha256": artifact_sha256,
               "attemptId": attempt_id, "outcome": outcome}
    if prior == desired:
        owner_check()
        return desired
    if current != expected:
        raise ValueError("partial cursor changed")
    directory = os.open(Path(lock_path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp = f".partial-cursor-{secrets.token_hex(16)}.tmp"
    created = False
    try:
        before = _safe_stat(directory, directory=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(desired, sort_keys=True, separators=(",", ":")) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        owner_check()
        os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
        after = _safe_stat(directory, directory=True)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("partial cursor directory changed")
        owner_check()
    finally:
        if created:
            try:
                os.unlink(tmp, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
    return desired
