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
