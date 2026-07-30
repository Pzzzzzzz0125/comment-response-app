import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_corpus as audit


def make_xlsx(path: Path, headers: list[str], rows: list[list[str]] | None = None, include_refs: bool = True) -> None:
    rows = rows or []
    all_rows = [headers, *rows]
    sheet_rows = []
    for row_number, values in enumerate(all_rows, start=1):
        cells = []
        for column_number, value in enumerate(values, start=1):
            number = column_number
            letters = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(65 + remainder) + letters
            reference = f' r="{letters}{row_number}"' if include_refs else ""
            cells.append(f'<c{reference} t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Review Matrix" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')


class CorpusAuditTests(unittest.TestCase):
    def test_human_override_replaces_classification_and_records_evidence(self):
        record = {
            "path": "data/comments.pdf",
            "document_type": "unknown",
            "classification_confidence": 0.25,
            "likely_contains_city_comments": False,
            "classification_evidence": ["no decisive signal"],
        }
        audit.apply_override(record, {
            "document_type": "city_comments",
            "classification_confidence": "1.0",
            "likely_contains_city_comments": "true",
            "note": "Human verified plan-review comments.",
        })
        self.assertEqual(record["document_type"], "city_comments")
        self.assertTrue(record["likely_contains_city_comments"])
        self.assertTrue(record["manual_override_applied"])
        self.assertIn("human-verified override", record["classification_evidence"][-1])

    def test_folder_inference_preserves_permit_scope(self):
        path = Path("100_example_ave_sanjose/deliverable and submittals/building/lot 2/2nd Round of Comments/file.xlsx")
        city, city_score, _ = audit.infer_city(path, "")
        project, project_score, _ = audit.infer_project(path)
        review_round, round_score, _ = audit.infer_round(path)
        self.assertEqual(city, "San Jose")
        self.assertGreater(city_score, 0.8)
        self.assertEqual(project, "100 Example Ave — Building / Lot 2")
        self.assertGreater(project_score, 0.8)
        self.assertEqual(review_round, "2")
        self.assertGreater(round_score, 0.9)

    def test_folder_filename_lot_conflict_lowers_confidence(self):
        path = Path("100_example_ave_sanjose/building/lot 1/1st comments/Unit LOT 2 comments.pdf")
        project, score, evidence = audit.infer_project(path)
        self.assertEqual(project, "100 Example Ave — Building / Lot 1")
        self.assertLess(score, 0.58)
        self.assertTrue(any("conflicts" in item for item in evidence))

    def test_submission_package_maps_to_round_it_answers(self):
        comment_path = Path(
            "25-001-100_example_ave_menlopark/building/3rd submission/"
            "2nd Round of Comments/comments.docx"
        )
        response_path = Path(
            "25-001-100_example_ave_menlopark/building/3rd submission/"
            "2nd Round Submission Package/Response Letter.pdf"
        )
        later_response = Path(
            "25-001-100_example_ave_menlopark/building/4th submission/"
            "4th Submission Package/Response Letter.pdf"
        )
        self.assertEqual(audit.infer_round(comment_path)[0], "2")
        self.assertEqual(audit.infer_round(response_path)[0], "2")
        self.assertEqual(audit.infer_round(later_response)[0], "3")

    def test_misspelled_submital_package_still_maps_to_prior_review_round(self):
        path = Path(
            "25-018-10344_el-prado_way_unit_a_cupertino/"
            "2nd submital package/response letter.docx"
        )
        self.assertEqual(audit.infer_round(path)[0], "1")
        project, _, _ = audit.infer_project(path)
        self.assertEqual(
            project, "25 018 10344 El Prado Way Unit A",
        )

    def test_menlo_park_city_and_project_suffix(self):
        path = Path(
            "25-001-100_example_ave_menlopark/building/"
            "2nd submission/file.pdf"
        )
        self.assertEqual(audit.infer_city(path, "")[0], "Menlo Park")
        project, _, _ = audit.infer_project(path)
        self.assertEqual(project, "25 001 100 Example Ave — Building")

    def test_xlsx_detects_comment_and_response_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "matrix.xlsx"
            make_xlsx(path, ["Item", "City Comment", "Applicant Response"], [["1", "Example", "Will revise"]])
            result = audit.inspect_xlsx(path)
            self.assertEqual(result["sheet_names"], ["Review Matrix"])
            self.assertEqual(result["headers"]["Review Matrix"]["row"], 1)
            self.assertEqual(result["comment_columns"]["Review Matrix"][0]["column"], "B")
            self.assertEqual(result["response_columns"]["Review Matrix"][0]["column"], "C")

    def test_xlsx_content_sample_supports_authoritative_city_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2026-110167 review.xlsx"
            make_xlsx(
                path,
                ["Reviewer", "City Comment", "Applicant Response"],
                [[
                    "reviewer@sanjoseca.gov",
                    "Please revise the plans.",
                    "Sheet updated.",
                ]],
            )
            result = audit.inspect_xlsx(path)
            city, confidence, evidence = audit.infer_city(
                Path("25-031-7298_Queensbridge_Way") / path.name,
                result["content_sample"],
            )
            self.assertEqual(city, "San Jose")
            self.assertEqual(confidence, 0.99)
            self.assertTrue(any("sanjoseca.gov" in item for item in evidence))

    def test_authoritative_source_city_overrides_conflicting_folder_name(self):
        city, confidence, evidence = audit.infer_city(
            Path("100_Main_St_Cupertino/comments.xlsx"),
            "Reviewer: yvonne.delgado@sanjoseca.gov",
        )
        self.assertEqual(city, "San Jose")
        self.assertEqual(confidence, 0.99)
        self.assertTrue(any("conflicting" in item for item in evidence))

    def test_xlsx_without_cell_references_uses_sequential_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "matrix.xlsx"
            make_xlsx(
                path,
                ["Item", "City Comment", "Applicant Response"],
                [["1", "Comment: revise", "Response: sheet updated"]],
                include_refs=False,
            )
            result = audit.inspect_xlsx(path)
            self.assertEqual(result["comment_columns"]["Review Matrix"][0]["column"], "B")
            self.assertEqual(result["response_columns"]["Review Matrix"][0]["column"], "C")

    def test_empty_response_template_is_not_a_response_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "comments.xlsx"
            make_xlsx(
                path,
                ["Ref", "Type", "Enter Your Comment Response Here", "Status"],
                [["1", "Comment: revise plans", "", "Unresolved"]],
                include_refs=False,
            )
            result = audit.inspect_xlsx(path)
            self.assertEqual(result["headers"]["Review Matrix"]["row"], 1)
            self.assertEqual(result["comment_columns"]["Review Matrix"][0]["column"], "B")
            self.assertNotIn("Review Matrix", result["response_columns"])

    def test_primary_source_priority_prefers_combined_spreadsheet(self):
        base = {
            "path": "comment.pdf", "classification_confidence": 0.9,
            "is_spreadsheet_table_source": False, "likely_contains_both": False,
            "likely_contains_city_comments": True, "likely_contains_company_responses": False,
            "document_type": "city_comments", "appears_drawing_heavy": False,
        }
        combined = dict(base, path="matrix.xlsx", is_spreadsheet_table_source=True,
                        likely_contains_both=True, likely_contains_company_responses=True,
                        document_type="combined_comment_response", classification_confidence=0.8)
        selected, status, _ = audit.select_primary_source([base, combined])
        self.assertEqual(selected, "matrix.xlsx")
        self.assertEqual(status, "spreadsheet_source_found")

    def test_full_run_is_repeatable_and_reports_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "data"
            folder = source / "100_example_ave_sanjose" / "grading" / "1st Round of Comments"
            folder.mkdir(parents=True)
            make_xlsx(folder / "Review Comments.xlsx", ["City Comment", "Applicant Response"])
            duplicate_data = b"supporting bytes"
            (folder / "report-a.bin").write_bytes(duplicate_data)
            (folder / "report-b.bin").write_bytes(duplicate_data)
            output = root / "audit"
            first = audit.run_audit(source, output, root)
            first_csv = (output / "file_inventory.csv").read_text()
            first_summary = (output / "review_round_summary.csv").read_text()
            first_json = (output / "file_inventory.json").read_text()
            second = audit.run_audit(source, output, root)
            self.assertEqual(first, second)
            self.assertEqual(first_csv, (output / "file_inventory.csv").read_text())
            self.assertEqual(first_summary, (output / "review_round_summary.csv").read_text())
            self.assertEqual(first_json, (output / "file_inventory.json").read_text())
            with (output / "duplicate_files.csv").open() as stream:
                duplicates = list(csv.DictReader(stream))
            self.assertEqual(len(duplicates), 2)
            inventory = json.loads((output / "file_inventory.json").read_text())
            self.assertEqual(len(inventory["files"]), 3)
            self.assertEqual(hashlib.sha256(duplicate_data).hexdigest(), duplicates[0]["sha256"])

    def test_output_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            with self.assertRaises(ValueError):
                audit.run_audit(source, source / "audit", Path(temp))

    def test_unchanged_inventory_record_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            folder = source / "sample_sunnyvale" / "1st comments"
            folder.mkdir(parents=True)
            (folder / "comments.txt").write_text("City comments")
            output = root / "audit"
            audit.run_audit(source, output, root)
            audit.run_audit(
                source, output, root,
                reuse_inventory_path=output / "file_inventory.json",
            )
            inventory = json.loads(
                (output / "file_inventory.json").read_text()
            )["files"]
            self.assertEqual(inventory[0]["audit_cache_status"], "reused")


if __name__ == "__main__":
    unittest.main()
