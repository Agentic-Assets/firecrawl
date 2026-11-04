"""Exercise generated contact SQL on a disposable socket-only PostgreSQL fixture.

Run explicitly with --execute-disposable-fixture. No application DB or migrations.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE / "observation_store")]
from owned_observation_fixture_cluster import OwnedCluster, tool

import cre_ingest as ingest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
        parser.error("explicit disposable fixture execution required")
    generated = ingest.build_sql([], [], "2026-10-29T00:00:00Z", set())
    contact_start = generated.index("CREATE TEMP TABLE _contact_preserve")
    contact_end = generated.index("DELETE FROM credeals.cre_listing_documents", contact_start)
    contact_selects = generated[contact_start:contact_end]
    insert_start = generated.index("DO $$ BEGIN", generated.index("-- Contacts refresh."))
    insert_end = generated.index("INSERT INTO credeals.cre_listing_documents", insert_start)
    refresh_inserts = generated[insert_start:insert_end]
    values = ",".join(f"({index}, {ingest.sql_lit(key)})" for index, key in enumerate(sorted(ingest.BUILDOUT_SOURCE_KEYS), 100))
    sql = r"""
BEGIN;
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages(id int PRIMARY KEY, slug text NOT NULL);
CREATE TABLE credeals.cre_listing_contacts(
 listing_id int, name text, title text, license text, email text, phone text,
 brokerage_name text, profile_url text, avatar_url text, vcard_url text, is_primary boolean
);
CREATE TEMP TABLE _up(id int, brokerage_id int, external_id text);
CREATE TEMP TABLE _src(brokerage_id int, external_id text, raw_data jsonb, contacts jsonb);
CREATE TEMP TABLE _prior_vals(brokerage_id int, external_id text);
CREATE TEMP TABLE _child_refresh(id int);
CREATE TEMP TABLE _child_additive(id int);
INSERT INTO credeals.cre_brokerages VALUES (1,'leibsohn'),(2,'cawley-chicago'),(3,'transwestern'),(4,'colliers');
INSERT INTO _up VALUES (1,1,'inventory'),(2,1,'empty-detail'),(3,2,'current-detail'),(4,3,'other-empty'),(5,4,'main:fixture'),(6,3,'spoof'),(7,1,'new-empty');
INSERT INTO _src VALUES
 (1,'inventory','{"sourceKey":"leibsohn","preserveChildCollections":true}','[]'),
 (1,'empty-detail','{"sourceKey":"leibsohn"}','[]'),
 (2,'current-detail','{"sourceKey":"cawley-chicago"}','[{"name":"Current","isPrimary":true}]'),
 (3,'other-empty','{"sourceKey":"transwestern"}','[]'),
 (4,'main:fixture','{"sourceKey":"colliers-main"}','[]'),
 (3,'spoof','{"sourceKey":"leibsohn"}','[]'),
 (1,'new-empty','{"sourceKey":"leibsohn"}','[]');
""" + f"""
INSERT INTO credeals.cre_brokerages(id,slug) VALUES {values};
INSERT INTO _up SELECT id,id,'family-fixture' FROM credeals.cre_brokerages WHERE id>=100;
INSERT INTO _src SELECT id,'family-fixture',jsonb_build_object('sourceKey',slug),'[]'::jsonb FROM credeals.cre_brokerages WHERE id>=100;
""" + r"""
INSERT INTO _prior_vals SELECT brokerage_id,external_id FROM _up WHERE id <> 7;
INSERT INTO _child_refresh SELECT id FROM _up WHERE id <> 1;
INSERT INTO _child_additive VALUES (1);
INSERT INTO credeals.cre_listing_contacts(listing_id,name,is_primary)
 SELECT id,'Before',true FROM _up WHERE id <> 7;
""" + contact_selects + refresh_inserts + f"""
DO $$ BEGIN
 IF (SELECT count(*) FROM _contact_preserve) <> {len(ingest.BUILDOUT_SOURCE_KEYS) + 2} OR EXISTS
    (SELECT 1 FROM _contact_preserve WHERE id NOT IN (1,2) AND id < 100) THEN
  RAISE EXCEPTION 'incorrect preserved identities'; END IF;
 IF (SELECT count(*) FROM credeals.cre_listing_contacts WHERE listing_id >= 100 AND name='Before' AND is_primary) <> {len(ingest.BUILDOUT_SOURCE_KEYS)} THEN
  RAISE EXCEPTION 'source-family contacts not retained'; END IF;
 IF (SELECT count(*) FROM credeals.cre_listing_contacts WHERE listing_id IN (1,2) AND name='Before' AND is_primary) <> 2 THEN
  RAISE EXCEPTION 'prior Buildout contacts not retained'; END IF;
 IF NOT EXISTS(SELECT 1 FROM credeals.cre_listing_contacts WHERE listing_id=3 AND name='Current' AND is_primary) OR
    EXISTS(SELECT 1 FROM credeals.cre_listing_contacts WHERE listing_id=3 AND name='Before') THEN
  RAISE EXCEPTION 'nonempty current contact refresh changed'; END IF;
 IF EXISTS(SELECT 1 FROM credeals.cre_listing_contacts WHERE listing_id IN (4,5,6,7)) THEN
  RAISE EXCEPTION 'non-Buildout or new behavior changed'; END IF;
END $$;
COMMIT;
"""
    with OwnedCluster(tool("initdb"), tool("pg_ctl"), port=55471) as cluster:
        subprocess.run(
            [tool("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", str(cluster.root),
             "-p", str(cluster.port), "-d", "postgres", "-f", "-"],
            input=sql, text=True, capture_output=True, check=True, timeout=30, env=cluster.env,
        )
    print("disposable generated contact SQL: passed; owned socket-only cluster stopped")


if __name__ == "__main__":
    main()
