"""Owned disposable PostgreSQL helper; no external connections."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def tool(name):
    for directory in ("/opt/homebrew/opt/postgresql@17/bin", "/usr/lib/postgresql/17/bin"):
        path = Path(directory) / name
        if path.is_file():
            return str(path)
    candidate = shutil.which(name)
    if candidate and (name != "initdb" or (Path(candidate).resolve().parent / "postgres").is_file()):
        return candidate
    raise RuntimeError(f"missing PostgreSQL server tool: {name}")


class OwnedCluster:
    """Retain the temporary directory unless the owned server is confirmed stopped."""

    def __init__(self, initdb, pg_ctl, *, port):
        self.initdb, self.pg_ctl, self.port = initdb, pg_ctl, port
        self.root = Path(tempfile.mkdtemp(prefix="lossless-planner-pg-", dir="/tmp"))
        self.db = self.root / "data"
        self.env = {"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root), "LC_ALL": "C"}
        self.start_attempted = False

    def __enter__(self):
        try:
            subprocess.run([self.initdb, "-D", str(self.db), "-A", "trust", "--no-locale"],
                           check=True, stdout=subprocess.DEVNULL, timeout=30, env=self.env)
            self.start_attempted = True
            subprocess.run([self.pg_ctl, "-D", str(self.db), "-o", f'-h "" -k {self.root} -p {self.port}',
                            "-l", str(self.root / "log"), "start"],
                           check=True, stdout=subprocess.DEVNULL, timeout=30, env=self.env)
        except BaseException:
            self.cleanup(retain=True)
            raise
        return self

    def cleanup(self, *, retain=False):
        if self.start_attempted:
            try:
                subprocess.run([self.pg_ctl, "-D", str(self.db), "-m", "immediate", "stop"],
                               check=False, capture_output=True, timeout=30, env=self.env)
                status = subprocess.run([self.pg_ctl, "-D", str(self.db), "status"],
                                        check=False, capture_output=True, timeout=10, env=self.env)
                if status.returncode != 3:
                    raise RuntimeError("disposable server stop could not be confirmed")
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                raise RuntimeError(f"cluster retained at {self.root}; stop could not be confirmed") from exc
        if not retain:
            shutil.rmtree(self.root)

    def __exit__(self, *_args):
        self.cleanup()
