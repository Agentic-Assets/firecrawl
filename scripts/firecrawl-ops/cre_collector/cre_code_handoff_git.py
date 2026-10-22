"""Bounded Git/path and unchanged-dependency checks for code-only handoff."""

import json
import os
import stat
import subprocess
from pathlib import Path

from cre_code_handoff_evidence import require, safe_relative, sha


def local_path(value):
    value = str(value).replace("\\", "/")
    if os.name != "nt" and len(value) > 2 and value[1:3] == ":/":
        return Path("/mnt/" + value[0].lower() + "/" + value[3:])
    return Path(value)


def windows_path(value):
    value = str(value).replace("\\", "/")
    if value.startswith("/mnt/") and value[6:7] == "/":
        value = value[5].upper() + ":/" + value[7:]
    require(len(value) > 3 and value[1:3] == ":/", "absolute native path required")
    return value


def read_file(value):
    path = local_path(value)
    # Refuse aliases at every existing ancestor, not just the final leaf.
    for ancestor in (path, *path.parents):
        require(not ancestor.is_symlink(), "input ancestor alias")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                "regular unaliased input required")
        require(before.st_size <= 128 * 1024 * 1024, "input exceeds bounded evidence size")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        after, named = os.fstat(fd), path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        require(all(getattr(before, f) == getattr(after, f) == getattr(named, f) for f in fields),
                "input changed during read")
        return raw
    finally:
        os.close(fd)


class Git:
    def __init__(self, native_git):
        self.native_git = str(local_path(native_git))

    def command(self, repo, *args, input_bytes=None, allowed=(0,)):
        repo = local_path(repo)
        native = str(repo).startswith("/mnt/")
        argv = [self.native_git if native else "git", "-C",
                windows_path(repo) if native else str(repo), *args]
        result = subprocess.run(argv, check=False, input=input_bytes, capture_output=True, timeout=60)
        require(result.returncode in allowed, f"Git {args[0]} failed ({result.returncode})")
        return result.stdout

    def text(self, repo, *args):
        return self.command(repo, *args).decode().strip()

    def hooks(self, repo):
        root = local_path(repo)
        folder = local_path(self.text(root, "rev-parse", "--git-path", "hooks"))
        if not folder.is_absolute():
            folder = root / folder
        require(not folder.is_symlink(), "effective hooks directory alias")
        for name in ("post-merge", "pre-merge-commit", "reference-transaction", "pre-auto-gc", "post-index-change"):
            require(not (folder / name).exists() and not (folder / name).is_symlink(),
                    "mutation hook requires separate review")
        monitor = self.command(root, "config", "--get", "core.fsmonitor", allowed=(0, 1)).strip()
        require(monitor in {b"", b"false"}, "active fsmonitor refused")

    def clean(self, repo, expected, *, tracking=None, branch="main"):
        root = local_path(repo)
        require(root.is_dir() and not root.is_symlink() and (root / ".git").is_dir()
                and not (root / ".git").is_symlink(), "standalone unaliased checkout required")
        self.hooks(root)
        require(self.text(root, "rev-parse", "HEAD") == expected["sha"]
                and self.text(root, "rev-parse", "HEAD^{tree}") == expected["tree"]
                and self.text(root, "branch", "--show-current") == branch
                and not self.command(root, "status", "--porcelain=v1", "--untracked-files=all"),
                "exact clean checkout required")
        if tracking:
            require(self.text(root, "rev-parse", "refs/remotes/origin/main") == tracking,
                    "tracking identity changed")

    def development(self, release):
        root = release["roots"]["development"]
        expected = release["development"]
        self.clean(root, expected, tracking=expected["sha"])
        self.command(root, "merge-base", "--is-ancestor", release["selected"]["sha"], expected["sha"])
        require(self.text(root, "rev-parse", release["selected"]["sha"] + "^{tree}") == release["selected"]["tree"],
                "selected release tree changed")

    def entries(self, repo, commit):
        result = {}
        for row in self.command(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if row:
                meta, path = row.split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                result[path.decode()] = {"mode": mode, "type": kind, "oid": oid}
        return result

    def paths(self, repo, release):
        old, new = release["deployed"]["sha"], release["selected"]["sha"]
        paths = self.command(repo, "diff", "--no-renames", "--name-only", "-z", old, new)
        require(paths == b"".join(p.encode() + b"\0" for p in release["allowed_paths"]),
                "exact changed-path NUL order differs")
        before, after = self.entries(repo, old), self.entries(repo, new)
        for path in set(before) | set(after):
            if Path(path).name in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements.txt", "pyproject.toml", "uv.lock"}:
                require(before.get(path) == after.get(path), "dependency manifests/locks changed")
        object_map = release["objects"]
        require(set(object_map) == set(release["allowed_paths"]), "object map incomplete")
        for raw in release["allowed_paths"]:
            path = safe_relative(raw)
            require(object_map[raw] == {"before": before.get(raw), "after": after.get(raw)},
                    "changed object map differs")
            for entry in (before.get(raw), after.get(raw)):
                require(entry is None or (entry["mode"] in {"100644", "100755"} and entry["type"] == "blob"),
                        "changed object not regular")
            require(not self.command(repo, "check-ignore", "--no-index", "--stdin", "-z",
                                     input_bytes=raw.encode() + b"\0", allowed=(0, 1)),
                    "ignored changed path")
            current = local_path(repo)
            for index, part in enumerate(path.parts):
                require(not current.is_symlink(), "changed ancestor alias")
                if current.is_dir():
                    aliases = [n for n in os.listdir(current) if n.casefold() == part.casefold()]
                    require(not aliases or aliases == [part], "changed-path case alias")
                current = current / part
                if not current.exists() and not current.is_symlink():
                    continue
                entry = current.lstat()
                require(not stat.S_ISLNK(entry.st_mode), "changed path symlink")
                if index < len(path.parts) - 1:
                    prefix = "/".join(path.parts[:index + 1]) + "/"
                    require(stat.S_ISDIR(entry.st_mode) and any(p.startswith(prefix) for p in before),
                            "untracked/non-directory ancestor collision")
                else:
                    require(raw in before and stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1,
                            "untracked/nonregular/hardlinked leaf collision")

    def fast_forward(self, repo, release, bundle):
        self.clean(repo, release["deployed"])
        new, old = release["selected"], release["deployed"]
        wire_bundle = windows_path(bundle) if str(local_path(repo)).startswith("/mnt/") else str(local_path(bundle))
        self.command(repo, "fetch", "--no-tags", wire_bundle, "refs/remotes/origin/main")
        require(self.text(repo, "rev-parse", "FETCH_HEAD") == new["sha"]
                and self.text(repo, "rev-parse", new["sha"] + "^{tree}") == new["tree"], "bundle identity differs")
        self.command(repo, "merge-base", "--is-ancestor", old["sha"], new["sha"])
        witness = self.text(repo, "rev-parse", "refs/remotes/origin/main")
        self.command(repo, "merge-base", "--is-ancestor", old["sha"], witness)
        self.command(repo, "merge-base", "--is-ancestor", witness, new["sha"])
        self.paths(repo, release)
        self.hooks(repo)
        self.command(repo, "update-ref", "refs/remotes/origin/main", new["sha"], witness)
        self.command(repo, "merge", "--ff-only", "--no-overwrite-ignore", new["sha"])
        self.clean(repo, new, tracking=new["sha"])


def installed_dependencies(collector):
    declared = json.loads(read_file(collector / "package.json"))
    lock = json.loads(read_file(collector / "package-lock.json"))
    installed = json.loads(read_file(collector / "node_modules/.package-lock.json"))
    require(lock.get("lockfileVersion") == 3, "unsupported dependency lock")
    packages = lock["packages"]
    for kind in ("dependencies", "devDependencies"):
        require(declared.get(kind, {}) == packages[""].get(kind, {}), "declared lock roots differ")
        for name in declared.get(kind, {}):
            key = "node_modules/" + name
            require(json.loads(read_file(collector / key / "package.json")).get("version") == packages[key].get("version"),
                    "installed direct version differs")
    for key, actual in installed["packages"].items():
        require(key in packages and key.startswith("node_modules/") and ".." not in Path(key).parts,
                "unexpected installed package")
        require(all(actual.get(f) == packages[key].get(f) for f in ("version", "resolved", "integrity")),
                "installed metadata differs")
    require(all(not k or v.get("optional") or k in installed["packages"] for k, v in packages.items()),
            "required installed package missing")
    return {name: sha(read_file(collector / name)) for name in
            ("package.json", "package-lock.json", "node_modules/.package-lock.json")}
