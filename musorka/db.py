from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    ext TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    ctime REAL NOT NULL DEFAULT 0,
    area TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    duplicate_group TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    later_note TEXT NOT NULL DEFAULT '',
    drive_folder_id TEXT NOT NULL DEFAULT '',
    drive_folder_path TEXT NOT NULL DEFAULT '',
    exists_now INTEGER NOT NULL DEFAULT 1,
    execution_status TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    last_seen REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_action ON files(action);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_duplicate ON files(duplicate_group);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    parent_path TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    action TEXT NOT NULL DEFAULT '',
    execution_status TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    exists_now INTEGER NOT NULL DEFAULT 1,
    last_seen REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_folders_area ON folders(area);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_path);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    install_location TEXT NOT NULL DEFAULT '',
    uninstall_string TEXT NOT NULL DEFAULT '',
    quiet_uninstall_string TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    migration_status TEXT NOT NULL DEFAULT '',
    last_seen REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def app_data_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        path = root / "Musorka"
    else:
        path = Path.home() / ".musorka"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or (app_data_dir() / "musorka.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        self._ensure_column("folders", "action", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("folders", "execution_status", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("folders", "last_error", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("programs", "quiet_uninstall_string", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_action ON folders(action)")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    def upsert_files(self, items: Iterable[Any]) -> None:
        seen = time.time()
        self.conn.execute("UPDATE files SET exists_now=0")
        sql = """
        INSERT INTO files(path,name,ext,size,mtime,ctime,area,category,sha256,duplicate_group,exists_now,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,1,?)
        ON CONFLICT(path) DO UPDATE SET
          name=excluded.name, ext=excluded.ext, size=excluded.size,
          mtime=excluded.mtime, ctime=excluded.ctime, area=excluded.area,
          category=excluded.category, sha256=excluded.sha256,
          duplicate_group=excluded.duplicate_group, exists_now=1,
          last_seen=excluded.last_seen
        """
        rows = [
            (
                i.path, i.name, i.ext, i.size, i.mtime, i.ctime,
                i.area, i.category, i.sha256, i.duplicate_group, seen
            )
            for i in items
        ]
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def upsert_folders(self, folders: Iterable[Any]) -> None:
        seen = time.time()
        self.conn.execute("UPDATE folders SET exists_now=0")
        sql = """
        INSERT INTO folders(path,name,parent_path,area,depth,file_count,total_size,mtime,exists_now,last_seen)
        VALUES(?,?,?,?,?,?,?,?,1,?)
        ON CONFLICT(path) DO UPDATE SET
          name=excluded.name, parent_path=excluded.parent_path, area=excluded.area,
          depth=excluded.depth, file_count=excluded.file_count, total_size=excluded.total_size,
          mtime=excluded.mtime, exists_now=1, last_seen=excluded.last_seen
        """
        rows = [
            (
                f.path, f.name, f.parent_path, f.area, int(f.depth),
                int(f.file_count), int(f.total_size), float(f.mtime), seen
            )
            for f in folders
        ]
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def list_files(
        self,
        search: str = "",
        category: str = "",
        ext: str | None = None,
        folder_path: str = "",
        action: str | None = None,
        exists_only: bool = True,
    ) -> list[sqlite3.Row]:
        where = []
        params: list[Any] = []
        if exists_only:
            where.append("exists_now=1")
        if search:
            where.append("(name LIKE ? OR path LIKE ?)")
            token = f"%{search}%"
            params.extend([token, token])
        if category:
            where.append("category=?")
            params.append(category)
        if ext is not None:
            where.append("ext=?")
            params.append(ext)
        if folder_path:
            normalized = folder_path.rstrip("\\/")
            where.append("(path=? OR path LIKE ?)")
            params.extend([normalized, normalized + os.sep + "%"])
        if action is not None:
            where.append("action=?")
            params.append(action)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return self.conn.execute(
            "SELECT * FROM files" + clause + " ORDER BY area, path COLLATE NOCASE",
            params,
        ).fetchall()

    def file_by_id(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def _clear_ancestor_folder_actions_for_files(self, ids: list[int]) -> None:
        if not ids:
            return
        rows = self.conn.execute(
            f"SELECT path FROM files WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        folder_rows = self.conn.execute(
            "SELECT id,path FROM folders WHERE exists_now=1 AND action IN ('trash','permanent')"
        ).fetchall()
        clear_ids: set[int] = set()
        for file_row in rows:
            file_path = os.path.normcase(os.path.normpath(str(file_row["path"])))
            for folder_row in folder_rows:
                folder_path = os.path.normcase(os.path.normpath(str(folder_row["path"])))
                try:
                    common = os.path.commonpath([file_path, folder_path])
                except ValueError:
                    continue
                if common == folder_path and file_path != folder_path:
                    clear_ids.add(int(folder_row["id"]))
        if clear_ids:
            self.conn.execute(
                f"UPDATE folders SET action='', execution_status='', last_error='' WHERE id IN ({','.join('?' for _ in clear_ids)})",
                list(clear_ids),
            )

    def set_file_action(
        self,
        ids: Iterable[int],
        action: str,
        later_note: str | None = None,
        drive_folder_id: str | None = None,
        drive_folder_path: str | None = None,
    ) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        self._clear_ancestor_folder_actions_for_files(ids)
        placeholders = ",".join("?" for _ in ids)
        fields = ["action=?", "execution_status=''", "last_error=''"]
        params: list[Any] = [action]
        if later_note is not None:
            fields.append("later_note=?")
            params.append(later_note)
        elif action != "later":
            fields.append("later_note='' ")
        if drive_folder_id is not None:
            fields.append("drive_folder_id=?")
            params.append(drive_folder_id)
        elif action != "drive":
            fields.append("drive_folder_id='' ")
        if drive_folder_path is not None:
            fields.append("drive_folder_path=?")
            params.append(drive_folder_path)
        elif action != "drive":
            fields.append("drive_folder_path='' ")
        params.extend(ids)
        self.conn.execute(
            f"UPDATE files SET {', '.join(fields)} WHERE id IN ({placeholders})",
            params,
        )
        self.conn.commit()

    def update_later_note(self, file_id: int, note: str) -> None:
        self.conn.execute("UPDATE files SET later_note=? WHERE id=?", (note, file_id))
        self.conn.commit()

    def pending_plan(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE exists_now=1 AND action IN ('trash','permanent','drive','local_downloads') "
            "ORDER BY action, path COLLATE NOCASE"
        ).fetchall()

    def later_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE action='later' ORDER BY exists_now DESC, path COLLATE NOCASE"
        ).fetchall()

    def mark_execution(self, file_id: int, status: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE files SET execution_status=?, last_error=?, exists_now=CASE WHEN ?='done' THEN 0 ELSE exists_now END WHERE id=?",
            (status, error, status, file_id),
        )
        self.conn.commit()

    def clear_action_after_success(self, file_id: int) -> None:
        self.conn.execute(
            "UPDATE files SET action='', drive_folder_id='', drive_folder_path='' WHERE id=?",
            (file_id,),
        )
        self.conn.commit()

    def folders(
        self,
        search: str = "",
        area: str = "",
        exists_only: bool = True,
    ) -> list[sqlite3.Row]:
        where = []
        params: list[Any] = []
        if exists_only:
            where.append("exists_now=1")
        if search:
            where.append("(name LIKE ? OR path LIKE ?)")
            token = f"%{search}%"
            params.extend([token, token])
        if area:
            where.append("area=?")
            params.append(area)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return self.conn.execute(
            "SELECT * FROM folders" + clause + " ORDER BY area, depth, path COLLATE NOCASE",
            params,
        ).fetchall()

    def folder_by_id(self, folder_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()

    def folder_areas(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT area FROM folders WHERE exists_now=1 AND area<>'' ORDER BY area"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def folder_action_conflicts(self, ids: Iterable[int]) -> list[sqlite3.Row]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        rows = self.conn.execute(
            f"SELECT path FROM folders WHERE id IN ({','.join('?' for _ in ids)}) AND exists_now=1 AND depth>0",
            ids,
        ).fetchall()
        conflicts: dict[int, sqlite3.Row] = {}
        for row in rows:
            folder_path = str(row["path"]).rstrip("\\/")
            found = self.conn.execute(
                "SELECT * FROM files WHERE exists_now=1 AND path LIKE ? "
                "AND action NOT IN ('','trash','permanent') ORDER BY path COLLATE NOCASE",
                (folder_path + os.sep + "%",),
            ).fetchall()
            for item in found:
                conflicts[int(item["id"])] = item
        return list(conflicts.values())

    def set_folder_action(self, ids: Iterable[int], action: str) -> list[int]:
        ids = [int(i) for i in ids]
        if not ids:
            return []

        rows = self.conn.execute(
            f"SELECT * FROM folders WHERE id IN ({','.join('?' for _ in ids)}) AND exists_now=1",
            ids,
        ).fetchall()
        if action == "":
            self.conn.execute(
                f"UPDATE folders SET action='', execution_status='', last_error='' WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
            self.conn.commit()
            return ids

        candidates = [r for r in rows if int(r["depth"]) > 0]
        candidates.sort(key=lambda r: (len(str(r["path"])), str(r["path"]).lower()))
        selected: list[sqlite3.Row] = []
        for row in candidates:
            raw_path = str(row["path"]).rstrip("\\/")
            protected = self.conn.execute(
                "SELECT 1 FROM files WHERE exists_now=1 AND path LIKE ? "
                "AND action NOT IN ('','trash','permanent') LIMIT 1",
                (raw_path + os.sep + "%",),
            ).fetchone()
            if protected:
                continue
            path = os.path.normcase(os.path.normpath(raw_path))
            nested = False
            for parent in selected:
                parent_path = os.path.normcase(os.path.normpath(str(parent["path"])))
                try:
                    if os.path.commonpath([path, parent_path]) == parent_path and path != parent_path:
                        nested = True
                        break
                except ValueError:
                    pass
            if not nested:
                selected.append(row)

        # Reset every explicitly selected row first. Nested selections are intentionally
        # absorbed by their selected parent so one tree is deleted only once.
        self.conn.execute(
            f"UPDATE folders SET action='', execution_status='', last_error='' WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        )

        chosen_ids: list[int] = []
        for row in selected:
            folder_id = int(row["id"])
            folder_path = str(row["path"]).rstrip("\\/")
            chosen_ids.append(folder_id)
            self.conn.execute(
                "UPDATE folders SET action=?, execution_status='', last_error='' WHERE id=?",
                (action, folder_id),
            )
            # Parent folder deletion supersedes every child file/folder action.
            self.conn.execute(
                "UPDATE files SET action='', execution_status='', last_error='', drive_folder_id='', drive_folder_path='' "
                "WHERE path LIKE ?",
                (folder_path + os.sep + "%",),
            )
            self.conn.execute(
                "UPDATE folders SET action='', execution_status='', last_error='' "
                "WHERE id<>? AND path LIKE ?",
                (folder_id, folder_path + os.sep + "%"),
            )
        self.conn.commit()
        return chosen_ids

    def pending_folder_plan(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM folders WHERE exists_now=1 AND depth>0 AND action IN ('trash','permanent') "
            "ORDER BY action, path COLLATE NOCASE"
        ).fetchall()

    def mark_folder_execution(self, folder_id: int, status: str, error: str = "") -> None:
        row = self.folder_by_id(folder_id)
        if not row:
            return
        folder_path = str(row["path"]).rstrip("\\/")
        self.conn.execute(
            "UPDATE folders SET execution_status=?, last_error=?, exists_now=CASE WHEN ?='done' THEN 0 ELSE exists_now END WHERE id=?",
            (status, error, status, folder_id),
        )
        if status == "done":
            self.conn.execute(
                "UPDATE folders SET exists_now=0, action='', execution_status='done', last_error='' WHERE path LIKE ?",
                (folder_path + os.sep + "%",),
            )
            self.conn.execute(
                "UPDATE files SET exists_now=0, action='', execution_status='done', last_error='' WHERE path LIKE ?",
                (folder_path + os.sep + "%",),
            )
        self.conn.commit()

    def clear_folder_action_after_success(self, folder_id: int) -> None:
        self.conn.execute("UPDATE folders SET action='' WHERE id=?", (folder_id,))
        self.conn.commit()

    def pending_plan_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in self.pending_plan():
            item = dict(row)
            item["item_type"] = "file"
            items.append(item)
        for row in self.pending_folder_plan():
            item = dict(row)
            item["item_type"] = "folder"
            item["size"] = int(item.get("total_size", 0))
            items.append(item)
        return sorted(items, key=lambda x: (str(x.get("action", "")), str(x.get("path", "")).lower()))

    def add_history(self, path: str, action: str, result: str, details: str = "") -> None:
        self.conn.execute(
            "INSERT INTO history(ts,path,action,result,details) VALUES(?,?,?,?,?)",
            (time.time(), path, action, result, details),
        )
        self.conn.commit()

    def history(self, limit: int = 1000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def summary(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
              COUNT(*) AS files,
              COALESCE(SUM(size),0) AS bytes,
              SUM(CASE WHEN action='' THEN 1 ELSE 0 END) AS undecided,
              SUM(CASE WHEN action='later' THEN 1 ELSE 0 END) AS later_count,
              SUM(CASE WHEN action IN ('trash','permanent','drive','local_downloads') THEN 1 ELSE 0 END) AS file_plan_count,
              SUM(CASE WHEN duplicate_group<>'' THEN 1 ELSE 0 END) AS duplicate_count
            FROM files WHERE exists_now=1
            """
        ).fetchone()
        result = dict(row or {})
        folder_row = self.conn.execute(
            "SELECT COUNT(*) AS folders, SUM(CASE WHEN action IN ('trash','permanent') THEN 1 ELSE 0 END) AS folder_plan_count "
            "FROM folders WHERE exists_now=1"
        ).fetchone()
        result["folders"] = int(folder_row["folders"] if folder_row else 0)
        result["folder_plan_count"] = int((folder_row["folder_plan_count"] or 0) if folder_row else 0)
        result["plan_count"] = int(result.get("file_plan_count") or 0) + result["folder_plan_count"]
        return result

    def categories(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT category FROM files WHERE exists_now=1 AND category<>'' ORDER BY category"
        ).fetchall()
        return [r[0] for r in rows]

    def extensions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT ext FROM files WHERE exists_now=1 ORDER BY CASE WHEN ext='' THEN 0 ELSE 1 END, ext"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def upsert_programs(self, programs: list[dict[str, str]]) -> None:
        seen = time.time()
        for p in programs:
            key = p["program_key"]
            self.conn.execute(
                """
                INSERT INTO programs(program_key,name,version,publisher,install_location,uninstall_string,quiet_uninstall_string,scope,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(program_key) DO UPDATE SET
                  name=excluded.name,version=excluded.version,publisher=excluded.publisher,
                  install_location=excluded.install_location,uninstall_string=excluded.uninstall_string,
                  quiet_uninstall_string=excluded.quiet_uninstall_string,
                  scope=excluded.scope,last_seen=excluded.last_seen
                """,
                (
                    key, p.get("name", ""), p.get("version", ""), p.get("publisher", ""),
                    p.get("install_location", ""), p.get("uninstall_string", ""),
                    p.get("quiet_uninstall_string", ""), p.get("scope", ""), seen,
                ),
            )
        self.conn.commit()

    def programs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM programs ORDER BY name COLLATE NOCASE, version COLLATE NOCASE"
        ).fetchall()

    def program_by_id(self, program_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM programs WHERE id=?", (program_id,)).fetchone()

    def set_program_status(self, ids: Iterable[int], status: str) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE programs SET migration_status=? WHERE id IN ({placeholders})",
            [status, *ids],
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()
