"""Isolated PostgreSQL contract; set TW_IMAGE_TEST_CONTAINER to a disposable container."""
import json
import os
import subprocess
import unittest

import cre_ingest as ingest
from cre_transwestern_image_sql import build_parts


def row():
    url = "https://transwestern.com/property/example"
    photo = "https://images.transwestern.com/property/photo.jpg"
    return ingest.to_row({
        "sourceKey": "transwestern", "id": "example", "url": url,
        "canonicalUrl": url, "name": "Example", "transactionType": "Sale",
        "photos": [photo], "rawTranswesternFeed": {"PropertyImage": photo},
        "transwesternPhotoRoleVersion": "v1", "detailObservedAt": "2026-10-19T20:00:00Z",
        "freshnessProvenance": {"method": "transwestern_detail_page", "cacheDisposition": "live"},
        "transwesternPrimaryPhotoEvidence": {"url": photo, "basis": "provider_feed"},
    }, {}, "2026-10-19T20:00:00Z")


def binding():
    return {"artifact_sha256": "a" * 64, "artifact_bytes": 100,
            "artifact_run_key": "ingest:v1:test", "generation_id": "fixture",
            "generation_started_at": "2026-10-19T20:00:00Z",
            "database_target_sha256": "b" * 64, "attempt_id": "c" * 32,
            "collector_git_sha": "d" * 40}


@unittest.skipUnless(os.environ.get("TW_IMAGE_TEST_CONTAINER"), "disposable PostgreSQL container required")
class ProjectionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ["TW_IMAGE_TEST_CONTAINER"]
        if not name.startswith("codex-tw-projection-qa-"):
            raise RuntimeError("refusing non-fixture container")
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", name],
            text=True, capture_output=True, check=True,
        )
        if result.stdout.strip() != "none":
            raise RuntimeError("fixture container must have networking disabled")

    def project(self, staged, prior, *, tamper="", expected_error=None):
        parts = build_parts([staged], binding())
        images = json.dumps(staged["images"]).replace("'", "''")
        sql = f"""
BEGIN;
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages(id int,slug text);
CREATE TABLE credeals.cre_listings(id int,brokerage_id int,external_id text,source_url text,canonical_url text,deleted_at text);
CREATE TABLE credeals.cre_listing_images(id int,listing_id int,url text,is_primary boolean,display_order int,alt_text text);
CREATE TEMP TABLE _src(brokerage_id int,slug text,external_id text,source_url text,canonical_url text,images jsonb);
INSERT INTO credeals.cre_brokerages VALUES (1,'transwestern');
INSERT INTO credeals.cre_listings VALUES (1,1,'example','{staged['source_url']}','{staged['canonical_url']}',NULL);
INSERT INTO _src VALUES (1,'transwestern','example','{staged['source_url']}','{staged['canonical_url']}','{images}');
INSERT INTO credeals.cre_listing_images VALUES {prior};
CREATE TEMP TABLE _child_counts_by_listing_before(source_key text,child_type text,child_count int);
INSERT INTO _child_counts_by_listing_before SELECT 'transwestern','images',count(*) FROM credeals.cre_listing_images;
{parts['before']}
{parts['project']}
DELETE FROM credeals.cre_listing_images;
INSERT INTO credeals.cre_listing_images(listing_id,url,is_primary,display_order,alt_text)
SELECT 1, x->>'url',(x->>'isPrimary')::boolean,(x->>'order')::integer,x->>'altText'
FROM _src CROSS JOIN LATERAL jsonb_array_elements(images) x;
{tamper}
CREATE TEMP TABLE _retained_child_scope_after(listing_id int,source_key text);
INSERT INTO _retained_child_scope_after VALUES(1,'transwestern');
{parts['verify']}
SELECT json_agg(json_build_array(url,is_primary,display_order,alt_text) ORDER BY url,alt_text)::text FROM _tw_image_projection;
ROLLBACK;
"""
        result = subprocess.run(
            ["docker", "exec", "-i", os.environ["TW_IMAGE_TEST_CONTAINER"], "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres"],
            input=sql, text=True, capture_output=True, check=False,
        )
        if expected_error:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)
            return None
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())

    def test_keeps_old_occurrences_and_current_images(self):
        actual = self.project(row(), """
 (1,1,'https://images.transwestern.com/property/photo.jpg',true,7,'first'),
 (2,1,'https://images.transwestern.com/property/photo.jpg',false,8,'second'),
 (3,1,'https://images.transwestern.com/property/old.jpg',false,NULL,'old'),
 (4,1,'https://transwestern.com/assets/images/transwestern-mapmarker.png',false,1,NULL)
""")
        self.assertEqual(actual, [
            ["https://images.transwestern.com/property/old.jpg", False, None, "old"],
            ["https://images.transwestern.com/property/photo.jpg", True, 0, "first"],
            ["https://images.transwestern.com/property/photo.jpg", False, 8, "second"],
        ])

    def test_absent_current_witness_retains_old_primary_and_null_metadata(self):
        staged = row()
        staged["raw_data"].pop("transwesternPrimaryPhotoEvidence")
        actual = self.project(staged, """
 (1,1,'https://images.transwestern.com/property/old.jpg',true,NULL,'hero'),
 (2,1,'https://images.transwestern.com/property/old.jpg',NULL,9,'other')
""")
        self.assertEqual(actual, [
            ["https://images.transwestern.com/property/old.jpg", True, None, "hero"],
            ["https://images.transwestern.com/property/old.jpg", None, 9, "other"],
            ["https://images.transwestern.com/property/photo.jpg", False, 0, None],
        ])

    def test_current_witness_replaces_old_primary_but_retains_old_photo(self):
        actual = self.project(row(), """
 (1,1,'https://images.transwestern.com/property/old.jpg',true,4,'old')
""")
        self.assertEqual(actual, [
            ["https://images.transwestern.com/property/old.jpg", False, 4, "old"],
            ["https://images.transwestern.com/property/photo.jpg", True, 0, None],
        ])

    def test_guard_rejects_loss_of_a_protected_occurrence(self):
        self.project(row(), """
 (1,1,'https://images.transwestern.com/property/old.jpg',false,4,'old')
""", tamper="DELETE FROM credeals.cre_listing_images WHERE url LIKE '%old.jpg';",
            expected_error="Transwestern protected image multiplicity lost")


if __name__ == "__main__":
    unittest.main()
