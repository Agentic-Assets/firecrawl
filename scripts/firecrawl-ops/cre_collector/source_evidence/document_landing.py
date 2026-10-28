"""Bounded, offline observations of an acquired HTML document landing page.

The caller retains the original bytes, receipt and source relationships. This
transform neither fetches references nor decides whether they are accessible.
"""
from hashlib import sha256
from html.parser import HTMLParser

MAX_HTML_BYTES = 2_000_000
MAX_TEXT_CHARS = 250_000
MAX_ITEMS = 1_000
MAX_VALUE_CHARS = 64_000


class _LandingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.text = []
        self.text_chars = 0
        self.omitted_text_chars = 0
        self.references = []
        self.metadata = []
        self.omitted_references = 0
        self.omitted_metadata = 0
        self.active_anchor = None
        self.anchor_open = False
        self.nested_anchors = 0
        self.table_structure_unpreserved = False
        self.scripts = []
        self.active_script = None
        self.omitted_scripts = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and not self.hidden:
            script_type = attrs.get('type', '') or ''
            if script_type.split(';', 1)[0].strip().lower() in ('application/json', 'application/ld+json'):
                if len(self.scripts) < MAX_ITEMS:
                    self.active_script = {'type': script_type, 'text': '', 'textComplete': True,
                                          'line': self.getpos()[0], 'column': self.getpos()[1]}
                    self.scripts.append(self.active_script)
                else:
                    self.omitted_scripts += 1
        if tag in ('script', 'style', 'template', 'noscript'):
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == 'table':
            self.table_structure_unpreserved = True
        if tag == 'a':
            if self.anchor_open:
                self.nested_anchors += 1
                if self.active_anchor is not None:
                    self.active_anchor['text_complete'] = False
            self.anchor_open = True
            self.active_anchor = None
        if tag == 'meta':
            if len(self.metadata) >= MAX_ITEMS:
                self.omitted_metadata += 1
            else:
                entry = {}
                for key in ('name', 'property', 'content', 'http-equiv'):
                    if key in attrs and attrs[key] is not None:
                        entry[key] = attrs[key] if len(attrs[key]) <= MAX_VALUE_CHARS else None
                        if entry[key] is None:
                            self.omitted_metadata += 1
                self.metadata.append(entry)
        ref_attribute = {'a': 'href', 'iframe': 'src', 'embed': 'src', 'object': 'data'}.get(tag)
        if ref_attribute and ref_attribute in attrs:
            raw = attrs[ref_attribute]
            if len(self.references) >= MAX_ITEMS:
                self.omitted_references += 1
                return
            entry = {'element': tag, 'attribute': ref_attribute, 'raw_reference': raw,
                     'reference_complete': isinstance(raw, str) and len(raw) <= MAX_VALUE_CHARS,
                     'line': self.getpos()[0], 'column': self.getpos()[1]}
            if not entry['reference_complete']:
                entry['raw_reference'] = None
            entry['labels'] = {k: (v if len(v) <= MAX_VALUE_CHARS else None)
                               for k, v in attrs.items()
                               if k in ('title', 'aria-label', 'download', 'type', 'rel') and v is not None}
            if tag == 'a':
                entry['text'] = ''
                entry['text_complete'] = True
                self.active_anchor = entry
            self.references.append(entry)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
                if tag == 'script':
                    if self.active_script is not None and not self.active_script['textComplete']:
                        self.active_script['text'] = None
                    self.active_script = None
            return
        if tag == 'a':
            self.anchor_open = False
            self.active_anchor = None
        if tag in ('p', 'div', 'li', 'br', 'h1', 'h2', 'h3', 'h4', 'tr'):
            self._text('\n')

    def _text(self, value):
        remaining = max(0, MAX_TEXT_CHARS - self.text_chars)
        part = value[:remaining]
        if part:
            self.text.append(part)
        self.text_chars += len(part)
        self.omitted_text_chars += len(value) - len(part)

    def handle_data(self, data):
        if self.hidden:
            if self.active_script is not None and self.hidden[-1] == 'script':
                remaining = max(0, MAX_VALUE_CHARS - len(self.active_script['text']))
                self.active_script['text'] += data[:remaining]
                if len(data) > remaining:
                    self.active_script['textComplete'] = False
            return
        self._text(data)
        anchor = self.active_anchor
        if anchor is not None:
            remaining = max(0, MAX_VALUE_CHARS - len(anchor['text']))
            anchor['text'] += data[:remaining]
            if len(data) > remaining:
                anchor['text_complete'] = False


def parse_document_landing(html_bytes, *, final_url):
    """Return an observation; final_url is the caller's receipt assertion.

    The digest binds the supplied bytes but does not validate the receipt. The
    result contains private source content and requires the caller's custody and
    credential policies. References are raw, unresolved, and unverified.
    """
    if not isinstance(html_bytes, bytes) or len(html_bytes) > MAX_HTML_BYTES:
        raise ValueError('HTML bytes exceed bound or are invalid')
    if not isinstance(final_url, str) or not final_url:
        raise ValueError('final_url is required')
    text = html_bytes.decode('utf-8', errors='replace')
    parser = _LandingParser()
    parser.feed(text)
    parser.close()
    return {'schema': 'cre-document-landing-observation/v1', 'finalUrl': final_url,
            'bodySha256': sha256(html_bytes).hexdigest(), 'bodyBytes': len(html_bytes),
            'decoding': 'utf-8-replacement', 'text': ''.join(parser.text),
            'references': parser.references, 'metadata': parser.metadata, 'scripts': parser.scripts,
            'omittedTextChars': parser.omitted_text_chars,
            'omittedReferences': parser.omitted_references,
            'omittedMetadata': parser.omitted_metadata, 'omittedScripts': parser.omitted_scripts,
            'nestedAnchors': parser.nested_anchors,
            'table_structure_unpreserved': parser.table_structure_unpreserved}


def page_context(html_bytes, *, final_url):
    """Make a source-page context from the acquired bytes for offline discovery."""
    observation = parse_document_landing(html_bytes, final_url=final_url)
    links = []
    for ref in observation['references']:
        link = {'href': ref['raw_reference'], 'referenceIntegrity': {
            'href': 'complete' if ref['reference_complete'] else 'truncated'},
            'element': ref['element'], 'attribute': ref['attribute'],
            'line': ref['line'], 'column': ref['column'], **ref['labels']}
        if 'text' in ref:
            link['text'] = ref['text'] if ref['text_complete'] else None
            link['textComplete'] = ref['text_complete']
        links.append(link)
    return {'schema': 'cre-source-page-context/v1', 'sourceUrl': final_url,
            'bodySha256': observation['bodySha256'], 'bodyBytes': observation['bodyBytes'],
            'decoding': observation['decoding'], 'text': observation['text'],
            'metadata': observation['metadata'], 'links': links, 'scripts': observation['scripts'],
            'table_structure_unpreserved': observation['table_structure_unpreserved'],
            'omissions': {'characters': observation['omittedTextChars'],
                          'references': observation['omittedReferences'],
                          'metadata': observation['omittedMetadata'],
                          'scripts': observation['omittedScripts'],
                          'nested_anchors': observation['nestedAnchors']}}


def is_html(body):
    """Recognize a narrow HTML signature, never infer HTML from a URL."""
    if not isinstance(body, bytes):
        return False
    prefix = body.removeprefix(b'\xef\xbb\xbf').lstrip()[:32].lower()
    return prefix.startswith((b'<!doctype html', b'<html'))


def protect_scripts(page, *, source_key):
    """Withhold JSON scripts that cannot pass the existing source policy."""
    import copy

    from source_evidence import acquisition, automatic, document_candidates

    result = copy.deepcopy(page)
    for script in result['scripts']:
        if script['textComplete'] is not True:
            continue
        try:
            decoded, _ = acquisition.decode_json(script['text'].encode('utf-8'), max_bytes=256 * 1024)
            automatic._refuse_credential_fields(decoded)
            document_candidates._safe_metadata(decoded, decoded_source_key=source_key)
        except (ValueError, UnicodeError):
            script['text'] = None
            script['textComplete'] = False
            script['withheldReason'] = 'invalid_or_source_policy'
    return result
