"""Private, source-bound marker for a coverage-held ordinary NAI artifact."""
import hashlib
import json
import os
import stat
from decimal import Decimal
from pathlib import Path

from cre_code_handoff_git import read_file
from nai_partial.partial import _encode_json

SCHEMA = "nai-post-coverage-partial/v1"
ORIGINAL = "sources/nai-global-coverage-original.json"


def derive(original):
    data = json.loads(original, parse_float=Decimal)
    if not isinstance(data, dict) or data.get("schema") is not None or "naiCoverage" in data:
        raise ValueError("NAI coverage original schema differs")
    data["schema"] = SCHEMA
    data["naiCoverage"] = {"originalSha256": hashlib.sha256(original).hexdigest()}
    return (_encode_json(data) + "\n").encode("ascii")


def _private_regular(path):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
        raise ValueError("NAI coverage artifact custody differs")
    return st


def prepare_owned(run_dir, manifest, owner):
    """Preserve exact original and atomically publish a marked candidate."""
    import cre_checkpoint_refresh as refresh
    if not isinstance(owner, refresh.SharedLock):
        raise TypeError("NAI coverage owner invalid")
    owner._require_authority()
    if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
        raise ValueError("NAI coverage lock differs")
    root = Path(run_dir)
    info = manifest["sources"]["nai-global"]["artifact"]
    path = root / "sources" / "nai-global.json"
    if info.get("path") != "sources/nai-global.json" or path.parent.is_symlink():
        raise ValueError("NAI coverage source path differs")
    before = _private_regular(path)
    original = read_file(path.resolve())
    digest = hashlib.sha256(original).hexdigest()
    if digest != info.get("sha256") or len(original) != info.get("bytes"):
        raise ValueError("NAI coverage source changed")
    marked = derive(original)
    archive = root / ORIGINAL
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(archive.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except FileExistsError:
            _private_regular(archive)
            if read_file(archive.resolve()) != original:
                raise ValueError("NAI coverage original archive differs")
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(directory)
        tmp = ".nai-global-coverage-candidate.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(marked)
            stream.flush()
            os.fsync(stream.fileno())
        owner._require_authority()
        after = _private_regular(path)
        if (read_file(path.resolve()) != original
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("NAI coverage source changed before publication")
        os.replace(tmp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    owner._require_authority()
    info.update(nai_coverage_partial=True, nai_coverage_original=ORIGINAL,
                nai_coverage_original_sha256=digest,
                sha256=hashlib.sha256(marked).hexdigest(), bytes=len(marked))
    refresh.save_manifest(root, manifest)
