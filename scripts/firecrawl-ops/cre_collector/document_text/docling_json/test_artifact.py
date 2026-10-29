import hashlib
import json
import unittest

import artifact


def prior(pdf):
    source = {'url': 'https://example.com/a.pdf', 'observed_at': '2026-10-28T00:00:00Z', 'format': 'pdf',
              'sha256': hashlib.sha256(pdf).hexdigest(), 'byte_length': len(pdf)}
    first = json.dumps({'schema_version': 'cre-document-text/v1', 'source': source,
                        'parser': {'name': '@firecrawl/anydoc', 'version': '0.2.4', 'config': {'ocr': 'disabled', 'max_bytes': 104857600, 'max_text_bytes': 4194304, 'timeout_ms': 60000}}, 'extraction': {'status': 'ocr_needed'}}).encode()
    second = json.dumps({'schema_version': 'cre-document-text/v1', 'source': source,
                         'parser': {'name': 'poppler-pdftotext', 'version': '26.10.0', 'config': {'executable': '/usr/bin/pdftotext', 'ocr': False, 'timeout_seconds': 120, 'max_output_file_bytes': 4194304, 'max_text_bytes': 4194304}}, 'extraction': {'status': 'failed', 'error_code': 'empty_output', 'text': None, 'text_sha256': None, 'text_format': 'plain', 'relationship': 'complement_of', 'warnings': [],
                         'coverage': 'unknown', 'previous_attempt_sha256': hashlib.sha256(first).hexdigest()}}).encode()
    return first, second


def native():
    return {'schema_name': 'DoclingDocument', 'version': '1.10.0', 'name': 'test', 'body': {'self_ref': '#/body', 'children': []},
            'furniture': {'self_ref': '#/furniture', 'children': []}, 'pages': {}, 'groups': [], 'texts': [], 'pictures': [], 'tables': []}


class ArtifactTest(unittest.TestCase):
    def test_native_bytes_and_cells_are_preserved(self):
        pdf = b'%PDF-test'; first, second = prior(pdf)
        payload = json.dumps({**native(), 'tables': [{'self_ref': '#/tables/0', 'children': [], 'cells': [{'value': i, 'bbox': [i, 1, 2, 3]} for i in range(80)]}],
                              'unmapped': {'value': False, 'relation': [None, 'x']}}, ensure_ascii=False).encode()
        result = artifact.build(pdf, first, second, payload, config={'mode': 'test'}, page_count=8, first_page=5, last_page=8)
        extraction = result['extraction']
        self.assertEqual(extraction['text'].encode(), payload)
        self.assertEqual(extraction['prior_attempt_sha256'], [hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest()])
        self.assertEqual(extraction['omitted_pages'], 4)
        self.assertEqual(len(json.loads(extraction['text'])['tables'][0]['cells']), 80)

    def test_prior_parser_and_state_must_match_supported_normal_contract(self):
        pdf = b'%PDF-test'; first, second = prior(pdf)
        original = json.loads(second)
        for field, value in [('version', 'nonsense'), ('config', {'ocr': True, 'mode': 'untrusted'})]:
            bad = json.loads(second); bad['parser'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                artifact.initialize(pdf, first, json.dumps(bad).encode(), config={})
        original['extraction']['text'] = 'unexpected'
        with self.assertRaises(ValueError):
            artifact.initialize(pdf, first, json.dumps(original).encode(), config={})

    def test_native_shape_and_pages_are_checked_without_dropping_unknowns(self):
        pdf = b'%PDF-test'; first, second = prior(pdf)
        for change in ({'schema_name': 'other'}, {'pages': {'1': {'page_no': 2}}}, {'tables': {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                artifact.build(pdf, first, second, json.dumps({**native(), **change}).encode(), config={}, page_count=1, last_page=1)
        result = artifact.build(pdf, first, second, json.dumps({**native(), 'unknown': {'x': 1}}).encode(), config={}, page_count=1, last_page=1)
        self.assertEqual(json.loads(result['extraction']['text'])['unknown'], {'x': 1})
        self.assertEqual(result['extraction']['unrepresented_requested_pages'], 1)

    def test_bad_roots_dangling_children_cycles_and_limits(self):
        pdf = b'%PDF-test'; first, second = prior(pdf)
        bad = native(); bad['body']['self_ref'] = '#/wrong'
        with self.assertRaises(ValueError):
            artifact.build(pdf, first, second, json.dumps(bad).encode(), config={}, page_count=1, last_page=1)
        bad = native(); bad['body']['children'] = [{'$ref': '#/texts/9999'}]
        with self.assertRaises(ValueError):
            artifact.build(pdf, first, second, json.dumps(bad).encode(), config={}, page_count=1, last_page=1)
        bad = native(); bad['body']['children'] = [{'$ref': '#/body'}]
        with self.assertRaises(ValueError):
            artifact.build(pdf, first, second, json.dumps(bad).encode(), config={}, page_count=1, last_page=1)
        bad = native(); bad['texts'] = [None] * 50000
        with self.assertRaises(ValueError):
            artifact.validate_core_references(bad)
        bad = native(); bad['body']['children'] = [{'$ref': '#/furniture'}] * 100001
        with self.assertRaises(ValueError):
            artifact.validate_core_references(bad)

    def test_refuses_tampering_and_malformed_or_oversized_content(self):
        pdf = b'%PDF-test'; first, second = prior(pdf)
        for data in (b'{', b'{"a":1,"a":2}', b'[]', b'{}', b' ' * (artifact.MAX_TEXT + 1)):
            with self.subTest(length=len(data)), self.assertRaises((ValueError, TypeError)):
                artifact.build(pdf, first, second, data, config={}, page_count=1, last_page=1)
        with self.assertRaises(ValueError):
            artifact.build(b'other', first, second, b'{}', config={}, page_count=1, last_page=1)
        with self.assertRaises(ValueError):
            artifact.build(pdf, first, second, json.dumps(native()).encode(), config={}, page_count=1, last_page=2)

class AdapterTest(unittest.TestCase):
    def test_partial_output_and_verified_settlement(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import adapter
        pdf = b'%PDF-test'; first, second = prior(pdf)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in [('source', pdf), ('first', first), ('second', second)]:
                (root / name).write_bytes(data)

            def fake_run(args, name, nonce, extraction, settled, removed_check, read_results, timeout):
                mount = next(x for x in args if x.startswith('type=bind,src=') and x.endswith(',dst=/output'))
                output = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/output'))
                (output / 'document.json').write_bytes(json.dumps({**native(), 'extra': False}).encode())
                (output / 'receipt.json').write_text(json.dumps({'page_count': 3, 'last_page': 2, 'status': 'partial_success'}))
                extraction['converter_exit_code'] = 0
                settled('a' * 64)
                read_results()

            with patch.object(adapter.owned_container, 'run', side_effect=fake_run):
                result = adapter.run(root / 'source', [root / 'first', root / 'second'], root / 'out', max_pages=2)
            self.assertEqual(result['extraction']['status'], 'extracted')
            self.assertEqual(result['extraction']['omitted_pages'], 1)
            self.assertIn('converter_partial', result['extraction']['warnings'])
            self.assertTrue((root / 'out' / 'owner-settled.json').exists())
            self.assertTrue((root / 'source').exists())

    def test_unknown_cleanup_is_failed_and_does_not_invent_pages(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import adapter
        pdf = b'%PDF-test'; first, second = prior(pdf)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in [('source', pdf), ('first', first), ('second', second)]:
                (root / name).write_bytes(data)
            def fail(args, name, nonce, extraction, settled, removed_check, read_results, timeout):
                extraction['warnings'].append('container_cleanup_unconfirmed')
            with patch.object(adapter.owned_container, 'run', side_effect=fail):
                result = adapter.run(root / 'source', [root / 'first', root / 'second'], root / 'out', max_pages=2)
            self.assertEqual(result['extraction']['status'], 'failed')
            self.assertIsNone(result['extraction']['page_count'])
            self.assertFalse((root / 'out' / 'owner-settled.json').exists())


if __name__ == '__main__':
    unittest.main()
