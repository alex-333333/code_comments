"""Тесты чекера и установщика хука на временном git-репозитории."""

import json
import subprocess
import sys

import pytest

import checker
import install_hook
from demo import GitRepo

GOOD = '''def total_size(orders):
    total = 0
    for order in orders:
        # Add the order size to the total
        total += order.size
    # Retry because the billing API drops idle connections after thirty seconds
    return total
'''

OLD = "# Loop through the orders and add each size to the total\ndef old(orders):\n    return sum(o.size for o in orders)\n"


def staged_repo(tmp_path, staged_body=GOOD):
    r = GitRepo(tmp_path / "r")
    r.commit({"old.py": OLD}, "old", "2019-01-01T10:00:00")
    (r.path / "new.py").write_text(staged_body, encoding="utf-8")
    r._git("add", "new.py")
    return r


def test_text_mode_verdicts(capsys):
    assert checker.main(["--text", "Loop over each order in the list", "--code", "for order in orders_list:\n    pass"]) == 0
    assert "ПЕРЕСКАЗ" in capsys.readouterr().out
    checker.main(["--text", "Retry because the vendor API drops idle connections", "--code", "for i in range(3):\n    call()"])
    assert "ОБЪЯСНЯЕТ" in capsys.readouterr().out


def test_text_mode_reports_undefined_overlap(capsys):
    checker.main(["--text", "ok", "--code", "x = 1"])
    assert "не определён" in capsys.readouterr().out


def test_staged_flags_only_restating_comments(tmp_path, capsys):
    r = staged_repo(tmp_path)
    code = checker.main(["--staged", "--repo", str(r.path), "--json"])
    data = json.loads(capsys.readouterr().out)
    texts = [f["text"] for f in data["flags"]]
    assert code == 0                                     # по умолчанию хук только предупреждает
    assert any("Add the order size" in t for t in texts)
    assert not any("Retry because" in t for t in texts)  # объясняющий комментарий не помечается
    assert data["files"] == 1


def test_only_added_lines_are_checked(tmp_path, capsys):
    r = staged_repo(tmp_path)
    # правим старый файл: добавляем одну строку кода, старый комментарий-пересказ не должен попасть в находки
    (r.path / "old.py").write_text(OLD + "\nx = 1\n", encoding="utf-8")
    r._git("add", "old.py")
    checker.main(["--staged", "--repo", str(r.path), "--json"])
    flags = json.loads(capsys.readouterr().out)["flags"]
    assert all(f["file"] != "old.py" for f in flags)


def test_strict_mode_exit_code(tmp_path, capsys):
    r = staged_repo(tmp_path)
    assert checker.main(["--staged", "--repo", str(r.path), "--strict"]) == 1
    clean = tmp_path / "c"
    r2 = staged_repo(clean, "def f(x):\n    # Retry because the API is flaky under load\n    return x\n")
    assert checker.main(["--staged", "--repo", str(r2.path), "--strict"]) == 0


def test_checker_error_does_not_block_commit(tmp_path, capsys):
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    assert checker.main(["--staged", "--repo", str(not_repo), "--strict"]) == 0
    assert "пропущено" in capsys.readouterr().err


def test_file_mode(tmp_path, capsys):
    p = tmp_path / "a.py"
    p.write_text(GOOD, encoding="utf-8")
    checker.main(["--file", str(p), "--json"])
    assert json.loads(capsys.readouterr().out)["flags"]


def test_hook_install_blocks_and_uninstall(tmp_path):
    r = staged_repo(tmp_path)
    path = install_hook.install(r.path, strict=True)
    assert path.exists() and install_hook.MARKER in path.read_text(encoding="utf-8")
    assert "\r" not in path.read_text(encoding="utf-8")                 # CRLF ломает sh
    res = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.st", "commit", "-m", "x"],
                         cwd=r.path, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode != 0, res.stdout + res.stderr                 # strict-хук остановил коммит
    res2 = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.st", "commit", "--no-verify", "-m", "x"],
                          cwd=r.path, capture_output=True, text=True, encoding="utf-8")
    assert res2.returncode == 0                                         # штатный обход работает
    assert "Хук удалён" in install_hook.uninstall(r.path)
    assert not path.exists()


def test_hook_preserves_existing_hook(tmp_path):
    r = GitRepo(tmp_path / "keep")
    hooks = install_hook.hooks_dir(r.path)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho custom-hook-ran\nexit 0\n", encoding="utf-8", newline="\n")
    install_hook.install(r.path)
    assert (hooks / install_hook.BACKUP).exists()
    assert install_hook.BACKUP in (hooks / "pre-commit").read_text(encoding="utf-8")
    install_hook.uninstall(r.path)
    assert "custom-hook-ran" in (hooks / "pre-commit").read_text(encoding="utf-8")   # прежний хук восстановлен
