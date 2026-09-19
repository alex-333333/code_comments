"""Тесты parser.py на локальных git-репозиториях: сеть и токен не нужны."""

import json
from pathlib import Path

import pytest

import parser as cp
from demo import GitRepo

OLD = "2019-03-01T12:00:00"
NEW = "2025-06-01T12:00:00"


def run_parser(tmp_path: Path, repos: list[Path], source: str, extra: list[str] | None = None) -> list[dict]:
    lst = tmp_path / f"{source}.txt"
    lst.write_text("\n".join(str(r) for r in repos), encoding="utf-8")
    out = tmp_path / "corpus"
    cp.main(["--repos", str(lst), "--source", source, "--outdir", str(out), *(extra or [])])
    mf = out / "manifest.jsonl"
    return [json.loads(x) for x in mf.read_text(encoding="utf-8").splitlines()] if mf.exists() else []


# --- разбор патча ---------------------------------------------------------------

def test_parse_added_ranges_counts_lines_not_prefixes():
    # Добавленная строка «++ x» в патче выглядит как «+++ x» - неотличима от заголовка файла.
    patch = "\n".join([
        "diff --git a/f.py b/f.py",
        "--- a/f.py",
        "+++ b/f.py",
        "@@ -3,0 +4,2 @@ ctx",
        "+++ looks like a header",
        "+second",
        "@@ -10 +12 @@",
        "-old",
        "+new",
        "diff --git a/g.py b/g.py",
        "--- a/g.py",
        "+++ b/g.py",
        "@@ -0,0 +1,3 @@",
        "+a", "+b", "+c",
    ])
    got = cp.parse_added_ranges(patch)
    assert got == {"f.py": [[4, 5], [12, 12]], "g.py": [[1, 3]]}


def test_parse_added_ranges_pure_deletion_gives_no_range():
    patch = "+++ b/f.py\n@@ -5,2 +4,0 @@\n-a\n-b\n"
    assert cp.parse_added_ranges(patch) == {"f.py": []}


def test_parse_entry_formats(tmp_path):
    assert cp.parse_entry("psf/requests") == ("psf", "requests", "https://github.com/psf/requests.git")
    assert cp.parse_entry("https://github.com/psf/requests.git")[:2] == ("psf", "requests")
    repo = GitRepo(tmp_path / "myrepo")
    assert cp.parse_entry(str(repo.path))[:2] == ("local", "myrepo")
    with pytest.raises(ValueError):
        cp.parse_entry("not a repo")


def test_sanitize_path_windows_safety():
    assert cp.sanitize_path("src/con.py") == "src/_con.py"          # зарезервированное имя Windows
    assert cp.sanitize_path("a b/c?d.py") == "a_b/c_d.py"
    assert cp.sanitize_path("dir./x.py") == "dir/x.py"              # точка в конце компонента


# --- критерии корпуса -------------------------------------------------------------

def make_mixed_repo(tmp_path) -> GitRepo:
    r = GitRepo(tmp_path / "mixed")
    r.commit({"old.py": "# human old\nx = 1\n"}, "human 2019", OLD)
    # трейлер 2019 года: не должен попасть в human-корпус (двойной критерий)
    r.commit({"faked.py": "# fake\ny = 1\n"}, "old but AI", OLD, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    r.commit({"c1.py": "# a\nz = 1\n"}, "claude", NEW, ["Co-Authored-By: Claude Opus <noreply@anthropic.com>"])  # заглавные!
    r.commit({"c2.py": "# a\nz = 2\n"}, "copilot", NEW, ["Co-authored-by: Copilot <x@users.noreply.github.com>"])
    r.commit({"c3.py": "# a\nz = 3\n"}, "cursor", NEW, ["Co-authored-by: Cursor Agent <cursoragent@cursor.com>"])
    r.commit({"h.py": "# no trailer new\nq = 1\n"}, "human but modern", NEW)
    return r


def test_ai_corpus_is_case_insensitive_and_tags_tools(tmp_path):
    repo = make_mixed_repo(tmp_path)
    rows = run_parser(tmp_path, [repo.path], "ai")
    by_file = {r["file"]: r for r in rows}
    assert set(by_file) == {"faked.py", "c1.py", "c2.py", "c3.py"}
    assert by_file["c1.py"]["tool"] == "claude"     # регистр 'Co-Authored-By' не помешал
    assert by_file["c2.py"]["tool"] == "copilot"
    assert by_file["c3.py"]["tool"] == "cursor"
    assert all(r["source"] == "ai" for r in rows)


def test_human_corpus_requires_old_date_and_no_trailer(tmp_path):
    repo = make_mixed_repo(tmp_path)
    rows = run_parser(tmp_path, [repo.path], "human")
    assert {r["file"] for r in rows} == {"old.py"}


# --- атрибуция ---------------------------------------------------------------------

def test_added_ranges_only_cover_lines_added_by_commit(tmp_path):
    r = GitRepo(tmp_path / "attr")
    r.commit({"m.py": "# very old human comment\ndef f():\n    return 1\n"}, "init", OLD)
    r.commit({"m.py": "# very old human comment\ndef f():\n    return 1\n\n# new AI comment\ndef g():\n    return 2\n"},
             "add g", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    rows = run_parser(tmp_path, [r.path], "ai")
    assert len(rows) == 1
    assert rows[0]["added_ranges"] == [[4, 7]]   # старые строки 1-3 в диапазон не входят


def test_pure_rename_does_not_attribute_old_content_to_ai_commit(tmp_path):
    r = GitRepo(tmp_path / "ren")
    r.commit({"old_name.py": "# human comment\ndef f():\n    return 1\n"}, "init", OLD)
    r.rename("old_name.py", "new_name.py", "rename", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    rows = run_parser(tmp_path, [r.path], "ai")
    assert rows == []    # без -M git показал бы файл целиком «добавленным»


def test_rename_with_edit_only_attributes_edited_lines(tmp_path):
    r = GitRepo(tmp_path / "ren2")
    body = "".join(f"# human line {i}\nx{i} = {i}\n" for i in range(10))
    r.commit({"old_name.py": body}, "init", OLD)
    r.rename("old_name.py", "new_name.py", "rename", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    r.commit({"new_name.py": body + "# added by ai\nnew = 1\n"}, "edit", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    rows = run_parser(tmp_path, [r.path], "ai")
    ranges = [row["added_ranges"] for row in rows if row["file"] == "new_name.py"]
    assert ranges == [[[21, 22]]]


# --- сохранность данных ----------------------------------------------------------------

def test_file_bytes_are_preserved_including_crlf(tmp_path):
    r = GitRepo(tmp_path / "crlf")
    content = b"# comment\r\nx = 1\r\n"
    r.commit({"w.py": content}, "crlf", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    rows = run_parser(tmp_path, [r.path], "ai")
    saved = (tmp_path / "corpus" / rows[0]["local_path"]).read_bytes()
    assert saved == content   # write_text на Windows превратил бы \r\n в \r\r\n


def test_second_run_is_idempotent(tmp_path):
    repo = make_mixed_repo(tmp_path)
    first = run_parser(tmp_path, [repo.path], "ai")
    second = run_parser(tmp_path, [repo.path], "ai")
    assert len(first) == len(second) > 0


def test_skips_vendored_and_irrelevant_files(tmp_path):
    r = GitRepo(tmp_path / "skip")
    r.commit({
        "node_modules/x/index.js": "// vendored\nvar a = 1;\n",
        "src/app.js": "// mine\nvar b = 1;\n",
        "README.md": "# not code\n",
        "bundle.min.js": "var c=1;\n",
    }, "mixed", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    rows = run_parser(tmp_path, [r.path], "ai")
    assert [x["file"] for x in rows] == ["src/app.js"]


def test_bulk_commits_are_skipped(tmp_path):
    r = GitRepo(tmp_path / "bulk")
    files = {f"f{i}.py": f"# c\nx = {i}\n" for i in range(5)}
    r.commit(files, "bulk", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    assert run_parser(tmp_path, [r.path], "ai", ["--max-files-per-commit", "3"]) == []


def test_per_repo_cap_and_determinism(tmp_path):
    r = GitRepo(tmp_path / "cap")
    for i in range(6):
        r.commit({f"f{i}.py": f"# c\nx = {i}\n"}, f"c{i}", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = run_parser(tmp_path / "a", [r.path], "ai", ["--max-files-per-repo", "3", "--seed", "1"])
    b = run_parser(tmp_path / "b", [r.path], "ai", ["--max-files-per-repo", "3", "--seed", "1"])
    assert len(a) == 3
    assert [x["sha"] for x in a] == [x["sha"] for x in b]   # тот же seed -> та же выборка


def test_budget_stops_run(tmp_path, capsys):
    r = GitRepo(tmp_path / "bud")
    for i in range(4):
        r.commit({f"f{i}.py": "# c\n" + "x = 1\n" * 200}, f"c{i}", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    rows = run_parser(tmp_path, [r.path], "ai", ["--gb", "0.000001"])   # ~1 KB
    assert 1 <= len(rows) < 4
    assert "Лимит" in capsys.readouterr().out


def test_failing_repo_does_not_stop_others(tmp_path):
    good = make_mixed_repo(tmp_path)
    lst = tmp_path / "ai.txt"
    lst.write_text(f"{tmp_path / 'nope'}\n{good.path}\n", encoding="utf-8")
    out = tmp_path / "corpus"
    (tmp_path / "nope").mkdir()   # существует, но не git-репозиторий
    cp.main(["--repos", str(lst), "--source", "ai", "--outdir", str(out)])
    assert (out / "errors.log").exists()
    assert len((out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()) == 4


def test_suspects_are_logged(tmp_path):
    r = GitRepo(tmp_path / "sus")
    r.commit({"a.py": "x = 1\n"}, "tidy", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    r.commit({"b.py": "y = 1\n"}, "refactor", NEW, ["🤖 Generated with [Claude Code](https://claude.com/claude-code)"])
    run_parser(tmp_path, [r.path], "ai")
    log = (tmp_path / "corpus" / "suspects.log").read_text(encoding="utf-8")
    assert "refactor" in log and "tidy" not in log


def test_interrupted_clone_is_not_mistaken_for_complete(tmp_path):
    """Убитый посреди клонирования процесс оставляет каталог. Он не должен считаться готовым клоном."""
    r = GitRepo(tmp_path / "src")
    r.commit({"a.py": "# c\nx = 1\n"}, "c", NEW, ["Co-authored-by: Claude <noreply@anthropic.com>"])
    out = tmp_path / "corpus"
    stale = out / "_clones" / f"local__{r.path.name}.part"       # остаток прерванного клонирования
    stale.mkdir(parents=True)
    (stale / "garbage").write_text("x")
    rows = run_parser(tmp_path, [r.path], "ai")
    assert len(rows) == 1                                          # старый мусор не помешал
    assert not stale.exists()                                      # и убран
    assert (out / "_clones" / f"local__{r.path.name}").exists()
