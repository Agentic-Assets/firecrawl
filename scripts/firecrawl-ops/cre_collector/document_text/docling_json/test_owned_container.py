import subprocess
import unittest
from unittest.mock import patch

import owned_container


class LifecycleTest(unittest.TestCase):
    def test_read_after_confirmed_removal(self):
        cid = 'a' * 64; events = []; extraction = {'error_code': None, 'warnings': []}
        calls = [subprocess.CompletedProcess([], 0, stdout=cid.encode()), subprocess.CompletedProcess([], 0),
                 subprocess.CompletedProcess([], 0)]
        with patch.object(owned_container.ocr, 'owned_id', return_value=cid), patch.object(owned_container.subprocess, 'run', side_effect=calls):
            owned_container.run([], 'x', 'n', extraction, lambda _: events.append('settled'),
                                lambda _: events.append('removed') or True, lambda: events.append('read'), 120)
        self.assertEqual(events, ['removed', 'settled', 'read'])

    def test_refuses_read_when_removal_unknown(self):
        cid = 'a' * 64; events = []; extraction = {'error_code': None, 'warnings': []}
        calls = [subprocess.CompletedProcess([], 0, stdout=cid.encode()), subprocess.CompletedProcess([], 0),
                 subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 1)]
        with patch.object(owned_container.ocr, 'owned_id', return_value=cid), patch.object(owned_container.subprocess, 'run', side_effect=calls):
            owned_container.run([], 'x', 'n', extraction, lambda _: events.append('settled'),
                                lambda _: True, lambda: events.append('read'), 120)
        self.assertEqual(events, [])
        self.assertIn('container_cleanup_unconfirmed', extraction['warnings'])

    def test_timeout_stops_before_read(self):
        cid = 'a' * 64; events = []; extraction = {'error_code': None, 'warnings': []}
        calls = [subprocess.CompletedProcess([], 0, stdout=cid.encode()), subprocess.TimeoutExpired('docker', 120),
                 subprocess.CompletedProcess([], 0)]
        with patch.object(owned_container.ocr, 'owned_id', return_value=cid), patch.object(owned_container.subprocess, 'run', side_effect=calls):
            owned_container.run([], 'x', 'n', extraction, lambda _: events.append('settled'),
                                lambda _: True, lambda: events.append('read'), 120)
        self.assertEqual(events, ['settled', 'read'])
        self.assertIn('worker_timeout_partial_output', extraction['warnings'])
