"""Franklin site observations cannot be retired by Buildout enumeration."""
import cre_ingest
import cre_monitor


def test_full_buildout_retirement_excludes_only_franklin_site_namespace():
    sql = cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", {"franklin-street"})
    candidates = sql.split("CREATE TEMP TABLE _retired_candidates", 1)[1].split(";", 1)[0]
    assert "b.slug = 'franklin-street' AND l.external_id LIKE 'franklin-site:%'" in candidates
    assert "NOT EXISTS (SELECT 1 FROM _up u WHERE u.id = l.id)" in candidates


def test_buildout_monitor_does_not_disappear_or_count_site_cards():
    site = ("broker", "franklin-site:sale:123")
    legacy = ("broker", "buildout-1")
    other = ("other", "franklin-site:sale:123")
    prior = {
        site: {"source_key": "franklin-street", "observation_present": True, "observed_status": None, "url": None, "fingerprint": None},
        legacy: {"source_key": "franklin-street", "observation_present": True, "observed_status": None, "url": None, "fingerprint": None},
        other: {"source_key": "other", "observation_present": True, "observed_status": None, "url": None, "fingerprint": None},
    }
    listings = {key: {"id": key[1], "status": "active", "deleted": False} for key in prior}
    total, live = cre_monitor.prior_source_counts(prior)
    assert total == {"franklin-street": 1, "other": 1}
    assert live == total
    events, _, _, disappear, _ = cre_monitor.derive_events(
        {}, prior, listings, {}, {"franklin-street", "other"}, set(),
        {"franklin-street": True, "other": True}, "00000000-0000-4000-8000-000000000000",
    )
    assert site not in disappear
    assert set(disappear) == {legacy, other}
    assert len(events) == 2