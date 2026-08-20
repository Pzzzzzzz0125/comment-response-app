"""Local-only, resumable upload and ingestion orchestration for maintainers.

The browser creates a bounded upload session and streams each selected file.
Files are committed below the workspace ``new`` directory only after the whole
manifest arrives. Actual parsing remains owned by ``phase2.incremental_update``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

try:
    from .platform_support import (
        ghostscript_executable, libreoffice_executable, tesseract_executable,
    )
except ImportError:
    from platform_support import (
        ghostscript_executable, libreoffice_executable, tesseract_executable,
    )


SUPPORTED_SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv"}
JOB_MODES = {"inventory", "prescan", "ingest"}
MAX_UPLOAD_FILES = 5_000
MAX_UPLOAD_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 10 * 1024 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class IngestionAdmin:
    """Run one bounded ingestion task at a time and expose sanitized status."""

    def __init__(
        self,
        workspace: Path,
        *,
        enabled: bool,
        gemini_api_key: str = "",
        on_dataset_changed: Callable[[], None] | None = None,
        run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ):
        self.workspace = workspace.resolve()
        self.staging_root = (self.workspace / "new").resolve()
        self.upload_root = self.staging_root / ".uploads"
        self.state_path = self.workspace / "phase2_dataset" / "ingestion_admin_jobs.json"
        self.log_root = self.workspace / "phase2_dataset" / "ingestion_jobs"
        self.enabled = enabled
        self.gemini_api_key = gemini_api_key
        self.on_dataset_changed = on_dataset_changed
        self._run_command = run_command or subprocess.run
        self._lock = threading.RLock()
        self._active_thread: threading.Thread | None = None
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            payload = {"schema_version": "1.0", "jobs": []}
        jobs = payload.get("jobs") if isinstance(payload, dict) else []
        if not isinstance(jobs, list):
            jobs = []
        for job in jobs:
            if isinstance(job, dict) and job.get("status") in {"queued", "running", "postprocessing"}:
                job["status"] = "interrupted"
                job["finished_at"] = _now()
                job["message"] = "The server stopped before this job completed. Checkpoints are preserved; start it again to resume."
        return {"schema_version": "1.0", "jobs": jobs[-25:]}

    def _save(self) -> None:
        _atomic_json(self.state_path, self._state)

    def _site_id(self, name: str) -> str:
        return "site-" + hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:12]

    def sites(self) -> list[dict[str, Any]]:
        if not self.staging_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for folder in sorted(
            (item for item in self.staging_root.iterdir() if item.is_dir() and not item.name.startswith(".")),
            key=lambda item: item.name.casefold(),
        ):
            files = [item for item in folder.rglob("*") if item.is_file() and item.suffix.casefold() in SUPPORTED_SUFFIXES]
            if not files:
                continue
            extensions: dict[str, int] = {}
            total_size = 0
            newest = 0.0
            for path in files:
                suffix = path.suffix.casefold().lstrip(".").upper() or "OTHER"
                extensions[suffix] = extensions.get(suffix, 0) + 1
                try:
                    stat = path.stat()
                    total_size += stat.st_size
                    newest = max(newest, stat.st_mtime)
                except OSError:
                    continue
            rows.append({
                "site_id": self._site_id(folder.name),
                "label": folder.name,
                "file_count": len(files),
                "size_bytes": total_size,
                "extensions": extensions,
                "last_modified": datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if newest else "",
            })
        return rows

    def _site_name(self, site_id: str) -> str:
        for site in self.sites():
            if site["site_id"] == site_id:
                return str(site["label"])
        raise ValueError("Unknown staged project folder")

    def _dependencies(self) -> dict[str, bool]:
        return {
            "ghostscript": bool(ghostscript_executable()),
            "tesseract": bool(tesseract_executable()),
            "libreoffice": bool(libreoffice_executable()),
            "gemini_key": bool(self.gemini_api_key or os.getenv("GEMINI_API_KEY")),
        }

    def _tail(self, job: dict[str, Any], lines: int = 35) -> list[str]:
        log_name = str(job.get("log_file") or "")
        if not log_name:
            return []
        path = (self.log_root / log_name).resolve()
        if not path.is_relative_to(self.log_root.resolve()):
            return []
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = []
            for source in reversed(self._state["jobs"][-10:]):
                job = {key: value for key, value in source.items() if key != "log_file"}
                if source is self._state["jobs"][-1]:
                    job["log_tail"] = self._tail(source)
                jobs.append(job)
            active = next((job for job in jobs if job.get("status") in {"queued", "running", "postprocessing"}), None)
            return {
                "enabled": self.enabled,
                "staging_folder": "new/",
                "sites": self.sites(),
                "dependencies": self._dependencies(),
                "active_job": active,
                "jobs": jobs,
            }

    def _assert_available(self, *, require_gemini: bool = False) -> None:
        if not self.enabled:
            raise PermissionError("The ingestion entrance is disabled on this server")
        if require_gemini and not self._dependencies()["gemini_key"]:
            raise RuntimeError("Gemini is not configured on this server")
        if any(job.get("status") in {"queued", "running", "postprocessing"} for job in self._state["jobs"]):
            raise RuntimeError("Another ingestion task is already running")

    @staticmethod
    def _project_name(value: str) -> str:
        value = " ".join(str(value or "").split()).strip(" .")
        if not value or value in {".", ".."} or len(value) > 160:
            raise ValueError("A valid project folder name is required")
        if any(character in value for character in {"/", "\\", "\x00"}):
            raise ValueError("Project folder name cannot contain path separators")
        return value

    @staticmethod
    def _relative_upload_path(value: str) -> Path:
        raw = str(value or "").replace("\\", "/").strip("/")
        path = Path(raw)
        if (
            not raw or path.is_absolute() or len(raw) > 1_000
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("Upload contains an unsafe relative path")
        if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"Unsupported upload type: {path.suffix or 'unknown'}")
        return path

    def _upload_dir(self, upload_id: str) -> Path:
        if not upload_id.startswith("upl-") or not upload_id[4:].isalnum():
            raise ValueError("Unknown upload session")
        path = (self.upload_root / upload_id).resolve()
        if not path.is_relative_to(self.upload_root.resolve()):
            raise ValueError("Unknown upload session")
        return path

    def _upload_manifest(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        directory = self._upload_dir(upload_id)
        try:
            payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Unknown or expired upload session") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            raise ValueError("Upload manifest is invalid")
        return directory, payload

    def initiate_upload(self, project_name: str, files: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self._assert_available(require_gemini=True)
            project_name = self._project_name(project_name)
            if not files or len(files) > MAX_UPLOAD_FILES:
                raise ValueError(f"Choose between 1 and {MAX_UPLOAD_FILES} supported files")
            manifest_files: list[dict[str, Any]] = []
            seen: set[str] = set()
            total_size = 0
            for index, item in enumerate(files):
                if not isinstance(item, dict):
                    raise ValueError("Upload manifest is invalid")
                relative = self._relative_upload_path(str(item.get("relative_path", "")))
                try:
                    size = int(item.get("size", -1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Upload file size is invalid") from exc
                if size < 0 or size > MAX_UPLOAD_FILE_BYTES:
                    raise ValueError("An uploaded file exceeds the 2 GB per-file limit")
                identity = relative.as_posix().casefold()
                if identity in seen:
                    raise ValueError("Upload contains duplicate relative file paths")
                seen.add(identity)
                total_size += size
                manifest_files.append({
                    "file_id": f"file-{index + 1:05d}",
                    "relative_path": relative.as_posix(),
                    "size": size,
                    "uploaded": False,
                })
            if total_size > MAX_UPLOAD_TOTAL_BYTES:
                raise ValueError("The selected folder exceeds the 10 GB upload limit")
            upload_id = "upl-" + uuid.uuid4().hex[:16]
            directory = self._upload_dir(upload_id)
            (directory / "files").mkdir(parents=True, exist_ok=False)
            payload = {
                "schema_version": "1.0",
                "upload_id": upload_id,
                "project_name": project_name,
                "created_at": _now(),
                "total_size": total_size,
                "files": manifest_files,
            }
            _atomic_json(directory / "manifest.json", payload)
            return {
                "upload_id": upload_id,
                "project_name": project_name,
                "file_count": len(manifest_files),
                "size_bytes": total_size,
                "files": [
                    {key: row[key] for key in ("file_id", "relative_path", "size")}
                    for row in manifest_files
                ],
            }

    def upload_file(self, upload_id: str, file_id: str, stream: BinaryIO, content_length: int) -> dict[str, Any]:
        with self._lock:
            if not self.enabled:
                raise PermissionError("The ingestion entrance is disabled on this server")
            directory, manifest = self._upload_manifest(upload_id)
            row = next((item for item in manifest["files"] if item.get("file_id") == file_id), None)
            if row is None:
                raise ValueError("Unknown file in upload session")
            expected = int(row["size"])
            if content_length != expected:
                raise ValueError("Uploaded file size does not match its manifest")
            relative = self._relative_upload_path(str(row["relative_path"]))
            destination = (directory / "files" / relative).resolve()
            if not destination.is_relative_to((directory / "files").resolve()):
                raise ValueError("Upload path escaped its staging directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".part")
            remaining = expected
            with temporary.open("wb") as output:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                temporary.unlink(missing_ok=True)
                raise ValueError("Upload ended before the declared file size")
            os.replace(temporary, destination)
            row["uploaded"] = True
            row["sha256"] = self._sha256(destination)
            _atomic_json(directory / "manifest.json", manifest)
            uploaded = sum(bool(item.get("uploaded")) for item in manifest["files"])
            return {"upload_id": upload_id, "file_id": file_id, "uploaded_files": uploaded, "total_files": len(manifest["files"])}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        with self._lock:
            self._assert_available(require_gemini=True)
            directory, manifest = self._upload_manifest(upload_id)
            missing = [row["relative_path"] for row in manifest["files"] if not row.get("uploaded")]
            if missing:
                raise ValueError(f"Upload is incomplete: {len(missing)} file(s) are missing")
            project_name = self._project_name(str(manifest["project_name"]))
            destination_root = (self.staging_root / project_name).resolve()
            if not destination_root.is_relative_to(self.staging_root):
                raise ValueError("Project folder escaped the staging directory")
            destination_root.mkdir(parents=True, exist_ok=True)
            for row in manifest["files"]:
                relative = self._relative_upload_path(str(row["relative_path"]))
                source = (directory / "files" / relative).resolve()
                destination = (destination_root / relative).resolve()
                if not destination.is_relative_to(destination_root):
                    raise ValueError("Upload path escaped the project folder")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if self._sha256(destination) == str(row.get("sha256", "")):
                        source.unlink(missing_ok=True)
                        continue
                    destination = destination.with_name(
                        f"{destination.stem}__upload_{upload_id[-6:]}{destination.suffix}"
                    )
                os.replace(source, destination)
            shutil.rmtree(directory, ignore_errors=True)
            site_id = self._site_id(project_name)
        return self.start("ingest", site_id, confirmed=True)

    def start(self, mode: str, site_id: str = "", *, confirmed: bool = False) -> dict[str, Any]:
        if not self.enabled:
            raise PermissionError("The ingestion entrance is disabled on this server")
        if mode not in JOB_MODES:
            raise ValueError("Unknown ingestion mode")
        if mode in {"prescan", "ingest"} and not site_id:
            raise ValueError("Choose one staged project folder")
        if mode == "ingest" and not confirmed:
            raise ValueError("Verified ingestion requires explicit confirmation")
        dependencies = self._dependencies()
        if mode in {"prescan", "ingest"} and not dependencies["gemini_key"]:
            raise RuntimeError("Gemini is not configured on this server")
        site_name = self._site_name(site_id) if site_id else "All staged sources"
        with self._lock:
            if any(job.get("status") in {"queued", "running", "postprocessing"} for job in self._state["jobs"]):
                raise RuntimeError("Another ingestion task is already running")
            job_id = "ing-" + uuid.uuid4().hex[:12]
            job = {
                "job_id": job_id,
                "mode": mode,
                "site_id": site_id,
                "site_label": site_name,
                "status": "queued",
                "stage": "queued",
                "created_at": _now(),
                "started_at": "",
                "finished_at": "",
                "message": "Queued",
                "return_code": None,
                "log_file": f"{job_id}.log",
            }
            self._state["jobs"].append(job)
            self._state["jobs"] = self._state["jobs"][-25:]
            self._save()
            self._active_thread = threading.Thread(target=self._execute, args=(job_id,), daemon=True)
            self._active_thread.start()
            return self.snapshot()

    def _command(self, mode: str, site_name: str) -> list[str]:
        command = [
            sys.executable, "phase2/incremental_update.py",
            "--workspace-root", str(self.workspace),
        ]
        if mode == "inventory":
            command.append("--inventory-only")
        elif mode == "prescan":
            command.extend(["--prescan-only", "--prescan-site", site_name])
        else:
            command.extend(["--site", site_name])
        return command

    def _set_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = next(item for item in self._state["jobs"] if item.get("job_id") == job_id)
            job.update(changes)
            self._save()
            return job

    def _run_step(self, command: list[str], log, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        result = self._run_command(
            command,
            cwd=self.workspace,
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Command failed with exit code {result.returncode}")
        return result

    def _execute(self, job_id: str) -> None:
        queued_job = self._job(job_id)
        mode = str(queued_job["mode"])
        stage = "inventory" if mode == "inventory" else "prescan" if mode == "prescan" else "ingestion"
        job = self._set_job(
            job_id,
            status="running",
            stage=stage,
            started_at=_now(),
            message="Running",
        )
        self.log_root.mkdir(parents=True, exist_ok=True)
        log_path = self.log_root / str(job["log_file"])
        env = dict(os.environ)
        if self.gemini_api_key:
            env["GEMINI_API_KEY"] = self.gemini_api_key
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"[{_now()}] Starting {job['mode']} for {job['site_label']}\n")
                if job["mode"] == "ingest":
                    self._set_job(job_id, stage="inventory", message="Inventorying uploaded files")
                    self._run_step(self._command("inventory", str(job["site_label"])), log, env)
                    self._set_job(job_id, stage="prescan", message="Prescanning the uploaded project")
                    self._run_step(self._command("prescan", str(job["site_label"])), log, env)
                    self._set_job(job_id, stage="ingestion", message="Extracting and verifying evidence")
                    self._run_step(self._command("ingest", str(job["site_label"])), log, env)
                else:
                    self._run_step(self._command(str(job["mode"]), str(job["site_label"])), log, env)
                if job["mode"] == "ingest":
                    self._set_job(job_id, status="postprocessing", stage="source_registry", message="Refreshing source registry")
                    self._run_step([sys.executable, "web_app/migrate_sources.py"], log, env)
                    self._set_job(job_id, stage="search_index", message="Refreshing search metadata")
                    self._run_step([sys.executable, "web_app/build_search_index.py", "--metadata-only"], log, env)
                log.write(f"[{_now()}] Completed successfully\n")
            if job["mode"] == "ingest" and self.on_dataset_changed:
                self.on_dataset_changed()
            self._set_job(job_id, status="completed", stage="complete", finished_at=_now(), message="Completed successfully", return_code=0)
        except Exception as exc:  # The failure is persisted for maintainers.
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"[{_now()}] ERROR: {exc}\n")
            except OSError:
                pass
            self._set_job(job_id, status="failed", stage="failed", finished_at=_now(), message=str(exc), return_code=1)

    def _job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return next(item for item in self._state["jobs"] if item.get("job_id") == job_id)
