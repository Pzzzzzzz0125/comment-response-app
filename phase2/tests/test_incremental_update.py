import unittest

from phase2 import incremental_update as incremental


class IncrementalParserTests(unittest.TestCase):
    def test_menlo_matrix_layout_extracts_comment_and_response(self):
        page = "\n".join([
            " Comment Page Ref  Reviewer : Department  Review Comments                                     Applicant Response",
            " ID",
            "                                                                                               Sheet updated.",
            " 130     A0.00     BPC WC3 1 : Building   Remove the deferred system from the cover.",
            "                                            Continue the city comment.",
        ])
        items = incremental.parse_menlo_matrix_pages([page])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["number"], "130")
        self.assertIn("Remove the deferred system", items[0]["comment"])
        self.assertIn("Sheet updated", items[0]["response"])

    def test_sunnyvale_comment_letter_keeps_information_unmatched(self):
        page = """
        1. Planning
        1.) Submit the landscape checklist.
        4. Architectural
        This project requires payment of school fees.
        Sheet A0.1 - Update dates.
        """
        units = incremental.sunnyvale_comment_units([page])
        self.assertEqual(
            [(unit["discipline"], unit["number"]) for unit in units],
            [("Planning", "1"), ("Architectural", "INFO-1"), ("Architectural", "1")],
        )


if __name__ == "__main__":
    unittest.main()
