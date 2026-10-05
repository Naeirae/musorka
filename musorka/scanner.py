from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


EXPORT_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".xml", ".parquet", ".sqlite", ".db"}
DOC_EXT = {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pages"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}
INSTALLER_EXT = {".exe", ".msi", ".msix", ".appx", ".appxbundle", ".bat", ".cmd", ".ps1"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".ipynb", ".java", ".kt", ".kts", ".html", ".css", ".yaml", ".yml", ".toml"}
PRESENTATION_EXT = {".ppt", ".pptx", ".odp", ".key"}
PDF_EXT = {".pdf"}


SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information"}


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in EXPORT_EXT:
        return "Выгрузки / аналитика"
    if ext in PDF_EXT:
        return "PDF"
    if ext in DOC_EXT:
        return "Документы"
    if ext in PRESENTATION_EXT:
        return "Презентации"
    if ext in ARCHIVE_EXT:
        return "Архивы"
    if ext in IMAGE_EXT:
        return "Изображения"
    if ext in VIDEO_EXT:
        return "Видео"
    if ext in AUDIO_EXT:
        return "Аудио"
    if ext in INSTALLER_EXT:
        return "Установщики / скрипты"
    if ext in CODE_EXT:
        return "Код / данные"
    return "Прочее"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@dataclass(slots=True)
class ScanItem:
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    ctime: float
    area: str
    category: str
    sha256: str = ""
    duplicate_group: str = ""


@dataclass(slots=True)
class ScanFolder:
    path: str
    name: str
    parent_path: str
    area: str
    depth: int
    file_count: int = 0
    total_size: int = 0
    mtime: float = 0.0


def _clean_dirnames(dirpath: Path, dirnames: list[str]) -> None:
    kept = []
    for name in dirnames:
        if name.startswith(".") or name in SKIP_DIRS:
            continue
        candidate = dirpath / name
        try:
            if candidate.is_symlink():
                continue
        except OSError:
            continue
        kept.append(name)
    dirnames[:] = kept


def _folder_depth(path: Path, root: Path) -> int:
    try:
        return len(path.relative_to(root).parts)
    except ValueError:
        return 0


def _ancestors_inside(path: Path, root: Path):
    current = path
    while True:
        try:
            current.relative_to(root)
        except ValueError:
            return
        yield current
        if current == root:
            return
        current = current.parent


def _mark_duplicates(items: list[ScanItem], progress: Callable[[str], None] | None = None) -> None:
    by_size: dict[int, list[ScanItem]] = defaultdict(list)
    for item in items:
        if item.size > 0:
            by_size[item.size].append(item)

    candidates = [group for group in by_size.values() if len(group) > 1]
    total = sum(len(group) for group in candidates)
    done = 0
    by_hash: dict[str, list[ScanItem]] = defaultdict(list)
    for group in candidates:
        for item in group:
            done += 1
            if progress and (done == 1 or done % 5 == 0 or done == total):
                progress(f"Проверяю дубли: {done}/{total}")
            try:
                item.sha256 = sha256_file(Path(item.path))
            except (OSError, PermissionError):
                item.sha256 = ""
            if item.sha256:
                by_hash[item.sha256].append(item)

    dup_index = 1
    for group in by_hash.values():
        if len(group) < 2:
            continue
        label = f"D{dup_index:04d}"
        dup_index += 1
        for item in group:
            item.duplicate_group = label


def scan_roots_with_folders(
    roots: list[tuple[str, Path]],
    progress: Callable[[str], None] | None = None,
) -> tuple[list[ScanItem], list[ScanFolder]]:
    """Scan files and folders under roots.

    Folder counters are recursive: a folder's file_count/total_size include files
    in all nested subfolders. The scanner records directories as navigation and
    filtering metadata only; destructive folder actions remain intentionally out
    of scope for the MVP.
    """
    items: list[ScanItem] = []
    folders: dict[str, ScanFolder] = {}

    for area, raw_root in roots:
        root = Path(raw_root)
        if not root.exists() or not root.is_dir():
            continue
        if progress:
            progress(f"Сканирую: {area} — {root}")

        for dirpath_raw, dirnames, filenames in os.walk(root, followlinks=False):
            dirpath = Path(dirpath_raw)
            _clean_dirnames(dirpath, dirnames)

            key = str(dirpath)
            try:
                st_dir = dirpath.stat()
                folder_mtime = float(st_dir.st_mtime)
            except (OSError, PermissionError):
                folder_mtime = 0.0

            folders.setdefault(
                key,
                ScanFolder(
                    path=key,
                    name=dirpath.name or str(dirpath),
                    parent_path="" if dirpath == root else str(dirpath.parent),
                    area=area,
                    depth=_folder_depth(dirpath, root),
                    mtime=folder_mtime,
                ),
            )

            # Ensure visible child directories are recorded even when empty.
            for dirname in dirnames:
                child = dirpath / dirname
                ckey = str(child)
                try:
                    child_mtime = float(child.stat().st_mtime)
                except (OSError, PermissionError):
                    child_mtime = 0.0
                folders.setdefault(
                    ckey,
                    ScanFolder(
                        path=ckey,
                        name=child.name,
                        parent_path=str(dirpath),
                        area=area,
                        depth=_folder_depth(child, root),
                        mtime=child_mtime,
                    ),
                )

            for filename in filenames:
                if filename.startswith("~$"):
                    continue
                path = dirpath / filename
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    st = path.stat()
                except (OSError, PermissionError):
                    continue

                item = ScanItem(
                    path=str(path),
                    name=path.name,
                    ext=path.suffix.lower(),
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    ctime=float(st.st_ctime),
                    area=area,
                    category=classify(path),
                )
                items.append(item)

                for ancestor in _ancestors_inside(dirpath, root):
                    akey = str(ancestor)
                    folder = folders.get(akey)
                    if folder is None:
                        folder = ScanFolder(
                            path=akey,
                            name=ancestor.name or str(ancestor),
                            parent_path="" if ancestor == root else str(ancestor.parent),
                            area=area,
                            depth=_folder_depth(ancestor, root),
                        )
                        folders[akey] = folder
                    folder.file_count += 1
                    folder.total_size += item.size

    _mark_duplicates(items, progress)
    ordered_folders = sorted(folders.values(), key=lambda f: (f.area.lower(), f.path.lower()))
    return items, ordered_folders


def scan_roots(
    roots: list[tuple[str, Path]],
    progress: Callable[[str], None] | None = None,
) -> list[ScanItem]:
    """Compatibility helper used by existing tests and callers."""
    items, _folders = scan_roots_with_folders(roots, progress)
    return items
