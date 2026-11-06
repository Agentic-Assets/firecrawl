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
ORIGINAL = "sources/nai-global.json"
CANDIDATE = "sources/nai-global-coverage-partial.json"


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
    """Publish a deterministic sibling; leave the original and old pointer intact."""
    import cre_checkpoint_refresh as refresh
    if not isinstance(owner, refresh.SharedLock):
        raise TypeError("NAI coverage owner invalid")
    owner._require_authority()
    if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
        raise ValueError("NAI coverage lock differs")
    root = Path(run_dir)
    info = manifest["sources"]["nai-global"]["artifact"]
    original_path = root / ORIGINAL
    path = root / CANDIDATE
    if info.get("path") != ORIGINAL or original_path.parent.is_symlink():
        raise ValueError("NAI coverage source path differs")
    before = _private_regular(original_path)
    original = read_file(original_path.resolve())
    digest = hashlib.sha256(original).hexdigest()
    if digest != info.get("sha256") or len(original) != info.get("bytes"):
        raise ValueError("NAI coverage source changed")
    marked = derive(original)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # A prior crash may leave either the complete candidate or its private
        # prefix. Reuse only exact output; remove only a provable output prefix.
        tmp = ".nai-global-coverage-candidate.tmp"
        tmp_path = path.parent / tmp
        if path.exists():
            # Link publication can be interrupted before unlinking the temp.
            # Verify both names are our exact private inode before cleanup.
            if tmp_path.exists():
                existing, temporary = path.lstat(), tmp_path.lstat()
                if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()
                        or stat.S_IMODE(existing.st_mode) != 0o600 or existing.st_nlink != 2
                        or (existing.st_dev, existing.st_ino) != (temporary.st_dev, temporary.st_ino)):
                    raise ValueError("NAI coverage publication links differ")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    current = os.fstat(fd)
                    if (current.st_dev, current.st_ino, current.st_size) != (existing.st_dev, existing.st_ino, len(marked)):
                        raise ValueError("NAI coverage linked candidate differs")
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        if stream.read(len(marked) + 1) != marked:
                            raise ValueError("NAI coverage linked candidate differs")
                    if os.fstat(fd).st_mtime_ns != current.st_mtime_ns:
                        raise ValueError("NAI coverage linked candidate changed")
                finally:
                    os.close(fd)
                owner._require_authority()
                candidate_after, temporary_after = path.lstat(), tmp_path.lstat()
                if ((candidate_after.st_dev, candidate_after.st_ino) != (existing.st_dev, existing.st_ino)
                        or (temporary_after.st_dev, temporary_after.st_ino) != (existing.st_dev, existing.st_ino)
                        or candidate_after.st_nlink != 2 or temporary_after.st_nlink != 2):
                    raise ValueError("NAI coverage publication links changed")
                os.unlink(tmp, dir_fd=directory)
                os.fsync(directory)
            _private_regular(path)
            if read_file(path.resolve()) != marked:
                raise ValueError("NAI coverage candidate differs")
        else:
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            except FileExistsError:
                tmp_path = path.parent / tmp
                _private_regular(tmp_path)
                partial = read_file(tmp_path.resolve())
                if not marked.startswith(partial):
                    raise ValueError("NAI coverage temporary candidate differs")
                owner._require_authority()
                os.unlink(tmp, dir_fd=directory)
                os.fsync(directory)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(marked)
                stream.flush()
                os.fsync(stream.fileno())
            owner._require_authority()
            after = _private_regular(original_path)
            if (read_file(original_path.resolve()) != original
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("NAI coverage source changed before publication")
            os.link(tmp, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(tmp, dir_fd=directory)
            os.fsync(directory)
    finally:
        os.close(directory)
    owner._require_authority()
    if read_file(original_path.resolve()) != original or read_file(path.resolve()) != marked:
        raise ValueError("NAI coverage evidence changed before checkpoint")
    info.update(path=CANDIDATE, nai_coverage_partial=True, nai_coverage_original=ORIGINAL,
                nai_coverage_original_sha256=digest,
                sha256=hashlib.sha256(marked).hexdigest(), bytes=len(marked))
    refresh.save_manifest(root, manifest)
    return path
