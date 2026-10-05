from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .db import app_data_dir


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class DriveUnavailable(RuntimeError):
    pass


class DriveClient:
    def __init__(self):
        self.data_dir = app_data_dir()
        self.credentials_path = self.data_dir / "google-oauth-client.json"
        self.token_path = self.data_dir / "google-token.json"
        self._service = None

    def has_client_credentials(self) -> bool:
        return self.credentials_path.exists()

    def import_client_credentials(self, source: str | Path) -> None:
        source = Path(source)
        data = json.loads(source.read_text(encoding="utf-8"))
        if not ("installed" in data or "web" in data):
            raise DriveUnavailable("Это не OAuth client JSON из Google Cloud Console.")
        shutil.copy2(source, self.credentials_path)
        self._service = None

    def disconnect(self) -> None:
        if self.token_path.exists():
            self.token_path.unlink()
        self._service = None

    def service(self):
        if self._service is not None:
            return self._service
        if not self.credentials_path.exists():
            raise DriveUnavailable(
                "Сначала выберите OAuth client JSON для Desktop app в разделе Google Drive."
            )
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DriveUnavailable(
                "Не установлены Google Drive зависимости. Запустите install_and_run.bat."
            ) from exc

        creds = None
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), [DRIVE_SCOPE])
            except Exception:
                creds = None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), [DRIVE_SCOPE]
            )
            creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
            self.token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def list_folders(self, parent_id: str = "root") -> list[dict[str, str]]:
        service = self.service()
        escaped = parent_id.replace("'", "\\'")
        q = (
            f"'{escaped}' in parents and trashed=false and "
            "mimeType='application/vnd.google-apps.folder'"
        )
        result = service.files().list(
            q=q,
            fields="files(id,name)",
            orderBy="name_natural",
            pageSize=1000,
            spaces="drive",
        ).execute()
        return [{"id": f["id"], "name": f["name"]} for f in result.get("files", [])]

    def create_folder(self, name: str, parent_id: str = "root") -> dict[str, str]:
        service = self.service()
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        item = service.files().create(body=body, fields="id,name").execute()
        return {"id": item["id"], "name": item["name"]}

    def upload_and_verify(self, local_path: str | Path, parent_id: str = "root") -> dict[str, Any]:
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise DriveUnavailable("Не установлены Google Drive зависимости.") from exc

        service = self.service()
        media = MediaFileUpload(str(path), resumable=True)
        body = {"name": path.name, "parents": [parent_id]}
        created = service.files().create(
            body=body,
            media_body=media,
            fields="id,name,size,md5Checksum,webViewLink",
        ).execute()
        remote = service.files().get(
            fileId=created["id"], fields="id,name,size,md5Checksum,webViewLink"
        ).execute()

        local_size = path.stat().st_size
        remote_size = int(remote.get("size") or 0)
        size_ok = remote_size == local_size

        md5_ok = None
        remote_md5 = remote.get("md5Checksum") or ""
        if remote_md5:
            h = hashlib.md5()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            md5_ok = h.hexdigest().lower() == remote_md5.lower()

        verified = size_ok and (md5_ok is not False)
        if not verified:
            raise DriveUnavailable(
                f"Google Drive принял файл, но проверка не совпала: {path.name}. "
                f"Локально {local_size}, в Drive {remote_size}."
            )
        return {
            "id": remote["id"],
            "name": remote.get("name", path.name),
            "size": remote_size,
            "md5Checksum": remote_md5,
            "webViewLink": remote.get("webViewLink", ""),
            "verified": True,
        }
