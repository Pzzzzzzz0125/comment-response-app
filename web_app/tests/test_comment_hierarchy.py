import tempfile
import unittest
from pathlib import Path

from web_app.comment_dedup import mark_duplicate_comments
from web_app.comment_hierarchy import merge_docx_comment_hierarchy


class CommentHierarchyTests(unittest.TestCase):
    def test_parent_and_lettered_subpoints_become_one_searchable_comment(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.docx"
            source.write_bytes(b"test fixture")
            dataset = {
                "comments": [
                    {
                        "comment_id": "C-parent",
                        "original_text": "The City is concerned about impacts.",
                        "verified_text": "The City is concerned about impacts.",
                        "source_document": "source.docx",
                        "source_row": 46,
                        "extraction_method": "docx_numbered_paragraph",
                        "response_id": "R-1",
                        "search_eligible": True,
                    },
                    {
                        "comment_id": "C-a",
                        "original_text": "Please change the design.",
                        "verified_text": "Please change the design.",
                        "source_document": "source.docx",
                        "source_row": 47,
                        "extraction_method": "docx_numbered_paragraph",
                        "response_id": "R-1",
                        "search_eligible": True,
                    },
                    {
                        "comment_id": "C-b",
                        "original_text": "Approval requires mitigation.",
                        "verified_text": "Approval requires mitigation.",
                        "source_document": "source.docx",
                        "source_row": 48,
                        "extraction_method": "docx_numbered_paragraph",
                        "response_id": "R-1",
                        "search_eligible": True,
                    },
                ],
                "comment_response_links": [
                    {
                        "comment_id": comment_id,
                        "response_id": "R-1",
                        "review_status": "confirmed",
                    }
                    for comment_id in ("C-parent", "C-a", "C-b")
                ],
            }
            paragraphs = [
                {
                    "source_number": 46, "text": "The City is concerned about impacts.",
                    "num_id": "4", "list_level": 0, "number_label": "4.",
                },
                {
                    "source_number": 47, "text": "Please change the design.",
                    "num_id": "4", "list_level": 1, "number_label": "a.",
                },
                {
                    "source_number": 48, "text": "Approval requires mitigation.",
                    "num_id": "4", "list_level": 1, "number_label": "b.",
                },
            ]

            report = merge_docx_comment_hierarchy(
                dataset, workspace, paragraph_loader=lambda _path: paragraphs,
            )
            mark_duplicate_comments(dataset)

            parent, first_child, second_child = dataset["comments"]
            self.assertEqual(report["hierarchy_groups_merged"], 1)
            self.assertEqual(
                parent["verified_text"],
                "The City is concerned about impacts.\n"
                "a. Please change the design.\n"
                "b. Approval requires mitigation.",
            )
            self.assertEqual(parent["source_location"], "paragraphs 46-48")
            self.assertEqual(
                parent["source_locator_json"]["paragraph_indices"], [46, 47, 48]
            )
            self.assertTrue(parent["search_eligible"])
            self.assertEqual(first_child["duplicate_of"], "C-parent")
            self.assertEqual(second_child["duplicate_status"], "hierarchical_subpoint")
            self.assertFalse(first_child["search_eligible"])
            self.assertFalse(second_child["search_eligible"])
            first_result = parent["verified_text"]
            merge_docx_comment_hierarchy(
                dataset, workspace, paragraph_loader=lambda _path: paragraphs,
            )
            self.assertEqual(parent["verified_text"], first_result)

    def test_conflicting_subpoint_responses_are_not_silently_merged(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "source.docx").write_bytes(b"test fixture")
            dataset = {
                "comments": [
                    {
                        "comment_id": "C-parent", "original_text": "Parent",
                        "source_document": "source.docx", "source_row": 1,
                        "extraction_method": "docx_numbered_paragraph",
                        "response_id": "R-1",
                    },
                    {
                        "comment_id": "C-child", "original_text": "Child",
                        "source_document": "source.docx", "source_row": 2,
                        "extraction_method": "docx_numbered_paragraph",
                        "response_id": "R-2",
                    },
                ],
                "comment_response_links": [],
            }
            paragraphs = [
                {
                    "source_number": 1, "text": "Parent", "num_id": "1",
                    "list_level": 0, "number_label": "1.",
                },
                {
                    "source_number": 2, "text": "Child", "num_id": "1",
                    "list_level": 1, "number_label": "a.",
                },
            ]

            report = merge_docx_comment_hierarchy(
                dataset, workspace, paragraph_loader=lambda _path: paragraphs,
            )

            self.assertEqual(report["hierarchy_groups_merged"], 0)
            self.assertEqual(len(report["hierarchy_conflicts"]), 1)
            self.assertNotIn("hierarchy_status", dataset["comments"][0])
