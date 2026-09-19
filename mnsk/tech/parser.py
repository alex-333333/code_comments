#!/usr/bin/env python3
"""
parser.py - сбор исходного кода для корпуса (МНСК-проект).

Задача: найти нужные коммиты (AI / human по критерию трейлера и даты)
и выгрузить ПОЛНОЕ содержимое затронутых файлов на этой ревизии, а также
диапазоны строк, которые коммит ДОБАВИЛ.
Разбор комментариев (tree-sitter, классификация referential/explanatory)
находится отдельно в extract_comments.py - этот модуль про сбор сырых
данных, ничего не знает про лингвистику.

Почему полный файл, а не diff-patch: привязка комментарий→код
(leading/trailing, многострочные блоки) требует валидного синтаксического
дерева, а diff обрезает контекст вокруг изменённых строк - дерево на
таком фрагменте не построить.

Почему при этом нужны added_ranges: файл на ревизии содержит и комментарии,
написанные людьми задолго до коммита. Без диапазонов добавленных строк они
попали бы в AI-корпус и размыли результат. Полный файл нужен парсеру,
диапазоны - атрибуции.

Методология критериев корпуса:
  AI-корпус:    коммит содержит хотя бы один известный AI co-author трейлер
                (config.AI_TRAILERS). Список заведомо неполный - новые AI-инструменты
                появляются быстрее, чем можно за ними уследить; подозрительные
                коммиты без известного трейлера пишутся в suspects.log.
  Human-корпус: коммит сделан до HUMAN_CUTOFF И не содержит ни одного
                AI-трейлера (двойная защита от ложного попадания).

Всё работает через локальный git (bare clone + git show/cat-file),
без обращений к GitHub REST API - значит нет проблем с rate limit
на этапе выгрузки контента, и результат детерминирован (в отличие
от индекса GitHub Search API, который переиндексируется без предупреждения).

Структура вывода:
  <outdir>/<source>/<owner>__<repo>/<sha10>/<sanitized_path>  - содержимое файла
  <outdir>/manifest.jsonl                                     - метаданные, по строке на файл
  <outdir>/suspects.log, <outdir>/errors.log                  - диагностика
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from config import (
    setup_console, rmtree_force, AI_TRAILER_MAP, AI_TRAILERS, DEFAULT_MAX_FILE_KB, DEFAULT_MAX_FILES_PER_COMMIT,
    DEFAULT_MAX_FILES_PER_REPO, DEFAULT_SEED, HUMAN_CUTOFF, RELEVANT_EXTS,
    SKIP_PATH_PATTERNS, SUSPECT_PATTERNS,
)

# Таймауты нужны, потому что без них зависший git (сеть, огромный репозиторий)
# останавливает многочасовой прогон навсегда и без единой строки в логе.
CLONE_TIMEOUT = 1800
CLONE_ATTEMPTS = 3
GIT_TIMEOUT = 300
# 240, а не 260: запас на длину имени корня и на \\?\-префиксы; Windows MAX_PATH = 260.
MAX_LOCAL_PATH = 240

_SKIP_RE = re.compile("|".join(SKIP_PATH_PATTERNS))
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Путь-фильтр для git: без него `git show` печатает патчи vendored-файлов на сотни мегабайт.
_PATHSPECS = [f"*{ext}" for ext in sorted(RELEVANT_EXTS)]


class BudgetExceeded(Exception):
    """Кидается, когда накопленный размер корпуса достиг лимита."""


# --- git -----------------------------------------------------------------------

def _git_env() -> dict:
    # GIT_TERMINAL_PROMPT=0: приватный/удалённый репозиторий иначе заставляет git
    # ждать логин с клавиатуры, и на Windows прогон висит без сообщений.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"}


def run(args: list[str], timeout: int = GIT_TIMEOUT) -> str:
    # encoding задан явно: text=True без него декодирует по локали Windows (cp1251/cp1252)
    # и ломает не-ASCII имена файлов и сообщения коммитов.
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env=_git_env())
    if result.returncode != 0:
        raise RuntimeError(f"git error: {' '.join(args)}\n{result.stderr}")
    return result.stdout


def _git(repo_path: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    # quotepath=false: иначе git печатает не-ASCII пути в виде "\303\251" в кавычках.
    return run(["git", "-c", "core.quotepath=false", "--git-dir", str(repo_path), *args], timeout=timeout)


def parse_entry(entry: str) -> tuple[str, str, str]:
    """Строка из файла репозиториев -> (owner, repo, откуда клонировать).

    Три формата: `owner/repo` (GitHub), полный URL, путь к локальному репозиторию.
    Локальные пути нужны для тестов и демо без сети."""
    e = entry.strip()
    p = Path(e)
    if p.is_dir() and ((p / ".git").exists() or (p / "HEAD").exists()):
        return "local", p.resolve().name, str(p.resolve())
    if e.startswith(("http://", "https://", "git@", "ssh://")):
        tail = re.split(r"[/:]", e.removesuffix(".git").rstrip("/"))
        return tail[-2], tail[-1], e
    if re.fullmatch(r"[\w.-]+/[\w.-]+", e):
        owner, repo = e.split("/")
        return owner, repo, f"https://github.com/{owner}/{repo}.git"
    raise ValueError(f"не понимаю запись репозитория: {entry!r}")


def clone_repo(owner: str, repo: str, source_url: str, workdir: Path) -> Path:
    dest = workdir / "_clones" / f"{owner}__{repo}"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  клонирую {owner}/{repo}...")
        # Клонирую во временный каталог и переименовываю только после успеха. Если процесс убит
        # (Ctrl+C, закрыто окно, сбой питания), обработчик исключений не успеет ничего очистить, и
        # недоклон при следующем запуске выглядел бы готовым клоном и молча давал неполные данные.
        tmp = dest.with_name(dest.name + ".part")
        for attempt in range(1, CLONE_ATTEMPTS + 1):
            rmtree_force(tmp)
            try:
                run(["git", "clone", "--bare", "--quiet", source_url, str(tmp)], timeout=CLONE_TIMEOUT)
                tmp.rename(dest)
                break
            except RuntimeError as exc:
                # Сетевые сбои («early EOF», обрыв соединения) случайны: повтор обычно проходит.
                rmtree_force(tmp)
                if attempt == CLONE_ATTEMPTS:
                    raise
                print(f"  клонирование не удалось ({str(exc).splitlines()[-1][:80]}), попытка {attempt + 1}/{CLONE_ATTEMPTS}...")
                time.sleep(5 * attempt)
            except BaseException:
                rmtree_force(tmp)
                raise
    return dest


def find_commits(repo_path: Path, source: str) -> list[dict]:
    """Коммиты нужного корпуса: [{sha, date, tool, tools}].

    -i: git grep по умолчанию регистрозависим, а Claude Code пишет 'Co-Authored-By'
    с заглавными - без -i такие коммиты молча терялись бы.
    -F: трейлер ищу как строку, а не как регулярное выражение.
    --no-merges: у merge-коммита «добавленные строки» не определены однозначно."""
    base = ["log", "--all", "--no-merges", "-i", "-F", "--format=%H%x09%cI"]

    def parse(out: str) -> list[tuple[str, str]]:
        rows = []
        for line in out.splitlines():
            if "\t" in line:
                sha, date = line.split("\t", 1)
                rows.append((sha, date))
        return rows

    if source == "ai":
        found: dict[str, dict] = {}
        # Отдельный поиск на каждый трейлер - так известно, КАКОЙ инструмент,
        # и корпус можно потом разбивать на Claude / Copilot / Cursor.
        for tool, trailer in AI_TRAILER_MAP.items():
            for sha, date in parse(_git(repo_path, *base, "--grep", trailer)):
                rec = found.setdefault(sha, {"sha": sha, "date": date, "tool": tool, "tools": []})
                rec["tools"].append(tool)
        return list(found.values())

    grep_args = [x for t in AI_TRAILERS for x in ("--grep", t)]
    out = _git(repo_path, *base, f"--before={HUMAN_CUTOFF}", *grep_args, "--invert-grep")
    return [{"sha": sha, "date": date, "tool": None, "tools": []} for sha, date in parse(out)]


def find_suspects(repo_path: Path, ai_shas: set[str]) -> list[tuple[str, str]]:
    """Коммиты, похожие на сгенерированные, но без известного трейлера.

    Так пополняется список AI_TRAILERS: если здесь много записей, критерий
    занижает AI-корпус, и это надо описать в Limitations."""
    grep_args = [x for p in SUSPECT_PATTERNS for x in ("--grep", p)]
    out = _git(repo_path, "log", "--all", "--no-merges", "-i", "-F", "--format=%H%x09%s", *grep_args)
    rows = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        if sha and sha not in ai_shas:
            rows.append((sha, subject))
    return rows


def parse_added_ranges(patch: str) -> dict[str, list[list[int]]]:
    """Патч `git show -U0` -> {путь: [[с, по], ...]} строки НОВОГО файла, добавленные коммитом.

    Разбор ведётся по счётчикам из заголовка hunk-а, а не по префиксам строк: добавленная
    строка «++ x» в патче выглядит как «+++ x», то есть неотличима от заголовка файла."""
    files: dict[str, list[list[int]]] = {}
    cur: str | None = None
    lines = patch.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("+++ "):
            path = line[4:].rstrip("\t")
            if path == "/dev/null":
                cur = None
            else:
                cur = path[2:] if path.startswith("b/") else path
                files.setdefault(cur, [])
            continue
        m = _HUNK_RE.match(line)
        if not m or cur is None:
            continue
        old_n = int(m.group(2)) if m.group(2) is not None else 1
        start = int(m.group(3))
        new_n = int(m.group(4)) if m.group(4) is not None else 1
        if new_n > 0:
            files[cur].append([start, start + new_n - 1])
        # пропускаем тело hunk-а по счётчикам
        left_old, left_new = old_n, new_n
        while i < len(lines) and (left_old > 0 or left_new > 0):
            body = lines[i]
            if body.startswith("\\"):
                i += 1
                continue
            if body.startswith("-") and left_old > 0:
                left_old -= 1
            elif body.startswith("+") and left_new > 0:
                left_new -= 1
            else:
                break
            i += 1
    return files


def added_ranges_for_commit(repo_path: Path, sha: str) -> dict[str, list[list[int]]]:
    """Один вызов git на коммит вместо «список файлов + отдельный вызов на каждый».

    -M: переименование без правок не даёт hunk-ов. Без -M git показал бы его как
       «удалён старый файл, добавлен новый целиком», и ВСЕ старые комментарии
       файла были бы приписаны этому коммиту.
    -U0: только изменённые строки, без контекста.
    Корневой коммит `git show` печатает целиком как добавленный (--root не нужен)."""
    patch = _git(repo_path, "show", "--format=", "-M", "-U0", "--no-color",
                 "--diff-filter=AMR", sha, "--", *_PATHSPECS)
    return parse_added_ranges(patch)


class BlobReader:
    """Постоянный `git cat-file --batch`: один процесс на репозиторий вместо процесса на файл.

    На Windows запуск процесса стоит десятки миллисекунд; на корпусе в сотни тысяч
    файлов это разница между минутами и часами."""

    def __init__(self, repo_path: Path):
        self.proc = subprocess.Popen(
            ["git", "--git-dir", str(repo_path), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_git_env(),
        )

    def read(self, sha: str, path: str, max_bytes: int) -> bytes | None:
        if "\n" in path:
            return None
        self.proc.stdin.write(f"{sha}:{path}\n".encode("utf-8"))
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode("utf-8", errors="replace").strip()
        parts = header.split()
        if len(parts) != 3 or parts[1] != "blob":
            return None  # missing / не blob (например, submodule)
        size = int(parts[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)  # завершающий \n, которым git разделяет ответы
        return data if size <= max_bytes else None

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --- файлы и манифест ------------------------------------------------------------

def sanitize_path(path: str) -> str:
    """Безопасное имя для Windows: недопустимые символы, зарезервированные имена (CON, NUL...),
    точка/пробел в конце компонента."""
    parts = []
    for comp in re.sub(r"[^\w./-]", "_", path).split("/"):
        comp = comp.rstrip(". ") or "_"
        if comp.split(".")[0].upper() in _WIN_RESERVED:
            comp = "_" + comp
        parts.append(comp)
    return "/".join(parts)


def local_file_path(workdir: Path, source: str, owner: str, repo: str, sha: str, path: str) -> Path:
    base = workdir / source / f"{owner}__{repo}" / sha[:10]
    out = base / sanitize_path(path)
    if len(str(out.resolve())) > MAX_LOCAL_PATH:
        # Длинный путь -> хеш + имя файла. Расширение сохраняется (по нему определяется язык),
        # а уникальность обеспечивает хеш полного исходного пути.
        h = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
        out = base / f"{h}_{sanitize_path(Path(path).name)[-60:]}"
    return out


def load_seen(manifest_path: Path) -> set[tuple]:
    """Ключи уже собранного: повторный запуск на том же outdir не плодит дубли
    (раньше манифест только дописывался, и дедупликацию надо было делать руками)."""
    seen = set()
    if manifest_path.exists():
        with open(manifest_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    e = json.loads(line)
                    seen.add((e["source"], e["repo"], e["sha"], e["file"]))
    return seen


def process_repo(entry: str, source: str, workdir: Path, manifest_f, seen: set, used: list[int],
                 budget: int, opts: argparse.Namespace) -> int:
    """used - мутабельный счётчик (list[int]), общий для всех репозиториев прогона."""
    owner, repo, url = parse_entry(entry)
    repo_id = f"{owner}/{repo}"
    repo_path = clone_repo(owner, repo, url, workdir)
    commits = find_commits(repo_path, source)
    print(f"  найдено коммитов ({source}): {len(commits)}")

    if source == "ai":
        suspects = find_suspects(repo_path, {c["sha"] for c in commits})
        if suspects:
            with open(workdir / "suspects.log", "a", encoding="utf-8") as sf:
                for sha, subject in suspects:
                    sf.write(f"{repo_id}\t{sha}\t{subject}\n")
            print(f"  подозрительных коммитов без известного трейлера: {len(suspects)} (см. suspects.log)")

    # Перемешиваю детерминированно: `git log` идёт от новых к старым, и без перемешивания
    # лимит на репозиторий брал бы только самые свежие коммиты - смещение по времени.
    random.Random(f"{opts.seed}:{repo_id}").shuffle(commits)

    saved = skipped_bulk = 0
    max_bytes = opts.max_file_kb * 1024
    with BlobReader(repo_path) as blobs:
        for c in commits:
            if saved >= opts.max_files_per_repo:
                print(f"  достигнут лимит {opts.max_files_per_repo} файлов на репозиторий")
                break
            try:
                per_file = added_ranges_for_commit(repo_path, c["sha"])
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                _log_error(workdir, f"{repo_id} {c['sha']}: {exc}")
                continue
            if len(per_file) > opts.max_files_per_commit:
                skipped_bulk += 1  # массовое переформатирование/вендоринг, не авторский код
                continue

            for path, ranges in per_file.items():
                if Path(path).suffix not in RELEVANT_EXTS or _SKIP_RE.search(path):
                    continue
                if not ranges:
                    continue  # чистое переименование: коммит ничего не добавил
                key = (source, repo_id, c["sha"], path)
                if key in seen:
                    continue
                data = blobs.read(c["sha"], path, max_bytes)
                if data is None:
                    continue
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # не-utf8 или бинарник - молча пропускается, см. Limitations
                if data.startswith(b"\xef\xbb\xbf"):
                    data = data[3:]  # BOM сдвигает колонки первой строки

                out_path = local_file_path(workdir, source, owner, repo, c["sha"], path)
                if out_path.exists():
                    # Файловая система без учёта регистра: A.py и a.py совпали бы в один файл.
                    out_path = out_path.with_name(hashlib.sha1(path.encode()).hexdigest()[:8] + "_" + out_path.name)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                # write_bytes, а не write_text: write_text на Windows превращает \n в \r\n
                # и портит файлы, которые в репозитории были с LF.
                out_path.write_bytes(data)

                manifest_f.write(json.dumps({
                    "repo": repo_id,
                    "sha": c["sha"],
                    "file": path,
                    "lang": Path(path).suffix,
                    "source": source,
                    "tool": c["tool"],
                    "commit_date": c["date"],
                    "added_ranges": ranges,
                    "size": len(data),
                    "local_path": out_path.relative_to(workdir).as_posix(),
                }, ensure_ascii=False) + "\n")
                manifest_f.flush()  # сбой на середине не должен терять уже записанные файлы

                seen.add(key)
                saved += 1
                used[0] += len(data)
                if used[0] >= budget:
                    raise BudgetExceeded()
    if skipped_bulk:
        print(f"  пропущено массовых коммитов (> {opts.max_files_per_commit} файлов): {skipped_bulk}")
    return saved


def _log_error(workdir: Path, message: str) -> None:
    with open(workdir / "errors.log", "a", encoding="utf-8") as f:
        f.write(message.strip() + "\n")


def read_repo_list(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")  # -sig: файл, сохранённый из Блокнота Windows, начинается с BOM
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Сбор корпуса: AI vs human код (выгрузка файлов и added_ranges)")
    ap.add_argument("--repos", required=True, help="файл со списком репозиториев, по одному в строке "
                                                     "(owner/repo, URL или путь к локальному репозиторию)")
    ap.add_argument("--source", choices=["ai", "human"], required=True)
    ap.add_argument("--outdir", default="./corpus")
    ap.add_argument("--gb", type=float, default=5.0,
                    help="лимит размера собранного корпуса в гигабайтах (1 GB = 1024**3 байт)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="seed выборки коммитов (воспроизводимость)")
    ap.add_argument("--max-files-per-repo", type=int, default=DEFAULT_MAX_FILES_PER_REPO,
                    help="потолок файлов на репозиторий: один гигантский проект не должен съесть весь корпус")
    ap.add_argument("--max-files-per-commit", type=int, default=DEFAULT_MAX_FILES_PER_COMMIT,
                    help="коммиты с большим числом файлов пропускаются (массовые переформатирования)")
    ap.add_argument("--max-file-kb", type=int, default=DEFAULT_MAX_FILE_KB, help="пропускать файлы больше этого размера")
    ap.add_argument("--delete-clones", action="store_true", help="удалять bare-клон после обработки (экономит диск)")
    return ap


def main(argv: list[str] | None = None) -> None:
    setup_console()
    args = build_parser().parse_args(argv)

    budget = int(args.gb * 1024 ** 3)
    workdir = Path(args.outdir)
    workdir.mkdir(parents=True, exist_ok=True)
    repo_list = read_repo_list(Path(args.repos))

    manifest_path = workdir / "manifest.jsonl"
    seen = load_seen(manifest_path)
    used = [0]
    total = failed = 0

    try:
        with open(manifest_path, "a", encoding="utf-8") as manifest_f:
            for entry in repo_list:
                print(f"[{args.source}] {entry}")
                try:
                    total += process_repo(entry, args.source, workdir, manifest_f, seen, used, budget, args)
                except BudgetExceeded:
                    raise
                except Exception as exc:  # один сбойный репозиторий не должен останавливать остальные
                    failed += 1
                    _log_error(workdir, f"{entry}: {type(exc).__name__}: {exc}")
                    print(f"  ! пропущен ({type(exc).__name__}); подробности в errors.log")
                finally:
                    if args.delete_clones:
                        try:
                            o, r, _ = parse_entry(entry)
                            rmtree_force(workdir / "_clones" / f"{o}__{r}")
                        except ValueError:
                            pass
    except BudgetExceeded:
        print(f"\nЛимит {args.gb} GB достигнут, остановка.")

    print(f"Готово. Файлов сохранено: {total}. Размер: {used[0] / 1024**3:.2f} GB. "
          f"Репозиториев с ошибкой: {failed}. Манифест: {manifest_path}")


if __name__ == "__main__":
    main()
