"""Сквозной тест: синтетические репозитории -> корпус -> комментарии -> анализ -> дашборд.

Главная проверка здесь - АТРИБУЦИЯ: старые человеческие комментарии из файла, который потом
правит AI-коммит, не должны оказаться в AI-корпусе. Это защита валидности всего исследования."""

import json
import re
from pathlib import Path

import pytest

import counter
import dashboard
import demo
import demo_data
import extract_comments
import parser as cp


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("pipeline")
    lists = demo.build_demo(work / "demo")
    corpus = work / "corpus"
    cp.main(["--repos", str(lists["ai"]), "--source", "ai", "--outdir", str(corpus)])
    cp.main(["--repos", str(lists["human"]), "--source", "human", "--outdir", str(corpus)])
    extract_comments.main(["--corpus", str(corpus)])
    results = work / "results"
    counter.main(["--corpus", str(corpus), "--results", str(results), "--demo", "--boot", "100"])
    return {"work": work, "corpus": corpus, "results": results}


def read(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def human_only_texts():
    """Все возможные формулировки человеческого пула (с подстановкой существительных)."""
    texts = set()
    for tpl in demo_data.HUMAN_WHY + demo_data.HUMAN_REF:
        for n in demo_data.NOUNS:
            texts.add(tpl.replace("{n}", n).strip().rstrip("."))
    return texts


def test_manifest_has_both_sources_and_tools(built):
    rows = read(built["corpus"] / "manifest.jsonl")
    assert {r["source"] for r in rows} == {"ai", "human"}
    assert {r["tool"] for r in rows if r["source"] == "ai"} == {"claude", "copilot", "cursor"}
    assert all(r["added_ranges"] for r in rows)


def test_human_corpus_has_only_pre_cutoff_commits(built):
    rows = [r for r in read(built["corpus"] / "manifest.jsonl") if r["source"] == "human"]
    assert rows and all(r["commit_date"] < "2020-11-01" for r in rows)


def test_ai_corpus_contains_no_old_human_comments(built):
    """paired_* : AI-коммит дописывает функцию в старый человеческий файл."""
    ai_comments = [c for c in read(built["corpus"] / "comments.jsonl") if c["source"] == "ai"]
    assert ai_comments
    human_texts = human_only_texts()
    leaked = [c["text"] for c in ai_comments
              if c["kind"] != "doc" and re.sub(r"\s+", " ", c["text"]).strip().rstrip(".") in human_texts]
    assert leaked == []


def test_extraction_dropped_old_comments_of_modified_files(built):
    stats = json.loads((built["corpus"] / "extract_stats.json").read_text(encoding="utf-8"))
    assert stats["by_source"]["ai"].get("dropped_not_added", 0) > 0     # старые строки реально отсеиваются


def test_every_comment_lies_inside_added_ranges(built):
    manifest = {(r["repo"], r["sha"], r["file"]): r["added_ranges"] for r in read(built["corpus"] / "manifest.jsonl")}
    for c in read(built["corpus"] / "comments.jsonl"):
        ranges = manifest[(c["repo"], c["sha"], c["file"])]
        assert any(s <= c["start_line"] and c["end_line"] <= e for s, e in ranges), c


def test_summary_is_consistent(built):
    s = json.loads((built["results"] / "summary.json").read_text(encoding="utf-8"))
    assert s["demo"] is True and s["schema"] == 1
    for src in ("ai", "human"):
        h = s["overlap"]["hist"][src]["all"]
        assert len(h["hist"]) == 100
        # гистограмма «без маркеров почему» - часть общей
        assert all(a <= b for a, b in zip(h["hist_nocue"], h["hist"]))
        mix = s["referential"]["mix"][src]
        assert mix["referential"] + mix["explanatory"] + mix["other"] == mix["classified"]
    # AI-пересказы в демо построены так, что overlap у AI выше - иначе метрика не работает
    d = s["overlap"]["describe"]["overlap"]
    assert d["ai"]["mean"] > d["human"]["mean"]
    assert s["referential"]["bootstrap"]["referential"]["lo"] is not None


def test_sensitivity_curve_is_monotone(built):
    s = json.loads((built["results"] / "summary.json").read_text(encoding="utf-8"))
    for src in ("ai", "human"):
        vals = [r[src] for r in s["referential"]["sensitivity"]]
        assert all(a >= b - 1e-9 for a, b in zip(vals, vals[1:]))     # выше порог - не больше «пересказов»


def test_dashboard_is_self_contained(built):
    out = dashboard.build(built["results"], built["work"] / "dash.html")
    html = out.read_text(encoding="utf-8")
    assert "ДЕМО-ДАННЫЕ" in html and "/*__DATA__*/" not in html
    assert not re.search(r"(?:src|href)\s*=\s*[\"']https?://", html)          # ни одного внешнего ресурса
    assert not re.search(r"@import|url\(\s*[\"']?https?://", html)
    assert re.search(r"const DATA = \{", html)
    # данные в <script> не должны содержать закрывающего тега
    data_part = html.split("const DATA = ", 1)[1].split("</script>", 1)[0]
    json.loads(data_part.strip().rstrip(";"))


def test_second_extract_run_is_reproducible(built):
    a = (built["corpus"] / "comments.jsonl").read_text(encoding="utf-8")
    extract_comments.main(["--corpus", str(built["corpus"])])
    assert (built["corpus"] / "comments.jsonl").read_text(encoding="utf-8") == a


def test_bin_edges_are_exact_for_every_hundredth():
    """0.57 * 100 = 56.99999999999999 в плавающей точке: без поправки значение ровно 0.57 попало бы
    в корзину 56 и не считалось бы «>= 0.57»."""
    for k in range(101):
        assert counter._bin(k / 100) == min(k, 99), k


def test_mass_points_expose_discreteness_of_overlap(built):
    s = json.loads((built["results"] / "summary.json").read_text(encoding="utf-8"))
    for src in ("ai", "human"):
        mp = s["overlap"]["mass_points"][src]
        assert mp["distinct"] <= 30                       # overlap = k/n: разных значений мало
        assert mp["top"] and mp["top"][0][2] == max(t[2] for t in mp["top"])
        assert 0 <= mp["zero_share"] <= 1


def test_warning_when_many_comments_sit_exactly_on_theta(built):
    s = json.loads((built["results"] / "summary.json").read_text(encoding="utf-8"))
    assert any("ровно 0.5" in w for w in s["warnings"])   # у AI в демо ~31% комментариев имеют overlap ровно 0.5


def test_density_matches_independent_recount(built):
    """Знаменатель и числитель пересчитываются заново из манифеста и comments.jsonl - не из file_stats."""
    s = json.loads((built["results"] / "summary.json").read_text(encoding="utf-8"))
    d = s["density"]
    assert d["available"]
    manifest = read(built["corpus"] / "manifest.jsonl")
    comments = read(built["corpus"] / "comments.jsonl")
    for src in ("ai", "human"):
        lines = sum(e - b + 1 for r in manifest if r["source"] == src for b, e in r["added_ranges"])
        com = sum(1 for c in comments if c["source"] == src and not c["exclude"])
        blk = d["per_source"][src]
        assert blk["added_lines"] == lines and blk["comments"] == com
        assert abs(blk["per100"] - 100 * com / lines) < 1e-3


def test_density_counts_files_without_comments_in_denominator(tmp_path):
    """Файл без единого комментария виден только в file_stats.jsonl, но обязан попасть в знаменатель:
    иначе плотность завышалась бы, а «молчаливый» автор выглядел бы комментатором."""
    r = demo.GitRepo(tmp_path / "quiet")
    talk = "\n".join(["# add the total", "x = 1", "# return it", "y = 2"]) + "\n"
    quiet = "\n".join(["a = 1", "b = 2", "c = 3", "d = 4"]) + "\n"
    r.commit({"talk.py": talk, "quiet.py": quiet},
             "two files", "2025-06-01T12:00:00", ["Co-authored-by: Claude <noreply@anthropic.com>"])
    lst = tmp_path / "ai.txt"
    lst.write_text(str(r.path), encoding="utf-8")
    out = tmp_path / "corpus"
    cp.main(["--repos", str(lst), "--source", "ai", "--outdir", str(out)])
    extract_comments.main(["--corpus", str(out)])
    fs = {row["file"]: row for row in read(out / "file_stats.jsonl")}
    assert fs["quiet.py"]["comments"] == 0 and fs["quiet.py"]["added_lines"] == 4
    assert fs["talk.py"]["comments"] == 2 and fs["talk.py"]["added_lines"] == 4
    block = counter._density_block(list(fs.values()))
    assert block["per100"] == 25.0                        # 2 комментария на 8 добавленных строк, а не на 4


def test_density_breakdowns_are_consistent(built):
    s = json.loads((built["results"] / "summary.json").read_text(encoding="utf-8"))["density"]
    for src in ("ai", "human"):
        blk = s["per_source"][src]
        assert abs(sum(v for v in blk["by_kind_per100"].values()) - blk["per100"]) < 0.01     # виды в сумме дают общую плотность
        assert abs(blk["main_per100"] + blk["doc_per100"] - blk["per100"]) < 0.01
        by_lang_lines = sum(s["by_lang"][lang][src]["added_lines"] for lang in s["by_lang"])
        assert by_lang_lines == blk["added_lines"]                                              # срезы по языкам дают целое
        assert 0 <= blk["line_share"] <= 1 and blk["per100_nonblank"] >= blk["per100"]
    assert set(s["by_tool"]) == {"claude", "copilot", "cursor"}
    assert s["bootstrap"]["all"]["ratio"] > 1            # в демо AI комментирует плотнее - иначе метрика сломана
    assert (built["results"] / "density.csv").exists()


def test_density_absent_file_stats_is_reported_not_fatal(built, tmp_path):
    (tmp_path / "corpus").mkdir()
    import shutil
    shutil.copy(built["corpus"] / "comments.jsonl", tmp_path / "corpus" / "comments.jsonl")   # без file_stats.jsonl
    counter.main(["--corpus", str(tmp_path / "corpus"), "--results", str(tmp_path / "res"), "--boot", "20"])
    s = json.loads((tmp_path / "res" / "summary.json").read_text(encoding="utf-8"))
    assert s["density"]["available"] is False and "file_stats" in s["density"]["reason"]
