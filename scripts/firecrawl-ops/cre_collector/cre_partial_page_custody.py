"""Source attempt-local partial page custody; never authorizes a database write."""
import hashlib
import json
import os
import stat
from pathlib import Path


PARTIAL_PAGE_SOURCES = ("cbre", "colliers", "avison-young", "lyon-stahl")


def partial_env_name(source: str, field: str) -> str:
    if source not in PARTIAL_PAGE_SOURCES or field not in {"PATH", "ATTEMPT"}:
        raise ValueError("invalid partial-page environment key")
    return f"CRE_{source.upper().replace('-', '_')}_PARTIAL_{field}"


def expected_path(run_dir: Path, attempt: int, source: str = "cbre") -> Path:
    if source not in PARTIAL_PAGE_SOURCES or type(attempt) is not int or attempt < 1:
        raise ValueError("invalid partial-page attempt")
    return run_dir / f"{source}-partial" / f"attempt-{attempt}.jsonl"


def prepare(run_dir: Path, attempt: int, source: str = "cbre") -> dict:
    path = expected_path(run_dir, attempt, source)
    result = {"path": str(path.relative_to(run_dir)), "state": "unavailable"}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700 or os.path.lexists(path)):
            return result
        result["state"] = "pending"
    except OSError:
        pass
    return result


def settle(run_dir: Path, attempt: dict, main: Path | None, source: str = "cbre") -> None:
    """Bind a settled worker's receipt to its exact main and sidecar bytes."""
    state = attempt.get(f"{source}_partial")
    if not isinstance(state, dict) or state.get("state") != "pending":
        return
    try:
        path = expected_path(run_dir, attempt["number"], source)
        if state["path"] != str(path.relative_to(run_dir)):
            raise ValueError()
        if not os.path.lexists(path):
            state["state"] = "not_published"
            return
        if main is None or not main.is_file() or main.is_symlink():
            state["state"] = "unverified"
            return
        # This input is the settled worker's artifact, not a source admission.
        from source_evidence.automatic import _read_private
        main_size = main.lstat().st_size
        main_bytes = _read_private(main, main_size)
        if len(main_bytes) != main_size:
            raise ValueError()
        data = json.loads(main_bytes)
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise TypeError()
        entries = [e for e in data["sources"] if isinstance(e, dict)
                   and e.get("sourceKey") == source and e.get("partialCapture")]
        if len(entries) != 1 or not isinstance(entries[0]["partialCapture"], dict):
            raise ValueError()
        receipt = entries[0]["partialCapture"]
        if (receipt.get("state") not in {"sealed", "sealed_partial"}
                or receipt.get("coverage") != "unverified"
                or receipt.get("outcome") not in {"source_error", "source_returned"}
                or (receipt["outcome"] == "source_error") != bool(entries[0].get("error"))
                or receipt.get("sourceKey") != source
                or receipt.get("generationId") != run_dir.name
                or receipt.get("attempt") != attempt["number"]
                or receipt.get("path") != state["path"]
                or type(receipt.get("bytes")) is not int or receipt["bytes"] <= 0
                or type(receipt.get("observationRecords")) is not int or receipt["observationRecords"] <= 0):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600):
                raise ValueError()
            digest = hashlib.sha256()
            size = 0
            lines = 0
            last_byte = b""
            header = b""
            while chunk := os.read(fd, 1024 * 1024):
                if b"\n" not in header and len(header) < 65536:
                    header += chunk[:65536 - len(header)]
                size += len(chunk)
                lines += chunk.count(b"\n")
                last_byte = chunk[-1:]
                digest.update(chunk)
            if lines != receipt["observationRecords"] + 1 or last_byte != b"\n":
                raise ValueError()
            first_line = header.split(b"\n", 1)[0]
            if json.loads(first_line) != {"schema": f"{source}-partial-pages/v1",
                    "sourceKey": source, "generation": run_dir.name,
                    "attempt": attempt["number"]}:
                raise ValueError()
            after = os.fstat(fd)
            named = path.lstat()
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                     item.st_ctime_ns, item.st_mode, item.st_uid, item.st_nlink)
            if identity(before) != identity(after) or identity(after) != identity(named):
                raise ValueError()
        finally:
            os.close(fd)
        if size != receipt["bytes"] or digest.hexdigest() != receipt.get("sha256"):
            raise ValueError()
        state.update(state="sealed_local_partial" if receipt["state"] == "sealed_partial" else "sealed_local",
                     coverage="unverified", outcome=receipt["outcome"], bytes=size, observationRecords=receipt["observationRecords"],
                     sha256=digest.hexdigest(),
                     main_sha256=hashlib.sha256(main_bytes).hexdigest())
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        state["state"] = "unverified"


def reconcile(run_dir: Path, manifest: dict, source: str = "cbre") -> None:
    """Preserve interrupted attempts without inferring worker settlement."""
    checkpoint = (manifest.get("sources") or {}).get(source) or {}
    for attempt in checkpoint.get("attempts") or []:
        state = attempt.get(f"{source}_partial")
        if not isinstance(state, dict) or state.get("state") != "pending":
            continue
        if not attempt.get("finished_at") or type(attempt.get("rc")) is not int:
            state["state"] = "unverified"
            continue
        candidates = []
        rejected = run_dir / "rejected" / f"{source}-attempt-{attempt['number']}.json"
        if rejected.is_file() and not rejected.is_symlink():
            candidates.append(rejected)
        temp = attempt.get("temporary_artifact")
        if isinstance(temp, str):
            rel = Path(temp)
            if not rel.is_absolute() and ".." not in rel.parts:
                path = run_dir / rel
                if path.is_file() and not path.is_symlink():
                    candidates.append(path)
        candidate = checkpoint.get("candidate") or {}
        if candidate.get("attempt_number") == attempt["number"]:
            rel = candidate.get("path")
            if isinstance(rel, str):
                rel_path = Path(rel)
                if not rel_path.is_absolute() and ".." not in rel_path.parts:
                    path = run_dir / rel_path
                    if path.is_file() and not path.is_symlink():
                        candidates.append(path)
        settle(run_dir, attempt, candidates[0] if len(candidates) == 1 else None, source)
