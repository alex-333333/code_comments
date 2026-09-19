#!/usr/bin/env python3
"""
extract_comments.py - комментарии из корпуса: tree-sitter -> comments.jsonl.

Вход:  <corpus>/manifest.jsonl (его пишет parser.py) и сами файлы ревизий.
Выход: <corpus>/comments.jsonl (по строке на наблюдение) и extract_stats.json.

Одно «наблюдение» - это смысловая единица комментария, а не строка исходника:
подряд идущие строки `# ...` - один комментарий, докстринг - один комментарий.

Почему tree-sitter, а не `ast` или `tokenize`: ast выбрасывает комментарии,
tokenize знает только Python, а мне нужно 9 языков и место комментария в дереве.

Что делает модуль (и почему именно так):

1. Привязка комментария к коду (нужна метрике overlap):
   - inline: перед комментарием на той же строке есть код -> код этой строки.
     Это то же самое, что сравнить строку начала комментария со строкой конца prev_sibling,
     но не ломается на комментариях внутри многострочных выражений.
   - leading: комментарий стоит на строке сразу над узлом-сиблингом -> код этого узла.
   - detached: между комментарием и кодом есть пустая строка или кода нет.
     Кода-цели нет, поэтому overlap для него не считается, но в распределение
     типов такой комментарий входит.
   - подряд идущие строчные комментарии склеиваются в один блок, если их строки
     отличаются ровно на 1.

2. Атрибуция коммиту. Файл на ревизии содержит и комментарии, написанные людьми
   за годы до коммита. Чтобы они не попали в AI-корпус, комментарий засчитывается
   только если ВСЕ его строки добавлены этим коммитом (added_ranges из manifest).
   Частично затронутый блок отбрасывается, а не «округляется»: любое округление
   тихо подмешивает чужой текст.

3. Окно кода. Контекст ограничен CODE_WINDOW строками цели. Без ограничения
   overlap зависел бы от размера функции: в 300 строках любое слово находится.

4. Не-язык. Лицензии, директивы линтеров и закомментированный код помечаются и
   исключаются из лингвистического анализа: это не высказывания автора о коде.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import inspect
import json
import re
import sys
from collections import Counter
from pathlib import Path

from config import EXT_TO_LANG, setup_console

CODE_WINDOW = 12
COMMENT_TYPES = {"comment", "line_comment", "block_comment"}

_DROP = -1  # пометка «строку из окна кода убрать целиком»

# --- распознавание не-языка --------------------------------------------------

_DIRECTIVE_RE = re.compile(
    r"""^(?:
        !/                                    # shebang ('#' уже срезан при очистке)
      | -\*-.*-\*-                            # -*- coding -*-
      | (?:type|noqa|pylint|flake8|isort|fmt|mypy|pragma|ruff|pyright|nosec|pytype)\b\s*[:\s]
      | (?:region|endregion)\b
      | eslint[- ](?:disable|enable)
      | @ts-(?:ignore|expect-error|nocheck|check)
      | prettier-ignore
      | istanbul\s+ignore
      | @(?:flow|jsx|license|preserve|vitest-environment|jest-environment)\b
      | go:(?:build|generate|embed|noinline)
      | \+build
      | nolint
      | lint:
      | NOLINT
      | clang-format\s+(?:on|off)
      | noinspection
      | NOPMD|CHECKSTYLE|NOSONAR
      | <reference\s
    )""",
    re.VERBOSE | re.IGNORECASE,
)
_LICENSE_STRONG = re.compile(r"SPDX-License-Identifier|copyright\s*(?:\(c\)|©|\d{4})", re.IGNORECASE)
_LICENSE_WEAK = re.compile(
    r"licen[cs]e|all rights reserved|permission is hereby granted|without warranty|apache license|mit license|gnu (?:general )?public",
    re.IGNORECASE,
)

_CODEY_LINE = [
    re.compile(r"[;{}]\s*$"),
    re.compile(r"^[\w.\[\]\"']+\s*(?:[-+*/|&]?=)(?!=)\s*\S"),
    re.compile(r"^[\w.]+\([^)]*\)\s*;?$"),
    re.compile(r"^(?:if|for|while|def|class|return|import|from|elif|else|try|except|catch|switch|case|"
               r"var|let|const|function|fmt\.|console\.|print|self\.)\b.*[:{(;]\s*$"),
    re.compile(r"^(?:return|break|continue|pass|raise|throw|else|try|finally)\s*;?$"),
]


def looks_like_code(text: str) -> bool:
    """Закомментированный код, а не высказывание.

    Эвристика по строкам: строка «кодоподобна», если заканчивается на ; { },
    похожа на присваивание/вызов или начинается с ключевого слова и оканчивается
    на : { ( ;. Блок считается кодом, если таких строк не меньше 60%.
    Ошибка в обе стороны возможна; её масштаб оценивается на ручной выборке (validate.py)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    hits = sum(1 for ln in lines if any(p.search(ln) for p in _CODEY_LINE))
    return hits / len(lines) >= 0.6


def exclusion_reason(text: str, start_row: int) -> str | None:
    if not re.search(r"[A-Za-z0-9]", text):
        return "empty"  # разделители вида ######## или // ------
    first = text.lstrip()
    if _DIRECTIVE_RE.match(first):
        return "directive"
    if _LICENSE_STRONG.search(text) or (start_row < 40 and _LICENSE_WEAK.search(text)):
        return "license"
    if looks_like_code(text):
        return "code"
    return None


# --- разбор ------------------------------------------------------------------

_parsers: dict = {}


def get_parser(lang: str):
    if lang not in _parsers:
        from tree_sitter_language_pack import get_parser as _gp
        _parsers[lang] = _gp(lang)
    return _parsers[lang]


def language_for(path: str, src: bytes | None = None) -> str | None:
    ext = Path(path).suffix
    lang = EXT_TO_LANG.get(ext)
    if ext == ".h" and src is not None and re.search(rb"\b(?:class|namespace|template)\b|\bstd::", src):
        return "cpp"  # заголовок C++ иначе разберётся с ошибками
    return lang


def _walk_comments(root) -> list:
    """Комментарии в порядке документа. Внутрь комментариев не захожу."""
    out, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type in COMMENT_TYPES:
            out.append(node)
            continue
        stack.extend(reversed(node.children))
    return out


class _Geo:
    """Строка и колонка узла - из БАЙТОВЫХ смещений, а не из node.start_point / node.end_point.

    Причина не косметическая. В tree-sitter 0.26.0 (Python 3.13, Windows) цепочка
    `node.start_point.row` на временном объекте Point портит память: процесс падает с access
    violation где-нибудь позже (у меня - в re.compile), причём только на реальных файлах в сотни
    узлов; на маленьких тестовых примерах баг не проявляется. start_byte/end_byte - простые
    целые числа и безопасны, а таблица начал строк даёт те же значения, что и Point
    (колонка в байтах, как и в tree-sitter)."""

    def __init__(self, src: bytes):
        newline = 10   # код байта перевода строки; так нет литерала с обратной косой чертой
        starts, pos = [0], src.find(bytes([newline]))
        while pos != -1:
            starts.append(pos + 1)
            pos = src.find(bytes([newline]), pos + 1)
        self.starts = starts

    def row(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset) - 1

    def srow(self, node) -> int:
        return self.row(node.start_byte)

    def scol(self, node) -> int:
        return node.start_byte - self.starts[self.row(node.start_byte)]

    def erow(self, node) -> int:
        """Последняя строка узла. Строчные комментарии некоторых грамматик (Rust) включают
        перевод строки, и конец узла приходится на колонку 0 СЛЕДУЮЩЕЙ строки - без поправки
        комментарий `/// doc` не находил бы код, стоящий сразу под ним."""
        r = self.row(node.end_byte)
        return r - 1 if node.end_byte == self.starts[r] and r > self.srow(node) else r


def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_STRING_RE = re.compile(r"""^[rRuUbBfF]{0,2}(\"\"\"|'''|\"|')(.*)\1$""", re.DOTALL)


def _python_docstrings(root, src: bytes) -> list[tuple]:
    """(узел_строки, владелец) для module/class/function: первая инструкция-строка тела."""
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("module", "class_definition", "function_definition"):
            body = node if node.type == "module" else node.child_by_field_name("body")
            if body is not None:
                for child in body.named_children:
                    if child.type == "comment":
                        continue
                    # В новых версиях грамматики докстринг - голый `string` без обёртки
                    # expression_statement, в старых - обёрнут. Понимаю обе формы.
                    if child.type == "string":
                        found.append((child, node))
                    elif child.type == "expression_statement" and child.named_child_count == 1 \
                            and child.named_children[0].type == "string":
                        found.append((child.named_children[0], node))
                    break  # docstring - только самая первая инструкция
        stack.extend(node.children)
    return found


def _clean_line_comments(raws: list[str]) -> str:
    out = []
    for raw in raws:
        raw = raw.rstrip("\r\n")
        out.append(re.sub(r"^(?:#+|/{2,}!?|;+)[ \t]?", "", raw).rstrip())
    return "\n".join(out).strip()


def _clean_block_comment(raw: str) -> str:
    body = raw[2:]
    if body.endswith("*/"):
        body = body[:-2]
    body = body.lstrip("*!")
    lines = []
    for ln in body.splitlines():
        ln = re.sub(r"^\s*\*+[ \t]?", "", ln).strip()
        lines.append(ln)
    return "\n".join(lines).strip()


def _doc_marker(raw: str) -> bool:
    """/** */, /*! */, ///, //! - комментарии-документация. //// (4+ слэша) - обычный разделитель."""
    return bool(re.match(r"/\*[*!](?!/)", raw) or re.match(r"//[/!](?!/)", raw))


# --- атрибуция ---------------------------------------------------------------

def _merge_ranges(ranges) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _attribution(ranges: list[list[int]] | None, start: int, end: int) -> str:
    """'all' - все строки добавлены коммитом, 'mixed' - часть, 'none' - ни одной."""
    if ranges is None:
        return "all"
    i = bisect.bisect_right([r[0] for r in ranges], start) - 1
    if i >= 0 and ranges[i][0] <= start and end <= ranges[i][1]:
        return "all"
    for s, e in ranges:
        if s <= end and start <= e:
            return "mixed"
    return "none"


# --- ядро --------------------------------------------------------------------

def _find_target(last, end_row: int, geo: _Geo):
    """Узел кода, к которому относится комментарий-над-кодом, или None."""
    nxt = last.next_sibling
    row = end_row
    while nxt is not None and nxt.type in COMMENT_TYPES and geo.srow(nxt) == row + 1:
        row = geo.erow(nxt)
        nxt = nxt.next_sibling
    if nxt is None or not nxt.is_named or nxt.type in COMMENT_TYPES:
        return None
    if geo.srow(nxt) != row + 1:
        return None
    return nxt


def extract_from_source(src: bytes, lang: str, added: list | None = None,
                        code_window: int = CODE_WINDOW) -> tuple[list[dict], dict]:
    """Все наблюдения одного файла. Чистая функция: её же использует чекер.

    added - [[start, end], ...] (1-based, включительно) или None («все строки»).
    Возвращает (наблюдения, статистика файла).
    """
    parser = get_parser(lang)
    tree = parser.parse(src)
    root = tree.root_node
    lines = src.split(b"\n")
    geo = _Geo(src)
    ranges = _merge_ranges(added) if added is not None else None

    stats = Counter()
    stats["parse_error"] = int(root.has_error)

    # Знаменатель плотности комментариев: сколько строк коммит добавил в этом файле (а при --all-comments -
    # сколько строк в файле). Хвостовой перевод строки не создаёт лишней пустой строки.
    n_lines = len(lines) - 1 if lines and lines[-1] == b"" else len(lines)
    spans = [(1, n_lines)] if ranges is None else [(max(1, s0), min(e0, n_lines)) for s0, e0 in ranges]
    spans = [(s0, e0) for s0, e0 in spans if e0 >= s0]
    stats["added_lines"] = sum(e0 - s0 + 1 for s0, e0 in spans)
    stats["added_nonblank"] = sum(1 for s0, e0 in spans for r0 in range(s0 - 1, e0) if lines[r0].strip())

    comments = _walk_comments(root)
    docstrings = _python_docstrings(root, src) if lang == "python" else []

    # Что убрать из окна кода: комментарии и докстринги - это не «код», и слова из них
    # нельзя засчитывать как пересечение с кодом.
    cut: dict[int, int] = {}
    for node in comments + [d[0] for d in docstrings]:
        srow, scol = geo.srow(node), geo.scol(node)
        for r in range(srow, geo.erow(node) + 1):
            if r == srow and lines[r][:scol].strip():
                cut[r] = scol
            else:
                cut[r] = _DROP

    def window(start_row: int, end_row: int) -> str:
        last = min(end_row, start_row + code_window - 1)
        chunk = []
        for r in range(start_row, last + 1):
            ln = lines[r] if r < len(lines) else b""
            c = cut.get(r)
            if c == _DROP:
                continue
            if c is not None:
                ln = ln[:c]
            chunk.append(ln.decode("utf-8", errors="replace").rstrip("\r"))
        return "\n".join(chunk).strip()

    records: list[dict] = []

    def emit(kind, form, start_row, end_row, text, code, target_type, attached):
        stats["raw_" + kind] += 1
        got = _attribution(ranges, start_row + 1, end_row + 1)
        if got != "all":
            stats["dropped_" + ("mixed" if got == "mixed" else "not_added")] += 1
            return
        records.append({
            "kind": kind, "form": form,
            "start_line": start_row + 1, "end_line": end_row + 1,
            "text": text, "code": code or None,
            "target_type": target_type, "attached": attached,
            "exclude": exclusion_reason(text, start_row),
        })

    # --- обычные комментарии: сначала группируем -------------------------------
    infos = []
    for node in comments:
        raw = _text(src, node)
        row, col = geo.srow(node), geo.scol(node)
        infos.append({
            "node": node, "raw": raw,
            "own_line": not lines[row][:col].strip(),
            "block": raw.startswith("/*"),
            "doc": _doc_marker(raw),
            "shebang": raw.startswith("#!"),
        })

    groups: list[list[dict]] = []
    for info in infos:
        prev = groups[-1][-1] if groups else None
        if (prev is not None and info["own_line"] and prev["own_line"]
                and not info["block"] and not prev["block"]
                and info["doc"] == prev["doc"]
                and not info["shebang"] and not prev["shebang"]
                and geo.srow(info["node"]) == geo.erow(prev["node"]) + 1):
            groups[-1].append(info)
        else:
            groups.append([info])

    for g in groups:
        first, last = g[0], g[-1]
        s_row, e_row = geo.srow(first["node"]), geo.erow(last["node"])
        if first["block"]:
            text, form = _clean_block_comment(first["raw"]), "block"
        else:
            text, form = _clean_line_comments([i["raw"] for i in g]), "line"

        if not first["own_line"]:
            prefix = lines[s_row][:geo.scol(first["node"])].decode("utf-8", errors="replace").strip()
            emit("inline", form, s_row, e_row, text, prefix, None, True)
            continue

        target = _find_target(last["node"], e_row, geo)
        code = window(geo.srow(target), geo.erow(target)) if target is not None else None
        ttype = target.type if target is not None else None

        is_go_doc = (lang == "go" and target is not None and target.parent is not None
                     and target.parent.type == "source_file" and target.type.endswith("_declaration"))
        if first["doc"] or is_go_doc:
            kind = "doc"
        elif first["block"]:
            kind = "block"
        elif target is not None:
            kind = "leading"
        else:
            kind = "detached"
        emit(kind, form, s_row, e_row, text, code, ttype, target is not None)

    # --- докстринги Python -----------------------------------------------------
    for node, owner in docstrings:
        m = _STRING_RE.match(_text(src, node).strip())
        raw_body = m.group(2) if m else _text(src, node)
        text = inspect.cleandoc(raw_body)
        if owner.type == "module":
            code, attached = None, False  # докстринг модуля описывает весь файл, а не соседний код
        else:
            code, attached = window(geo.srow(owner), geo.erow(owner)), True
        emit("doc", "docstring", geo.srow(node), geo.erow(node), text, code, owner.type, attached)

    records.sort(key=lambda r: r["start_line"])
    return records, dict(stats)


# --- запуск по манифесту -------------------------------------------------------

def _obs_id(rec: dict) -> str:
    key = f'{rec["repo"]}|{rec["sha"]}|{rec["file"]}|{rec["start_line"]}'
    return hashlib.blake2b(key.encode(), digest_size=6).hexdigest()


def run(corpus: Path, out: Path | None, all_comments: bool, code_window: int, limit: int | None = None) -> dict:
    manifest = corpus / "manifest.jsonl"
    if not manifest.exists():
        raise SystemExit(f"Нет {manifest}. Сначала соберите корпус: python parser.py ...")
    out = out or corpus / "comments.jsonl"

    total = Counter()
    by_source = {"ai": Counter(), "human": Counter()}
    missing_ranges = 0

    # file_stats.jsonl - по строке на файл: сколько строк добавлено и сколько комментариев в них.
    # Плотность комментариев считается отсюда, а не из comments.jsonl: в comments.jsonl файл без
    # единого комментария вообще не виден, а именно такие файлы и определяют знаменатель.
    fs_path = out.with_name("file_stats.jsonl")

    with open(manifest, encoding="utf-8") as mf, open(out, "w", encoding="utf-8") as of, \
            open(fs_path, "w", encoding="utf-8") as ff:
        for n, line in enumerate(mf):
            if limit is not None and n >= limit:
                break
            if not line.strip():
                continue
            entry = json.loads(line)
            path = Path(entry["local_path"])
            if not path.is_absolute():
                path = corpus / path
            if not path.exists():
                total["files_missing"] += 1
                continue
            src = path.read_bytes()
            lang = language_for(entry["file"], src)
            if lang is None:
                total["files_unsupported"] += 1
                continue

            added = None if all_comments else entry.get("added_ranges")
            if not all_comments and "added_ranges" not in entry:
                missing_ranges += 1  # манифест старого формата: атрибуцию сделать нечем

            try:
                recs, st = extract_from_source(src, lang, added, code_window)
            except Exception as exc:  # один битый файл не должен ронять прогон на 100k файлов
                total["files_failed"] += 1
                print(f"  ! {entry['repo']}:{entry['file']}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue

            total["files"] += 1
            src_name = entry.get("source", "unknown")
            by_source.setdefault(src_name, Counter())["files"] += 1
            for k, v in st.items():
                total[k] += v
                by_source[src_name][k] += v
            for r in recs:
                r.update(source=src_name, tool=entry.get("tool") or None, repo=entry["repo"],
                         sha=entry["sha"], file=entry["file"], lang=lang,
                         commit_date=entry.get("commit_date"))
                r["id"] = _obs_id(r)
                of.write(json.dumps(r, ensure_ascii=False) + "\n")
                total["comments"] += 1
                by_source[src_name]["comments"] += 1
                by_source[src_name]["kind_" + r["kind"]] += 1
                if r["exclude"]:
                    by_source[src_name]["excluded_" + r["exclude"]] += 1

            # В числитель плотности идут комментарии, которые автор написал словами: без лицензий, директив
            # линтеров, закомментированного кода и разделителей. Язык (английский или нет) не фильтруется:
            # плотность - это поведение автора, а не свойство английского текста.
            authored = [r for r in recs if not r["exclude"]]
            ff.write(json.dumps({
                "source": src_name, "tool": entry.get("tool") or None, "repo": entry["repo"], "sha": entry["sha"],
                "file": entry["file"], "lang": lang,
                "added_lines": st["added_lines"], "added_nonblank": st["added_nonblank"],
                "comments": len(authored),
                "comment_lines": sum(r["end_line"] - r["start_line"] + 1 for r in authored),
                "by_kind": dict(Counter(r["kind"] for r in authored)),
            }, ensure_ascii=False) + "\n")

            if total["files"] % 500 == 0:
                print(f"  обработано файлов: {total['files']}, комментариев: {total['comments']}")

    if missing_ranges:
        print(f"Внимание: у {missing_ranges} записей манифеста нет added_ranges - "
              f"взяты ВСЕ комментарии файла. Пересоберите корпус свежим parser.py.", file=sys.stderr)

    stats = {"total": dict(total), "by_source": {k: dict(v) for k, v in by_source.items()},
             "all_comments": all_comments, "code_window": code_window}
    (corpus / "extract_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Извлечение комментариев из корпуса (tree-sitter)")
    ap.add_argument("--corpus", default="./corpus", help="каталог с manifest.jsonl (по умолчанию ./corpus)")
    ap.add_argument("--out", default=None, help="куда писать comments.jsonl (по умолчанию <corpus>/comments.jsonl)")
    ap.add_argument("--all-comments", action="store_true",
                    help="брать все комментарии файла, а не только добавленные коммитом (для сравнения; методологически слабее)")
    ap.add_argument("--code-window", type=int, default=CODE_WINDOW, help="сколько строк кода-цели учитывать (по умолчанию 12)")
    ap.add_argument("--limit", type=int, default=None, help="обработать только первые N записей манифеста (отладка)")
    args = ap.parse_args(argv)

    stats = run(Path(args.corpus), Path(args.out) if args.out else None,
                args.all_comments, args.code_window, args.limit)
    t = stats["total"]
    print(f"Готово. Файлов: {t.get('files', 0)}, комментариев: {t.get('comments', 0)}. "
          f"Отброшено (не добавлены коммитом): {t.get('dropped_not_added', 0)}, "
          f"смешанные: {t.get('dropped_mixed', 0)}. Статистика: {Path(args.corpus) / 'extract_stats.json'}")


if __name__ == "__main__":
    main()
