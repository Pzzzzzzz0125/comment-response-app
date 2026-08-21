import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from web_app.ingestion_admin import IngestionAdmin


class IngestionAdminTests(unittest.TestCase):
    def make_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        site = workspace / "new" / "100 Main Street"
        site.mkdir(parents=True)
        (site / "review.pdf").write_bytes(b"%PDF-test")
        (site / "notes.txt").write_text("ignored", encoding="utf-8")
        return workspace

    def test_snapshot_exposes_only_staged_project_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            admin = IngestionAdmin(workspace, enabled=True)

            snapshot = admin.snapshot()

            self.assertEqual(len(snapshot["sites"]), 1)
            self.assertEqual(snapshot["sites"][0]["label"], "100 Main Street")
            self.assertEqual(snapshot["sites"][0]["file_count"], 1)
            self.assertEqual(snapshot["sites"][0]["extensions"], {"PDF": 1})
            self.assertNotIn(str(workspace), json.dumps(snapshot))

    def test_browser_cannot_submit_a_path_or_unconfirmed_ingestion(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            admin = IngestionAdmin(workspace, enabled=True, gemini_api_key="configured")
            site_id = admin.snapshot()["sites"][0]["site_id"]

            with self.assertRaisesRegex(ValueError, "Unknown staged"):
                admin.start("prescan", "../../outside")
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                admin.start("ingest", site_id, confirmed=False)

    def test_inventory_runs_once_and_persists_completion(self):
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            kwargs["stdout"].write("inventory complete\n")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            admin = IngestionAdmin(workspace, enabled=True, run_command=fake_run)
            admin.start("inventory")
            deadline = time.monotonic() + 2
            while admin.snapshot()["jobs"][0]["status"] not in {"completed", "failed"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

            job = admin.snapshot()["jobs"][0]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(len(calls), 1)
            self.assertIn("--inventory-only", calls[0])

    def test_folder_upload_runs_the_complete_pipeline_once(self):
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            kwargs["stdout"].write("step complete\n")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            admin = IngestionAdmin(
                workspace, enabled=True, gemini_api_key="configured", run_command=fake_run,
            )
            session = admin.initiate_upload("200 Oak Avenue", [
                {"relative_path": "review/round-1.pdf", "size": 8},
            ])
            upload = session["files"][0]
            admin.upload_file(
                session["upload_id"], upload["file_id"], io.BytesIO(b"%PDF-new"), 8,
            )
            admin.complete_upload(session["upload_id"])

            deadline = time.monotonic() + 2
            while admin.snapshot()["jobs"][0]["status"] not in {"completed", "failed"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

            self.assertEqual(admin.snapshot()["jobs"][0]["status"], "completed")
            self.assertEqual(len(calls), 5)
            self.assertIn("--inventory-only", calls[0])
            self.assertIn("--prescan-only", calls[1])
            self.assertIn("--site", calls[2])
            self.assertEqual(calls[3][-1], "web_app/migrate_sources.py")
            self.assertIn("--metadata-only", calls[4])
            self.assertEqual(
                (workspace / "new" / "200 Oak Avenue" / "review" / "round-1.pdf").read_bytes(),
                b"%PDF-new",
            )

    def test_upload_rejects_unsafe_or_unsupported_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            admin = IngestionAdmin(workspace, enabled=True, gemini_api_key="configured")

            with self.assertRaisesRegex(ValueError, "unsafe"):
                admin.initiate_upload("200 Oak Avenue", [{"relative_path": "../secret.pdf", "size": 4}])
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                admin.initiate_upload("200 Oak Avenue", [{"relative_path": "notes.exe", "size": 4}])


if __name__ == "__main__":
    unittest.main()
