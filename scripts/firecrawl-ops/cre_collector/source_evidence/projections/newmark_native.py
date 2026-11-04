"""Withhold only credential-bearing Newmark native row document references.

Receipts describe a local projection and do not authenticate the source or
prove what was present at a null pointer. Original bytes remain private.
"""
import re
from urllib.parse import urlsplit

from buildout_observation_artifact import sensitive_string

REASON = 'credential-bearing-document-reference'
POINTER = re.compile(r'/data/(?:0|[1-9][0-9]*)/secureDocumentsUrl\Z')


def withheld_reference(value):
    if not isinstance(value, str) or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port  # Force validation even though the reference is never fetched.
        return (parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
                and (port is None or 0 <= port <= 65535) and sensitive_string(value))
    except ValueError:
        return False


def project(document):
    """Modify the caller-owned copy and return exact pointer witnesses."""
    rows = document.get('data') if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return []
    withheld = []
    for index, row in enumerate(rows):
        if isinstance(row, dict) and withheld_reference(row.get('secureDocumentsUrl')):
            row['secureDocumentsUrl'] = None
            withheld.append({'pointer': f'/data/{index}/secureDocumentsUrl', 'reason': REASON})
    return withheld
