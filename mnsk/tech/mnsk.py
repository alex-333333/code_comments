#!/usr/bin/env python3
"""
mnsk.py - единая точка входа проекта.

    python mnsk.py setup        # один раз: скачать грамматики tree-sitter и всё проверить
    python mnsk.py doctor       # диагностика окружения
    python mnsk.py demo         # сквозной прогон на синтетических данных (без сети и токена)

    python mnsk.py discover ... # найти репозитории (нужен GITHUB_TOKEN)
    python mnsk.py collect ...  # собрать корпус (parser.py)
    python mnsk.py run          # извлечение комментариев -> анализ -> дашборд
    python mnsk.py check ...    # чекер комментариев;  install-hook - поставить его в git
    python mnsk.py validate ... # слепая выборка для ручной разметки и калибровка порога

У каждой команды свой --help:  python mnsk.py collect --help
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from config import LANG_LABELS, ROOT, setup_console

COMMANDS = {
    "discover": ("discover_repos", "поиск репозиториев-кандидатов (ai / human / paired)"),
    "collect": ("parser", "сбор корпуса из репозиториев"),
    "extract": ("extract_comments", "извлечение комментариев (tree-sitter)"),
    "analyze": ("counter", "анализ корпуса -> results/"),
    "dashboard": ("dashboard", "сборка dashboard.html"),
    "check": ("checker", "проверка комментариев на пересказ кода"),
    "install-hook": ("install_hook", "установить/удалить pre-commit хук"),
    "validate": ("validate", "выборка для ручной разметки и калибровка θ"),
    "demo": ("demo", "сквозной демо-прогон на синтетических данных"),
}
# Флаги, которые относятся к этапу extract, а не analyze (нужно только команде run).
EXTRACT_FLAGS = {"--all-comments": 0, "--code-window": 1, "--limit": 1}

REQUIRED = [("tree_sitter", "tree-sitter"), ("tree_sitter_language_pack", "tree-sitter-language-pack"),
            ("requests", "requests"), ("nltk", "nltk"), ("vaderSentiment", "vaderSentiment")]


# --- doctor / setup ------------------------------------------------------------------

def _ok(flag: bool | None) -> str:
    return "OK " if flag else ("-- " if flag is None else "ERR")


def doctor() -> int:
    rows: list[tuple[bool | None, str, str]] = []   # (ok | None=предупреждение, что, подсказка)
    v = sys.version_info
    rows.append((v >= (3, 10), f"Python {v.major}.{v.minor}.{v.micro}", "нужен Python 3.10 или новее"))

    git = shutil.which("git")
    if git:
        ver = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
        rows.append((True, ver, ""))
    else:
        rows.append((False, "git не найден", "установите Git for Windows: https://git-scm.com/download/win"))

    for mod, pkg in REQUIRED:
        try:
            m = importlib.import_module(mod)
            rows.append((True, f"{pkg} {getattr(m, '__version__', '')}".strip(), ""))
        except ImportError:
            rows.append((False, f"{pkg} не установлен", "python -m pip install -r requirements.txt"))

    try:
        import tree_sitter_language_pack as tslp
        have = set(tslp.downloaded_languages())
        need = sorted(LANG_LABELS)
        missing = [x for x in need if x not in have]
        rows.append((not missing, f"грамматики tree-sitter: {len(need) - len(missing)}/{len(need)}",
                     f"python mnsk.py setup   (не хватает: {', '.join(missing)})" if missing else ""))
    except ImportError:
        pass

    try:
        import metrics
        metrics.stem("returning")
        metrics.sentiment("nice")
        rows.append((True, f"стеммер и VADER работают; NLP-бэкенд признаков: {metrics.nlp_backend()}", ""))
    except Exception as exc:  # noqa: BLE001 - доктор должен показать любую причину, а не упасть
        rows.append((False, f"metrics не работает: {type(exc).__name__}: {exc}", ""))

    rows.append((True if os.environ.get("GITHUB_TOKEN") else None, "GITHUB_TOKEN задан" if os.environ.get("GITHUB_TOKEN") else "GITHUB_TOKEN не задан",
                 'нужен только для discover; PowerShell:  $env:GITHUB_TOKEN="ghp_..."'))
    if platform.system() == "Windows":
        lp = subprocess.run(["git", "config", "--global", "core.longpaths"], capture_output=True, text=True).stdout.strip() if git else ""
        rows.append((True if lp == "true" else None, f"git core.longpaths = {lp or 'не задано'}",
                     "рекомендуется: git config --global core.longpaths true (глубокие пути в больших репозиториях)"))
    try:
        import spacy  # noqa: F401
        rows.append((True, "spaCy установлен (необязательно)", ""))
    except ImportError:
        rows.append((None, "spaCy не установлен (необязательно)", "точный POS-тег для признака «начинается с глагола»; без него работает эвристика"))
    probe = ROOT / ".mnsk_write_test"
    try:
        probe.write_text("x"); probe.unlink()
        rows.append((True, "запись в каталог проекта разрешена", ""))
    except OSError as exc:
        rows.append((False, f"нет записи в каталог проекта: {exc}", ""))

    for ok, what, hint in rows:
        print(f"[{_ok(ok)}] {what}" + (f"\n        → {hint}" if hint and not ok else ""))
    bad = sum(1 for ok, *_ in rows if ok is False)
    print("\nВсё готово к работе." if not bad else f"\nПроблем: {bad}. Исправьте их и запустите doctor снова.")
    return 1 if bad else 0


def setup() -> int:
    """Скачать грамматики. tree-sitter-language-pack тянет их из сети при ПЕРВОМ использовании:
    без этого шага первый запуск extract на чужой машине или без интернета упал бы посреди работы."""
    try:
        import tree_sitter_language_pack as tslp
    except ImportError:
        print("Сначала: python -m pip install -r requirements.txt")
        return 1
    need = sorted(set(LANG_LABELS) - set(tslp.downloaded_languages()))
    if need:
        print(f"Скачиваю грамматики: {', '.join(need)} ...")
        try:
            tslp.download(need)
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось скачать ({type(exc).__name__}: {exc}). Проверьте интернет и повторите: python mnsk.py setup")
            return 1
    else:
        print("Грамматики tree-sitter уже скачаны.")
    print()
    return doctor()


# --- run --------------------------------------------------------------------------------------

def run_pipeline(rest: list[str]) -> int:
    """extract -> analyze -> dashboard с общими каталогами. Остальные флаги уходят нужному этапу."""
    corpus, results, out = "./corpus", "./results", "./dashboard.html"
    ex_args: list[str] = []
    an_args: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--corpus", "--results", "--out") and i + 1 < len(rest):
            corpus, results, out = (rest[i + 1] if a == "--corpus" else corpus, rest[i + 1] if a == "--results" else results,
                                    rest[i + 1] if a == "--out" else out)
            i += 2
        elif a in EXTRACT_FLAGS:
            n = EXTRACT_FLAGS[a]
            ex_args += rest[i:i + 1 + n]
            i += 1 + n
        else:
            an_args.append(a)
            i += 1
    import extract_comments, counter, dashboard
    print("== 1/3 Извлечение комментариев")
    extract_comments.main(["--corpus", corpus, *ex_args])
    print("== 2/3 Анализ")
    counter.main(["--corpus", corpus, "--results", results, *an_args])
    print("== 3/3 Дашборд")
    dashboard.main(["--results", results, "--out", out])
    return 0


def usage() -> None:
    print(__doc__)
    print("Команды:")
    for name, (_, help_) in COMMANDS.items():
        print(f"  {name:13} {help_}")
    print(f"  {'run':13} extract -> analyze -> dashboard одной командой")
    print(f"  {'setup':13} скачать грамматики и проверить окружение")
    print(f"  {'doctor':13} диагностика окружения")


def main(argv: list[str] | None = None) -> int:
    setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "doctor":
        return doctor()
    if cmd == "setup":
        return setup()
    if cmd == "run":
        return run_pipeline(rest)
    if cmd not in COMMANDS:
        print(f"Неизвестная команда: {cmd}\n")
        usage()
        return 2
    module = importlib.import_module(COMMANDS[cmd][0])
    result = module.main(rest)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
