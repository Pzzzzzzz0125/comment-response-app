import unittest

from web_app.document_identity import (
    annotate_hierarchy_ids,
    canonical_city_name,
    canonical_city_id,
    canonical_project_id,
    canonical_site_name,
    canonicalize_documents,
)


class DocumentHierarchyTests(unittest.TestCase):
    def test_city_case_and_accent_aliases_share_one_identity(self):
        self.assertEqual(canonical_city_name("LOS ALTOS"), "Los Altos")
        self.assertEqual(canonical_city_id("LOS ALTOS"), canonical_city_id("Los Altos"))
        self.assertEqual(canonical_city_name("San Jos\u00e9"), "San Jose")
        self.assertEqual(canonical_city_id("San Jos\u00e9"), canonical_city_id("San Jose"))

    def test_aliases_share_city_and_site(self):
        first = {
            "city": "Menlo Park", "property_project": "2311 WARNER RANGE AVE",
            "source_document": "comments&response/25-001-2311_warner_range/building/PC1.pdf",
            "discipline": "Building",
        }
        second = {
            "city": "Menlo Park", "property_project": "25 001 2311 Warner Range Ave — Building",
            "source_document": "comments&response/25-001-2311_warner_range/building/PC2.pdf",
            "discipline": "Building",
        }
        annotate_hierarchy_ids(first)
        annotate_hierarchy_ids(second)
        self.assertEqual(first["city_id"], second["city_id"])
        self.assertEqual(first["site_id"], second["site_id"])
        self.assertEqual(first["project_id"], second["project_id"])

        avenue = {
            "city": "Menlo Park", "property_project": "2311 Warner Range Avenue",
            "source_document": "comments&response/25-001-2311_warner_range/building/PC3.pdf",
            "discipline": "Building",
        }
        annotate_hierarchy_ids(avenue)
        self.assertEqual(first["site_id"], avenue["site_id"])

    def test_different_scopes_share_one_permit_project(self):
        building = {
            "city": "Menlo Park", "property_project": "2311 Warner Range Ave",
            "source_document": "comments&response/site/building/PC1.pdf",
            "discipline": "Building",
        }
        planning = {**building, "source_document": "comments&response/site/planning/PC1.pdf", "discipline": "Planning"}
        annotate_hierarchy_ids(building)
        annotate_hierarchy_ids(planning)
        self.assertEqual(building["project_id"], planning["project_id"])

    def test_new_and_legacy_roots_share_case_project(self):
        legacy = {
            "city": "Menlo Park", "property_project": "2311 WARNER RANGE AVE",
            "source_document": "comments&response/25-001-2311_warner_range_ave_menlopark/building/PC2.pdf",
            "discipline": "Building",
        }
        new = {
            "city": "Menlo Park", "property_project": "25-001-2311 Warner Range Ave, Menlo Park, CA 94025",
            "source_document": "new/25-001-2311 Warner Range Ave, Menlo Park, CA 94025/04 Deliverables/PC2.pdf",
            "discipline": "Structural",
        }
        annotate_hierarchy_ids(legacy)
        annotate_hierarchy_ids(new)
        self.assertEqual(legacy["site_id"], new["site_id"])
        self.assertEqual(legacy["project_id"], new["project_id"])
        self.assertEqual(legacy["site_name"], "2311 Warner Range Ave")

    def test_incomplete_address_alias_uses_project_folder(self):
        short = {
            "city": "Atherton", "property_project": "110 Glenwood",
            "source_document": "new/25-015-110 Glenwood Ave, Atherton, CA 94027/Building/comments.pdf",
        }
        full = {
            "city": "Atherton", "property_project": "110 GLENWOOD AVE",
            "source_document": "new/25-015-110 Glenwood Ave, Atherton, CA 94027/Planning/comments.pdf",
        }
        annotate_hierarchy_ids(short)
        annotate_hierarchy_ids(full)
        self.assertEqual(short["site_id"], full["site_id"])
        self.assertEqual(short["project_id"], full["project_id"])

    def test_project_label_is_consolidated_across_address_aliases(self):
        comments = [
            {
                "comment_id": "a", "city": "San Jose",
                "property_project": "1263 Flickinger",
                "source_document": "comments&response/25-002-1263_flickinger_sanjose/building/PC1.pdf",
                "original_text": "A comment",
            },
            {
                "comment_id": "b", "city": "San Jose",
                "property_project": "1263 Flickinger Ave",
                "source_document": "new/25-002-1263 Flickinger Ave, San Jose, CA 95131/Building/PC1.pdf",
                "original_text": "Another comment",
            },
        ]
        canonicalize_documents(comments)
        self.assertEqual(comments[0]["project_id"], comments[1]["project_id"])
        self.assertEqual(comments[0]["project_name"], "1263 Flickinger Ave")
        self.assertEqual(comments[1]["project_name"], "1263 Flickinger Ave")
        self.assertIn("1263 Flickinger", comments[0]["project_aliases"])


if __name__ == "__main__":
    unittest.main()
