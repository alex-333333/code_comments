#!/usr/bin/env python3
"""
install_hook.py - установка / удаление pre-commit хука с checker.py в git-репозиторий.

Хук - обычный sh-скрипт: Git for Windows выполняет его встроенным sh, отдельная
установка bash не нужна. В скрипт вписан абсолютный путь к тому Python, которым запущен
установщик, - поэтому хук работает без активации виртуального окружения.

Если в репозитории уже был свой pre-commit, он не теряется: сохраняется рядом как
pre-commit.mnsk-backup и вызывается из нового хука первым.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

from config import ROOT, setup_console

MARKER = "# mnsk-checker: managed by install_hook.py"
BACKUP = "pre-commit.mnsk-backup"


def hooks_dir(repo: Path) -> Path:
    res = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        raise SystemExit(f"{repo} - не git-репозиторий (git rev-parse: {res.stderr.strip()})")
    p = Path(res.stdout.strip())
    return p if p.is_absolute() else (repo / p)


def hook_text(python: str, checker: str, strict: bool, has_backup: bool) -> str:
    flags = " --strict" if strict else ""
    chain = f'"$(dirname "$0")/{BACKUP}" "$@" || exit $?\n' if has_backup else ""
    # Пути в POSIX-виде (C:/...): sh из Git for Windows не понимает обратные слэши.
    return (f"#!/bin/sh\n{MARKER}\n{chain}"
            f'"{Path(python).as_posix()}" "{Path(checker).as_posix()}" --staged{flags}\n'
            f"exit $?\n")


def install(repo: Path, strict: bool = False, python: str | None = None) -> Path:
    hooks = hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-commit"
    has_backup = (hooks / BACKUP).exists()
    if target.exists() and MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        target.replace(hooks / BACKUP)  # чужой хук не перезаписываю: сохраняю и вызываю
        has_backup = True
    target.write_text(hook_text(python or sys.executable, str(ROOT / "checker.py"), strict, has_backup),
                      encoding="utf-8", newline="\n")  # CRLF в sh-скрипте ломает shebang
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def uninstall(repo: Path) -> str:
    hooks = hooks_dir(repo)
    target, backup = hooks / "pre-commit", hooks / BACKUP
    if not target.exists() or MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        return "Хук MNSK не установлен - ничего не удалено."
    target.unlink()
    if backup.exists():
        backup.replace(target)
        return "Хук удалён, прежний pre-commit восстановлен."
    return "Хук удалён."


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Установка pre-commit хука с проверкой комментариев")
    ap.add_argument("--repo", default=".", help="репозиторий, куда ставить хук (по умолчанию текущий)")
    ap.add_argument("--strict", action="store_true", help="блокировать коммит, если найдены пересказы")
    ap.add_argument("--uninstall", action="store_true", help="удалить хук")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.uninstall:
        print(uninstall(repo))
        return
    path = install(repo, args.strict)
    mode = "БЛОКИРУЕТ коммит при находках" if args.strict else "только предупреждает"
    print(f"Хук установлен: {path}\nРежим: {mode}. Пропустить: git commit --no-verify. Удалить: python install_hook.py --uninstall")


if __name__ == "__main__":
    main()
