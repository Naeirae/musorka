from __future__ import annotations

from pathlib import Path

from musorka.db import Database
from musorka.scanner import classify, scan_roots, scan_roots_with_folders
from musorka.known_folders import scan_roots_with_custom


def test_classify():
    assert classify(Path("export.csv")) == "Выгрузки / аналитика"
    assert classify(Path("book.pdf")) == "PDF"
    assert classify(Path("installer.exe")) == "Установщики / скрипты"


def test_scan_and_duplicate(tmp_path):
    a = tmp_path / "Desktop"
    b = tmp_path / "Downloads"
    a.mkdir(); b.mkdir()
    (a / "one.txt").write_text("same", encoding="utf-8")
    (b / "two.txt").write_text("same", encoding="utf-8")
    (b / "data.csv").write_text("x;y\n1;2\n", encoding="utf-8")
    items = scan_roots([("Рабочий стол", a), ("Загрузки", b)])
    assert len(items) == 3
    dup = [i for i in items if i.duplicate_group]
    assert len(dup) == 2
    assert len({i.duplicate_group for i in dup}) == 1


def test_folder_scan_is_recursive(tmp_path):
    root = tmp_path / "Downloads"
    nested = root / "analytics" / "2026"
    nested.mkdir(parents=True)
    (root / "top.txt").write_text("1234", encoding="utf-8")
    (nested / "data.csv").write_text("123456", encoding="utf-8")

    items, folders = scan_roots_with_folders([("Загрузки", root)])
    assert len(items) == 2
    by_path = {f.path: f for f in folders}
    assert by_path[str(root)].file_count == 2
    assert by_path[str(root)].total_size == 10
    assert by_path[str(root / "analytics")].file_count == 1
    assert by_path[str(nested)].file_count == 1


def test_format_and_folder_filter(tmp_path):
    root = tmp_path / "Downloads"
    nested = root / "analytics"
    nested.mkdir(parents=True)
    (root / "note.txt").write_text("a", encoding="utf-8")
    (nested / "data.csv").write_text("b", encoding="utf-8")
    (nested / "more.csv").write_text("c", encoding="utf-8")

    items, folders = scan_roots_with_folders([("Загрузки", root)])
    db = Database(tmp_path / "filter.sqlite3")
    db.upsert_files(items)
    db.upsert_folders(folders)

    csv_rows = db.list_files(ext=".csv")
    assert len(csv_rows) == 2
    nested_rows = db.list_files(folder_path=str(nested))
    assert len(nested_rows) == 2
    assert all(row["ext"] == ".csv" for row in nested_rows)
    assert len(db.folders()) >= 2
    db.close()


def test_later_registry_keeps_path(tmp_path):
    db = Database(tmp_path / "m.sqlite3")
    class Item:
        path = str(tmp_path / "sample.txt")
        name = "sample.txt"
        ext = ".txt"
        size = 10
        mtime = 1.0
        ctime = 1.0
        area = "Загрузки"
        category = "Документы"
        sha256 = ""
        duplicate_group = ""
    db.upsert_files([Item()])
    row = db.list_files()[0]
    db.set_file_action([row["id"]], "later", later_note="проверить")
    later = db.later_files()
    assert len(later) == 1
    assert later[0]["path"].endswith("sample.txt")
    assert later[0]["later_note"] == "проверить"
    db.close()


def test_folder_delete_plan_protects_scan_root_and_kept_files(tmp_path):
    root = tmp_path / "Downloads"
    child = root / "old_export"
    child.mkdir(parents=True)
    target = child / "keep.csv"
    target.write_text("keep", encoding="utf-8")

    items, folders = scan_roots_with_folders([("Загрузки", root)])
    db = Database(tmp_path / "folders.sqlite3")
    db.upsert_files(items)
    db.upsert_folders(folders)

    root_row = next(r for r in db.folders() if r["path"] == str(root))
    child_row = next(r for r in db.folders() if r["path"] == str(child))
    file_row = next(r for r in db.list_files() if r["path"] == str(target))

    assert db.set_folder_action([root_row["id"]], "permanent") == []
    db.set_file_action([file_row["id"]], "keep")
    assert len(db.folder_action_conflicts([child_row["id"]])) == 1
    assert db.set_folder_action([child_row["id"]], "permanent") == []
    assert db.folder_by_id(child_row["id"])["action"] == ""
    db.close()


def test_folder_permanent_delete_and_stale_guard(tmp_path):
    from musorka.actions import execute_plan

    class DummyDrive:
        pass

    root = tmp_path / "Downloads"
    good = root / "delete_me"
    stale = root / "changed_after_scan"
    good.mkdir(parents=True)
    stale.mkdir(parents=True)
    (good / "a.txt").write_text("abc", encoding="utf-8")
    (stale / "b.txt").write_text("123", encoding="utf-8")

    items, folders = scan_roots_with_folders([("Загрузки", root)])
    db = Database(tmp_path / "delete.sqlite3")
    db.upsert_files(items)
    db.upsert_folders(folders)
    rows = {r["path"]: r for r in db.folders()}
    db.set_folder_action([rows[str(good)]["id"], rows[str(stale)]["id"]], "permanent")

    (stale / "new.txt").write_text("new", encoding="utf-8")
    stats = execute_plan(db, DummyDrive())
    assert stats == {"done": 1, "failed": 1}
    assert not good.exists()
    assert stale.exists()
    assert db.folder_by_id(rows[str(stale)]["id"])["execution_status"] == "failed"
    db.close()


def test_prepare_msi_uninstall_command():
    from musorka.programs import prepare_uninstall_command

    assert prepare_uninstall_command('MsiExec.exe /I{ABC-123}') == 'MsiExec.exe /X{ABC-123}'
    assert prepare_uninstall_command('"C:\\Program Files\\App\\uninstall.exe" /remove') == '"C:\\Program Files\\App\\uninstall.exe" /remove'
    assert prepare_uninstall_command('') == ''



def test_migration_from_011_schema(tmp_path):
    import sqlite3
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            parent_path TEXT NOT NULL DEFAULT '',
            area TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            total_size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            exists_now INTEGER NOT NULL DEFAULT 1,
            last_seen REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            program_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '',
            publisher TEXT NOT NULL DEFAULT '',
            install_location TEXT NOT NULL DEFAULT '',
            uninstall_string TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            migration_status TEXT NOT NULL DEFAULT '',
            last_seen REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.close()
    db = Database(db_path)
    folder_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(folders)")}
    program_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(programs)")}
    assert {"action", "execution_status", "last_error"} <= folder_cols
    assert "quiet_uninstall_string" in program_cols
    db.close()


def test_local_downloads_action_is_in_plan(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    target = root / "move.txt"
    target.write_text("x", encoding="utf-8")
    items, folders = scan_roots_with_folders([("Загрузки", root)])
    db = Database(tmp_path / "plan.sqlite3")
    db.upsert_files(items)
    db.upsert_folders(folders)
    row = db.list_files()[0]
    db.set_file_action([row["id"]], "local_downloads")
    plan = db.pending_plan()
    assert len(plan) == 1
    assert plan[0]["action"] == "local_downloads"
    assert db.summary()["plan_count"] == 1
    db.close()


def test_custom_scan_root_is_added_and_overlap_is_collapsed(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    downloads = tmp_path / "Downloads"
    outside = tmp_path / "Elsewhere"
    nested = downloads / "nested"
    for path in [desktop, downloads, outside, nested]:
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "musorka.known_folders.default_scan_roots",
        lambda: [("Рабочий стол", desktop), ("Загрузки", downloads)],
    )

    roots = scan_roots_with_custom([outside, nested])
    paths = [path for _, path in roots]
    assert desktop in paths
    assert downloads in paths
    assert outside in paths
    assert nested not in paths  # already covered by Downloads

    # Choosing an ancestor replaces contained default roots to avoid double scans.
    roots = scan_roots_with_custom([tmp_path])
    assert roots == [(f"Выбранная · {tmp_path.name}", tmp_path)]
