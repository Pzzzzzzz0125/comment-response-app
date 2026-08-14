from pathlib import Path
import unittest

from web_app.source_registry import pdf_same_row_context


def layout_line(text: str, x: float, y: float) -> dict:
    characters = []
    cursor = x
    for _character in text:
        characters.append((cursor, y, cursor + 4.0, y + 7.0, 8.0))
        cursor += 4.0
    return {"text": text, "characters": characters}


class PdfRowDateTests(unittest.TestCase):
    def test_recovers_adjacent_reviewer_timestamp_from_same_row(self) -> None:
        lines = [
            layout_line("54", 15, 108),
            layout_line("RS Building Review", 40, 92),
            layout_line("Gregg Schwartz", 40, 100),
            layout_line("9/23/25 3:52 PM", 40, 110),
            layout_line("Shear wall missing per calc page 23", 220, 108),
            layout_line("Added shearwalls and calculations to show bays blocking are adequate on page 63.", 430, 108),
        ]

        context = pdf_same_row_context(
            Path("unused.pdf"), 1,
            "Shear wall missing per calc page 23",
            "54", page_layout=(800.0, lines),
        )

        self.assertEqual(context["event_date"], "2025-09-23")
        self.assertEqual(context["event_date_raw"], "9/23/25 3:52 PM")
        self.assertEqual(context["reviewer"], "Gregg Schwartz")
        self.assertTrue(context["printed_comment_id_seen"])
        self.assertEqual(context["event_date_source"], "pdf_adjacent_reviewer_cell")


if __name__ == "__main__":
    unittest.main()
