"""Response observations preserve native values without claiming source identity."""

import copy
import json
from decimal import Decimal

import pytest
from source_evidence import acquisition, events


def receipt(raw, source="nai-global"):
    return {"sourceKey": source, "requestUrl": "https://example.org/graphql",
            "finalUrl": "https://example.org/graphql", "method": "POST", "status": 200,
            "startedAt": "2026-10-28T12:00:00Z", "finishedAt": "2026-10-28T12:00:01Z",
            "bodySha256": events.sha256(raw), "bodyBytes": len(raw)}


@pytest.mark.parametrize("source", ["nai-global", "berger-commercial"])
def test_reversible_many_native_values_and_envelope(source):
    records = [{"id": i, "nested": [None, False, {"unknown": "é"}]} for i in range(60)]
    records.extend([None, 0, "", copy.deepcopy(records[0])])
    body = {"data": {"a/b~c": [{"rows": records}], "unknown": [1, None]},
            "extensions": {"warning": None}}
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
    out = acquisition.convert(raw, receipt=receipt(raw, source), array_pointer="/data/a~1b~0c/0/rows")
    assert out["records"] == records
    assert acquisition.reconstruct(out) == body
    assert out["provenance"]["identity"] == out["provenance"]["visibility"] == "unverified"
    assert out["provenance"]["completeness"] == "unverified"
    assert out["provenance"]["bodyHashKind"] == "caller_supplied_response_body_bytes_not_wire"
    assert out["receipt"]["bodySha256"] == events.sha256(raw)


def test_root_array_and_placeholder_collision_are_reversible():
    body = [acquisition.PLACEHOLDER, None, [False, 0]]
    raw = json.dumps(body).encode()
    out = acquisition.convert(raw, receipt=receipt(raw), array_pointer="")
    assert acquisition.reconstruct(out) == body
    out["context"] = {"tampered": True}
    with pytest.raises(ValueError):
        acquisition.reconstruct(out)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"x":NaN}', b'\xff'])
def test_invalid_json_refused(raw):
    with pytest.raises(ValueError):
        acquisition.convert(raw, receipt=receipt(raw), array_pointer="")


def test_exceptional_numbers_preserve_value_and_type_without_object_collision():
    raw = (b'{"a/b~c":[1.234567890123456789,1e-4000,1e999,-0,-0.0,'
           + b'9' * 1100 + b',null,{"$cre_json_response_array":"records"}],"other":1.00000000000000000001}')
    result = acquisition.convert(raw, receipt=receipt(raw), array_pointer="/a~1b~0c")
    encoded = acquisition.reconstruct_json(result)
    expected = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    restored = json.loads(encoded, parse_float=Decimal, parse_int=Decimal)
    assert restored == expected
    assert restored["a/b~c"][3].is_signed()
    assert restored["a/b~c"][4].is_signed()
    assert restored["a/b~c"][6] is None
    assert restored["a/b~c"][7] == {"$cre_json_response_array": "records"}
    assert "/a~1b~0c/0" in result["numericLiterals"]
    with pytest.raises(ValueError, match="reconstruct_json"):
        acquisition.reconstruct(result)
    result["numericLiterals"]["/a~1b~0c/0"] = "1] true"
    with pytest.raises(ValueError, match="invalid numeric literal"):
        acquisition.reconstruct_json(result)


def test_ordinary_numbers_remain_ordinary():
    raw = b'{"data":[0.1,1e3]}'
    result = acquisition.convert(raw, receipt=receipt(raw), array_pointer="/data")
    assert result["numericLiterals"] == {}
    assert acquisition.reconstruct(result) == json.loads(raw)
    assert json.loads(acquisition.reconstruct_json(result)) == json.loads(raw)


@pytest.mark.parametrize("pointer", ["not-pointer", "/bad~2", "/data/01", "/missing", "/data/-"])
def test_invalid_pointers_refused(pointer):
    raw = b'{"data":[[]]}'
    with pytest.raises(ValueError):
        acquisition.convert(raw, receipt=receipt(raw), array_pointer=pointer)


def test_limits_and_receipt_tampering_refused():
    for body in ([None] * 10001, "x" * (events.MAX_ARTIFACT_BYTES + 1)):
        raw = json.dumps(body).encode()
        with pytest.raises(ValueError):
            acquisition.convert(raw, receipt=receipt(raw), array_pointer="")
    raw = b'{"records":[null]}'
    for change in ({"bodySha256": "0" * 64}, {"bodyBytes": len(raw) + 1}, {"status": 403},
                   {"method": "PATCH"}, {"requestUrl": "https://user:password@example.org"},
                   {"finalUrl": "https://example.org/?token=secret"},
                   {"startedAt": "2026-10-28T13:00:00Z"}):
        with pytest.raises(ValueError):
            acquisition.convert(raw, receipt={**receipt(raw), **change}, array_pointer="/records")


def test_response_plan_reconstructs_over_fifty_records_with_no_identity_requirement():
    raw = json.dumps({'data': [None, False, {'unknown': [None, {'units': 'per acre'}]}] * 20,
                      'other': {'error': None}}).encode()
    observation = acquisition.convert(raw, receipt=receipt(raw), array_pointer='/data')
    result = events.plan(observation, source_key='nai-global')
    events.validate_content_policy(result)
    assert len(result['events']) == 61
    restored = {**result['events'][0]['facts'][0]['value'],
                'records': [row['facts'][0]['value'] for row in result['events'][1:]]}
    assert acquisition.reconstruct(restored) == json.loads(raw)
    assert all(row['external_id'] is None for row in result['events'])
    assert all(row['source_url'] == receipt(raw)['finalUrl'] for row in result['events'])
    # Rehashing a tampered metadata assertion cannot bypass envelope derivation.
    restored['provenance']['identity'] = 'verified'
    with pytest.raises(ValueError):
        events.plan(restored, source_key='nai-global')


def test_response_plan_retains_numeric_literals_and_rejects_tampered_map():
    raw = b'{"rows":[{"unknown":1.0000000000000001},null],"context":-0}'
    observation = acquisition.convert(raw, receipt=receipt(raw), array_pointer='/rows')
    value = events.plan(observation, source_key='nai-global')
    events.validate_plan(value)
    assert value['events'][0]['facts'][0]['value']['numericLiterals'] == {
        '/rows/0/unknown': '1.0000000000000001', '/context': '-0'}
    assert value['events'][1]['facts'][0]['value']['unknown'] is None
    observation['numericLiterals']['/rows/0/unknown'] = '123'
    with pytest.raises(ValueError):
        events.plan(observation, source_key='nai-global')


def test_response_plan_scope_and_source_policy_remain_enforced():
    raw = b'{"rows":[{"missingAddress":true,"url":"https://example.org"}]}'
    observation = acquisition.convert(raw, receipt=receipt(raw), array_pointer='/rows')
    with pytest.raises(ValueError):
        events.plan(observation, source_key='cbre')
    with pytest.raises(ValueError):
        events.plan(observation, source_key='nai-global', external_id='invented')
    with pytest.raises(ValueError):
        events.plan(observation, source_key='nai-global', observed_at='2026-10-28T13:00:00Z')
    for source in ('jll', 'avison-young'):
        protected = acquisition.convert(raw, receipt=receipt(raw, source), array_pointer='/rows')
        plan = events.plan(protected, source_key=source)
        with pytest.raises(ValueError):
            events.validate_content_policy(plan)


def test_response_cli_roundtrip_is_offline_and_private(tmp_path, monkeypatch, capsys):
    from source_evidence import cli
    raw = b'{"data":[{"amount":1.0000000000000001,"extra":false},null]}'
    source = tmp_path / 'response.json'; source.write_bytes(raw)
    observed = tmp_path / 'receipt.json'; observed.write_bytes(events.encoded(receipt(raw)))
    converted = tmp_path / 'observation.json'
    output = tmp_path / 'plan.json'
    monkeypatch.setattr(cli, 'run_psql', lambda *_a, **_k: pytest.fail('unexpected DB call'))
    cli.main(['convert-response', '--body', str(source), '--receipt', str(observed),
              '--array-pointer', '/data', '--out', str(converted)])
    cli.main(['plan', '--artifact', str(converted), '--source-key', 'nai-global', '--out', str(output)])
    planned = json.loads(output.read_bytes())
    events.validate_plan(planned)
    assert len(planned['events']) == 3
    assert '1.0000000000000001' not in capsys.readouterr().out
    assert converted.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        cli.read_json(source, 1000)


def test_capture_context_preserves_query_scope_and_is_scanned_and_bounded():
    raw = b'{"rows":[null]}'
    context = {'request': {'query': 'PublicPosts', 'variables': {'offset': 0, 'limit': 100}},
               'scope': 'one public page; inventory completeness unverified'}
    out = acquisition.convert(raw, receipt=receipt(raw), array_pointer='/rows', capture_context=context)
    acquisition.validate(out)
    value = events.plan(out, source_key='nai-global')
    events.validate_content_policy(value)
    assert value['events'][0]['facts'][0]['value']['captureContext'] == context
    out['captureContext']['sourceKey'] = 'jll'
    value = events.plan(out, source_key='nai-global')
    with pytest.raises(ValueError, match='protected'):
        events.validate_content_policy(value)
    with pytest.raises(ValueError, match='context exceeds'):
        acquisition.convert(raw, receipt=receipt(raw), array_pointer='/rows', capture_context='x' * 65537)
