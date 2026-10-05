from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def human_size(value: int | None) -> str:
    size = float(value or 0)
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "Б":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value or 0)} Б"


def fmt_ts(value: float | int | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def open_path(path: str | Path) -> None:
    p = str(path)
    if os.name == "nt":
        os.startfile(p)  # type: ignore[attr-defined]
    elif os.name == "posix":
        import subprocess
        subprocess.Popen(["xdg-open", p])
    else:
        raise RuntimeError("Открытие пути не поддерживается на этой системе.")
