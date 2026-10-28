import hashlib

import pytest
from source_evidence.document_landing import (
    MAX_HTML_BYTES,
    MAX_VALUE_CHARS,
    parse_document_landing,
)


def test_landing_retains_text_metadata_and_unresolved_reference_provenance():
    body = b'''<html><head><title>Example</title><meta name="description" content="Available space">
    <script>hidden script content</script></head><body><h1>Example property</h1>
    <a href="../brochure.pdf" title="Brochure">Read brochure</a>
    <iframe src="/viewer?id=5"></iframe><object data="https://example.com/file.pdf"></object></body></html>'''
    result = parse_document_landing(body, final_url='https://example.com/landing')
    assert result['bodySha256'] == hashlib.sha256(body).hexdigest()
    assert 'Example property' in result['text']
    assert 'hidden script content' not in result['text']
    assert result['metadata'][0]['content'] == 'Available space'
    assert [(r['element'], r['raw_reference']) for r in result['references']] == [
        ('a', '../brochure.pdf'), ('iframe', '/viewer?id=5'), ('object', 'https://example.com/file.pdf')]
    assert result['references'][0]['text'] == 'Read brochure'
    assert result['references'][0]['line'] > 0
    assert result['omittedReferences'] == 0


def test_oversized_reference_is_not_exposed_as_fetchable_prefix():
    body = ('<a href="' + 'x' * (MAX_VALUE_CHARS + 1) + '">x</a>').encode()
    ref = parse_document_landing(body, final_url='https://example.com')['references'][0]
    assert ref['raw_reference'] is None
    assert not ref['reference_complete']


def test_oversized_body_refused():
    with pytest.raises(ValueError):
        parse_document_landing(b'x' * (MAX_HTML_BYTES + 1), final_url='https://example.com')


def test_unsupported_html_is_sealed_discoverable_and_tampering_is_refused(tmp_path):
    import json

    from source_evidence import document_candidates, document_landing, document_seal, events
    from test_document_seal import context_and_artifact

    context, artifact = context_and_artifact()
    body = b'<html><h1>Property</h1><a href="/flyer.pdf">Flyer</a></html>'
    digest = hashlib.sha256(body).hexdigest()
    context['receipt']['body_sha256'] = digest
    context['receipt']['body_bytes'] = len(body)
    artifact['source'].update(sha256=digest, byte_length=len(body))
    artifact['extraction'].update(status='failed', text=None, text_sha256=None, error_code='unsupported')
    landing = document_landing.page_context(body, final_url=context['receipt']['final_url'])
    tmp_path.chmod(0o700)
    directory = tmp_path / 'seal'
    marker = document_seal.write(directory, context, artifact, landing=landing)
    checked = document_seal.read(directory, expected_context=context, expected_marker=marker)
    assert checked['artifact'] == artifact
    assert checked['landing'] == landing
    enriched = json.loads((directory / 'artifact.json').read_bytes())
    plan = events.plan(enriched, source_key=context['source_key'])
    discovery = document_candidates.discover(plan)
    assert any(c['url'].endswith('/flyer.pdf') for c in discovery['candidates'])
    (directory / 'landing-page.json').write_bytes(b'{}')
    with pytest.raises(ValueError):
        document_seal.read(directory, expected_context=context, expected_marker=marker)


def test_landing_source_policy_rejects_credential_links():
    from source_evidence import document_landing, document_seal
    from test_document_seal import context_and_artifact

    context, artifact = context_and_artifact()
    body = b'<html><a href="https://example.com/flyer.pdf?token=secret">Flyer</a></html>'
    context['receipt'].update(body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
    artifact['source'].update(sha256=context['receipt']['body_sha256'], byte_length=len(body))
    artifact['extraction'].update(status='failed', text=None, text_sha256=None, error_code='unsupported')
    landing = document_landing.page_context(body, final_url=context['receipt']['final_url'])
    with pytest.raises(ValueError):
        document_seal.prepare(context, artifact, landing=landing)


def test_json_scripts_preserved_with_explicit_completeness_and_no_execution():
    from source_evidence.document_landing import page_context

    body = (b'<html><script type="application/ld+json">{"pdf_url":"/a.pdf"}</script>'
            b'<script>alert(1)</script><script type="application/json">' +
            b'x' * (MAX_VALUE_CHARS + 1) + b'</script></html>')
    page = page_context(body, final_url='https://example.com/')
    assert len(page['scripts']) == 2
    assert page['scripts'][0]['text'] == '{"pdf_url":"/a.pdf"}'
    assert page['scripts'][0]['textComplete'] is True
    assert page['scripts'][1]['textComplete'] is False
    assert 'alert(1)' not in page['text']


def test_credential_bearing_json_script_is_withheld_without_losing_page_text():
    from source_evidence.document_landing import page_context, protect_scripts

    body = b'<html><h1>Property</h1><script type="application/json">{"access_token":"secret"}</script></html>'
    page = protect_scripts(page_context(body, final_url='https://example.com/'), source_key='svn')
    assert 'Property' in page['text']
    assert page['scripts'][0]['text'] is None
    assert page['scripts'][0]['withheldReason'] == 'invalid_or_source_policy'


def test_malformed_deeply_nested_anchors_are_bounded_and_tables_are_marked():
    from source_evidence.document_landing import MAX_ITEMS, page_context

    body = (b'<html>' + b'<a href="/flyer.pdf">' * 50_000 + b'x' * 64_000 +
            b'</a>' * 50_000 + b'<table><tr><td>Value</td></tr></table></html>')
    page = page_context(body, final_url='https://example.com/')
    assert len(page['links']) == MAX_ITEMS
    assert page['omissions']['nested_anchors'] == 49_999
    assert page['omissions']['references'] == 50_000 - MAX_ITEMS
    assert all(link['text'] is None and not link['textComplete'] for link in page['links'])
    assert page['table_structure_unpreserved'] is True
    assert 'Value' in page['text']
