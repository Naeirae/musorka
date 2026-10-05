from __future__ import annotations

import csv
import json
import os
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

from .actions import ACTION_LABELS, execute_plan
from .db import Database
from .drive import DriveClient, DriveUnavailable
from .known_folders import default_scan_roots, scan_roots_with_custom
from .programs import launch_uninstaller, scan_installed_programs
from .scanner import scan_roots_with_folders
from .utils import fmt_ts, human_size, open_path


APP_TITLE = "Мусорка"


def app_icon_path() -> Path | None:
    candidates = [Path(__file__).resolve().parent.parent / "assets" / "musorka.ico"]
    if getattr(sys, "_MEIPASS", ""):
        candidates.append(Path(sys._MEIPASS) / "assets" / "musorka.ico")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Naeirae.Musorka")
    except Exception:
        pass


FILE_ACTION_LABELS = {
    "": "Не решено",
    "keep": "Оставить",
    "trash": "В корзину",
    "permanent": "Удалить навсегда",
    "drive": "В Google Drive",
    "local_downloads": r"В C:\\Загрузки",
    "later": "Потом",
}

PROGRAM_STATUS_LABELS = {
    "": "Не решено",
    "move": "Перенести",
    "skip": "Не нужно",
    "unknown": "Не знаю",
}


class ScanWorker(QThread):
    status = Signal(str)
    finished_ok = Signal(int, int)
    failed = Signal(str)

    def __init__(self, db_path: Path, roots: list[tuple[str, Path]]):
        super().__init__()
        self.db_path = db_path
        self.roots = roots

    def run(self):
        db = Database(self.db_path)
        try:
            items, folders = scan_roots_with_folders(self.roots, self.status.emit)
            db.upsert_files(items)
            db.upsert_folders(folders)
            self.finished_ok.emit(len(items), len(folders))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()


class PlanWorker(QThread):
    status = Signal(str)
    finished_ok = Signal(int, int)
    failed = Signal(str)

    def __init__(self, db_path: Path):
        super().__init__()
        self.db_path = db_path

    def run(self):
        db = Database(self.db_path)
        drive = DriveClient()
        try:
            stats = execute_plan(
                db,
                drive,
                lambda i, total, msg: self.status.emit(f"{i}/{total} — {msg}"),
            )
            self.finished_ok.emit(stats["done"], stats["failed"])
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()


class ProgramsWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            self.finished_ok.emit(scan_installed_programs())
        except Exception as exc:
            self.failed.emit(str(exc))


class DriveFolderDialog(QDialog):
    def __init__(self, drive: DriveClient, parent=None):
        super().__init__(parent)
        self.drive = drive
        self.setWindowTitle("Выбрать папку Google Drive")
        self.resize(560, 520)
        self.current_id = "root"
        self.stack: list[tuple[str, str]] = [("root", "Мой диск")]
        self.selected_folder_id = ""
        self.selected_folder_path = ""

        layout = QVBoxLayout(self)
        self.breadcrumb = QLabel("Мой диск")
        self.breadcrumb.setWordWrap(True)
        layout.addWidget(self.breadcrumb)

        row = QHBoxLayout()
        self.up_btn = QPushButton("Вверх")
        self.refresh_btn = QPushButton("Обновить список")
        self.create_btn = QPushButton("Создать папку")
        row.addWidget(self.up_btn)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.create_btn)
        layout.addLayout(row)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.list, 1)

        hint = QLabel("Двойной щелчок — открыть папку. «Выбрать эту папку» выбирает текущую открытую папку.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions = QHBoxLayout()
        self.select_btn = QPushButton("Выбрать эту папку")
        self.cancel_btn = QPushButton("Отмена")
        actions.addWidget(self.select_btn)
        actions.addWidget(self.cancel_btn)
        layout.addLayout(actions)

        self.up_btn.clicked.connect(self.go_up)
        self.refresh_btn.clicked.connect(self.refresh)
        self.create_btn.clicked.connect(self.create_folder)
        self.list.itemDoubleClicked.connect(self.enter_folder)
        self.select_btn.clicked.connect(self.select_current)
        self.cancel_btn.clicked.connect(self.reject)

        self.refresh()

    def _path_text(self) -> str:
        return " / ".join(name for _, name in self.stack)

    def refresh(self):
        try:
            folders = self.drive.list_folders(self.current_id)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        self.list.clear()
        for folder in folders:
            item = QListWidgetItem(folder["name"])
            item.setData(Qt.UserRole, folder["id"])
            self.list.addItem(item)
        self.breadcrumb.setText(self._path_text())
        self.up_btn.setEnabled(len(self.stack) > 1)

    def enter_folder(self, item: QListWidgetItem):
        folder_id = item.data(Qt.UserRole)
        self.stack.append((folder_id, item.text()))
        self.current_id = folder_id
        self.refresh()

    def go_up(self):
        if len(self.stack) <= 1:
            return
        self.stack.pop()
        self.current_id = self.stack[-1][0]
        self.refresh()

    def create_folder(self):
        name, ok = QInputDialog.getText(self, "Новая папка", "Название папки:")
        name = name.strip()
        if not ok or not name:
            return
        try:
            self.drive.create_folder(name, self.current_id)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        self.refresh()

    def select_current(self):
        self.selected_folder_id = self.current_id
        self.selected_folder_path = self._path_text()
        self.accept()


class OverviewPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        title = QLabel("Быстрый разбор рабочего хвоста")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        roots_title = QLabel("Папки для сканирования")
        roots_title.setObjectName("sectionTitle")
        layout.addWidget(roots_title)

        roots_hint = QLabel(
            "Рабочий стол и Загрузки включены всегда. Ниже можно добавить любую другую папку с диска. "
            "Выбранная корневая папка защищается от удаления так же, как Рабочий стол и Загрузки."
        )
        roots_hint.setWordWrap(True)
        layout.addWidget(roots_hint)

        self.custom_roots = QListWidget()
        self.custom_roots.setSelectionMode(QAbstractItemView.SingleSelection)
        self.custom_roots.setMaximumHeight(120)
        layout.addWidget(self.custom_roots)

        roots_actions = QHBoxLayout()
        add_root = QPushButton("Добавить папку…")
        remove_root = QPushButton("Убрать выбранную")
        add_root.clicked.connect(main.add_custom_scan_root)
        remove_root.clicked.connect(main.remove_custom_scan_root)
        roots_actions.addWidget(add_root)
        roots_actions.addWidget(remove_root)
        roots_actions.addStretch(1)
        layout.addLayout(roots_actions)

        self.scan_btn = QPushButton("Сканировать выбранные папки")
        self.scan_btn.clicked.connect(main.start_scan)
        layout.addWidget(self.scan_btn)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.refresh()

    def refresh(self):
        self.custom_roots.clear()
        for path in self.main.custom_scan_roots():
            self.custom_roots.addItem(str(path))
        if self.custom_roots.count() == 0:
            item = QListWidgetItem("Дополнительные папки не выбраны")
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.custom_roots.addItem(item)

        s = self.main.db.summary()
        self.summary.setText(
            f"Файлов сейчас: {s.get('files', 0)} · {human_size(s.get('bytes', 0))} · "
            f"Папок: {s.get('folders', 0)}\n"
            f"Не решено: {s.get('undecided', 0)} · Потом: {s.get('later_count', 0)} · "
            f"В плане: {s.get('plan_count', 0)} · Файлов в группах дублей: {s.get('duplicate_count', 0)}"
        )


class FilesPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по имени или пути")
        self.category = QComboBox()
        self.category.addItem("Все категории", "")
        self.extension = QComboBox()
        self.extension.addItem("Все форматы", None)
        self.action_filter = QComboBox()
        self.action_filter.addItem("Все решения", None)
        for value, label in FILE_ACTION_LABELS.items():
            self.action_filter.addItem(label, value)
        refresh = QPushButton("Обновить список")
        scan = QPushButton("Пересканировать")
        top.addWidget(self.search, 2)
        top.addWidget(self.category, 1)
        top.addWidget(self.extension, 1)
        top.addWidget(self.action_filter, 1)
        top.addWidget(refresh)
        top.addWidget(scan)
        layout.addLayout(top)

        folder_row = QHBoxLayout()
        folder_label = QLabel("Папка:")
        self.folder_filter = QComboBox()
        self.folder_filter.addItem("Все папки", "")
        self.folder_filter.setMinimumContentsLength(35)
        folder_row.addWidget(folder_label)
        folder_row.addWidget(self.folder_filter, 1)
        layout.addLayout(folder_row)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Имя", "Где", "Категория", "Формат", "Размер", "Изменён", "Дубль", "Решение", "Путь"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        self.table.setColumnWidth(8, 520)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        layout.addWidget(self.table, 1)

        selection = QHBoxLayout()
        select_all = QPushButton("Выделить все показанные")
        clear_selection = QPushButton("Снять выделение")
        self.selection_status = QLabel("Выбрано: 0")
        select_all.clicked.connect(self.select_all_visible)
        clear_selection.clicked.connect(self.clear_selection)
        self.table.itemSelectionChanged.connect(self.update_selection_status)
        self.table.itemSelectionChanged.connect(self.update_selected_path)
        selection.addWidget(select_all)
        selection.addWidget(clear_selection)
        selection.addWidget(QLabel("Для диапазона: Shift; отдельные строки: Ctrl"))
        selection.addStretch(1)
        selection.addWidget(self.selection_status)
        layout.addLayout(selection)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Полный путь выбранного файла:"))
        self.selected_path = QLineEdit()
        self.selected_path.setReadOnly(True)
        self.selected_path.setPlaceholderText("Выберите строку — здесь будет полный путь без обрезания")
        copy_path_btn = QPushButton("Копировать путь")
        copy_path_btn.clicked.connect(self.copy_selected_path)
        path_row.addWidget(self.selected_path, 1)
        path_row.addWidget(copy_path_btn)
        layout.addLayout(path_row)

        actions = QHBoxLayout()
        for label, handler in [
            ("Оставить", lambda: self.mark("keep")),
            ("В Google Drive", self.mark_drive),
            (r"В C:\Загрузки", lambda: self.mark("local_downloads")),
            ("В корзину", lambda: self.mark("trash")),
            ("Удалить навсегда", lambda: self.mark("permanent")),
            ("Потом", self.mark_later),
            ("Сбросить решение", lambda: self.mark("")),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            actions.addWidget(btn)
        layout.addLayout(actions)

        more = QHBoxLayout()
        open_file = QPushButton("Открыть файл")
        open_folder = QPushButton("Открыть папку")
        open_file.clicked.connect(self.open_selected_file)
        open_folder.clicked.connect(self.open_selected_folder)
        more.addWidget(open_file)
        more.addWidget(open_folder)
        layout.addLayout(more)

        self.search.textChanged.connect(self.refresh)
        self.category.currentIndexChanged.connect(self.refresh)
        self.extension.currentIndexChanged.connect(self.refresh)
        self.folder_filter.currentIndexChanged.connect(self.refresh)
        self.action_filter.currentIndexChanged.connect(self.refresh)
        refresh.clicked.connect(self.refresh)
        scan.clicked.connect(main.start_scan)

    def refresh_filters(self):
        current_category = self.category.currentData()
        current_ext = self.extension.currentData()
        current_folder = self.folder_filter.currentData()

        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("Все категории", "")
        for cat in self.main.db.categories():
            self.category.addItem(cat, cat)
        idx = self.category.findData(current_category)
        self.category.setCurrentIndex(idx if idx >= 0 else 0)
        self.category.blockSignals(False)

        self.extension.blockSignals(True)
        self.extension.clear()
        self.extension.addItem("Все форматы", None)
        for ext in self.main.db.extensions():
            self.extension.addItem(ext or "Без расширения", ext)
        idx = self.extension.findData(current_ext)
        self.extension.setCurrentIndex(idx if idx >= 0 else 0)
        self.extension.blockSignals(False)

        self.folder_filter.blockSignals(True)
        self.folder_filter.clear()
        self.folder_filter.addItem("Все папки", "")
        for folder in self.main.db.folders():
            label = f"{folder['area']} · {folder['path']}"
            self.folder_filter.addItem(label, folder["path"])
        idx = self.folder_filter.findData(current_folder)
        self.folder_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.folder_filter.blockSignals(False)

    def set_folder_filter(self, folder_path: str):
        self.refresh_filters()
        idx = self.folder_filter.findData(folder_path)
        if idx < 0 and folder_path:
            self.folder_filter.addItem(folder_path, folder_path)
            idx = self.folder_filter.count() - 1
        self.folder_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.refresh()

    def refresh(self):
        self.refresh_filters()
        rows = self.main.db.list_files(
            search=self.search.text().strip(),
            category=self.category.currentData() or "",
            ext=self.extension.currentData(),
            folder_path=self.folder_filter.currentData() or "",
            action=self.action_filter.currentData(),
        )
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row["name"], row["area"], row["category"], row["ext"] or "—",
                human_size(row["size"]), fmt_ts(row["mtime"]), row["duplicate_group"],
                FILE_ACTION_LABELS.get(row["action"], row["action"]), row["path"],
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if c == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                item.setToolTip(str(row["path"]) if c in (0, 8) else str(value))
                if row["action"] == "permanent":
                    item.setBackground(QColor("#ffd9d9"))
                elif row["action"] == "drive":
                    item.setBackground(QColor("#e1f1ff"))
                elif row["action"] == "local_downloads":
                    item.setBackground(QColor("#e8f5e9"))
                elif row["action"] == "later":
                    item.setBackground(QColor("#fff2c7"))
                self.table.setItem(r, c, item)
        self.update_selection_status()
        self.update_selected_path()

    def update_selected_path(self):
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if len(indexes) != 1:
            self.selected_path.clear()
            return
        path_item = self.table.item(indexes[0].row(), 8)
        self.selected_path.setText(path_item.text() if path_item else "")

    def copy_selected_path(self):
        path = self.selected_path.text().strip()
        if not path:
            QMessageBox.information(self, APP_TITLE, "Выберите один файл, чтобы скопировать его путь.")
            return
        QApplication.clipboard().setText(path)

    def select_all_visible(self):
        self.table.selectAll()
        self.update_selection_status()

    def clear_selection(self):
        self.table.clearSelection()
        self.update_selection_status()

    def update_selection_status(self):
        selected = len(self.table.selectionModel().selectedRows()) if self.table.selectionModel() else 0
        self.selection_status.setText(f"Выбрано: {selected} из {self.table.rowCount()}")

    def selected_ids(self) -> list[int]:
        result = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item:
                result.append(int(item.data(Qt.UserRole)))
        return result

    def selected_row(self):
        ids = self.selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, APP_TITLE, "Выберите ровно один файл.")
            return None
        return self.main.db.file_by_id(ids[0])

    def mark(self, action: str):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, APP_TITLE, "Сначала выделите строки.")
            return
        self.main.db.set_file_action(ids, action)
        self.main.refresh_all()

    def mark_later(self):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, APP_TITLE, "Сначала выделите строки.")
            return
        note, ok = QInputDialog.getText(self, "Потом", "Заметка (можно оставить пустой):")
        if not ok:
            return
        self.main.db.set_file_action(ids, "later", later_note=note.strip())
        self.main.refresh_all()

    def mark_drive(self):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, APP_TITLE, "Сначала выделите строки.")
            return
        folder = self.main.choose_drive_folder()
        if not folder:
            return
        folder_id, folder_path = folder
        self.main.db.set_file_action(
            ids, "drive", drive_folder_id=folder_id, drive_folder_path=folder_path
        )
        self.main.refresh_all()

    def open_selected_file(self):
        row = self.selected_row()
        if row:
            try:
                open_path(row["path"])
            except Exception as exc:
                QMessageBox.critical(self, APP_TITLE, str(exc))

    def open_selected_folder(self):
        row = self.selected_row()
        if row:
            try:
                open_path(Path(row["path"]).parent)
            except Exception as exc:
                QMessageBox.critical(self, APP_TITLE, str(exc))


class FoldersPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)

        info = QLabel(
            "Папки можно помечать пачкой в Корзину или на окончательное удаление. "
            "Корневые папки всех зон сканирования защищены. Перед выполнением содержимое "
            "сверяется с последним сканированием; если оно изменилось, удаление останавливается."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию или пути папки")
        self.area = QComboBox()
        self.area.addItem("Все зоны", "")
        refresh = QPushButton("Обновить список")
        top.addWidget(self.search, 2)
        top.addWidget(self.area, 1)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Папка", "Где", "Файлов внутри", "Размер", "Изменена", "Решение", "Ошибка", "Полный путь"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        for column in range(7):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.setColumnWidth(7, 520)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        layout.addWidget(self.table, 1)

        folder_path_row = QHBoxLayout()
        folder_path_row.addWidget(QLabel("Полный путь выбранной папки:"))
        self.selected_path = QLineEdit()
        self.selected_path.setReadOnly(True)
        self.selected_path.setPlaceholderText("Выберите строку — здесь будет полный путь без обрезания")
        copy_path_btn = QPushButton("Копировать путь")
        copy_path_btn.clicked.connect(self.copy_selected_path)
        folder_path_row.addWidget(self.selected_path, 1)
        folder_path_row.addWidget(copy_path_btn)
        layout.addLayout(folder_path_row)
        self.table.itemSelectionChanged.connect(self.update_selected_path)

        destructive = QHBoxLayout()
        trash_btn = QPushButton("Папку в корзину")
        permanent_btn = QPushButton("Удалить папку навсегда")
        reset_btn = QPushButton("Сбросить решение")
        trash_btn.clicked.connect(lambda: self.mark("trash"))
        permanent_btn.clicked.connect(lambda: self.mark("permanent"))
        reset_btn.clicked.connect(lambda: self.mark(""))
        destructive.addWidget(trash_btn)
        destructive.addWidget(permanent_btn)
        destructive.addWidget(reset_btn)
        layout.addLayout(destructive)

        actions = QHBoxLayout()
        open_btn = QPushButton("Открыть папку")
        show_btn = QPushButton("Показать её файлы")
        open_btn.clicked.connect(self.open_selected)
        show_btn.clicked.connect(self.show_files)
        actions.addWidget(open_btn)
        actions.addWidget(show_btn)
        layout.addLayout(actions)

        self.search.textChanged.connect(self.refresh)
        self.area.currentIndexChanged.connect(self.refresh)
        refresh.clicked.connect(self.refresh)

    def refresh_filters(self):
        current = self.area.currentData()
        self.area.blockSignals(True)
        self.area.clear()
        self.area.addItem("Все зоны", "")
        for area in self.main.db.folder_areas():
            self.area.addItem(area, area)
        idx = self.area.findData(current)
        self.area.setCurrentIndex(idx if idx >= 0 else 0)
        self.area.blockSignals(False)

    def refresh(self):
        self.refresh_filters()
        rows = self.main.db.folders(
            search=self.search.text().strip(),
            area=self.area.currentData() or "",
        )
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            action_label = ACTION_LABELS.get(row["action"], row["action"]) if row["action"] else "—"
            values = [
                row["name"], row["area"], row["file_count"], human_size(row["total_size"]),
                fmt_ts(row["mtime"]), action_label, row["last_error"], row["path"],
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if c == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                item.setToolTip(str(row["path"]) if c in (0, 7) else str(value))
                if int(row["depth"]) <= 0:
                    item.setForeground(QColor("#999999"))
                elif row["action"] == "permanent":
                    item.setBackground(QColor("#ffd9d9"))
                elif row["action"] == "trash":
                    item.setBackground(QColor("#fff2c7"))
                self.table.setItem(r, c, item)
        self.update_selected_path()

    def update_selected_path(self):
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if len(indexes) != 1:
            self.selected_path.clear()
            return
        path_item = self.table.item(indexes[0].row(), 7)
        self.selected_path.setText(path_item.text() if path_item else "")

    def copy_selected_path(self):
        path = self.selected_path.text().strip()
        if not path:
            QMessageBox.information(self, APP_TITLE, "Выберите одну папку, чтобы скопировать её путь.")
            return
        QApplication.clipboard().setText(path)

    def selected_ids(self) -> list[int]:
        ids = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def selected_row(self):
        ids = self.selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, APP_TITLE, "Выберите одну папку.")
            return None
        return self.main.db.folder_by_id(ids[0])

    def mark(self, action: str):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, APP_TITLE, "Сначала выделите папки.")
            return
        if action:
            protected = [self.main.db.folder_by_id(i) for i in ids]
            protected = [r for r in protected if r and int(r["depth"]) <= 0]
            if protected:
                QMessageBox.information(
                    self,
                    APP_TITLE,
                    "Корневую папку зоны сканирования удалять нельзя. "
                    "Выделите только вложенные папки."
                )
                return
            conflicts = self.main.db.folder_action_conflicts(ids)
            if conflicts:
                preview = "\n".join(
                    f"• {r['action']}: {r['path']}" for r in conflicts[:10]
                )
                if len(conflicts) > 10:
                    preview += f"\n… и ещё {len(conflicts)-10}"
                QMessageBox.warning(
                    self,
                    "Папка содержит сохранённые решения",
                    "Удаление папки не поставлено в план, потому что внутри есть файлы со статусом "
                    "«Оставить», «Потом» или «В Google Drive»:\n\n" + preview +
                    "\n\nСначала измените решения по этим файлам."
                )
                return
        chosen = self.main.db.set_folder_action(ids, action)
        if action and len(chosen) < len(ids):
            QMessageBox.information(
                self,
                APP_TITLE,
                "Вложенные выбранные папки объединены с родительской: дерево будет удалено один раз."
            )
        self.main.refresh_all()

    def open_selected(self):
        row = self.selected_row()
        if not row:
            return
        try:
            open_path(row["path"])
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def show_files(self):
        row = self.selected_row()
        if not row:
            return
        self.main.files.set_folder_filter(row["path"])
        self.main.tabs.setCurrentWidget(self.main.files)


class LaterPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        info = QLabel("«Потом» — отдельный реестр. Полный путь сохраняется даже если файл позже исчезнет или будет перемещён вручную.")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Файл", "Полный путь", "Размер", "Изменён", "Заметка", "Существует"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        for label, handler in [
            ("Изменить заметку", self.edit_note),
            ("Вернуть в разбор", lambda: self.mark("")),
            ("В Google Drive", self.mark_drive),
            (r"В C:\Загрузки", lambda: self.mark("local_downloads")),
            ("В корзину", lambda: self.mark("trash")),
            ("Удалить навсегда", lambda: self.mark("permanent")),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            row.addWidget(btn)
        layout.addLayout(row)

    def refresh(self):
        rows = self.main.db.later_files()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row["name"], row["path"], human_size(row["size"]), fmt_ts(row["mtime"]),
                row["later_note"], "Да" if row["exists_now"] else "Нет",
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if c == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                if not row["exists_now"]:
                    item.setForeground(QColor("#999999"))
                self.table.setItem(r, c, item)

    def selected_ids(self) -> list[int]:
        ids = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def mark(self, action: str):
        ids = self.selected_ids()
        if not ids:
            return
        self.main.db.set_file_action(ids, action)
        self.main.refresh_all()

    def edit_note(self):
        ids = self.selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, APP_TITLE, "Выберите одну строку.")
            return
        row = self.main.db.file_by_id(ids[0])
        note, ok = QInputDialog.getText(self, "Заметка", "Почему отложено:", text=row["later_note"] if row else "")
        if ok:
            self.main.db.update_later_note(ids[0], note.strip())
            self.refresh()

    def mark_drive(self):
        ids = self.selected_ids()
        if not ids:
            return
        folder = self.main.choose_drive_folder()
        if not folder:
            return
        self.main.db.set_file_action(ids, "drive", drive_folder_id=folder[0], drive_folder_path=folder[1])
        self.main.refresh_all()


class DrivePage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        title = QLabel("Google Drive")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        text = QLabel(
            "Подключение делается один раз: включите Google Drive API в Google Cloud, создайте OAuth client "
            "типа Desktop app, скачайте JSON и выберите его здесь. После «Подключить» откроется Chrome для входа. "
            "JSON и токен хранятся только локально в %APPDATA%\\Musorka."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        cloud = QPushButton("Открыть Google Cloud Console")
        help_btn = QPushButton("Открыть официальную инструкцию Google Drive API")
        choose = QPushButton("Выбрать OAuth client JSON")
        connect = QPushButton("Подключить / проверить Drive")
        browse = QPushButton("Открыть выбор папки")
        disconnect = QPushButton("Отключить Drive на этом компьютере")
        cloud.clicked.connect(lambda: webbrowser.open("https://console.cloud.google.com/"))
        help_btn.clicked.connect(lambda: webbrowser.open("https://developers.google.com/workspace/drive/api/quickstart/python"))
        choose.clicked.connect(self.choose_credentials)
        connect.clicked.connect(self.connect_drive)
        browse.clicked.connect(lambda: self.main.choose_drive_folder())
        disconnect.clicked.connect(self.disconnect)
        for btn in [cloud, help_btn, choose, connect, browse, disconnect]:
            layout.addWidget(btn)
        layout.addStretch(1)
        self.refresh()

    def refresh(self):
        if self.main.drive.has_client_credentials():
            self.status.setText(f"OAuth client JSON: {self.main.drive.credentials_path}")
        else:
            self.status.setText("OAuth client JSON ещё не выбран.")

    def choose_credentials(self):
        path, _ = QFileDialog.getOpenFileName(self, "OAuth client JSON", "", "JSON (*.json)")
        if not path:
            return
        try:
            self.main.drive.import_client_credentials(path)
            self.refresh()
            QMessageBox.information(self, APP_TITLE, "OAuth client JSON сохранён локально.")
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def connect_drive(self):
        try:
            self.main.drive.service()
            QMessageBox.information(self, APP_TITLE, "Google Drive подключён.")
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def disconnect(self):
        self.main.drive.disconnect()
        QMessageBox.information(self, APP_TITLE, "Локальный OAuth-токен удалён.")


class ProgramsPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        scan = QPushButton("Собрать установленные программы")
        export = QPushButton("Экспорт списка «Перенести»")
        scan.clicked.connect(self.main.scan_programs)
        export.clicked.connect(self.export_move_list)
        top.addWidget(scan)
        top.addWidget(export)
        layout.addLayout(top)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Программа", "Версия", "Издатель", "Где установлена", "Scope", "Для переноса", "Uninstall"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        for label, status in [("Перенести", "move"), ("Не нужно", "skip"), ("Не знаю", "unknown"), ("Сбросить", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, s=status: self.mark(s))
            actions.addWidget(btn)
        layout.addLayout(actions)

        uninstall_row = QHBoxLayout()
        uninstall = QPushButton("Удалить программу")
        open_install = QPushButton("Открыть папку установки")
        uninstall.clicked.connect(self.uninstall_selected)
        open_install.clicked.connect(self.open_install_location)
        uninstall_row.addWidget(uninstall)
        uninstall_row.addWidget(open_install)
        layout.addLayout(uninstall_row)

        note = QLabel(
            "«Удалить программу» не стирает её папку напрямую: запускается штатный деинсталлятор, "
            "который зарегистрирован в Windows. Для MSI команда /I преобразуется в /X. "
            "После завершения нажмите «Собрать установленные программы» ещё раз."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

    def refresh(self):
        rows = self.main.db.programs()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row["name"], row["version"], row["publisher"], row["install_location"], row["scope"],
                PROGRAM_STATUS_LABELS.get(row["migration_status"], row["migration_status"]), row["uninstall_string"],
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if c == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                if row["migration_status"] == "move":
                    item.setBackground(QColor("#e2f5df"))
                self.table.setItem(r, c, item)

    def selected_ids(self) -> list[int]:
        ids = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def mark(self, status: str):
        ids = self.selected_ids()
        if not ids:
            return
        self.main.db.set_program_status(ids, status)
        self.refresh()

    def selected_program(self):
        ids = self.selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, APP_TITLE, "Выберите ровно одну программу.")
            return None
        return self.main.db.program_by_id(ids[0])

    def uninstall_selected(self):
        row = self.selected_program()
        if not row:
            return
        command = str(row["uninstall_string"] or "").strip()
        if not command:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Windows не зарегистрировал для этой программы штатную команду удаления."
            )
            return
        message = (
            f"Запустить штатный деинсталлятор?\n\n{row['name']}"
            + (f" · {row['version']}" if row["version"] else "")
            + "\n\nМусорка не будет удалять файлы программы вручную. "
              "Дальше управление перейдёт деинсталлятору Windows/программы; возможен запрос UAC."
        )
        if QMessageBox.warning(self, "Удалить программу", message, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            launched = launch_uninstaller(command)
            self.main.db.add_history(
                row["install_location"] or row["name"],
                "program_uninstall",
                "launched",
                f"Запущен штатный деинсталлятор: {launched}",
            )
            self.main.history.refresh()
            QMessageBox.information(
                self,
                APP_TITLE,
                "Деинсталлятор запущен. Завершите его, затем пересканируйте список программ."
            )
        except Exception as exc:
            self.main.db.add_history(
                row["install_location"] or row["name"],
                "program_uninstall",
                "failed",
                str(exc),
            )
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def open_install_location(self):
        row = self.selected_program()
        if not row:
            return
        path = str(row["install_location"] or "").strip()
        if not path:
            QMessageBox.information(self, APP_TITLE, "Путь установки для этой программы не указан в Windows Registry.")
            return
        try:
            open_path(path)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def export_move_list(self):
        rows = [r for r in self.main.db.programs() if r["migration_status"] == "move"]
        if not rows:
            QMessageBox.information(self, APP_TITLE, "Нет программ со статусом «Перенести».")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт программ", "programs-to-move.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["Программа", "Версия", "Издатель", "Путь установки", "Scope"])
            for row in rows:
                writer.writerow([row["name"], row["version"], row["publisher"], row["install_location"], row["scope"]])
        QMessageBox.information(self, APP_TITLE, f"Сохранено: {path}")


class PlanPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Тип", "Действие", "Путь", "Размер", "Куда", "Последняя ошибка"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.execute_btn = QPushButton("Выполнить план")
        self.execute_btn.clicked.connect(main.execute_plan)
        layout.addWidget(self.execute_btn)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def refresh(self):
        rows = self.main.db.pending_plan_items()
        total_size = sum(int(r.get("size", 0)) for r in rows)
        permanent = sum(1 for r in rows if r["action"] == "permanent")
        drive = sum(1 for r in rows if r["action"] == "drive")
        local_downloads = sum(1 for r in rows if r["action"] == "local_downloads")
        folder_count = sum(1 for r in rows if r["item_type"] == "folder")
        self.summary.setText(
            f"В плане: {len(rows)} объектов · {human_size(total_size)}. "
            f"Папок: {folder_count}. Навсегда: {permanent}. В Google Drive: {drive}. "
            f"В C:\\Загрузки: {local_downloads}."
        )
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            target = row.get("drive_folder_path", "") if row["action"] == "drive" else ""
            values = [
                "Папка" if row["item_type"] == "folder" else "Файл",
                ACTION_LABELS.get(row["action"], row["action"]),
                row["path"], human_size(row.get("size", 0)), target, row.get("last_error", "")
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if row["action"] == "permanent":
                    item.setBackground(QColor("#ffd9d9"))
                elif row["item_type"] == "folder":
                    item.setBackground(QColor("#fff2c7"))
                self.table.setItem(r, c, item)
        self.execute_btn.setEnabled(bool(rows))


class HistoryPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        refresh = QPushButton("Обновить историю")
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Когда", "Действие", "Результат", "Путь", "Подробности"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

    def refresh(self):
        rows = self.main.db.history()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [fmt_ts(row["ts"]), row["action"], row["result"], row["path"], row["details"]]
            for c, value in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(str(value)))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1360, 820)
        self.setMinimumSize(1040, 680)
        self.db = Database()
        self.drive = DriveClient()
        self.scan_worker = None
        self.plan_worker = None
        self.program_worker = None

        self.tabs = QTabWidget()
        self.overview = OverviewPage(self)
        self.files = FilesPage(self)
        self.folders = FoldersPage(self)
        self.later = LaterPage(self)
        self.drive_page = DrivePage(self)
        self.programs = ProgramsPage(self)
        self.plan = PlanPage(self)
        self.history = HistoryPage(self)
        for widget, label in [
            (self.overview, "Обзор"),
            (self.files, "Файлы"),
            (self.folders, "Папки"),
            (self.later, "Потом"),
            (self.drive_page, "Google Drive"),
            (self.programs, "Программы"),
            (self.plan, "План уборки"),
            (self.history, "История"),
        ]:
            self.tabs.addTab(widget, label)
        self.setCentralWidget(self.tabs)
        self.refresh_all()

    def closeEvent(self, event):
        self.db.close()
        super().closeEvent(event)

    def refresh_all(self):
        self.overview.refresh()
        self.files.refresh()
        self.folders.refresh()
        self.later.refresh()
        self.drive_page.refresh()
        self.programs.refresh()
        self.plan.refresh()
        self.history.refresh()

    def custom_scan_roots(self) -> list[Path]:
        raw = self.db.get_setting("custom_scan_roots", "[]")
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            values = []
        if not isinstance(values, list):
            return []
        roots: list[Path] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            path = Path(value)
            key = os.path.normcase(os.path.normpath(str(path)))
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    def _save_custom_scan_roots(self, roots: list[Path]) -> None:
        self.db.set_setting("custom_scan_roots", json.dumps([str(p) for p in roots], ensure_ascii=False))
        self.overview.refresh()

    def add_custom_scan_root(self):
        path = QFileDialog.getExistingDirectory(self, "Выбрать папку для Мусорки")
        if not path:
            return
        chosen = Path(path)
        if not chosen.exists() or not chosen.is_dir():
            QMessageBox.warning(self, APP_TITLE, "Выбранная папка недоступна.")
            return
        roots = self.custom_scan_roots()
        chosen_key = os.path.normcase(os.path.normpath(str(chosen)))
        if any(os.path.normcase(os.path.normpath(str(existing))) == chosen_key for existing in roots):
            QMessageBox.information(self, APP_TITLE, "Эта папка уже добавлена.")
            return
        roots.append(chosen)
        self._save_custom_scan_roots(roots)
        self.overview.status.setText(f"Добавлена папка: {chosen}. Нажмите «Сканировать выбранные папки».")

    def remove_custom_scan_root(self):
        item = self.overview.custom_roots.currentItem()
        if not item or not (item.flags() & Qt.ItemIsSelectable):
            QMessageBox.information(self, APP_TITLE, "Выберите дополнительную папку в списке.")
            return
        target = Path(item.text())
        target_key = os.path.normcase(os.path.normpath(str(target)))
        roots = [
            path for path in self.custom_scan_roots()
            if os.path.normcase(os.path.normpath(str(path))) != target_key
        ]
        self._save_custom_scan_roots(roots)
        self.overview.status.setText(
            "Папка убрана из будущих сканирований. Уже найденные записи исчезнут после следующего сканирования."
        )

    def start_scan(self):
        if self.scan_worker and self.scan_worker.isRunning():
            return
        self.overview.scan_btn.setEnabled(False)
        roots = scan_roots_with_custom(self.custom_scan_roots())
        if not roots:
            QMessageBox.information(self, APP_TITLE, "Нет доступных папок для сканирования.")
            self.overview.scan_btn.setEnabled(True)
            return
        self.overview.status.setText(f"Запускаю сканирование: {len(roots)} корневых папок…")
        self.scan_worker = ScanWorker(self.db.path, roots)
        self.scan_worker.status.connect(self.overview.status.setText)
        self.scan_worker.finished_ok.connect(self.scan_done)
        self.scan_worker.failed.connect(self.scan_failed)
        self.scan_worker.start()

    def scan_done(self, count: int, folder_count: int):
        self.overview.scan_btn.setEnabled(True)
        self.overview.status.setText(f"Готово: найдено {count} файлов и {folder_count} папок.")
        self.refresh_all()

    def scan_failed(self, message: str):
        self.overview.scan_btn.setEnabled(True)
        self.overview.status.setText("Ошибка: " + message)
        QMessageBox.critical(self, APP_TITLE, message)

    def ensure_drive_credentials(self) -> bool:
        if self.drive.has_client_credentials():
            return True
        answer = QMessageBox.question(
            self,
            APP_TITLE,
            "Для Google Drive нужен OAuth client JSON типа Desktop app. Выбрать его сейчас?",
        )
        if answer != QMessageBox.Yes:
            return False
        path, _ = QFileDialog.getOpenFileName(self, "OAuth client JSON", "", "JSON (*.json)")
        if not path:
            return False
        try:
            self.drive.import_client_credentials(path)
            return True
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return False

    def choose_drive_folder(self):
        if not self.ensure_drive_credentials():
            return None
        try:
            self.drive.service()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return None
        dialog = DriveFolderDialog(self.drive, self)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.selected_folder_id, dialog.selected_folder_path

    def execute_plan(self):
        rows = self.db.pending_plan_items()
        if not rows:
            return
        total = sum(int(r.get("size", 0)) for r in rows)
        permanent = [r for r in rows if r["action"] == "permanent"]
        drive = [r for r in rows if r["action"] == "drive"]
        folders = [r for r in rows if r["item_type"] == "folder"]
        message = (
            f"Выполнить план для {len(rows)} объектов ({human_size(total)})?\n\n"
            f"Файлов: {sum(1 for r in rows if r['item_type']=='file')}\n"
            f"Папок: {len(folders)}\n"
            f"В корзину: {sum(1 for r in rows if r['action']=='trash')}\n"
            f"Удалить навсегда: {len(permanent)}\n"
            f"В Google Drive: {len(drive)}\n"
            f"В C:\\Загрузки: {sum(1 for r in rows if r['action']=='local_downloads')}\n\n"
            "Папка удаляется целиком вместе с вложенным содержимым. Перед этим Мусорка ещё раз "
            "сверит число файлов и размер с последним сканированием.\n\n"
            "Для Google Drive локальный файл уйдёт в Корзину только после успешной загрузки и проверки размера/hash."
        )
        if QMessageBox.question(self, APP_TITLE, message) != QMessageBox.Yes:
            return
        if permanent:
            names = "\n".join(
                f"• {'[ПАПКА] ' if r['item_type']=='folder' else ''}{r['path']}" for r in permanent[:12]
            )
            if len(permanent) > 12:
                names += f"\n… и ещё {len(permanent)-12}"
            warning = (
                "В плане есть необратимое удаление. Эти объекты НЕ попадут в Корзину Windows:\n\n"
                + names
                + "\n\nПродолжить?"
            )
            if QMessageBox.warning(self, "Удалить навсегда", warning, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        if drive:
            if not self.ensure_drive_credentials():
                return
            try:
                self.drive.service()
            except Exception as exc:
                QMessageBox.critical(self, APP_TITLE, str(exc))
                return

        self.plan.execute_btn.setEnabled(False)
        self.plan.status.setText("Выполняю план…")
        self.plan_worker = PlanWorker(self.db.path)
        self.plan_worker.status.connect(self.plan.status.setText)
        self.plan_worker.finished_ok.connect(self.plan_done)
        self.plan_worker.failed.connect(self.plan_failed)
        self.plan_worker.start()

    def plan_done(self, done: int, failed: int):
        self.plan.execute_btn.setEnabled(True)
        self.plan.status.setText(f"Готово: {done} успешно, {failed} с ошибкой.")
        self.refresh_all()
        QMessageBox.information(self, APP_TITLE, f"План выполнен: {done} успешно, {failed} с ошибкой.")

    def plan_failed(self, message: str):
        self.plan.execute_btn.setEnabled(True)
        self.plan.status.setText("Ошибка: " + message)
        QMessageBox.critical(self, APP_TITLE, message)

    def scan_programs(self):
        if os.name != "nt":
            QMessageBox.information(self, APP_TITLE, "Реестр установленных программ доступен только на Windows.")
            return
        if self.program_worker and self.program_worker.isRunning():
            return
        self.program_worker = ProgramsWorker()
        self.program_worker.finished_ok.connect(self.programs_done)
        self.program_worker.failed.connect(lambda m: QMessageBox.critical(self, APP_TITLE, m))
        self.program_worker.start()

    def programs_done(self, programs):
        self.db.upsert_programs(programs)
        self.programs.refresh()
        QMessageBox.information(self, APP_TITLE, f"Найдено программ: {len(programs)}")


def run() -> int:
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyleSheet(
        """
        QWidget { font-size: 12px; }
        QLabel#pageTitle { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
        QPushButton { padding: 7px 10px; }
        QTableWidget { gridline-color: #dddddd; }
        """
    )
    window = MainWindow()
    if icon_path:
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    return app.exec()
