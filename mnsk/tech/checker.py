#!/usr/bin/env python3
"""
checker.py - pre-commit проверка: комментарии, которые пересказывают код.

Практическое следствие исследования. Корпусный анализ измеряет, как часто комментарии
повторяют код; чекер применяет ТУ ЖЕ линейку (metrics.py, extract_comments.py) к новому
коду и показывает автору места, где комментарий не добавляет ничего сверх кода.
Поэтому здесь нет собственных правил: любое расхождение чекера и корпусного анализа -
ошибка, а не «настройка».

Что проверяется. Только комментарии, ДОБАВЛЕННЫЕ в индекс (git diff --cached): чекер не
придирается к чужому старому коду. Помечается комментарий, который целиком добавлен
в этом коммите и относится к классу referential (overlap >= θ, нет маркеров «почему»).

Режимы:
  --staged           (по умолчанию) индекс git; для хука pre-commit
  --file A B ...     все комментарии указанных файлов
  --text "..." --code "..."   один комментарий и код-цель: для живой демонстрации

По умолчанию чекер только предупреждает (код возврата 0). --strict блокирует коммит (1).
Обойти проверку можно штатно: git commit --no-verify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import metrics
from config import DEFAULT_THETA, MIN_CONTENT_WORDS, RELEVANT_EXTS, SKIP_PATH_PATTERNS, setup_console
from extract_comments import extract_from_source, language_for
from parser import parse_added_ranges

_SKIP_RE = re.compile("|".join(SKIP_PATH_PATTERNS))
HINT = "Объясните ПОЧЕМУ так сделано (причина, ограничение, ссылка на issue), а не ЧТО делает код."


class Style:
    """ANSI-цвета только в настоящем терминале и если не задан NO_COLOR."""

    def __init__(self, enabled: bool):
        self.on = enabled

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    bold = lambda self, t: self.c("1", t)
    red = lambda self, t: self.c("31", t)
    yellow = lambda self, t: self.c("33", t)
    dim = lambda self, t: self.c("2", t)
    green = lambda self, t: self.c("32", t)


def make_style() -> Style:
    on = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    if on and os.name == "nt":
        os.system("")  # включает обработку ANSI-последовательностей в консоли Windows 10+
    return Style(on)


# --- поиск и оценка ---------------------------------------------------------------------

def evaluate(text: str, code: str | None, theta: float, lex=None) -> dict:
    lex = lex or metrics.load_lexicon()
    f = metrics.analyze(text, code, lex, with_sentiment=False)
    return {"features": f, "cls": metrics.classify(f, theta), "overlap": f["overlap"], "matched": f["matched"],
            "cues": sorted(f["cues"])}


def check_source(src: bytes, lang: str, added: list | None, theta: float, include_doc: bool = False,
                 path: str = "<text>") -> tuple[list[dict], int]:
    """(находки, сколько комментариев проверено)."""
    lex = metrics.load_lexicon()
    recs, _ = extract_from_source(src, lang, added)
    flags, checked = [], 0
    for r in recs:
        if r["exclude"] or (r["kind"] == "doc" and not include_doc) or not r.get("code"):
            continue
        if not metrics.is_english(r["text"], lex):
            continue
        ev = evaluate(r["text"], r["code"], theta, lex)
        if ev["overlap"] is None:
            continue
        checked += 1
        if ev["cls"] == "referential":
            flags.append({"file": path, "line": r["start_line"], "kind": r["kind"], "text": r["text"],
                          "overlap": round(ev["overlap"], 3), "matched": ev["matched"]})
    return flags, checked


def _git(repo: Path, *args: str) -> bytes:
    res = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", errors="replace").strip() or f"git {' '.join(args)} завершился с ошибкой")
    return res.stdout


def staged_targets(repo: Path) -> dict[str, list[list[int]]]:
    """{путь: добавленные строки} по индексу. -M: переименование без правок ничего не добавляет."""
    patch = _git(repo, "diff", "--cached", "-M", "-U0", "--no-color", "--diff-filter=AMR").decode("utf-8", errors="replace")
    return {p: r for p, r in parse_added_ranges(patch).items()
            if r and Path(p).suffix in RELEVANT_EXTS and not _SKIP_RE.search(p)}


def check_staged(repo: Path, theta: float, include_doc: bool) -> tuple[list[dict], int, int]:
    flags, checked, files = [], 0, 0
    for path, ranges in staged_targets(repo).items():
        try:
            src = _git(repo, "show", f":{path}")  # версия из ИНДЕКСА: проверяется то, что реально уйдёт в коммит
        except RuntimeError:
            continue
        lang = language_for(path, src)
        if lang is None:
            continue
        files += 1
        fl, n = check_source(src, lang, ranges, theta, include_doc, path)
        flags += fl
        checked += n
    return flags, checked, files


# --- вывод -------------------------------------------------------------------------------

def print_flags(flags: list[dict], checked: int, files: int, st: Style, theta: float) -> None:
    if not flags:
        print(st.green(f"✓ Комментарии-пересказы не найдены (проверено комментариев: {checked}, файлов: {files})."))
        return
    print(st.bold(f"Найдено комментариев-пересказов: {len(flags)} из {checked} проверенных (θ = {theta}).\n"))
    for f in flags:
        first = f["text"].splitlines()[0][:100]
        print(f"{st.bold(f['file'] + ':' + str(f['line']))}  {st.dim('[' + f['kind'] + ']')}  overlap {st.red(f'{f['overlap']:.2f}')}")
        print(f"    {first}")
        print(f"    {st.yellow('↳')} совпадает с кодом: {', '.join(f['matched'][:8])}")
    print(f"\n{st.yellow('Подсказка:')} {HINT}")
    print(st.dim("Пропустить проверку: git commit --no-verify"))


def run_text_mode(text: str, code: str, theta: float, as_json: bool, st: Style) -> int:
    lex = metrics.load_lexicon()
    ev = evaluate(text, code, theta, lex)
    f = ev["features"]
    if as_json:
        print(json.dumps({"cls": ev["cls"], "overlap": f["overlap"], "overlap_ext": f["overlap_ext"], "matched": f["matched"],
                          "cues": f["cues"], "n_content": f["n_content"]}, ensure_ascii=False))
        return 0
    print(st.bold("Комментарий:"), text)
    print(st.bold("Код:       "), code.replace("\n", " ⏎ ")[:120])
    if f["overlap"] is None:
        print(st.yellow(f"overlap не определён: в комментарии меньше {MIN_CONTENT_WORDS} содержательных слов или у кода нет слов."))
    else:
        print(f"overlap = {f['overlap']:.2f}  (с учётом парафраз: {f['overlap_ext']:.2f}), "
              f"содержательных слов: {f['n_content']}, совпало: {', '.join(f['matched']) or '-'}")
    if f["cues"]:
        print("Маркеры «почему»/прагматики:", ", ".join(f"{k}×{v}" for k, v in f["cues"].items()))
    verdict = {"referential": st.red("ПЕРЕСКАЗ КОДА"), "explanatory": st.green("ОБЪЯСНЯЕТ"), "other": "добавляет новую информацию", None: "не определено"}
    print(st.bold("Вердикт:"), verdict[ev["cls"]])
    if ev["cls"] == "referential":
        print(st.yellow("Подсказка:"), HINT)
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Проверка комментариев на пересказ кода (pre-commit)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="проверить индекс git (по умолчанию)")
    mode.add_argument("--file", nargs="+", metavar="PATH", help="проверить все комментарии указанных файлов")
    mode.add_argument("--text", help="один комментарий; вместе с --code")
    ap.add_argument("--code", help="код-цель для --text")
    ap.add_argument("--repo", default=".", help="каталог репозитория (по умолчанию текущий)")
    ap.add_argument("--theta", type=float, default=DEFAULT_THETA, help=f"порог overlap (по умолчанию {DEFAULT_THETA}, как в корпусном анализе)")
    ap.add_argument("--strict", action="store_true", default=os.environ.get("MNSK_CHECKER_STRICT") == "1",
                    help="код возврата 1, если найдены пересказы (блокирует коммит)")
    ap.add_argument("--include-doc", action="store_true", help="проверять и докстринги (по умолчанию нет: они описывают функцию целиком)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args(argv)
    st = make_style()

    if args.text is not None:
        if args.code is None:
            ap.error("--text требует --code")
        return run_text_mode(args.text, args.code, args.theta, args.json, st)

    try:
        if args.file:
            flags, checked, files = [], 0, 0
            for p in args.file:
                path = Path(p)
                lang = language_for(str(path), path.read_bytes())
                if lang is None:
                    continue
                fl, n = check_source(path.read_bytes(), lang, None, args.theta, args.include_doc, str(path))
                flags, checked, files = flags + fl, checked + n, files + 1
        else:
            flags, checked, files = check_staged(Path(args.repo), args.theta, args.include_doc)
    except (RuntimeError, OSError) as exc:
        # Сбой самого чекера не должен блокировать коммит: это инструмент подсказки, а не шлагбаум.
        print(f"checker: пропущено из-за ошибки: {exc}", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps({"flags": flags, "checked": checked, "files": files, "theta": args.theta}, ensure_ascii=False))
    else:
        print_flags(flags, checked, files, st, args.theta)
    return 1 if (flags and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
