import copy
import os
import signal
import subprocess
from types import SimpleNamespace

import pytest

from source_evidence import doc_complement_process, events
from source_evidence.document_process import DocumentProcessCleanupUnknown
from test_doc_extract_process import artifact
from test_doc_fetch_process import receipt


def test_bad_anydoc_is_refused_before_process_or_input_read(tmp_path):
    first = artifact()
    first['extraction']['text_sha256'] = None
    with pytest.raises(ValueError):
        doc_complement_process.run(tmp_path / 'missing', first, receipt(), tmp_path / 'out',
                                   executable='/usr/bin/pdftotext', source_key='svn')
    assert not (tmp_path / 'out').exists()


def test_executable_symlink_and_nonexecutables_refused(tmp_path):
    target = tmp_path / 'parser'
    target.write_text('binary')
    target.chmod(0o700)
    link = tmp_path / 'link'
    link.symlink_to(target)
    assert doc_complement_process.executable_identity(str(target))
    with pytest.raises(ValueError):
        doc_complement_process.executable_identity(str(link))
    target.chmod(0o600)
    with pytest.raises(ValueError):
        doc_complement_process.executable_identity(str(target))
    with pytest.raises(ValueError):
        doc_complement_process.executable_identity(os.path.basename(target))


@pytest.mark.parametrize('failure', [OSError('failed'), KeyboardInterrupt()])
def test_spawn_distinguishes_definite_failure_and_uncertainty(monkeypatch, tmp_path, failure):
    source = tmp_path / 'input.pdf'
    source.write_bytes(b'pdf')
    source.chmod(0o600)
    first = artifact()
    actual_receipt = {**receipt(), 'body_sha256': events.sha256(b'pdf'), 'body_bytes': 3}
    monkeypatch.setattr(doc_complement_process.doc_extract_process, 'validate_artifact', lambda *a, **k: None)
    monkeypatch.setattr(doc_complement_process, 'executable_identity', lambda path: (1,))
    def spawn(*args, **kwargs):
        raise failure
    monkeypatch.setattr(doc_complement_process.subprocess, 'Popen', spawn)
    expected = ValueError if isinstance(failure, OSError) else DocumentProcessCleanupUnknown
    with pytest.raises(expected):
        doc_complement_process.run(source, first, actual_receipt, tmp_path / 'out',
                                   executable='/usr/bin/pdftotext', source_key='svn')


def test_registration_failure_attempts_cleanup_but_remains_unknown(monkeypatch, tmp_path):
    source = tmp_path / 'input.pdf'
    source.write_bytes(b'pdf')
    source.chmod(0o600)
    first = artifact()
    actual_receipt = {**receipt(), 'body_sha256': events.sha256(b'pdf'), 'body_bytes': 3}
    child = SimpleNamespace(pid=4321, poll=lambda: None, wait=lambda **kwargs: None)
    monkeypatch.setattr(doc_complement_process.doc_extract_process, 'validate_artifact', lambda *a, **k: None)
    monkeypatch.setattr(doc_complement_process, 'executable_identity', lambda path: (1,))
    monkeypatch.setattr(doc_complement_process.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(doc_complement_process.OwnedProcessGroup, 'from_spawn', lambda child: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(doc_complement_process.os, 'getpgid', lambda pid: pid)
    killed = []
    monkeypatch.setattr(doc_complement_process.os, 'killpg', lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(DocumentProcessCleanupUnknown):
        doc_complement_process.run(source, first, actual_receipt, tmp_path / 'out',
                                   executable='/usr/bin/pdftotext', source_key='svn')
    assert killed and killed[0][0] == child.pid


def test_retention_includes_failed_and_partial_complements():
    first = artifact()
    selection = {'original_retention': 'keep_unresolved_until_review', 'pdf_complement': {}}
    assert doc_complement_process.required(first, selection)
    assert doc_complement_process.retain_unresolved(first, None, selection)
    assert doc_complement_process.retain_unresolved(first, {'extraction': {'status': 'failed'}}, selection)
    assert doc_complement_process.retain_unresolved(first, {'extraction': {
        'status': 'extracted', 'warnings': ['converter_failed_partial_output']}}, selection)
    assert not doc_complement_process.retain_unresolved(first, {'extraction': {'status': 'extracted'}}, selection)


def test_series_blocks_until_complement_append_is_settled():
    import cre_checkpoint_series
    document = {'state': 'extraction_sealed', 'sealed': {'complement': {}},
                'append': {'kind': 'document_text', 'state': 'appended'},
                'complement_append': {'kind': 'document_text', 'state': 'outcome_unknown'}}
    attempt = {'source_evidence': {'state': 'appended'}, 'document_source_evidence': {'documents': [document]}}
    manifest = {'config': {'automatic_source_evidence': {'source_keys': ['svn'],
                'document_text_formats': {'svn': 'local_anydoc_public_listing_v1'}}},
                'sources': {'svn': {'attempts': [attempt]}}}
    assert cre_checkpoint_series.evidence_reconciliation_required(manifest)
    document['complement_append']['state'] = 'appended'
    assert not cre_checkpoint_series.evidence_reconciliation_required(manifest)


@pytest.mark.skipif(os.name != 'posix', reason='owned process groups require POSIX')
def test_real_timeout_settles_parser_and_descendant(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    source = tmp_path / 'input.pdf'
    source.write_bytes(b'pdf')
    source.chmod(0o600)
    pidfile = tmp_path / 'descendant.pid'
    executable = tmp_path / 'pdftotext'
    executable.write_text('#!/bin/sh\nif [ "$1" = "-v" ]; then echo "pdftotext version 1.2" >&2; exit 0; fi\n'
                          f'sleep 60 &\necho $! > "{pidfile}"\nwait\n')
    executable.chmod(0o700)
    monkeypatch.setattr(doc_complement_process, 'TIMEOUT_SECONDS', 1)
    try:
        with pytest.raises(ValueError, match='outcome unknown'):
            doc_complement_process.run(source, artifact(), receipt(), tmp_path / 'out',
                                       executable=str(executable), source_key='svn')
        pid = int(pidfile.read_text())
        result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False)
        assert result.returncode != 0 or not result.stdout.strip() or result.stdout.strip().startswith('Z')
    finally:
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text())
                command = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True, check=False)
                if command.stdout.strip() == 'sleep 60':
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_sparse_ocr_strategy_retention_and_series_barrier():
    from source_evidence import doc_sparse_ocr_process
    import cre_checkpoint_series
    first = artifact()
    complement = {'extraction': {'status': 'extracted', 'text': 'native text'}}
    selection = {'original_retention': 'keep_unresolved_until_review', 'pdf_complement': {},
                 'sparse_ocr': {'strategy': 'no_text', 'max_pages': 3}}
    assert not doc_sparse_ocr_process.required(first, complement, selection)
    no_text = copy.deepcopy(first)
    no_text['extraction']['text'] = None
    assert doc_sparse_ocr_process.required(no_text, {'extraction': {'status': 'failed', 'text': None}}, selection)
    selection['sparse_ocr']['strategy'] = 'all_pages'
    assert doc_sparse_ocr_process.required(first, complement, selection)
    partial = {'extraction': {'status': 'extracted', 'omitted_pages': 1, 'warnings': ['coverage_unknown', 'pages_omitted']}}
    assert doc_sparse_ocr_process.retain_unresolved(first, complement, partial, selection)
    document = {'state': 'extraction_sealed', 'sealed': {'complement': {}, 'ocr': {}},
                'append': {'kind': 'document_text', 'state': 'appended'},
                'complement_append': {'kind': 'document_text', 'state': 'appended'},
                'ocr_append': {'kind': 'document_text', 'state': 'outcome_unknown'}}
    attempt = {'source_evidence': {'state': 'appended'}, 'document_source_evidence': {'documents': [document]}}
    manifest = {'config': {'automatic_source_evidence': {'source_keys': ['svn'],
                'document_text_formats': {'svn': 'local_anydoc_public_listing_v1'}}},
                'sources': {'svn': {'attempts': [attempt]}}}
    assert cre_checkpoint_series.evidence_reconciliation_required(manifest)
    document['ocr_append']['state'] = 'appended'
    assert not cre_checkpoint_series.evidence_reconciliation_required(manifest)


def test_missing_ocr_output_keeps_container_settlement_unknown(monkeypatch, tmp_path):
    from source_evidence import doc_sparse_ocr_process
    source = tmp_path / 'input.pdf'
    source.write_bytes(b'pdf')
    source.chmod(0o600)
    actual_receipt = {**receipt(), 'body_sha256': events.sha256(b'pdf'), 'body_bytes': 3}
    child = SimpleNamespace(wait=lambda **kwargs: 0)
    monkeypatch.setattr(doc_sparse_ocr_process.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(doc_sparse_ocr_process.OwnedProcessGroup, 'from_spawn', lambda child: object())
    monkeypatch.setattr(doc_sparse_ocr_process, 'settle_owned_groups', lambda *a, **k: SimpleNamespace(settled=True))
    with pytest.raises(DocumentProcessCleanupUnknown):
        doc_sparse_ocr_process.run(source, artifact(), {}, actual_receipt, tmp_path / 'missing',
                                   source_key='svn', policy={'max_pages': 1, 'strategy': 'all_pages'}, prior_dir=tmp_path)


def test_ocr_completion_publication_failure_leaves_no_marker(monkeypatch, tmp_path):
    from source_evidence import doc_sparse_ocr_process, private_export
    tmp_path.chmod(0o700)
    identity = doc_sparse_ocr_process.doc_extract_process._directory_identity(tmp_path)
    monkeypatch.setattr(private_export.os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        doc_sparse_ocr_process._publish_completion(tmp_path, {'schema_version': 'test'}, identity)
    assert not (tmp_path / 'ocr-completion').exists()
