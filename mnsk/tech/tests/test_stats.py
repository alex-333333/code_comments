"""Эталонные значения известны из справочников/scipy; допуск указан в каждом assert."""

import math
from collections import Counter

import stats


def test_norm_sf_reference():
    assert abs(stats.norm_sf(1.959963984540054) - 0.025) < 1e-9
    assert abs(stats.norm_sf(0) - 0.5) < 1e-12


def test_chi2_sf_reference():
    # критические значения таблицы хи-квадрат для p = 0.05
    assert abs(stats.chi2_sf(3.841458820694124, 1) - 0.05) < 1e-6
    assert abs(stats.chi2_sf(5.991464547107979, 2) - 0.05) < 1e-6
    assert abs(stats.chi2_sf(18.307038053275146, 10) - 0.05) < 1e-6
    assert stats.chi2_sf(0, 3) == 1.0


def test_chi2_contingency_2x2():
    # [[10, 20], [30, 40]] без поправки Йейтса: chi2 = 0.7937, p = 0.3730 (scipy correction=False)
    r = stats.chi2_contingency([[10, 20], [30, 40]])
    assert abs(r["chi2"] - 0.79365) < 1e-4
    assert abs(r["p"] - 0.37306) < 1e-3
    assert r["df"] == 1


def test_chi2_ignores_empty_columns():
    r = stats.chi2_contingency([[10, 0, 20], [30, 0, 40]])
    assert r["df"] == 1


def test_fisher_exact_reference():
    # классическая таблица «чай с молоком»: [[3,1],[1,3]] -> p = 0.4857 (двусторонний)
    assert abs(stats.fisher_exact(3, 1, 1, 3)["p"] - 0.4857142857) < 1e-6
    # [[1,9],[11,3]] -> p = 0.002759 (scipy.stats.fisher_exact)
    assert abs(stats.fisher_exact(1, 9, 11, 3)["p"] - 0.002759) < 1e-5


def test_fisher_no_effect_gives_p_one():
    assert abs(stats.fisher_exact(5, 5, 5, 5)["p"] - 1.0) < 1e-9


def test_mann_whitney_separated_groups():
    r = stats.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert r["u"] == 0
    assert r["cliffs_delta"] == -1.0
    assert 0.010 < r["p"] < 0.015      # нормальное приближение с поправкой: 0.0122


def test_mann_whitney_identical_groups():
    r = stats.mann_whitney([1, 2, 3, 4], [1, 2, 3, 4])
    assert abs(r["cliffs_delta"]) < 1e-12
    assert r["p"] > 0.9


def test_mann_whitney_handles_ties():
    x = [0.5] * 50 + [1.0] * 50
    y = [0.5] * 50 + [0.0] * 50
    r = stats.mann_whitney(x, y)
    assert r["cliffs_delta"] > 0 and r["p"] < 0.05


def test_cliffs_labels():
    assert stats.cliffs_label(0.05) == "пренебрежимый"
    assert stats.cliffs_label(-0.6) == "большой"


def test_bh_adjust_monotone_and_bounded():
    p = [0.001, 0.02, 0.03, 0.5]
    adj = stats.bh_adjust(p)
    assert all(a >= b for a, b in zip(adj, p))
    assert adj[0] == 0.004
    assert max(adj) <= 1.0


def test_cluster_bootstrap_detects_difference_and_no_difference():
    same = {f"r{i}": (5, 10) for i in range(20)}
    r0 = stats.cluster_bootstrap_diff(same, dict(same), iters=300)
    assert r0["lo"] <= 0 <= r0["hi"]

    high = {f"a{i}": (8, 10) for i in range(20)}
    low = {f"b{i}": (2, 10) for i in range(20)}
    r1 = stats.cluster_bootstrap_diff(high, low, iters=300)
    assert r1["lo"] > 0.4 and abs(r1["diff"] - 0.6) < 1e-9


def test_cluster_bootstrap_interval_widens_with_few_clusters():
    # тот же общий объём, но 3 проекта вместо 30: неопределённость должна быть больше
    many_a = {f"a{i}": (i % 4, 10) for i in range(30)}
    many_b = {f"b{i}": ((i + 1) % 4, 10) for i in range(30)}
    few_a = {"a0": (30, 100), "a1": (10, 100), "a2": (50, 100)}
    few_b = {"b0": (40, 100), "b1": (20, 100), "b2": (60, 100)}
    w_many = (lambda r: r["hi"] - r["lo"])(stats.cluster_bootstrap_diff(many_a, many_b, iters=400))
    w_few = (lambda r: r["hi"] - r["lo"])(stats.cluster_bootstrap_diff(few_a, few_b, iters=400))
    assert w_few > w_many


def test_log_odds_prefers_distinctive_words():
    # Корпуса одного размера (по 1100 слов): иначе даже 'the' честно окажется
    # статистически различимым - при таких частотах дисперсия ничтожна.
    ca = Counter({"the": 1000, "ensure": 60, "hack": 0, "other": 40})
    cb = Counter({"the": 1000, "ensure": 5, "hack": 40, "other": 55})
    z = stats.log_odds_informative_prior(ca, cb)
    assert z["ensure"] > 2 > -2 > z["hack"]
    assert abs(z["the"]) < 0.5      # общее слово нейтрально


def test_cohen_kappa():
    assert stats.cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == 0.5
    assert stats.cohen_kappa([1, 0], [1, 0]) == 1.0
    assert stats.cohen_kappa([], []) is None


def test_quantile_matches_linear_interpolation():
    xs = [1, 2, 3, 4]
    assert stats.quantile(xs, 0.5) == 2.5
    assert stats.quantile(xs, 0) == 1 and stats.quantile(xs, 1) == 4


def test_bootstrap_with_single_cluster_gives_no_interval():
    """Один репозиторий на группу: интервал [x; x] выглядел бы как точный результат - он не должен выдаваться."""
    r = stats.cluster_bootstrap_diff({"a": (3, 10)}, {"b": (1, 10)}, iters=100)
    assert abs(r["diff"] - 0.2) < 1e-12 and r["lo"] is None and r["hi"] is None
    r2 = stats.cluster_bootstrap_diff({"a": (3, 10), "c": (5, 10)}, {"b": (1, 10)}, iters=100)
    assert r2["lo"] is None                     # достаточно, чтобы ОДНА из групп была из одного проекта


def test_bootstrap_returns_ratio_with_interval():
    """Для плотности (комментариев на строку) важно отношение групп, а не только разность."""
    a = {f"a{i}": (30, 100) for i in range(20)}       # 0.30 комментария на строку
    b = {f"b{i}": (10, 100) for i in range(20)}       # 0.10
    r = stats.cluster_bootstrap_diff(a, b, iters=300)
    assert abs(r["ratio"] - 3.0) < 1e-9 and r["ratio_lo"] <= 3.0 + 1e-9 and r["ratio_hi"] >= 3.0 - 1e-9   # float: 3 может выйти как 2.9999999999999996
    r1 = stats.cluster_bootstrap_diff({"x": (3, 10)}, {"y": (1, 10)}, iters=50)
    assert abs(r1["ratio"] - 3.0) < 1e-9 and r1["ratio_lo"] is None      # один репозиторий: точечная оценка есть, интервала нет


def test_bootstrap_ratio_undefined_when_denominator_group_is_zero():
    r = stats.cluster_bootstrap_diff({"a": (3, 10), "b": (2, 10)}, {"c": (0, 10), "d": (0, 10)}, iters=50)
    assert r["ratio"] is None and r["ratio_lo"] is None
