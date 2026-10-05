from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from .db import Database
from .drive import DriveClient


ACTION_LABELS = {
    "trash": "В корзину",
    "permanent": "Удалить навсегда",
    "drive": "В Google Drive",
    "local_downloads": r"В C:\\Загрузки",
}


def _send_to_trash(path: Path) -> None:
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise RuntimeError("Не установлен send2trash. Запустите install_and_run.bat.") from exc
    send2trash(str(path))


def folder_snapshot(path: Path) -> tuple[int, int]:
    """Return recursive regular-file count/bytes, failing closed on symlinks/errors."""
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(dirpath)
        for dirname in list(dirnames):
            child = current / dirname
            try:
                if child.is_symlink():
                    raise RuntimeError(f"В папке есть ссылка/junction: {child}. Удаление остановлено.")
            except OSError as exc:
                raise RuntimeError(f"Не удалось проверить папку {child}: {exc}") from exc
        for filename in filenames:
            file_path = current / filename
            try:
                if file_path.is_symlink():
                    raise RuntimeError(f"В папке есть символическая ссылка: {file_path}. Удаление остановлено.")
                st = file_path.stat()
            except OSError as exc:
                raise RuntimeError(f"Не удалось проверить файл {file_path}: {exc}") from exc
            if file_path.is_file():
                count += 1
                total += int(st.st_size)
    return count, total


def _execute_file_row(db: Database, drive: DriveClient, row) -> tuple[bool, str]:
    path = Path(row["path"])
    action = row["action"]
    if not path.exists():
        error = "Файл уже отсутствует по указанному пути."
        db.mark_execution(row["id"], "failed", error)
        db.add_history(str(path), action, "failed", error)
        return False, error

    try:
        st = path.stat()
        if int(st.st_size) != int(row["size"]) or abs(float(st.st_mtime) - float(row["mtime"])) > 1.0:
            raise RuntimeError(
                "Файл изменился после сканирования. Действие остановлено; пересканируйте и примите решение заново."
            )

        if action == "trash":
            _send_to_trash(path)
            details = "Перемещено в Корзину Windows."
        elif action == "permanent":
            if path.is_dir():
                raise RuntimeError("Ожидался файл, но по пути находится каталог.")
            path.unlink()
            details = "Файл удалён навсегда, минуя Корзину Windows."
        elif action == "drive":
            folder_id = row["drive_folder_id"] or "root"
            uploaded = drive.upload_and_verify(path, folder_id)
            _send_to_trash(path)
            details = (
                f"Загружено и проверено в Google Drive: {uploaded['name']} "
                f"({uploaded['id']}); локальный файл перемещён в Корзину."
            )
        elif action == "local_downloads":
            if os.name != "nt":
                raise RuntimeError(r"Перенос в C:\Загрузки доступен только на Windows.")
            target_dir = Path(r"C:\Загрузки")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            try:
                same = path.resolve() == target.resolve()
            except OSError:
                same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(target))
            if same:
                details = r"Файл уже находится в C:\Загрузки."
            else:
                if target.exists():
                    raise RuntimeError(
                        f"В C:\\Загрузки уже есть файл с именем {path.name}. "
                        "Ничего не перезаписано; переименуйте один из файлов и повторите."
                    )
                shutil.move(str(path), str(target))
                details = f"Перемещено в {target}."
        else:
            raise RuntimeError(f"Неизвестное действие: {action}")
        db.mark_execution(row["id"], "done", "")
        db.add_history(str(path), action, "done", details)
        db.clear_action_after_success(row["id"])
        return True, details
    except Exception as exc:
        error = str(exc)
        db.mark_execution(row["id"], "failed", error)
        db.add_history(str(path), action, "failed", error)
        return False, error


def _execute_folder_row(db: Database, row) -> tuple[bool, str]:
    path = Path(row["path"])
    action = row["action"]
    if int(row["depth"]) <= 0:
        error = "Корневая папка зоны сканирования защищена от удаления."
        db.mark_folder_execution(row["id"], "failed", error)
        db.add_history(str(path), f"folder_{action}", "failed", error)
        return False, error
    if not path.exists():
        error = "Папка уже отсутствует по указанному пути."
        db.mark_folder_execution(row["id"], "failed", error)
        db.add_history(str(path), f"folder_{action}", "failed", error)
        return False, error
    if not path.is_dir():
        error = "По указанному пути больше не папка."
        db.mark_folder_execution(row["id"], "failed", error)
        db.add_history(str(path), f"folder_{action}", "failed", error)
        return False, error

    try:
        file_count, total_size = folder_snapshot(path)
        if file_count != int(row["file_count"]) or total_size != int(row["total_size"]):
            raise RuntimeError(
                "Содержимое папки изменилось после сканирования "
                f"(было {row['file_count']} файлов / {row['total_size']} байт, "
                f"сейчас {file_count} / {total_size}). Пересканируйте перед удалением."
            )
        if action == "trash":
            _send_to_trash(path)
            details = f"Папка целиком перемещена в Корзину Windows: {file_count} файлов."
        elif action == "permanent":
            shutil.rmtree(path)
            details = f"Папка и её содержимое удалены навсегда: {file_count} файлов."
        else:
            raise RuntimeError(f"Неизвестное действие для папки: {action}")
        db.mark_folder_execution(row["id"], "done", "")
        db.add_history(str(path), f"folder_{action}", "done", details)
        db.clear_folder_action_after_success(row["id"])
        return True, details
    except Exception as exc:
        error = str(exc)
        db.mark_folder_execution(row["id"], "failed", error)
        db.add_history(str(path), f"folder_{action}", "failed", error)
        return False, error


def execute_plan(
    db: Database,
    drive: DriveClient,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    file_rows = db.pending_plan()
    folder_rows = db.pending_folder_plan()
    stats = {"done": 0, "failed": 0}
    total = len(file_rows) + len(folder_rows)
    index = 0

    # Files first. Folder actions clear descendant file actions at marking time,
    # so this order mainly preserves previous behaviour for unrelated items.
    for row in file_rows:
        index += 1
        path = Path(row["path"])
        action = row["action"]
        if progress:
            progress(index, total, f"{ACTION_LABELS.get(action, action)}: {path.name}")
        ok, _ = _execute_file_row(db, drive, row)
        stats["done" if ok else "failed"] += 1

    for row in folder_rows:
        index += 1
        path = Path(row["path"])
        action = row["action"]
        if progress:
            progress(index, total, f"Папка · {ACTION_LABELS.get(action, action)}: {path.name}")
        ok, _ = _execute_folder_row(db, row)
        stats["done" if ok else "failed"] += 1

    return stats
