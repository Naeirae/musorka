from __future__ import annotations

import hashlib
import os
import re
import subprocess
from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def scan_installed_programs() -> list[dict[str, str]]:
    if os.name != "nt":
        return []

    import winreg

    locations = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "Пользователь"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "Компьютер"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "Компьютер (32-bit)"),
    ]

    result: list[dict[str, str]] = []
    seen: set[str] = set()

    def read_value(key, name: str) -> str:
        try:
            return _clean(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return ""

    for hive, path, scope in locations:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            count = winreg.QueryInfoKey(root)[0]
            for index in range(count):
                try:
                    sub_name = winreg.EnumKey(root, index)
                    sub = winreg.OpenKey(root, sub_name)
                except OSError:
                    continue
                with sub:
                    name = read_value(sub, "DisplayName")
                    if not name:
                        continue
                    version = read_value(sub, "DisplayVersion")
                    publisher = read_value(sub, "Publisher")
                    install_location = read_value(sub, "InstallLocation")
                    uninstall_string = read_value(sub, "UninstallString")
                    quiet_uninstall_string = read_value(sub, "QuietUninstallString")
                    signature = "|".join([name.lower(), version.lower(), publisher.lower(), scope.lower()])
                    if signature in seen:
                        continue
                    seen.add(signature)
                    key = hashlib.sha1(signature.encode("utf-8", errors="ignore")).hexdigest()
                    result.append(
                        {
                            "program_key": key,
                            "name": name,
                            "version": version,
                            "publisher": publisher,
                            "install_location": install_location,
                            "uninstall_string": uninstall_string,
                            "quiet_uninstall_string": quiet_uninstall_string,
                            "scope": scope,
                        }
                    )
    return sorted(result, key=lambda x: x["name"].lower())


def prepare_uninstall_command(uninstall_string: str) -> str:
    """Normalize the registered interactive uninstaller command.

    MSI entries are frequently registered as `/I{product-code}` (maintenance UI).
    For an explicit uninstall action we switch only that MSI verb to `/X` and keep
    all other arguments intact. We never synthesize a command when Registry did
    not provide one.
    """
    command = _clean(uninstall_string)
    if not command:
        return ""
    if re.match(r'^\s*"?msiexec(?:\.exe)?"?(?:\s|$)', command, flags=re.I):
        command = re.sub(r'(?i)(\s)/I(?=\s*\{)', r'\1/X', command, count=1)
        command = re.sub(r'(?i)(\s)/I(?=\s)', r'\1/X', command, count=1)
    return command


def launch_uninstaller(uninstall_string: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Деинсталлятор программ можно запускать только на Windows.")
    command = prepare_uninstall_command(uninstall_string)
    if not command:
        raise RuntimeError("У этой записи Windows нет команды удаления.")
    try:
        subprocess.Popen(command, shell=False)
    except OSError as exc:
        raise RuntimeError(f"Не удалось запустить деинсталлятор: {exc}") from exc
    return command
