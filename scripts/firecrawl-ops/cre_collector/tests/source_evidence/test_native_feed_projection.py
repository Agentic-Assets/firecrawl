import json

from source_evidence import acquisition
from source_evidence import native_feed_projection as projection


def decode(value):
    return acquisition.decode_json(json.dumps(value).encode())


def test_hanley_withholds_nonpublic_and_preserves_positions_and_literals():
    raw = ('[' + ','.join('{"visibility":"Public","unknown":{"empty":"","flag":false,"x":1.0000000000000001}}' for _ in range(81))
           + ',{"visibility":"Private","hidden":123456789012345678901},null]')
    original, literals = acquisition.decode_json(raw.encode())
    retained, witnesses, receipt = projection.project(original, literals, source='hanley')
    assert len(retained) == 83 and retained[-2:] == [None, None]
    assert retained[0]['unknown']['flag'] is False and retained[0]['unknown']['empty'] == ''
    assert len(receipt['withheld']) == 2 and all(not key.startswith('/81/') for key in witnesses)
    encoded = acquisition.encode_json(retained, witnesses)
    assert '123456789012345678901' not in encoded and '1.0000000000000001' in encoded
    assert original[-2]['visibility'] == 'Private'


def test_srs_retains_flags_values_conflicts_and_unmapped_siblings():
    raw = b'{"properties":[{"apto_data":{"Hide_Sale_Price__c":true,"Sale_Price__c":123.00000000000000001},"other":[false,null,""]},{"apto_data":{"Hide_Sale_Price__c":false,"Sale_Price__c":null}},{"id":null}],"summary":{"unknown":"literal"}}'
    document, literals = acquisition.decode_json(raw)
    result, witnesses, receipt = projection.project(document, literals, source='srs')
    assert result == document and witnesses == literals and receipt['withheld'] == []
    recovered = acquisition.encode_json(result, witnesses)
    assert '123.00000000000000001' in recovered
    assert result['properties'][0]['apto_data']['Hide_Sale_Price__c'] is True
    assert result['properties'][1]['apto_data']['Hide_Sale_Price__c'] is False
    assert result['properties'][2]['id'] is None and result['summary']['unknown'] == 'literal'
