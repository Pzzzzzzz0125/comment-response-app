import unittest

from phase2.benchmark_site_intake import estimate_file_cost, pdf_page_texts


class SitePreflightEstimateTests(unittest.TestCase):
    def test_context_only_file_has_no_gemini_cost(self):
        result = estimate_file_cost({
            "preflight_route": "context_only",
            "file_type": "pdf",
            "page_count": 100,
        }, 500_000)
        self.assertEqual(result["estimated_input_tokens"]["central"], 0)
        self.assertFalse(result["requires_confirmation"])

    def test_cached_file_has_no_incremental_gemini_cost(self):
        result = estimate_file_cost({
            "preflight_route": "cache_reuse",
            "file_type": "pdf",
            "page_count": 34,
        }, 400_000)
        self.assertEqual(result["estimated_input_tokens"]["central"], 0)
        self.assertEqual(result["estimated_minutes"]["central"], 0.0)

    def test_dense_visual_document_is_flagged_for_confirmation(self):
        result = estimate_file_cost({
            "preflight_route": "visual_full_read",
            "file_type": "pdf",
            "page_count": 20,
        }, 400_000, batch_pages=2, batch_overlap=1, batch_workers=2)
        self.assertGreaterEqual(
            result["estimated_input_tokens"]["central"], 100_000,
        )
        self.assertTrue(result["high_cost"])
        self.assertTrue(result["requires_confirmation"])

    def test_structured_spreadsheet_uses_row_units_not_page_images(self):
        result = estimate_file_cost({
            "preflight_route": "structured_spreadsheet",
            "file_type": "xlsx",
            "page_count": 0,
        }, 100_000, spreadsheet_rows=47)
        self.assertEqual(result["evidence_unit_count"], 47)
        self.assertLess(
            result["estimated_input_tokens"]["central"], 30_000,
        )

    def test_pdf_physical_pages_are_not_inferred_from_text_length(self):
        pages = pdf_page_texts({
            "kind": "pdf_text_pages",
            "pages": [
                {"page": 1, "text": "A" * 100_000},
                {"page": 2, "text": "B" * 100_000},
            ],
        })
        self.assertEqual(len(pages), 2)


if __name__ == "__main__":
    unittest.main()
