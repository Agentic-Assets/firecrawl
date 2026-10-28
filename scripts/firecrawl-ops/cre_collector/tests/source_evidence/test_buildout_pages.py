import base64
import copy
import hashlib
import json
from decimal import Decimal

import pytest

from source_evidence import acquisition, buildout_pages, buildout_response


def entry(body, *, source="faris-lee", page=0, sort="created_at asc, id asc"):
    plugin = buildout_pages.supported_sources()[source]
    scope = json.dumps([source, plugin, sort], ensure_ascii=False, separators=(",", ":")).encode()
    receipt = {"sourceKey": source, "page": page, "providerScopeSha256": hashlib.sha256(scope).hexdigest(),
               "inventorySort": sort, "capturedAt": "2026-10-28T15:00:00Z",
               "sourceObservedAt": "2026-10-28T14:59:59Z", "cacheDisposition": "live",
               "transport": "direct_json", "bodySha256": hashlib.sha256(body).hexdigest(), "bodyBytes": len(body)}
    value = {"schema": "cre-buildout-parsed-page/v1", "receipt": receipt,
             "responseBodyBase64": base64.b64encode(body).decode(), "provenance": buildout_pages.PROVENANCE}
    raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    return raw, f"page-{page}-{hashlib.sha256(raw).hexdigest()}.json"


def test_native_array_roundtrips_unknown_rows_and_literal_values():
    body = b'{"meta":{"total":9},"inventory":[{"id":1,"unknown":null},{"id":1},null,false,0.1234567890123456789]}'
    raw, name = entry(body)
    observation, reason = buildout_pages.decode_entry(raw, name, {"attempt": 1}, source_key="faris-lee")
    assert reason is None and len(observation["records"]) == 5
    assert observation["arrayPointer"] == "/inventory"
    assert observation["provenance"]["numbers"] == "javascript_number_precision"
    buildout_response.validate(observation)
    recovered = acquisition.encode_json(buildout_response.reconstruct(observation), observation["numericLiterals"])
    assert json.loads(recovered, parse_float=Decimal) == json.loads(body, parse_float=Decimal)
    assert buildout_response.validate(json.loads(json.dumps(observation, sort_keys=True))) is None


@pytest.mark.parametrize("body", [b'{"meta":{},"inventory":null}', b'null', b'[1,null,false]', b'"unmapped"'])
def test_whole_unmapped_json_does_not_claim_records(body):
    raw, name = entry(body)
    observation, reason = buildout_pages.decode_entry(raw, name, {}, source_key="faris-lee")
    assert reason is None and observation["arrayPointer"] is None and observation["records"] == []
    buildout_response.validate(observation)
    assert json.loads(acquisition.encode_json(buildout_response.reconstruct(observation), observation["numericLiterals"])) == json.loads(body)


def test_invalid_json_remains_diagnostic():
    raw, name = entry(b"invalid")
    assert buildout_pages.decode_entry(raw, name, {}, source_key="faris-lee") == (None, "invalid_json")


def test_scope_filename_body_and_source_are_bound():
    raw, name = entry(b'{"inventory":[]}')
    with pytest.raises(ValueError):
        buildout_pages.decode_entry(raw, name, {}, source_key="fortis-net-lease")
    with pytest.raises(ValueError):
        buildout_pages.decode_entry(raw + b" ", name, {}, source_key="faris-lee")
    value = json.loads(raw)
    value["receipt"]["providerScopeSha256"] = "0" * 64
    forged = json.dumps(value).encode()
    with pytest.raises(ValueError):
        buildout_pages.decode_entry(forged, f"page-0-{hashlib.sha256(forged).hexdigest()}.json", {}, source_key="faris-lee")


def test_clocks_and_receipt_shapes_fail_closed():
    raw, name = entry(b'{"inventory":[]}')
    original = json.loads(raw)["receipt"]
    for changes in ({"sourceObservedAt": "2026-10-28T15:00:01Z"}, {"page": True},
                    {"cacheDisposition": "new"}, {"transport": "invented"}, {"status": 200}):
        invalid = {**original, **changes}
        with pytest.raises(ValueError):
            buildout_response.receipt_checked(invalid)
    nullable = {**original, "sourceObservedAt": None, "transport": None, "cacheDisposition": "generation_cache"}
    buildout_response.receipt_checked(nullable)
    obs, _ = buildout_pages.decode_entry(raw, name, {}, source_key="faris-lee")
    altered = copy.deepcopy(obs)
    altered["arrayPointer"] = None
    with pytest.raises(ValueError):
        buildout_response.validate(altered)


def test_manual_observation_cannot_forge_source_or_scope():
    raw, name = entry(b'{"inventory":[]}')
    observation, _ = buildout_pages.decode_entry(raw, name, {}, source_key="faris-lee")
    for changes in ({"sourceKey": "unregistered-source"},
                    {"sourceKey": "fortis-net-lease"}, {"providerScopeSha256": "0" * 64}):
        forged = copy.deepcopy(observation)
        forged["receipt"].update(changes)
        with pytest.raises(ValueError):
            buildout_response.validate(forged)


def test_numeric_literal_map_requires_canonical_exceptional_values():
    raw, name = entry(b'{"inventory":[null]}')
    observation, _ = buildout_pages.decode_entry(raw, name, {}, source_key="faris-lee")
    observation["numericLiterals"] = {"/inventory/0": "0.1"}
    with pytest.raises(ValueError):
        buildout_response.validate(observation)


def test_collector_git_binding_requires_exact_sha1_length():
    raw, name = entry(b'{"inventory":[]}')
    with pytest.raises(ValueError):
        buildout_pages.decode_entry(raw, name, {"collectorGitSha": "a" * 64}, source_key="faris-lee")
