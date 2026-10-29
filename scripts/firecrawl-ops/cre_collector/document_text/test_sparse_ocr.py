import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sparse_ocr


class SparseResultsTest(unittest.TestCase):
    def test_short_unicode_text_and_incomplete_pages_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            data = '地'.encode()
            (folder / 'page-1.txt').write_bytes(data)
            (folder / 'receipt.json').write_text(json.dumps({'page_count': 3, 'pages': [
                {'page': 1, 'worker_exit': 0, 'text_bytes': len(data), 'elapsed_seconds': 1},
                {'page': 2, 'error': 'page_timeout', 'elapsed_seconds': 60}]}))
            extraction = {'warnings': [], 'status': 'failed'}
            sparse_ocr._read_results(folder, extraction, 3)
            self.assertEqual(extraction['text'], '地')
            self.assertEqual(extraction.get('coverage', 'unknown'), 'unknown')
            self.assertEqual(extraction['omitted_pages'], 1)
            self.assertEqual(extraction['pages'][0]['text_end_byte'], 3)
            self.assertIn('page_failure_or_partial_output', extraction['warnings'])

    def test_bad_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'receipt.json').write_text(json.dumps({'page_count': 2, 'pages': [{'page': 2}]}))
            extraction = {'warnings': [], 'status': 'failed'}
            sparse_ocr._read_results(folder, extraction, 2)
            self.assertEqual(extraction['error_code'], 'invalid_or_unavailable_worker_output')

    def test_max_pages_is_strictly_bounded(self):
        for value in (0, 1001, True):
            with self.assertRaises(ValueError):
                sparse_ocr.run('unused', [], 'unused', max_pages=value, trigger_policy='no_text')

    def test_refuses_changed_source_and_prior_text_under_no_text_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf = folder / 'input.pdf'
            pdf.write_bytes(b'%PDF-test')
            source = {'url': 'https://example.com/a.pdf', 'observed_at': '2026-10-28T00:00:00Z',
                      'format': 'pdf', 'sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
                      'byte_length': len(pdf.read_bytes())}
            first = {'schema_version': 'cre-document-text/v1', 'source': source,
                     'parser': {'name': '@firecrawl/anydoc'},
                     'extraction': {'status': 'ocr_needed', 'page_count': 1, 'ocr_needed_pages': [1]}}
            first_path = folder / 'first.json'
            first_path.write_text(json.dumps(first))
            second = {'schema_version': 'cre-document-text/v1', 'source': dict(source),
                      'parser': {'name': 'poppler-pdftotext'}, 'extraction': {
                          'status': 'failed', 'coverage': 'unknown',
                          'previous_attempt_sha256': hashlib.sha256(first_path.read_bytes()).hexdigest()}}
            second_path = folder / 'second.json'
            second_path.write_text(json.dumps(second))
            pdf.write_bytes(b'%PDF-other')
            with self.assertRaises(ValueError):
                sparse_ocr.run(pdf, [first_path, second_path], folder / 'out', max_pages=1, trigger_policy='no_text')
            pdf.write_bytes(b'%PDF-test')
            second['extraction']['text'] = 'short'
            second_path.write_text(json.dumps(second))
            with self.assertRaises(ValueError):
                sparse_ocr.run(pdf, [first_path, second_path], folder / 'out', max_pages=1, trigger_policy='no_text')
            self.assertFalse((folder / 'out').exists())

    def test_layout_is_bound_and_partial_layout_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            layout = 'level\ttext\n5\t地\n'.encode()
            (folder / 'page-1.tsv').write_bytes(layout)
            (folder / 'page-1.txt').write_text('地')
            (folder / 'receipt.json').write_text(json.dumps({'page_count': 1, 'pages': [{
                'page': 1, 'worker_exit': 1, 'layout_bytes': len(layout), 'text_bytes': 3}]}))
            extraction = {'warnings': [], 'status': 'failed'}
            sparse_ocr._read_results(folder, extraction, 1)
            self.assertEqual(extraction['pages'][0]['layout_sha256'], hashlib.sha256(layout).hexdigest())
            self.assertEqual(extraction['layout_omitted_pages'], 1)
            self.assertIn('layout_omitted', extraction['warnings'])
            self.assertEqual(extraction['text'], '地')

    def test_removal_requires_successful_empty_docker_listing(self):
        from subprocess import CompletedProcess
        for result, expected in ((CompletedProcess([], 0, b'', b''), True),
                                 (CompletedProcess([], 1, b'', b'error'), False),
                                 (CompletedProcess([], 0, b'container', b''), False)):
            with patch.object(sparse_ocr.subprocess, 'run', return_value=result):
                self.assertEqual(sparse_ocr._removed('a' * 64), expected)

    def test_whitespace_only_text_has_no_dangling_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); (folder / 'page-1.txt').write_text('  \n')
            (folder / 'receipt.json').write_text(json.dumps({'page_count': 1, 'pages': [
                {'page': 1, 'worker_exit': 0, 'text_bytes': 3}]}))
            extraction = {'warnings': [], 'status': 'failed'}
            sparse_ocr._read_results(folder, extraction, 1)
            self.assertEqual(extraction['status'], 'failed')
            self.assertEqual(extraction['pages'][0]['text_end_byte'], 0)
            self.assertIn('whitespace_only_text_discarded', extraction['warnings'])

    def test_layout_receipt_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'page-1.tsv').write_text('abc')
            (folder / 'receipt.json').write_text(json.dumps({'page_count': 1, 'pages': [
                {'page': 1, 'worker_exit': 0, 'layout_bytes': 2}]}))
            extraction = {'warnings': [], 'status': 'failed'}
            sparse_ocr._read_results(folder, extraction, 1)
            self.assertEqual(extraction['error_code'], 'invalid_or_unavailable_worker_output')

    def test_invalid_utf8_on_later_page_preserves_earlier_text(self):
        for invalid_file in ('text', 'layout'):
            with self.subTest(invalid_file=invalid_file), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / 'page-1.txt').write_text('valid 地')
                second = b'partial \xe2\x82' if invalid_file == 'text' else b'valid'
                (folder / 'page-2.txt').write_bytes(second)
                layout = b'partial \xe2\x82' if invalid_file == 'layout' else b'header\n'
                (folder / 'page-2.tsv').write_bytes(layout)
                (folder / 'receipt.json').write_text(json.dumps({'page_count': 2, 'pages': [
                    {'page': 1, 'worker_exit': 0, 'text_bytes': len('valid 地'.encode())},
                    {'page': 2, 'worker_exit': -9, 'error': 'page_timeout',
                     'text_bytes': len(second), 'layout_bytes': len(layout)}]}))
                extraction = {'warnings': [], 'status': 'failed'}
                sparse_ocr._read_results(folder, extraction, 2)
                self.assertEqual(extraction['status'], 'extracted')
                self.assertTrue(extraction['text'].startswith('valid 地'))
                self.assertEqual(extraction['page_count'], 2)
                self.assertIn('page_failure_or_partial_output', extraction['warnings'])
                self.assertIn('layout_omitted', extraction['warnings'])
                if invalid_file == 'text':
                    self.assertEqual(extraction['pages'][1]['discarded_text_bytes'], len(second))
                    self.assertEqual(extraction['text'], 'valid 地')
                else:
                    self.assertEqual(extraction['pages'][1]['layout_error'], 'invalid_utf8_layout_discarded')

    def test_escaped_layout_over_metadata_budget_is_omitted_without_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); layout = b'\n' * sparse_ocr.MAX_LAYOUT
            (folder / 'page-1.tsv').write_bytes(layout)
            (folder / 'page-1.txt').write_text('x')
            (folder / 'receipt.json').write_text(json.dumps({'page_count': 1, 'pages': [{
                'page': 1, 'worker_exit': 0, 'layout_bytes': len(layout), 'text_bytes': 1}]}))
            extraction = {'warnings': [], 'status': 'failed'}
            sparse_ocr._read_results(folder, extraction, 1)
            self.assertEqual(extraction['pages'][0]['layout_error'], 'metadata_limit')
            self.assertNotIn('layout_tsv', extraction['pages'][0])
            self.assertEqual(extraction['layout_omitted_pages'], 1)
            self.assertEqual(extraction['text'], 'x')

    def test_ambiguous_container_creation_keeps_cleanup_unconfirmed(self):
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); pdf = folder / 'input.pdf'; pdf.write_bytes(b'%PDF-test')
            source = {'url': 'https://example.com/a.pdf', 'observed_at': '2026-10-28T00:00:00Z',
                      'format': 'pdf', 'sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
                      'byte_length': len(pdf.read_bytes())}
            first = {'schema_version': 'cre-document-text/v1', 'source': source,
                     'parser': {'name': '@firecrawl/anydoc'},
                     'extraction': {'status': 'ocr_needed', 'page_count': 1, 'ocr_needed_pages': [1]}}
            first_path = folder / 'first.json'; first_path.write_text(json.dumps(first))
            second = {'schema_version': 'cre-document-text/v1', 'source': source,
                      'parser': {'name': 'poppler-pdftotext'}, 'extraction': {
                          'status': 'failed', 'coverage': 'unknown',
                          'previous_attempt_sha256': hashlib.sha256(first_path.read_bytes()).hexdigest()}}
            second_path = folder / 'second.json'; second_path.write_text(json.dumps(second))
            def create(command, **_kwargs):
                intent = json.loads((folder / 'out' / 'owner-intent.json').read_bytes())
                self.assertEqual(command[:2], ['docker', 'create'])
                self.assertEqual(command[3], intent['name'])
                self.assertIn('cre.ocr.owner=' + intent['nonce'], command)
                self.assertEqual(intent['source_sha256'], hashlib.sha256(pdf.read_bytes()).hexdigest())
                return CompletedProcess([], 1, b'', b'')
            with patch.object(sparse_ocr.subprocess, 'run', side_effect=create), \
                    patch.object(sparse_ocr.ocr, 'owned_id', return_value=None):
                result = sparse_ocr.run(pdf, [first_path, second_path], folder / 'out',
                                        max_pages=1, trigger_policy='no_text')
            self.assertEqual(result['extraction']['error_code'], 'container_identity_unconfirmed')
            self.assertIn('container_cleanup_unconfirmed', result['extraction']['warnings'])
            self.assertFalse((folder / 'out' / 'owner-settled.json').exists())

            def interrupted(command, **_kwargs):
                self.assertEqual(command[:2], ['docker', 'create'])
                self.assertTrue((folder / 'interrupted' / 'owner-intent.json').is_file())
                raise KeyboardInterrupt()
            with patch.object(sparse_ocr.subprocess, 'run', side_effect=interrupted), \
                    patch.object(sparse_ocr.ocr, 'owned_id', return_value=None), \
                    self.assertRaises(KeyboardInterrupt):
                sparse_ocr.run(pdf, [first_path, second_path], folder / 'interrupted',
                               max_pages=1, trigger_policy='no_text')
            self.assertFalse((folder / 'interrupted' / 'owner-settled.json').exists())

            def interrupted_after_create(command, **_kwargs):
                if command[:2] == ['docker', 'create']:
                    raise KeyboardInterrupt()
                self.assertEqual(command[:3], ['docker', 'rm', '-f'])
                return CompletedProcess([], 0, b'', b'')
            with patch.object(sparse_ocr.subprocess, 'run', side_effect=interrupted_after_create), \
                    patch.object(sparse_ocr.ocr, 'owned_id', return_value='a' * 64), \
                    patch.object(sparse_ocr, '_removed', return_value=True), \
                    self.assertRaises(KeyboardInterrupt):
                sparse_ocr.run(pdf, [first_path, second_path], folder / 'interrupted-owned',
                               max_pages=1, trigger_policy='no_text')
            self.assertTrue((folder / 'interrupted-owned' / 'owner-intent.json').exists())
            self.assertFalse((folder / 'interrupted-owned' / 'owner-settled.json').exists())

    def test_docker_contract_is_offline_and_pinned(self):
        args = sparse_ocr._arguments('/tmp/input', '/tmp/output', 'name', 'nonce', 17)
        for item in ('--pull=never', '--network=none', '--read-only', '--cpus=1', '--memory=3g', '--pids-limit=128'):
            self.assertIn(item, args)
        self.assertIn('fsize=134217728:134217728', args)
        self.assertIn(sparse_ocr.ocr.IMAGE, args)
        self.assertEqual(args[-1], '17')


if __name__ == '__main__':
    unittest.main()
