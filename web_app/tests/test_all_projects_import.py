import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_app.import_all_projects_rematch import import_verified_workbook


class AllProjectsImportTests(unittest.TestCase):
    def test_verified_workbook_import_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            dataset_path = workspace / "phase2_dataset" / "dataset.json"
            dataset_path.parent.mkdir()
            source_root = workspace / "comments&response"
            source_root.mkdir()
            (source_root / "new.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            workbook = workspace / "verified.xlsx"
            workbook.write_bytes(b"fixture")
            comments, responses, links = [], [], []
            pairs = []
            for index in range(74):
                comment_id = f"C-P-{index}"
                keep = index < 49
                response_id = f"R-E-{index}" if keep else f"R-N-{(index - 49) % 11}"
                response_text = f"response {index}" if keep else f"new response {(index - 49) % 11}"
                comments.append({
                    "comment_id": comment_id, "original_text": f"comment {index}",
                    "source_document": "comments&response/comment.xlsx", "source_location": f"sheet Comments, row {index + 2}",
                    "response_id": response_id if keep else "", "match_status": "matched" if keep else "unmatched",
                })
                links.append({
                    "link_id": f"L-{comment_id}", "comment_id": comment_id,
                    "response_id": response_id if keep else "",
                    "review_status": "confirmed" if index < 39 else ("suggested" if keep else "not_applicable"),
                })
                if keep:
                    responses.append({
                        "response_id": response_id, "comment_id": comment_id, "original_text": response_text,
                        "source_document": "comments&response/response.pdf", "source_location": "page 1",
                    })
                pairs.append({
                    "Import Action": "KEEP_EXISTING_LINK" if keep else "ADD_RESPONSE_AND_LINK",
                    "Comment ID": comment_id, "Response ID": response_id,
                    "Match Status": "verified_direct", "Confidence": "high",
                    "Government Comment": f"comment {index}", "Company Response": response_text,
                    "Comment Source File": "comment.xlsx", "Comment Locator": f"sheet Comments, row {index + 2}",
                    "Response Source File": "response.pdf" if keep else "new.pdf", "Response Locator": "page 1",
                    "Match Basis": "verified fixture",
                })
            no_response = []
            for index in range(181):
                comment_id = f"C-N-{index}"
                comments.append({
                    "comment_id": comment_id, "original_text": f"unpaired {index}",
                    "source_document": "comments&response/comment.xlsx", "source_location": f"sheet Comments, row {index + 100}",
                    "response_id": "", "match_status": "unmatched",
                })
                links.append({"link_id": f"L-{comment_id}", "comment_id": comment_id, "response_id": "", "review_status": "not_applicable"})
                no_response.append({
                    "Comment ID": comment_id, "Government Comment": f"unpaired {index}",
                    "Source File": "comment.xlsx", "Source Locator": f"sheet Comments, row {index + 100}",
                    "Reason": "No response",
                })
            new_responses = [{
                "Response ID": f"R-N-{index}", "Source File": "new.pdf", "Source Locator": "page 1",
                "Response Label": str(index), "Exact Response Text": f"new response {index}",
            } for index in range(11)]
            dataset_path.write_text(json.dumps({
                "comments": comments, "responses": responses, "comment_response_links": links,
            }), encoding="utf-8")

            sheets = {"Pairs to Import": pairs, "New Responses": new_responses, "No Source Response": no_response}
            with patch("web_app.import_all_projects_rematch.workbook_rows", side_effect=lambda _path, sheet: sheets[sheet]):
                first = import_verified_workbook(workbook, dataset_path, source_root, apply=True)
                second = import_verified_workbook(workbook, dataset_path, source_root, apply=False)
            self.assertEqual(first["conflicts"], [])
            self.assertEqual(first["responses_inserted"], 11)
            self.assertEqual(first["links_created"], 25)
            self.assertEqual(first["links_confirmed"], 35)
            self.assertEqual(second["responses_inserted"], 0)
            self.assertEqual(second["links_created"], 0)
            self.assertEqual(second["links_confirmed"], 0)
            imported = json.loads(dataset_path.read_text(encoding="utf-8"))
            self.assertEqual(len(imported["responses"]), 60)
            self.assertEqual(sum(row.get("review_status") == "confirmed" for row in imported["comment_response_links"]), 74)
            self.assertEqual(sum(row.get("no_response_verified") is True for row in imported["comment_response_links"]), 181)


if __name__ == "__main__":
    unittest.main()
