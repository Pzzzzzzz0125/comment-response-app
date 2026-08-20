from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_app.platform_support import (
    ghostscript_executable,
    libreoffice_executable,
    tesseract_executable,
)


class PlatformSupportTests(unittest.TestCase):
    def test_explicit_tool_overrides_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for helper, name in (
                (ghostscript_executable, "gswin64c.exe"),
                (tesseract_executable, "tesseract.exe"),
            ):
                executable = root / name
                executable.touch()
                with patch(
                    "web_app.platform_support.runtime_setting",
                    return_value=str(executable),
                ):
                    self.assertEqual(helper(), str(executable))

            soffice = root / "soffice.exe"
            soffice.touch()
            self.assertEqual(libreoffice_executable(str(soffice)), str(soffice))

    def test_windows_standard_install_directories_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            program_files = Path(temporary)
            ghostscript = program_files / "gs" / "gs10.05.1" / "bin" / "gswin64c.exe"
            tesseract = program_files / "Tesseract-OCR" / "tesseract.exe"
            soffice = program_files / "LibreOffice" / "program" / "soffice.exe"
            for executable in (ghostscript, tesseract, soffice):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()

            environment = {
                "ProgramFiles": str(program_files),
                "ProgramFiles(x86)": "",
                "LOCALAPPDATA": "",
            }
            with (
                patch("web_app.platform_support.runtime_setting", return_value=""),
                patch("web_app.platform_support._is_windows", return_value=True),
                patch("web_app.platform_support.shutil.which", return_value=None),
                patch.dict("web_app.platform_support.os.environ", environment, clear=False),
            ):
                self.assertEqual(ghostscript_executable(), str(ghostscript))
                self.assertEqual(tesseract_executable(), str(tesseract))
                self.assertEqual(libreoffice_executable(), str(soffice))


if __name__ == "__main__":
    unittest.main()
