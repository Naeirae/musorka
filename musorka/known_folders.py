from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes
from pathlib import Path


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    value = uuid.UUID(text)
    fields = value.fields
    data4 = (ctypes.c_ubyte * 8)(fields[3], fields[4], *fields[5].to_bytes(6, "big"))
    return GUID(fields[0], fields[1], fields[2], data4)


def _known_folder(guid_text: str) -> Path | None:
    if os.name != "nt":
        return None
    path_ptr = ctypes.c_wchar_p()
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        folder_id = _guid(guid_text)
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
        hr = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, wintypes.HANDLE(0), ctypes.byref(path_ptr)
        )
        if hr != 0 or not path_ptr.value:
            return None
        return Path(path_ptr.value)
    except Exception:
        return None
    finally:
        try:
            if path_ptr.value:
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        except Exception:
            pass


def default_scan_roots() -> list[tuple[str, Path]]:
    home = Path.home()
    desktop = _known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641") or (home / "Desktop")
    downloads = _known_folder("374DE290-123F-4565-9164-39C4925E467B") or (home / "Downloads")
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, path in [("Рабочий стол", desktop), ("Загрузки", downloads)]:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append((label, path))
    return roots



def _normalized_path_key(path: Path) -> str:
    try:
        value = path.resolve(strict=False)
    except OSError:
        value = path.absolute()
    return os.path.normcase(os.path.normpath(str(value)))


def _is_same_or_child(path: Path, parent: Path) -> bool:
    child_key = _normalized_path_key(path)
    parent_key = _normalized_path_key(parent)
    try:
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except ValueError:
        return False


def scan_roots_with_custom(custom_paths: list[Path] | tuple[Path, ...] | None = None) -> list[tuple[str, Path]]:
    """Return default roots plus user-selected folders, without overlapping scans.

    If one selected root contains another, only the outer root is scanned. Every
    effective root is still represented at depth 0 by the scanner and therefore
    remains protected from folder-delete actions.
    """
    candidates: list[tuple[str, Path]] = list(default_scan_roots())
    for raw in custom_paths or []:
        path = Path(raw)
        name = path.name or str(path)
        candidates.append((f"Выбранная · {name}", path))

    roots: list[tuple[str, Path]] = []
    for label, path in candidates:
        # Exact duplicate or a folder already covered by an existing root.
        if any(_is_same_or_child(path, existing_path) for _, existing_path in roots):
            continue
        # If this new root contains earlier roots, it supersedes them and avoids
        # duplicate traversal / conflicting depth values for the same folder.
        roots = [
            (existing_label, existing_path)
            for existing_label, existing_path in roots
            if not _is_same_or_child(existing_path, path)
        ]
        roots.append((label, path))
    return roots
