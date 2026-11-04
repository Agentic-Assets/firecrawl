"""Explicit local Docling JSON attempt; no acquisition, ingestion or cleanup of input."""
import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if __package__:
    from . import artifact
else:
    import artifact

import ocr
import owned_container
import sparse_ocr

WORKER = Path(__file__).with_name('worker.py')
TIMEOUT = 120


def run(pdf_path, prior_paths, output_dir, *, max_pages):
    if type(max_pages) is not int or not 1 <= max_pages <= 1000 or len(prior_paths) != 2:
        raise ValueError('invalid page limit or priors')
    pdf = ocr.read_limited(pdf_path, ocr.MAX_INPUT)
    first, second = [ocr.read_limited(p, ocr.MAX_ARTIFACT) for p in prior_paths]
    worker = WORKER.read_bytes()
    config = {'image': ocr.IMAGE, 'worker_sha256': ocr.digest(worker), 'network': 'none', 'device': 'cpu',
              'cpu': 1, 'memory_bytes': 3 * 1024**3, 'max_pages': max_pages, 'max_text_bytes': ocr.MAX_TEXT,
              'document_timeout_seconds': 90, 'client_timeout_seconds': TIMEOUT, 'ocr': False,
              'tables': True, 'table_mode': 'accurate', 'generated_images': False}
    attempt = artifact.initialize(pdf, first, second, config=config)
    extraction = attempt['extraction']
    out = Path(output_dir); out.mkdir(mode=0o700, parents=False, exist_ok=False)
    directory = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    nonce = uuid.uuid4().hex; name = 'cre-docling-json-' + nonce
    intent_sha = sparse_ocr._write_receipt(out, 'owner-intent.json', {
        'schema_version': 'cre-docling-json-owner/v1', 'name': name, 'nonce': nonce,
        'source_sha256': ocr.digest(pdf), 'prior_attempt_sha256': [ocr.digest(first), ocr.digest(second)],
        'worker_sha256': ocr.digest(worker), 'config_sha256': ocr.digest(json.dumps(config, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()), 'uid': os.getuid()})

    def settled(cid):
        sparse_ocr._write_receipt(out, 'owner-settled.json', {'schema_version': 'cre-docling-json-settlement/v1',
            'intent_sha256': intent_sha, 'container_id': cid, 'state': 'removed_verified'})

    with tempfile.TemporaryDirectory(prefix='cre-docling-json-') as tmp:
        root = Path(tmp); root.chmod(0o700)
        source = root / 'input.pdf'; source.write_bytes(pdf); source.chmod(0o600)
        worker_path = root / 'worker.py'; worker_path.write_bytes(worker); worker_path.chmod(0o600)
        results = root / 'output'; results.mkdir(mode=0o700)
        args = ['docker', 'create', '--name', name, '--label', f'cre.ocr.owner={nonce}', '--pull=never',
                '--network=none', '--read-only', '--cpus=1', '--memory=3g', '--pids-limit=128',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--ulimit', 'fsize=8388608:8388608',
                '--tmpfs=/tmp:rw,nosuid,size=512m', '--user', f'{os.getuid()}:{os.getgid()}',
                '--env', 'USER=docling', '--env', 'HF_HUB_OFFLINE=1', '--env', 'TRANSFORMERS_OFFLINE=1',
                '--env', 'HF_HOME=/opt/app-root/src/.cache/huggingface',
                '--mount', f'type=bind,src={source},dst=/input.pdf,readonly',
                '--mount', f'type=bind,src={worker_path},dst=/worker.py,readonly',
                '--mount', f'type=bind,src={results},dst=/output', '--entrypoint', 'python',
                ocr.IMAGE, '/worker.py', str(max_pages)]

        def read_results():
            if extraction.get('converter_exit_code') != 0:
                extraction['error_code'] = 'worker_failed_or_timeout'
                return
            try:
                receipt = json.loads(ocr.read_limited(results / 'receipt.json', 4096), object_pairs_hook=ocr.unique_object)
                if (set(receipt) != {'page_count', 'last_page', 'status'} or receipt['status'] not in ('success', 'partial_success')
                        or type(receipt['page_count']) is not int or type(receipt['last_page']) is not int
                        or receipt['last_page'] != min(max_pages, receipt['page_count'])):
                    raise ValueError('invalid receipt')
                data = ocr.read_limited(results / 'document.json', ocr.MAX_TEXT)
                converted = artifact.build(pdf, first, second, data, config=config, page_count=receipt['page_count'],
                                           last_page=receipt['last_page'], partial=receipt['status'] == 'partial_success')
                extraction.clear(); extraction.update(converted['extraction']); extraction.update(converter_exit_code=0, error_code=None)
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                extraction['error_code'] = 'invalid_or_oversized_output'

        owned_container.run(args, name, nonce, extraction, settled, sparse_ocr._removed, read_results, TIMEOUT)
    if extraction['error_code'] is None and extraction['status'] != 'extracted':
        extraction['error_code'] = 'output_unavailable'
    ocr.write_private(out / 'docling-json.json', attempt)
    return attempt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True); parser.add_argument('--anydoc-attempt', required=True)
    parser.add_argument('--poppler-attempt', required=True); parser.add_argument('--out-dir', required=True)
    parser.add_argument('--max-pages', required=True, type=int)
    args = parser.parse_args()
    result = run(args.input, [args.anydoc_attempt, args.poppler_attempt], args.out_dir, max_pages=args.max_pages)
    return 0 if result['extraction']['status'] == 'extracted' else 2


if __name__ == '__main__':
    sys.exit(main())
