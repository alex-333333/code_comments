#!/usr/bin/env python3
"""
demo.py - сквозной прогон конвейера на СИНТЕТИЧЕСКИХ данных, без сети и токена.

Что это и чем НЕ является. Создаются три локальных git-репозитория с вымышленным
кодом: «человеческий» (2019, комментарии в человеческом стиле) и два «AI»
(2025, трейлеры Claude / Copilot / Cursor, комментарии-пересказы). Затем
запускаются collect -> extract -> analyze -> dashboard.

Комментарии в демо написаны вручную под ожидаемую гипотезу, поэтому демо
доказывает только одно: конвейер работает от начала до конца. Ни один
показанный в нём результат нельзя цитировать как научный, и дашборд
из демо помечен баннером «ДЕМО-ДАННЫЕ».

Все файлы создаются в demo_work/ и никогда не попадают в ./corpus.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from config import rmtree_force, setup_console

DEMO_DIR = Path(__file__).resolve().parent / "demo_work"


class GitRepo:
    """Тонкая обёртка над git для построения репозиториев с заданными датами и трейлерами.

    Даты задаются через GIT_AUTHOR_DATE/GIT_COMMITTER_DATE: критерий human-корпуса
    опирается на дату коммита, а «настоящих» коммитов 2019 года у синтетического
    репозитория нет."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")

    def _git(self, *args: str, env: dict | None = None) -> str:
        res = subprocess.run(
            ["git", "-c", "user.name=Demo Author", "-c", "user.email=demo@example.com",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
            cwd=self.path, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, **(env or {})},
        )
        if res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {res.stderr}")
        return res.stdout.strip()

    def commit(self, files: dict[str, str | bytes], message: str, date: str,
               trailers: list[str] | None = None) -> str:
        """Записать файлы (создать/перезаписать) и закоммитить. Возвращает sha."""
        for rel, content in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        self._git("add", "-A")
        msg = [message] + ([("\n".join(trailers))] if trailers else [])
        args = ["commit", "-q"]
        for part in msg:
            args += ["-m", part]
        self._git(*args, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return self._git("rev-parse", "HEAD")

    def rename(self, old: str, new: str, message: str, date: str, trailers: list[str] | None = None) -> str:
        (self.path / new).parent.mkdir(parents=True, exist_ok=True)
        self._git("mv", old, new)
        args = ["commit", "-q", "-m", message]
        if trailers:
            args += ["-m", "\n".join(trailers)]
        self._git(*args, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return self._git("rev-parse", "HEAD")


# Содержимое демо-репозиториев вынесено в demo_data.py, чтобы не смешивать
# механику (git) и «литературу» (комментарии); подгружается лениво в build_demo().


def build_demo(work: Path = DEMO_DIR) -> dict:
    """Создать репозитории и вернуть пути к спискам репозиториев."""
    from demo_data import build_repos
    if work.exists():
        rmtree_force(work)
    work.mkdir(parents=True)
    return build_repos(work, GitRepo)


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Сквозной демо-прогон на синтетических данных")
    ap.add_argument("--keep", action="store_true", help="не удалять demo_work перед запуском")
    ap.add_argument("--no-dashboard", action="store_true", help="остановиться после analyze")
    args = ap.parse_args(argv)

    import parser as corpus_parser
    import extract_comments
    import counter
    import dashboard

    work = DEMO_DIR
    lists = build_demo(work) if not args.keep else {"ai": work / "ai_repos.txt", "human": work / "human_repos.txt"}
    corpus = work / "corpus"

    print("== 1/4 Сбор корпуса")
    corpus_parser.main(["--repos", str(lists["ai"]), "--source", "ai", "--outdir", str(corpus), "--gb", "0.1"])
    corpus_parser.main(["--repos", str(lists["human"]), "--source", "human", "--outdir", str(corpus), "--gb", "0.1"])

    print("== 2/4 Извлечение комментариев")
    extract_comments.main(["--corpus", str(corpus)])

    print("== 3/4 Анализ")
    counter.main(["--corpus", str(corpus), "--results", str(work / "results"), "--demo",
                  "--boot", "300", "--min-per-repo", "1"])

    if not args.no_dashboard:
        print("== 4/4 Дашборд")
        dashboard.main(["--results", str(work / "results"), "--out", str(work / "dashboard.html")])
        print(f"\nОткройте: {work / 'dashboard.html'}")


if __name__ == "__main__":
    main()
