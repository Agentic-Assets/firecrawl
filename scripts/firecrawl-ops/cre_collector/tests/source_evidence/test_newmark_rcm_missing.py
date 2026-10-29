"""Unqualified source observations retain bounded generic context explicitly."""
import copy

import pytest
from test_newmark_rcm_projection import artifact

from source_evidence import collector, events
from source_evidence.projections import newmark_rcm


def missing_artifact():
    data = artifact(13)
    rows = data['sources'][0]['retainedObservations']
    for row in rows[8:]:
        row.pop('sourceObservation')
        row['scrapedDoc']['markdown'] = 'private source text'
        row['pageContext']['scripts'] = [{'text':'unverified script', 'textComplete':False}]
        row['pageContext']['headings'] = [{'text':'Public heading'}]
    return data


def test_missing_literal_retains_all_rows_with_explicit_content_omissions():
    original = missing_artifact()
    projected, literals, receipt = newmark_rcm.prepare(events.encoded(original),source_key='newmark')
    rows = projected['sources'][0]['retainedObservations']
    assert len(rows) == 13
    for row in rows[8:]:
        assert row['scrapedDoc']['rawHtml'] is None and row['scrapedDoc']['markdown'] is None
        assert row['pageContext']['scripts'][0]['text'] is None
        assert row['pageContext']['headings'] == [{'text':'Public heading'}]
        assert row['omissions']['newmarkRcmProjection'] == 'unverified_content_omitted'
    newmark_rcm.validate(projected,receipt,source_key='newmark',numeric_literals=literals)
    obs = collector.convert(projected,source_key='newmark',numeric_literals=literals,projection=receipt)
    plan = events.plan(obs,source_key='newmark'); events.validate_content_policy(plan)
    assert len(plan['events']) == 14 and collector.reconstruct(obs) == projected
    assert original['sources'][0]['retainedObservations'][-1]['scrapedDoc']['rawHtml'] is not None


def test_unprojected_missing_literal_text_and_tamper_refuse():
    original = missing_artifact()
    obs = collector.convert(original,source_key='newmark')
    with pytest.raises(ValueError): events.validate_content_policy(events.plan(obs,source_key='newmark'))
    projected, literals, receipt = newmark_rcm.prepare(events.encoded(original),source_key='newmark')
    forged = copy.deepcopy(receipt)
    next(item for item in forged['withheld'] if item['reason']=='unverified_content_omitted')['pointer'] += '/invalid'
    with pytest.raises(ValueError): newmark_rcm.validate(projected,forged,source_key='newmark',numeric_literals=literals)
