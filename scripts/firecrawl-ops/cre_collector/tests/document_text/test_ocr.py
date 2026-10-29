import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / 'document_text/ocr.py'
spec = importlib.util.spec_from_file_location('cre_document_ocr', PATH)
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)


def fixture(raw=b'%PDF-fixture'):
    first = {'schema_version': 'cre-document-text/v1', 'source': {'url': 'https://example.test/file.pdf',
             'observed_at': '2026-10-28T12:00:00Z', 'format': 'pdf', 'byte_length': len(raw), 'sha256': ocr.digest(raw)},
             'parser': {'name': '@firecrawl/anydoc'}, 'extraction': {'status': 'ocr_needed', 'page_count': 2, 'ocr_needed_pages': [1]}}
    return ocr.make_attempt(first, json.dumps(first).encode(), raw)


def test_attempt_requires_matched_file():
    with pytest.raises(ValueError):
        ocr.make_attempt({'schema_version': 'other'}, b'{}', b'x')
    result = fixture()
    assert result['extraction']['coverage'] == 'unknown'
    assert result['extraction']['previous_attempt_sha256']
    args = ocr.docker_arguments('/private/input.pdf', '/private/output', 'name', 'nonce')
    for flag in ('--pull=never', '--network=none', '--read-only', '--ulimit', 'fsize=8388608:8388608'):
        assert flag in args
    assert any('dst=/output' in arg for arg in args)


def test_timeout_removes_only_inspected_owned_container(monkeypatch):
    container = 'a' * 64
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'create':
            return subprocess.CompletedProcess(args, 0, stdout=(container + '\n').encode())
        if args[1] == 'inspect':
            label = calls[0][calls[0].index('--label') + 1].split('=', 1)[1]
            return subprocess.CompletedProcess(args, 0, stdout=f'{container} {label}\n'.encode())
        if args[1] == 'start':
            raise subprocess.TimeoutExpired(args, 120)
        if args[1] == 'rm':
            return subprocess.CompletedProcess(args, 0)
        pytest.fail('unexpected command')
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert(b'%PDF-fixture', fixture())
    assert result['extraction']['error_code'] == 'timeout'
    assert result['extraction']['text'] is None
    assert [args for args in calls if args[1] == 'rm'] == [['docker', 'rm', '-f', container]]


def test_unverified_identity_is_not_removed(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'create':
            return subprocess.CompletedProcess(args, 0, stdout=(('a' * 64) + '\n').encode())
        if args[1] == 'inspect':
            return subprocess.CompletedProcess(args, 0, stdout=(('b' * 64) + ' wrong-owner\n').encode())
        pytest.fail('unexpected command')
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert(b'%PDF-fixture', fixture())
    assert result['extraction']['error_code'] == 'container_identity_unconfirmed'
    assert not any(args[1] == 'rm' for args in calls)


def test_success_keeps_warning_and_utf8_digest(monkeypatch):
    container = 'a' * 64
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'create':
            return subprocess.CompletedProcess(args, 0, stdout=(container + '\n').encode())
        if args[1] == 'inspect':
            label = calls[0][calls[0].index('--label') + 1].split('=', 1)[1]
            return subprocess.CompletedProcess(args, 0, stdout=f'{container} {label}\n'.encode())
        if args[1] == 'start':
            output_mount = next(x for x in calls[0] if 'dst=/output' in x)
            output_dir = output_mount.split('src=', 1)[1].split(',dst=', 1)[0]
            Path(output_dir, 'input.md').write_text('café', encoding='utf-8')
            kwargs['stdout'].write(b'ERROR OSD failed')
            return subprocess.CompletedProcess(args, 0)
        if args[1] == 'rm':
            return subprocess.CompletedProcess(args, 0)
        pytest.fail('unexpected command')
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert(b'%PDF-fixture', fixture())
    assert result['extraction']['status'] == 'extracted'
    assert result['extraction']['text_sha256'] == ocr.digest('café'.encode())
    assert result['extraction']['coverage'] == 'unknown'
    assert result['extraction']['warnings'] == ['tesseract_osd_failed', 'docling_logged_error']


@pytest.mark.parametrize('text,failed', [
    ('<!-- image -->\n\n<!-- image -->\n', True),
    (' \t<!-- image -->\r\n', True),
    ('<!-- image -->\n图', False),
    ('<!-- image -->\n1', False),
    ('<!-- image --> caption', False),
    ('<!-- image -->\n<!-- other -->', False),
])
def test_docling_image_placeholders_only_are_failed(monkeypatch, text, failed):
    from source_evidence import events
    container = 'a' * 64
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'create':
            return subprocess.CompletedProcess(args, 0, stdout=(container + '\n').encode())
        if args[1] == 'inspect':
            label = calls[0][calls[0].index('--label') + 1].split('=', 1)[1]
            return subprocess.CompletedProcess(args, 0, stdout=f'{container} {label}\n'.encode())
        if args[1] == 'start':
            mount = next(x for x in calls[0] if 'dst=/output' in x)
            Path(mount.split('src=', 1)[1].split(',dst=', 1)[0], 'input.md').write_text(text, encoding='utf-8')
            kwargs['stdout'].write(b'ERROR OSD failed')
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert(b'%PDF-fixture', fixture())
    extraction = result['extraction']
    assert extraction['coverage'] == 'unknown'
    assert extraction['warnings'] == ['tesseract_osd_failed', 'docling_logged_error']
    if failed:
        assert extraction['status'] == 'failed'
        assert extraction['error_code'] == 'placeholder_only_output'
        assert extraction['text'] is None and extraction['text_sha256'] is None
        plan = events.plan(result, source_key='svn')
        events.validate_content_policy(plan)
    else:
        assert extraction['status'] == 'extracted'
        assert extraction['text'] == text
        assert extraction['text_sha256'] == ocr.digest(text.encode())


def test_unsupported_pdf_only_and_original_hash_unchanged():
    raw = b'%PDF-fixture'
    first = {'schema_version': 'cre-document-text/v1', 'source': {'url': 'https://example.test/file.pdf',
             'observed_at': '2026-10-28T12:00:00.123456Z', 'format': 'pdf', 'byte_length': len(raw), 'sha256': ocr.digest(raw)},
             'parser': {'name': '@firecrawl/anydoc'}, 'extraction': {'status': 'failed', 'error_code': 'unsupported'}}
    before = json.dumps(first).encode()
    result = ocr.make_attempt(first, before, raw, 'poppler', '26.10.0')
    assert json.dumps(first).encode() == before
    assert result['source']['observed_at'] == first['source']['observed_at']
    assert result['extraction']['previous_attempt_sha256'] == ocr.digest(before)
    assert result['parser']['name'] == 'poppler-pdftotext'
    assert result['extraction']['text_format'] == 'plain'
    assert result['extraction']['coverage'] == 'unknown'
    for code in ('unknown', 'malformed', 'encrypted', 'timeout'):
        first['extraction']['error_code'] = code
        with pytest.raises(ValueError):
            ocr.make_attempt(first, before, raw)
    first['extraction']['error_code'] = 'unsupported'
    with pytest.raises(ValueError):
        ocr.make_attempt(first, before, b'%PDF-different')
    first['source']['format'] = 'html'
    with pytest.raises(ValueError):
        ocr.make_attempt(first, before, raw)


def test_poppler_success_retains_diagnostic_and_hash(monkeypatch):
    def run(args, **kwargs):
        Path(args[-1]).write_bytes(b'partial text')
        kwargs['stderr'].write(b'private diagnostic')
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert_poppler(b'%PDF-fixture', fixture())
    assert result['extraction']['status'] == 'extracted'
    assert result['extraction']['text_sha256'] == ocr.digest(b'partial text')
    assert result['extraction']['warnings'] == ['poppler_logged_diagnostic']
    assert result['extraction']['coverage'] == 'unknown'


def test_pdf_complement_preserves_success_and_binds_exact_first_bytes():
    raw = b'%PDF-fixture'
    first = {'schema_version': 'cre-document-text/v1', 'source': {'url': 'https://example.test/a.pdf',
             'observed_at': '2026-10-28T12:00:00Z', 'format': 'pdf', 'byte_length': len(raw), 'sha256': ocr.digest(raw)},
             'parser': {'name': '@firecrawl/anydoc'}, 'extraction': {'status': 'extracted', 'text': 'original',
                       'text_sha256': ocr.digest(b'original'), 'coverage': 'unknown'}}
    first_raw = json.dumps(first, indent=2).encode()
    with pytest.raises(ValueError):
        ocr.make_attempt(first, first_raw, raw, 'poppler', '26.10.0')
    attempt = ocr.make_attempt(first, first_raw, raw, 'poppler', '26.10.0', relationship='complement')
    assert attempt['extraction']['relationship'] == 'complement_of'
    assert attempt['extraction']['previous_attempt_sha256'] == ocr.digest(first_raw)
    assert first['extraction']['text'] == 'original'
    with pytest.raises(ValueError):
        ocr.make_attempt(first, b'{}', raw, 'poppler', '26.10.0', relationship='complement')
    with pytest.raises(ValueError):
        ocr.make_attempt(first, first_raw, raw, 'docling', relationship='complement')
    first['extraction']['text_sha256'] = None
    with pytest.raises(ValueError):
        ocr.make_attempt(first, json.dumps(first).encode(), raw, 'poppler', '26.10.0', relationship='complement')


def test_poppler_empty_output_is_failure(monkeypatch):
    def run(args, **kwargs):
        Path(args[-1]).write_bytes(b'')
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    assert ocr.convert_poppler(b'%PDF-fixture', fixture())['extraction']['error_code'] == 'empty_output'


def test_docling_nonzero_with_text_preserves_partial_output(monkeypatch):
    container = 'a' * 64
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'create':
            return subprocess.CompletedProcess(args, 0, stdout=(container + '\n').encode())
        if args[1] == 'inspect':
            label = calls[0][calls[0].index('--label') + 1].split('=', 1)[1]
            return subprocess.CompletedProcess(args, 0, stdout=f'{container} {label}\n'.encode())
        if args[1] == 'start':
            mount = next(x for x in calls[0] if 'dst=/output' in x)
            Path(mount.split('src=', 1)[1].split(',dst=', 1)[0], 'input.md').write_text('partial')
            return subprocess.CompletedProcess(args, 1)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert(b'%PDF-fixture', fixture())
    assert result['extraction']['status'] == 'extracted'
    assert 'converter_failed_partial_output' in result['extraction']['warnings']
    assert result['extraction']['converter_exit_code'] == 1
    assert result['extraction']['coverage'] == 'unknown'


def test_poppler_nonzero_partial_output_is_not_discarded(monkeypatch):
    def run(args, **kwargs):
        Path(args[-1]).write_bytes(b'partial')
        return subprocess.CompletedProcess(args, 1)
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    result = ocr.convert_poppler(b'%PDF-fixture', fixture())
    assert result['extraction']['status'] == 'extracted'
    assert result['extraction']['warnings'] == ['converter_failed_partial_output']
    assert result['extraction']['converter_exit_code'] == 1
    assert result['extraction']['coverage'] == 'unknown'


def test_plain_attempt_metadata_survives_evidence_reconstruction(tmp_path):
    from source_evidence import document_export, events
    raw = b'%PDF-fixture'
    first = {'schema_version': 'cre-document-text/v1', 'source': {'url': 'https://example.test/a.pdf',
             'observed_at': '2026-10-28T12:00:00Z', 'format': 'pdf', 'byte_length': len(raw), 'sha256': ocr.digest(raw)},
             'parser': {'name': '@firecrawl/anydoc'}, 'extraction': {'status': 'failed', 'error_code': 'unsupported'}}
    artifact = ocr.make_attempt(first, json.dumps(first).encode(), raw, 'poppler', '26.10.0')
    artifact['extraction'].update(status='extracted', text='partial', text_sha256=ocr.digest(b'partial'),
                                  warnings=['converter_failed_partial_output'], converter_exit_code=1)
    rows = events.plan(artifact, source_key='svn')['events']
    text, metadata = document_export.reconstruct(rows)
    assert text == 'partial'
    extraction = metadata['document']['extraction']
    assert extraction['text_format'] == 'plain' and extraction['converter_exit_code'] == 1
    assert extraction['warnings'] == ['converter_failed_partial_output']
    assert metadata['coverage'] == 'unknown'
