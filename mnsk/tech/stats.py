"""
stats.py - статистика для counter.py на чистом Python (math + random).

Зачем не scipy/numpy: пакеты весят десятки мегабайт, тянут бинарные колёса и ломают
установку на свежих версиях Python. Нужные критерии короткие и хорошо известны;
каждый проверен в tests/test_stats.py по эталонным значениям.

Что здесь есть и зачем проекту:
- mann_whitney + Cliff's delta: сравнение распределений overlap у AI и human.
  Критерий ранговый - он не требует нормальности, а overlap ограничен [0, 1] и далёк
  от нормального. Cliff's delta - размер эффекта: p-value при 100 тысячах комментариев
  «значимо» почти всегда, а вот насколько различие велико - отдельный вопрос.
- chi2_contingency + Cramér's V: распределение типов комментариев по источникам.
- fisher_exact: сравнение частоты идиом (редкие события, малые счётчики).
- cluster_bootstrap_diff: доверительный интервал для разницы долей с передискретизацией
  РЕПОЗИТОРИЕВ. Комментарии одного проекта не независимы (один автор, один стиль), и
  обычный бутстреп по комментариям дал бы слишком узкий интервал - псевдорепликация.
- log_odds_informative_prior: «характерные слова» (Monroe et al., 2008). Сырые частоты
  дают в облаке слов только 'the' и 'to' с обеих сторон; log-odds с априором показывает,
  чем корпуса РАЗЛИЧАЮТСЯ, и не раздувает редкие слова.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Sequence


# --- базовое -----------------------------------------------------------------------

def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def quantile(sorted_xs: Sequence[float], q: float) -> float | None:
    """Линейная интерполяция (как numpy.percentile по умолчанию). На вход - отсортированный список."""
    if not sorted_xs:
        return None
    pos = q * (len(sorted_xs) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def describe(xs: Sequence[float]) -> dict:
    s = sorted(xs)
    return {"n": len(s), "mean": mean(s), "median": quantile(s, 0.5),
            "q1": quantile(s, 0.25), "q3": quantile(s, 0.75)}


def norm_sf(z: float) -> float:
    """P(Z > z) для стандартной нормали."""
    return 0.5 * math.erfc(z / math.sqrt(2))


# --- Манн-Уитни ---------------------------------------------------------------------

def _ranks(values: Sequence[float]) -> tuple[list[float], list[int]]:
    """Средние ранги (для связок) и размеры групп одинаковых значений."""
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    ties: list[int] = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        ties.append(j - i + 1)
        i = j + 1
    return ranks, ties


def mann_whitney(x: Sequence[float], y: Sequence[float]) -> dict:
    """Двусторонний критерий Манна-Уитни (нормальное приближение с поправкой на связки
    и на непрерывность) и Cliff's delta.

    Связок у overlap много (значения вида k/n повторяются), поэтому поправка обязательна.
    delta > 0 - значения x, как правило, больше значений y; |delta| < 0.147 принято считать
    пренебрежимым, < 0.33 малым, < 0.474 средним, иначе большим (Romano et al., 2006)."""
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return {"u": None, "z": None, "p": None, "cliffs_delta": None, "n1": n1, "n2": n2}
    ranks, ties = _ranks(list(x) + list(y))
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    mu = n1 * n2 / 2
    tie_term = sum(t ** 3 - t for t in ties) / (n * (n - 1)) if n > 1 else 0
    var = n1 * n2 / 12 * ((n + 1) - tie_term)
    if var <= 0:
        return {"u": u1, "z": 0.0, "p": 1.0, "cliffs_delta": 0.0, "n1": n1, "n2": n2}
    diff = u1 - mu
    z = (abs(diff) - 0.5) / math.sqrt(var) * (1 if diff >= 0 else -1) if abs(diff) > 0.5 else 0.0
    return {"u": u1, "z": z, "p": min(1.0, 2 * norm_sf(abs(z))),
            "cliffs_delta": 2 * u1 / (n1 * n2) - 1, "n1": n1, "n2": n2}


def cliffs_label(delta: float | None) -> str:
    if delta is None:
        return "н/д"
    d = abs(delta)
    return "пренебрежимый" if d < 0.147 else "малый" if d < 0.33 else "средний" if d < 0.474 else "большой"


# --- хи-квадрат ----------------------------------------------------------------------

def _gamma_q(a: float, x: float) -> float:
    """Регуляризованная верхняя неполная гамма Q(a, x) (Numerical Recipes: ряд и цепная дробь)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-14:
                break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - gln))
    b = x + 1 - a
    c = 1e300
    d = 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - gln) * h)


def chi2_sf(x: float, df: int) -> float:
    return _gamma_q(df / 2, x / 2)


def chi2_contingency(table: Sequence[Sequence[float]]) -> dict:
    """Критерий независимости для таблицы r×c и Cramér's V (0 - нет связи, 1 - полная).
    Столбцы/строки с нулевой суммой отбрасываются: для них ожидание равно нулю."""
    rows = [list(r) for r in table if sum(r) > 0]
    if not rows:
        return {"chi2": None, "df": 0, "p": None, "cramers_v": None}
    keep = [j for j in range(len(rows[0])) if sum(r[j] for r in rows) > 0]
    rows = [[r[j] for j in keep] for r in rows]
    r_tot = [sum(r) for r in rows]
    c_tot = [sum(r[j] for r in rows) for j in range(len(rows[0]))]
    n = sum(r_tot)
    df = (len(rows) - 1) * (len(c_tot) - 1)
    if df <= 0 or n == 0:
        return {"chi2": 0.0, "df": max(df, 0), "p": 1.0, "cramers_v": 0.0}
    chi2 = sum((rows[i][j] - r_tot[i] * c_tot[j] / n) ** 2 / (r_tot[i] * c_tot[j] / n)
               for i in range(len(rows)) for j in range(len(c_tot)))
    v = math.sqrt(chi2 / (n * min(len(rows) - 1, len(c_tot) - 1)))
    return {"chi2": chi2, "df": df, "p": chi2_sf(chi2, df), "cramers_v": v}


# --- Фишер ----------------------------------------------------------------------------

def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(a: int, b: int, c: int, d: int) -> dict:
    """Точный двусторонний критерий Фишера для таблицы [[a, b], [c, d]] и odds ratio.
    Для очень больших таблиц (слишком много слагаемых) используется z-критерий для двух долей."""
    r1, r2, c1, n = a + b, c + d, a + c, a + b + c + d
    odds = ((a + 0.5) / (b + 0.5)) / ((c + 0.5) / (d + 0.5))  # +0.5 (Холдейн): определён и при нулях
    if n == 0:
        return {"p": 1.0, "odds_ratio": None}
    lo, hi = max(0, c1 - r2), min(r1, c1)
    if hi - lo > 30000:
        p1, p2, p = a / r1 if r1 else 0, c / r2 if r2 else 0, c1 / n
        se = math.sqrt(p * (1 - p) * (1 / max(r1, 1) + 1 / max(r2, 1)))
        return {"p": min(1.0, 2 * norm_sf(abs(p1 - p2) / se)) if se else 1.0, "odds_ratio": odds}

    def logp(k: int) -> float:
        return _log_choose(r1, k) + _log_choose(r2, c1 - k) - _log_choose(n, c1)

    obs = logp(a)
    total = sum(math.exp(logp(k)) for k in range(lo, hi + 1) if logp(k) <= obs + 1e-7)
    return {"p": min(1.0, total), "odds_ratio": odds}


def bh_adjust(pvals: Sequence[float]) -> list[float]:
    """Поправка Бенджамини-Хохберга. Идиом проверяются сотни: без поправки часть «значимых»
    различий возникла бы случайно (проблема множественных сравнений)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        adj[i] = prev
    return adj


# --- бутстреп по кластерам --------------------------------------------------------------

def cluster_bootstrap_diff(a: dict[str, tuple[int, int]], b: dict[str, tuple[int, int]],
                           iters: int = 2000, seed: int = 42, alpha: float = 0.05) -> dict:
    """Разница (A − B) и отношение (A / B) с доверительными интервалами; передискретизируются РЕПОЗИТОРИИ.

    a, b: {репозиторий: (числитель, знаменатель)}. Величина группы = Σчислителей / Σзнаменателей
    по выбранным репозиториям. Для долей это доля успехов; для плотности комментариев - комментариев
    на строку. Один проект с тысячей комментариев остаётся одним «наблюдением» - поэтому интервал
    честно отражает, что независимых единиц у меня столько, сколько проектов."""
    def share(groups: list[tuple[int, int]]) -> float | None:
        tot = sum(t for _, t in groups)
        return sum(k for k, _ in groups) / tot if tot else None

    ga, gb = list(a.values()), list(b.values())
    sa, sb = share(ga), share(gb)
    n_cl = (len(ga), len(gb))
    empty = {"diff": None, "lo": None, "hi": None, "ratio": None, "ratio_lo": None, "ratio_hi": None,
             "share_a": sa, "share_b": sb, "n_clusters": n_cl}
    if sa is None or sb is None:
        return empty
    ratio = sa / sb if sb else None
    if min(len(ga), len(gb)) < 2:
        # Один репозиторий в группе: передискретизировать нечего, все итерации дали бы одно и то же число,
        # и интервал [x; x] выглядел бы как абсолютно точный результат. Честнее сказать «не определён».
        return {**empty, "diff": sa - sb, "ratio": ratio}
    rng = random.Random(seed)
    diffs, ratios = [], []
    for _ in range(iters):
        ra = share(rng.choices(ga, k=len(ga)))
        rb = share(rng.choices(gb, k=len(gb)))
        if ra is not None and rb is not None:
            diffs.append(ra - rb)
            if rb:
                ratios.append(ra / rb)
    diffs.sort()
    ratios.sort()
    return {"diff": sa - sb, "lo": quantile(diffs, alpha / 2), "hi": quantile(diffs, 1 - alpha / 2),
            "ratio": ratio, "ratio_lo": quantile(ratios, alpha / 2) if ratios else None,
            "ratio_hi": quantile(ratios, 1 - alpha / 2) if ratios else None,
            "share_a": sa, "share_b": sb, "n_clusters": n_cl}


# --- характерные слова ---------------------------------------------------------------------

def log_odds_informative_prior(ca: Counter, cb: Counter, prior_strength: float = 500.0) -> dict[str, float]:
    """z-оценка «характерности» слова для A относительно B (Monroe, Colaresi, Quinn, 2008).

    Априор пропорционален частоте слова в объединённом корпусе; prior_strength - суммарный вес
    априора в «псевдонаблюдениях» (сглаживает редкие слова). z > 0 - слово характерно для A."""
    na, nb = sum(ca.values()), sum(cb.values())
    if na == 0 or nb == 0:
        return {}
    total = Counter(ca) + Counter(cb)
    n_all = sum(total.values())
    a0 = prior_strength
    out = {}
    for w, tot in total.items():
        aw = a0 * tot / n_all
        ya, yb = ca.get(w, 0), cb.get(w, 0)
        da = math.log((ya + aw) / (na + a0 - ya - aw))
        db = math.log((yb + aw) / (nb + a0 - yb - aw))
        var = 1 / (ya + aw) + 1 / (yb + aw)
        out[w] = (da - db) / math.sqrt(var)
    return out


# --- согласие разметчиков --------------------------------------------------------------------

def cohen_kappa(a: Sequence, b: Sequence) -> float | None:
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)
