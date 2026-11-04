# Synthetic observation-store fixtures

These modules, migrations and disposable PostgreSQL runners are fixture-only.
They do not authorize source data, apply production DDL, connect to live systems,
or implement a production writer. Design, policy and review limits are in
[store-design.md](store-design.md), [artifact-planner.md](artifact-planner.md),
and [lossless-planner.md](lossless-planner.md). The real-input offline lossless
cache reader remains separately under the collector root.

Run tests only from a standalone `git clone --no-hardlinks`:

```sh
python3 -m pytest tests/observation_store tests/test_buildout_lossless_cache_evidence.py -q
python3 tests/observation_store/run_buildout_observation_store_postgres.py --execute-disposable-fixture
python3 tests/observation_store/run_buildout_observation_fixture_planner_postgres.py --execute-disposable-fixture
python3 tests/observation_store/run_buildout_lossless_store_fixture_postgres.py --execute-disposable-fixture
```

The private socket-only clusters use bounded subprocess waits and a minimal
environment. Failed initialization, failed startup or unconfirmed shutdown retains the cluster
for inspection. An initialization or start timeout cannot guarantee that a
delayed process or descendant will not appear; verify manually before deleting a retained directory.
