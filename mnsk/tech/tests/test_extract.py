"""Тесты extract_comments: привязка, атрибуция, исключения. Каждый тест - одно правило методологии."""

import pytest

from extract_comments import extract_from_source, language_for, looks_like_code

PY = b'''#!/usr/bin/env python3
# Copyright (c) 2019 Someone
"""Module docstring."""
import os

# Retry because the server drops idle connections
# after thirty seconds of silence.
def connect(host, retries=3):
    """Connect to the host and return a socket."""
    # Loop through the retries
    for i in range(retries):
        sock = open_socket(host)  # open the socket
        if sock:
            return sock

    # detached note

# x = compute(y);
# print(x)
class Foo:
    pass
'''


def by_line(recs):
    return {r["start_line"]: r for r in recs}


def test_python_kinds_and_targets():
    recs, _ = extract_from_source(PY, "python")
    r = by_line(recs)
    assert r[6]["kind"] == "leading" and r[6]["end_line"] == 7          # две строки склеены в один комментарий
    assert r[6]["target_type"] == "function_definition"
    assert r[10]["kind"] == "leading" and r[10]["target_type"] == "for_statement"
    assert r[12]["kind"] == "inline" and r[12]["code"] == "sock = open_socket(host)"
    assert r[16]["kind"] == "detached" and r[16]["code"] is None         # кода-цели нет
    assert r[9]["kind"] == "doc" and r[9]["form"] == "docstring"
    assert r[3]["kind"] == "doc" and r[3]["attached"] is False           # докстринг модуля не привязан к соседнему коду


def test_merged_block_text_is_clean():
    recs, _ = extract_from_source(PY, "python")
    assert by_line(recs)[6]["text"] == "Retry because the server drops idle connections\nafter thirty seconds of silence."


def test_shebang_does_not_swallow_next_comment():
    recs, _ = extract_from_source(PY, "python")
    r = by_line(recs)
    assert r[1]["exclude"] == "directive"
    assert r[2]["exclude"] == "license"
    assert r[6]["exclude"] is None


def test_commented_out_code_is_flagged():
    recs, _ = extract_from_source(PY, "python")
    assert by_line(recs)[18]["exclude"] == "code"


def test_code_window_excludes_comments_and_docstrings():
    recs, _ = extract_from_source(PY, "python")
    code = by_line(recs)[6]["code"]
    assert "def connect" in code
    # слова из докстринга и вложенных комментариев - не код: иначе они давали бы ложный overlap
    assert "Connect to the host" not in code and "Loop through" not in code and "open the socket" not in code


def test_docstring_code_is_definition_without_docstring():
    recs, _ = extract_from_source(PY, "python")
    code = by_line(recs)[9]["code"]
    assert code.startswith("def connect") and "Connect to the host" not in code


def test_attribution_only_fully_added_comments():
    # Добавлены строки 8-9 (def + docstring) и 14: старый блок 6-7 в диапазон не входит.
    recs, st = extract_from_source(PY, "python", added=[[8, 9], [10, 10]])
    lines = {r["start_line"] for r in recs}
    assert lines == {9, 10}
    assert st["dropped_not_added"] > 0


def test_partially_added_block_is_dropped_as_mixed():
    recs, st = extract_from_source(PY, "python", added=[[7, 7]])   # только вторая строка двустрочного комментария
    assert 6 not in {r["start_line"] for r in recs}
    assert st["dropped_mixed"] == 1


def test_none_added_means_all_lines():
    recs_all, _ = extract_from_source(PY, "python", added=None)
    recs_full, _ = extract_from_source(PY, "python", added=[[1, 1000]])
    assert len(recs_all) == len(recs_full)


JS = b'''// Fetch users
function getUsers() {
  return db.query(); // query db
}
/**
 * Returns the list of users.
 * @returns {User[]}
 */
export function list() {}
/* block one
   two */
const x = 1;
'''


def test_js_doc_block_inline():
    r = by_line(extract_from_source(JS, "javascript")[0])
    assert r[1]["kind"] == "leading" and r[1]["target_type"] == "function_declaration"
    assert r[3]["kind"] == "inline" and r[3]["code"] == "return db.query();"
    assert r[5]["kind"] == "doc" and r[5]["text"].startswith("Returns the list of users.")
    assert r[10]["kind"] == "block" and r[10]["text"] == "block one\ntwo"


def test_go_godoc_convention():
    go = b"package main\n\n// Add returns the sum.\nfunc Add(a, b int) int { return a + b }\n\nfunc main() {\n\tx := 1 // one\n}\n"
    r = by_line(extract_from_source(go, "go")[0])
    assert r[3]["kind"] == "doc"
    assert r[7]["kind"] == "inline"


def test_rust_doc_comment_finds_its_target():
    # Строчный комментарий Rust включает перевод строки: без поправки цель не находилась.
    rs = b"/// Adds two numbers.\nfn add(a: i32, b: i32) -> i32 { a + b }\n// plain\nfn sub() {}\n"
    r = by_line(extract_from_source(rs, "rust")[0])
    assert r[1]["kind"] == "doc" and r[1]["target_type"] == "function_item"
    assert r[3]["kind"] == "leading" and r[3]["target_type"] == "function_item"


def test_java_line_and_block_comment_types():
    java = b"class A {\n  // adds\n  int add(int a, int b) { return a + b; }\n  /* multi\n line */\n  void f() {}\n}\n"
    r = by_line(extract_from_source(java, "java")[0])
    assert r[2]["kind"] == "leading"
    assert r[4]["kind"] == "block"


def test_non_ascii_source_keeps_columns_correct():
    # tree-sitter считает колонки в БАЙТАХ; кириллица в строке кода не должна ломать срез.
    src = 'x = "привет мир"  # greeting text\n'.encode("utf-8")
    recs, _ = extract_from_source(src, "python")
    assert recs[0]["kind"] == "inline" and recs[0]["text"] == "greeting text"
    assert recs[0]["code"] == 'x = "привет мир"'


def test_crlf_source():
    src = b"# first\r\n# second\r\nx = 1\r\n"
    recs, _ = extract_from_source(src, "python")
    assert recs[0]["text"] == "first\nsecond" and recs[0]["kind"] == "leading"


def test_divider_lines_are_empty():
    recs, _ = extract_from_source(b"# ----------\nx = 1\n", "python")
    assert recs[0]["exclude"] == "empty"


@pytest.mark.parametrize("text,expected", [
    ("x = compute(y);", True),
    ("for i in range(10):\n    total += i", True),    # Python-код без ; и { } тоже ловится: ключевое слово + двоеточие, присваивание
    ("Retry because the server drops connections", False),
    ("if (a > b) {\n  return a;\n}", True),
    ("Check whether x = 5 is valid", False),
])
def test_looks_like_code(text, expected):
    assert looks_like_code(text) is expected


def test_h_header_with_cpp_content_uses_cpp_grammar():
    assert language_for("a.h", b"class Foo {};") == "cpp"
    assert language_for("a.h", b"int x;") == "c"
    assert language_for("a.txt") is None


def test_parse_error_is_reported_not_fatal():
    recs, st = extract_from_source(b"# note\ndef broken(:\n", "python")
    assert st["parse_error"] == 1
    assert isinstance(recs, list)


def test_large_realistic_file_does_not_crash_native_bindings():
    """Регрессия: в tree-sitter 0.26.0 `node.start_point.row` на временном Point портил память,
    и процесс падал с access violation ТОЛЬКО на реальных файлах в сотни узлов (на маленьких
    примерах - никогда). Поэтому большой файл разбирается в подпроцессе: segfault не должен
    ронять сам pytest, а ненулевой код возврата сразу выдаст проблему."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    nl = chr(10)
    parts = ['"' * 3 + "Module doc." + '"' * 3]
    for i in range(400):
        parts.append(nl.join([
            f"# Helper number {i} does the work",
            f"def fn_{i}(a, b):",
            "    " + '"' * 3 + f"Docstring of function {i}." + '"' * 3,
            f"    # step one because reasons {i}",
            f"    x = a + b  # add {i}",
            "    return x",
        ]))
    src = (nl * 2).join(parts).encode()
    script = nl.join([
        "import re, sys",
        f"sys.path.insert(0, {str(root)!r})",
        "from extract_comments import extract_from_source",
        "src = sys.stdin.buffer.read()",
        "for _ in range(3):",
        "    recs, st = extract_from_source(src, 'python')",
        # компиляция регулярок ПОСЛЕ разбора - именно там проявлялась порча памяти
        "for i in range(500):",
        "    re.compile('probe%d(?:#+|/{2,}!?|;+)' % i)",
        "print('OK', len(recs))",
    ])
    res = subprocess.run([sys.executable, "-c", script], input=src, capture_output=True)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")[-500:]
    assert res.stdout.decode().startswith("OK") and int(res.stdout.decode().split()[1]) >= 1600


def test_added_lines_counters_for_density_denominator():
    src = b"# a\nx = 1\n\ny = 2  # b\n"
    _, st = extract_from_source(src, "python", [[1, 4]])
    assert st["added_lines"] == 4 and st["added_nonblank"] == 3          # пустая строка входит в строки, но не в непустые
    _, st = extract_from_source(src, "python", [[2, 3]])
    assert st["added_lines"] == 2 and st["added_nonblank"] == 1
    _, st = extract_from_source(src, "python", None)                     # --all-comments: знаменатель - весь файл
    assert st["added_lines"] == 4                                        # хвостовой \n не создаёт пятую строку


def test_added_lines_clipped_to_file_length():
    _, st = extract_from_source(b"x = 1\n", "python", [[1, 50]])
    assert st["added_lines"] == 1
