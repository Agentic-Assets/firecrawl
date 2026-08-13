#!/usr/bin/env python3
"""Tests for local capability matrix evidence selection."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "local_capability_matrix.py"
SPEC = importlib.util.spec_from_file_location("local_capability_matrix", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LocalCapabilityMatrixTests(unittest.TestCase):
    def test_latest_smoke_file_uses_modification_time_not_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            older = tmp / "legacy" / "20260813-235959-local-api-smoke.json"
            newer = tmp / "current" / "20260812-000000-local-api-smoke.json"
            older.parent.mkdir()
            newer.parent.mkdir()
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(older, (now - 60, now - 60))
            os.utime(newer, (now, now))

            self.assertEqual(MODULE.latest_smoke_file(tmp), newer)


if __name__ == "__main__":
    unittest.main()
