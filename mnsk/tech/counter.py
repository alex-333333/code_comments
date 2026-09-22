#!/usr/bin/env python3
"""
counter.py - анализ корпуса: comments.jsonl -> results/summary.json (+ CSV, примеры).

Модель обучать не нужно: только счётчики (collections.Counter), словари и статистика.

Что и в каком порядке делается - и зачем:

1. Отбор (pool). Из наблюдений убираются лицензии/директивы/закомментированный код (их
   пометил extract_comments), не-английские комментарии (словари английские), точные дубли
   внутри репозитория (копипаста и вендоринг раздувают одну «мысль» в сотни наблюдений).
2. Потолок на репозиторий (--max-per-repo). Иначе один огромный проект определяет облако слов
   и «результат» корпуса.
3. Балансировка по языкам (--balance, по желанию). Иначе разница AI/human смешивается с разницей
   «TypeScript против C»: языки комментируют по-разному.
4. Признаки (metrics.analyze) для каждого комментария - те же, что использует чекер.
5. Агрегаты и критерии. Главное - доля referential при пороге θ и КРИВАЯ ЧУВСТВИТЕЛЬНОСТИ:
   вывод не должен держаться на одном произвольно выбранном пороге.

Основная метрика считается по обычным комментариям (inline/leading/block); докстринги (kind=doc)
анализируются отдельным блоком, потому что описывают функцию целиком и по определению «пересказывают».
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import metrics
import stats
from config import DEFAULT_THETA, MIN_CONTENT_WORDS, SCHEMA_VERSION, setup_console

# 100 корзин по 0.01: значения overlap - дроби k/n, и порог θ на дашборде можно двигать с шагом 0.01
# так, что он всегда попадает точно на границу корзины (пересчёт доли без потери точности).
BINS = 100
THETA_GRID = [round(0.2 + 0.05 * i, 2) for i in range(13)]  # 0.20 ... 0.80
SOURCES = ("ai", "human")
KINDS = ("inline", "leading", "block", "detached", "doc")
# Порядок групп маркеров в дашборде и CSV: дискурсивные связки сначала, дальше -
# авторская позиция, обращение к читателю, условности кода. См. lexicon/groups.csv.
MARKER_GROUP_ORDER = ("discourse", "evidential", "stance", "directive", "code_convention")


def r5(x):
    return None if x is None else round(x, 5)


def _bin(v: float) -> int:
    # + 1e-9: 0.57 * 100 в плавающей точке равно 56.99999999999999, и int() отправил бы значение
    # ровно 0.57 в корзину 56, то есть под порог 0.57 оно бы не попало, хотя 0.57 >= 0.57.
    return min(int(v * BINS + 1e-9), BINS - 1)


def mass_points(vals: list[float], top: int = 4) -> dict:
    """Самые частые ТОЧНЫЕ значения overlap.

    overlap = k/n, где n - число содержательных слов комментария (обычно 3-8), поэтому у метрики
    мало разных значений и на некоторых (0, 1/3, 1/2, 2/3) лежит вес целых десятков процентов
    комментариев. Порог, попадающий на такое значение, даёт «ступеньку» на кривой - это свойство
    метрики, а не ошибка счёта, и читатель должен видеть это сразу."""
    n = len(vals)
    c = Counter(round(v, 4) for v in vals)
    return {"n": n, "distinct": len(c),
            "top": [[v, k, r5(k / n)] for v, k in c.most_common(top)] if n else [],
            "zero_share": r5(c.get(0.0, 0) / n) if n else None}


def norm_key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


# --- отбор ------------------------------------------------------------------------------

def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def build_pool(path: Path, args, lex) -> tuple[list[dict], dict]:
    comp: dict[str, Counter] = {s: Counter() for s in SOURCES}
    seen: set[tuple] = set()
    pool: list[dict] = []
    for rec in read_jsonl(path):
        src = rec["source"]
        if src not in comp:
            continue
        c = comp[src]
        c["observed"] += 1
        if rec.get("exclude"):
            c["excluded_" + rec["exclude"]] += 1
            continue
        if not metrics.is_english(rec["text"], lex):
            c["non_english"] += 1
            continue
        key = (src, rec["repo"], norm_key(rec["text"]))
        if not key[2] or key in seen:
            c["duplicate"] += 1
            continue
        seen.add(key)
        pool.append(rec)

    # потолок на репозиторий: детерминированная выборка (seed + имя репозитория)
    by_repo: dict[tuple, list[dict]] = defaultdict(list)
    for rec in pool:
        by_repo[(rec["source"], rec["repo"])].append(rec)
    kept: list[dict] = []
    for (src, repo), items in sorted(by_repo.items()):
        if len(items) < args.min_per_repo:
            comp[src]["repo_below_min"] += len(items)
            continue
        if len(items) > args.max_per_repo:
            random.Random(f"{args.seed}:{repo}").shuffle(items)
            comp[src]["capped_per_repo"] += len(items) - args.max_per_repo
            items = items[:args.max_per_repo]
        kept.extend(items)
    pool = kept

    if args.balance:
        rng = random.Random(args.seed)
        by_lang: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {s: [] for s in SOURCES})
        for rec in pool:
            by_lang[rec["lang"]][rec["source"]].append(rec)
        pool = []
        for lang, d in sorted(by_lang.items()):
            n = min(len(d["ai"]), len(d["human"]))
            for s in SOURCES:
                rng.shuffle(d[s])
                comp[s]["dropped_by_balance"] += len(d[s]) - n
                pool.extend(d[s][:n])

    for s in SOURCES:
        comp[s]["analyzed"] = sum(1 for r in pool if r["source"] == s)
    return pool, {s: dict(comp[s]) for s in SOURCES}


# --- агрегаты ------------------------------------------------------------------------------

def function_mix(recs: list[dict], key: str = "cls") -> dict:
    c = Counter(r[key] or "undecided" for r in recs)
    classified = c["referential"] + c["explanatory"] + c["other"]
    return {"referential": c["referential"], "explanatory": c["explanatory"], "other": c["other"],
            "undecided": c["undecided"], "classified": classified,
            "referential_share": r5(c["referential"] / classified) if classified else None,
            "explanatory_share": r5(c["explanatory"] / classified) if classified else None}


def hist_block(recs: list[dict]) -> dict:
    """Гистограммы overlap: все комментарии и «без маркеров почему» (из них состоит referential)."""
    out = {k: [0] * BINS for k in ("hist", "hist_nocue", "hist_ext", "hist_nocue_ext")}
    classified = 0
    for r in recs:
        f = r["f"]
        if f["overlap"] is not None:
            out["hist"][_bin(f["overlap"])] += 1
            out["hist_ext"][_bin(f["overlap_ext"])] += 1
            if not f["explanatory_cue"]:
                out["hist_nocue"][_bin(f["overlap"])] += 1
                out["hist_nocue_ext"][_bin(f["overlap_ext"])] += 1
        if r["cls"] is not None:
            classified += 1
    out["classified"] = classified
    out["cue"] = sum(1 for r in recs if r["f"]["explanatory_cue"])
    out["n"] = len(recs)
    return out


def share_at(recs: list[dict], theta: float, key: str = "overlap") -> float | None:
    classified = [r for r in recs if r["cls"] is not None]
    if not classified:
        return None
    ref = sum(1 for r in classified if not r["f"]["explanatory_cue"]
              and r["f"][key] is not None and r["f"][key] >= theta - 1e-12)
    return ref / len(classified)


def overlap_values(recs: list[dict], key: str = "overlap") -> list[float]:
    return [r["f"][key] for r in recs if r["f"][key] is not None]


def presence_rows(recs_by_src: dict[str, list[dict]], getter, labels: list[str]) -> dict[str, dict]:
    """Для каждой метки: в скольких комментариях источника она встретилась."""
    counts = {s: Counter() for s in SOURCES}
    for s in SOURCES:
        for r in recs_by_src[s]:
            for lab in set(getter(r)):
                counts[s][lab] += 1
    return counts


def compare_rows(counts: dict[str, Counter], totals: dict[str, int], labels: list[str]) -> list[dict]:
    rows = []
    for lab in labels:
        a, h = counts["ai"][lab], counts["human"][lab]
        fe = stats.fisher_exact(a, totals["ai"] - a, h, totals["human"] - h)
        rows.append({"label": lab, "ai_n": a, "human_n": h,
                     "ai_per1k": r5(1000 * a / totals["ai"]) if totals["ai"] else None,
                     "human_per1k": r5(1000 * h / totals["human"]) if totals["human"] else None,
                     "odds_ratio": r5(fe["odds_ratio"]), "p": fe["p"]})
    qs = stats.bh_adjust([r["p"] for r in rows]) if rows else []
    for r, q in zip(rows, qs):
        r["q"] = q
    return rows


def word_stats(recs_by_src: dict[str, list[dict]], lex, top: int, min_count: int) -> dict:
    """Частоты слов и «характерность» (log-odds с информативным априором)."""
    cnt = {s: Counter() for s in SOURCES}
    bigr = {s: Counter() for s in SOURCES}
    for s in SOURCES:
        for r in recs_by_src[s]:
            words = [w for w in metrics.comment_words(r["text"]) if len(w) >= 3 and w not in lex.stopwords and not w.isdigit()]
            cnt[s].update(words)
            seq = [w for w in metrics.comment_words(r["text"])]
            for a, b in zip(seq, seq[1:]):
                if a not in lex.stopwords and b not in lex.stopwords and len(a) >= 3 and len(b) >= 3:
                    bigr[s][f"{a} {b}"] += 1
    z = stats.log_odds_informative_prior(cnt["ai"], cnt["human"])
    total = Counter(cnt["ai"]) + Counter(cnt["human"])
    eligible = {w: v for w, v in z.items() if total[w] >= min_count}

    def pack_freq(s):
        return [[w, c] for w, c in cnt[s].most_common(top)]

    ai_d = sorted(((w, v) for w, v in eligible.items() if v > 0), key=lambda x: -x[1])[:top]
    hu_d = sorted(((w, v) for w, v in eligible.items() if v < 0), key=lambda x: x[1])[:top]
    return {
        "freq": {s: pack_freq(s) for s in SOURCES},
        "distinctive": {"ai": [[w, r5(v), cnt["ai"][w], cnt["human"][w]] for w, v in ai_d],
                        "human": [[w, r5(-v), cnt["human"][w], cnt["ai"][w]] for w, v in hu_d]},
        "bigrams": {s: [[w, c] for w, c in bigr[s].most_common(25)] for s in SOURCES},
        "tokens": {s: sum(cnt[s].values()) for s in SOURCES},
    }


def make_examples(pool: list[dict], theta: float, per_band: int, lex, seed: int) -> dict:
    """Пары «комментарий - код» с подсветкой пересечения: наглядное доказательство, что метрика измеряет то, что заявлено."""
    rng = random.Random(seed)
    out = {s: [] for s in SOURCES}
    for s in SOURCES:
        cand = [r for r in pool if r["source"] == s and r["f"]["overlap"] is not None and r["kind"] != "doc" and r.get("code")]
        bands = {"high": [r for r in cand if r["f"]["overlap"] >= max(theta, 0.6)],
                 "mid": [r for r in cand if 0.25 <= r["f"]["overlap"] < max(theta, 0.6)],
                 "low": [r for r in cand if r["f"]["overlap"] < 0.25]}
        for band, items in bands.items():
            rng.shuffle(items)
            for r in items[:per_band]:
                code = "\n".join(r["code"].splitlines()[:6])
                out[s].append({"band": band, "text": r["text"][:400], "code": code[:500], "overlap": r5(r["f"]["overlap"]),
                               "overlap_ext": r5(r["f"]["overlap_ext"]), "kind": r["kind"], "lang": r["lang"], "repo": r["repo"],
                               "cls": r["cls"], "cues": sorted(r["f"]["cues"]), "hl": metrics.highlight(r["text"][:400], r["code"], lex)})
    return out


def summarize(pool: list[dict], args, lex) -> dict:
    main = [r for r in pool if r["kind"] != "doc"]
    docs = [r for r in pool if r["kind"] == "doc"]
    split = lambda recs: {s: [r for r in recs if r["source"] == s] for s in SOURCES}
    M, D = split(main), split(docs)
    theta = args.theta
    langs = sorted({r["lang"] for r in pool})
    warnings: list[str] = []

    # --- размеры
    repos = {s: len({r["repo"] for r in pool if r["source"] == s}) for s in SOURCES}
    sizes = {s: {"comments": sum(1 for r in pool if r["source"] == s), "main": len(M[s]), "docs": len(D[s]), "repos": repos[s]}
             for s in SOURCES}
    for s in SOURCES:
        if repos[s] < 30:
            warnings.append(f"{s}: всего {repos[s]} репозиториев - интервалы по кластерам будут широкими, выводы предварительные")
        if sizes[s]["main"] < 300:
            warnings.append(f"{s}: всего {sizes[s]['main']} обычных комментариев - мало для устойчивых оценок")

    # --- overlap и функция
    scope = {"all": main}
    scope.update({lang: [r for r in main if r["lang"] == lang] for lang in langs})
    overlap: dict = {"bins": BINS, "hist": {}, "describe": {}}
    for s in SOURCES:
        overlap["hist"][s] = {}
        for name, recs in scope.items():
            overlap["hist"][s][name] = hist_block([r for r in recs if r["source"] == s])
        for key in ("overlap", "overlap_ext"):
            overlap["describe"].setdefault(key, {})[s] = stats.describe(overlap_values(M[s], key))
    tests = {}
    for key in ("overlap", "overlap_ext"):
        mw = stats.mann_whitney(overlap_values(M["ai"], key), overlap_values(M["human"], key))
        mw["effect"] = stats.cliffs_label(mw["cliffs_delta"])
        tests[key] = {k: (r5(v) if isinstance(v, float) else v) for k, v in mw.items()}
    overlap["mann_whitney"] = tests
    overlap["mass_points"] = {s: mass_points(overlap_values(M[s])) for s in SOURCES}

    def cluster(recs, cls_name):
        d: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in recs:
            if r["cls"] is None:
                continue
            d[r["repo"]][1] += 1
            d[r["repo"]][0] += int(r["cls"] == cls_name)
        return {k: (v[0], v[1]) for k, v in d.items()}

    boot = {}
    for cls_name in ("referential", "explanatory"):
        b = stats.cluster_bootstrap_diff(cluster(M["ai"], cls_name), cluster(M["human"], cls_name), iters=args.boot, seed=args.seed)
        boot[cls_name] = {k: (r5(v) if isinstance(v, float) else v) for k, v in b.items()}

    referential = {
        "theta": theta,
        "mix": {s: function_mix(M[s]) for s in SOURCES},
        "mix_ext": {s: function_mix(M[s], "cls_ext") for s in SOURCES},
        "by_lang": {lang: {s: function_mix([r for r in M[s] if r["lang"] == lang]) for s in SOURCES} for lang in langs},
        "sensitivity": [{"theta": t, **{s: r5(share_at(M[s], t)) for s in SOURCES},
                         **{s + "_ext": r5(share_at(M[s], t, "overlap_ext")) for s in SOURCES}} for t in THETA_GRID],
        "bootstrap": boot,
    }
    # Ступенька у порога: если много комментариев лежит ровно на θ, доля referential при θ и при
    # θ + 0.01 сильно различается. Это надо сказать сразу, а не оставлять читателю искать причину.
    for s in SOURCES:
        here, above = share_at(M[s], theta), share_at(M[s], round(theta + 0.01, 4))
        if here is not None and above is not None and here - above >= 0.10:
            exact = sum(1 for r in M[s] if r["f"]["overlap"] is not None and abs(r["f"]["overlap"] - theta) < 1e-9)
            warnings.append(
                f"{s}: доля referential при θ={theta} равна {here:.0%}, а при θ={theta + 0.01:.2f} - {above:.0%}. "
                f"У {exact} комментариев overlap ровно {theta}: overlap - дробь k/n от малого числа слов, "
                f"поэтому на некоторых значениях лежит много комментариев. Смотрите кривую чувствительности "
                f"и калибруйте θ по ручной разметке (validate.py)")
    tools = {}
    for tool in sorted({r["tool"] for r in M["ai"] if r.get("tool")}):
        sub = [r for r in M["ai"] if r.get("tool") == tool]
        tools[tool] = {"n": len(sub), "mean_overlap": r5(stats.mean(overlap_values(sub))), **function_mix(sub)}

    # --- типы комментариев (включая doc)
    kinds = {s: {k: sum(1 for r in pool if r["source"] == s and r["kind"] == k) for k in KINDS} for s in SOURCES}
    kinds_chi = stats.chi2_contingency([[kinds[s][k] for k in KINDS] for s in SOURCES])
    kinds_lang = {lang: {s: {k: sum(1 for r in pool if r["source"] == s and r["lang"] == lang and r["kind"] == k) for k in KINDS}
                         for s in SOURCES} for lang in langs}

    # --- маркеры, идиомы, тональность
    cats = [m.category for m in lex.markers]
    tot = {s: len(M[s]) for s in SOURCES}
    mcounts = presence_rows(M, lambda r: r["f"]["cues"].keys(), cats)
    mrows = compare_rows(mcounts, tot, cats)
    info = {m.category: m for m in lex.markers}
    for row in mrows:
        row["explanatory"] = info[row["label"]].explanatory
        row["note"] = info[row["label"]].note
        row["group"] = info[row["label"]].group
        row["label_ru"] = info[row["label"]].label_ru
    order = {g: i for i, g in enumerate(MARKER_GROUP_ORDER)}
    mrows.sort(key=lambda r: order.get(r["group"], len(order)))

    lemmas = [i.lemma for i in lex.idioms]
    icounts = presence_rows(M, lambda r: r["f"]["idioms"], lemmas)
    irows = compare_rows(icounts, tot, lemmas)
    imeta = {i.lemma: i for i in lex.idioms}
    for row in irows:
        row["category"] = imeta[row["label"]].category
        row["connotation"] = imeta[row["label"]].connotation
        row["note"] = imeta[row["label"]].note
        row["group"] = imeta[row["label"]].group
        row["category_ru"] = imeta[row["label"]].category_ru
    irows = [r for r in irows if r["ai_n"] + r["human_n"] > 0]
    irows.sort(key=lambda r: -(r["ai_n"] + r["human_n"]))

    conn = {s: Counter() for s in SOURCES}
    cat_c = {s: Counter() for s in SOURCES}
    for s in SOURCES:
        for r in M[s]:
            seen_c, seen_k = set(), set()
            for lemma in r["f"]["idioms"]:
                seen_c.add(imeta[lemma].connotation)
                seen_k.add(imeta[lemma].category)
            conn[s].update(seen_c)
            cat_c[s].update(seen_k)

    sent = {}
    for s in SOURCES:
        vals = [r["f"]["sentiment"] for r in M[s]]
        n = len(vals) or 1
        sent[s] = {"mean": r5(stats.mean(vals)), "positive": r5(sum(v >= 0.05 for v in vals) / n),
                   "negative": r5(sum(v <= -0.05 for v in vals) / n),
                   "neutral": r5(sum(-0.05 < v < 0.05 for v in vals) / n)}

    # --- стиль
    style, verb = {}, {}
    for s in SOURCES:
        n = len(M[s]) or 1
        fs = [r["f"] for r in M[s]]
        style[s] = {"mean_words": r5(sum(f["n_words"] for f in fs) / n),
                    "ends_with_period": r5(sum(f["ends_with_period"] for f in fs) / n),
                    "starts_upper": r5(sum(f["starts_upper"] for f in fs) / n),
                    "question": r5(sum(f["is_question"] for f in fs) / n),
                    "has_url": r5(sum(f["has_url"] for f in fs) / n),
                    "has_issue_ref": r5(sum(f["has_issue_ref"] for f in fs) / n)}
        vc = Counter(f["verb_form"] or "none" for f in fs)
        verb[s] = {k: r5(vc[k] / n) for k in ("imperative", "third_person", "gerund", "none")}

    # --- документация отдельным блоком
    docs_block = {s: {"n": len(D[s]), "overlap": stats.describe(overlap_values(D[s])), **function_mix(D[s]),
                      "verb_third_person": r5(sum(r["f"]["verb_form"] == "third_person" for r in D[s]) / len(D[s])) if D[s] else None}
                  for s in SOURCES}

    words = word_stats(M, lex, args.top, args.min_word_count)

    return {
        "sizes": sizes, "langs": langs, "overlap": overlap, "referential": referential, "tools": tools,
        "kinds": {"counts": kinds, "chi2": {k: (r5(v) if isinstance(v, float) else v) for k, v in kinds_chi.items()},
                  "by_lang": kinds_lang},
        "markers": mrows, "idioms": irows, "groups": lex.groups,
        "connotation": {s: {k: r5(1000 * conn[s][k] / tot[s]) if tot[s] else None for k in ("negative", "neutral", "positive")} for s in SOURCES},
        "idiom_categories": {s: {k: r5(1000 * v / tot[s]) if tot[s] else None for k, v in cat_c[s].items()} for s in SOURCES},
        "sentiment": sent, "style": style, "verb_form": verb, "docs": docs_block, "words": words,
        "warnings": warnings,
    }


# --- вывод -----------------------------------------------------------------------------------

# --- плотность комментариев -------------------------------------------------------------------

DENSITY_KINDS = ("inline", "leading", "block", "detached", "doc")


def _density_block(rows: list[dict]) -> dict:
    """Плотность по группе файлов: отношение СУММ (комментарии / добавленные строки), а не среднее
    плотностей по файлам. Среднее по файлам отдало бы коротким файлам (одна добавленная строка и один
    комментарий = 100 на 100) тот же вес, что и файлу в тысячу строк."""
    lines = sum(r["added_lines"] for r in rows)
    nonblank = sum(r["added_nonblank"] for r in rows)
    com = sum(r["comments"] for r in rows)
    kinds: Counter = Counter()
    for r in rows:
        kinds.update(r["by_kind"])
    per = lambda n, d: r5(100 * n / d) if d else None  # noqa: E731
    by_repo: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        by_repo[r["repo"]][0] += r["comments"]
        by_repo[r["repo"]][1] += r["added_lines"]
    repo_dens = sorted(100 * c / n for c, n in by_repo.values() if n)
    return {
        "files": len(rows), "repos": len(by_repo), "added_lines": lines, "added_nonblank": nonblank,
        "comments": com, "comment_lines": sum(r["comment_lines"] for r in rows),
        "per100": per(com, lines),                   # главный показатель: комментариев на 100 добавленных строк
        "per100_nonblank": per(com, nonblank),       # то же, но на 100 НЕПУСТЫХ строк (не зависит от манеры ставить пустые строки)
        "line_share": r5(sum(r["comment_lines"] for r in rows) / lines) if lines else None,   # доля строк, занятых комментариями
        "main_per100": per(com - kinds.get("doc", 0), lines),
        "doc_per100": per(kinds.get("doc", 0), lines),
        "by_kind_per100": {k: per(kinds.get(k, 0), lines) for k in DENSITY_KINDS},
        "repo_median_per100": r5(stats.quantile(repo_dens, 0.5)) if repo_dens else None,
    }


def compute_density(path: Path, boot: int, seed: int) -> dict:
    """Плотность комментариев AI против human. Источник - file_stats.jsonl, который пишет extract_comments.

    Определение: число комментариев на 100 строк, ДОБАВЛЕННЫХ коммитами (для обоих корпусов одинаково).
    В числитель входят комментарии, целиком добавленные коммитом, без лицензий, директив, закомментированного
    кода и разделителей; в знаменатель - все добавленные строки файлов поддерживаемых языков."""
    if not path.exists():
        return {"available": False, "reason": f"нет {path.name}: пересоберите комментарии свежим extract_comments.py"}
    rows = [r for r in read_jsonl(path) if r["source"] in SOURCES]
    if not rows:
        return {"available": False, "reason": "file_stats.jsonl пуст"}

    per_source = {s: _density_block([r for r in rows if r["source"] == s]) for s in SOURCES}
    langs = sorted({r["lang"] for r in rows})
    by_lang = {lang: {s: _density_block([r for r in rows if r["source"] == s and r["lang"] == lang]) for s in SOURCES} for lang in langs}
    tools = sorted({r["tool"] for r in rows if r["source"] == "ai" and r["tool"]})
    by_tool = {t: _density_block([r for r in rows if r["source"] == "ai" and r["tool"] == t]) for t in tools}

    def clusters(src: str, drop_doc: bool) -> dict[str, tuple[int, int]]:
        d: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            if r["source"] == src:
                d[r["repo"]][0] += r["comments"] - (r["by_kind"].get("doc", 0) if drop_doc else 0)
                d[r["repo"]][1] += r["added_lines"]
        return {k: (v[0], v[1]) for k, v in d.items()}

    boot_res = {}
    for name, drop in (("all", False), ("main", True)):
        b = stats.cluster_bootstrap_diff(clusters("ai", drop), clusters("human", drop), iters=boot, seed=seed)
        # бутстреп возвращает «комментариев на строку»: привожу к «на 100 строк»
        for k in ("diff", "lo", "hi", "share_a", "share_b"):
            b[k] = r5(100 * b[k]) if b[k] is not None else None
        for k in ("ratio", "ratio_lo", "ratio_hi"):
            b[k] = r5(b[k]) if b[k] is not None else None
        boot_res[name] = b
    return {"available": True, "per_source": per_source, "by_lang": by_lang, "by_tool": by_tool, "bootstrap": boot_res}


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: Excel на Windows читает кириллицу
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def print_report(summ: dict, composition: dict) -> None:
    ref = summ["referential"]
    mw = summ["overlap"]["mann_whitney"]["overlap"]
    boot = ref["bootstrap"]["referential"]
    d = summ["overlap"]["describe"]["overlap"]
    print("\n=== Итоги ===")
    for s in SOURCES:
        z = summ["sizes"][s]
        print(f"{s:>5}: комментариев {z['main']} (+{z['docs']} doc), репозиториев {z['repos']}, "
              f"средний overlap {d[s]['mean']:.3f}, медиана {d[s]['median']:.3f}"
              if d[s]["n"] else f"{s:>5}: нет данных для overlap")
    if ref["mix"]["ai"]["classified"] and ref["mix"]["human"]["classified"]:
        ci = (f"95% CI по репозиториям [{boot['lo']:+.3f}; {boot['hi']:+.3f}]" if boot["lo"] is not None
              else "CI не определён: нужно хотя бы 2 репозитория в каждой группе")
        print(f"Доля referential при θ={ref['theta']}: AI {ref['mix']['ai']['referential_share']:.3f} "
              f"| human {ref['mix']['human']['referential_share']:.3f} | разница {boot['diff']:+.3f} ({ci})")
        p_txt = "< 0.001" if mw["p"] < 0.001 else f"= {mw['p']:.3f}"
        print(f"Mann-Whitney по overlap: p {p_txt}, Cliff's δ={mw['cliffs_delta']:+.3f} ({mw['effect']})")
    dn = summ["density"]
    if dn["available"]:
        a, h = dn["per_source"]["ai"], dn["per_source"]["human"]
        b = dn["bootstrap"]["all"]
        ci = (f", отношение AI/человек ×{b['ratio']:.2f} (95% CI [{b['ratio_lo']:.2f}; {b['ratio_hi']:.2f}])" if b["ratio_lo"] is not None
              else (f", отношение AI/человек ×{b['ratio']:.2f} (CI не определён: мало репозиториев)" if b["ratio"] is not None else ""))
        print(f"Плотность комментариев на 100 добавленных строк: AI {a['per100']:.1f} ({a['comments']} комм. / {a['added_lines']} строк) "
              f"| human {h['per100']:.1f} ({h['comments']} / {h['added_lines']}){ci}")
    else:
        print(f"Плотность комментариев не посчитана: {dn['reason']}")
    for w in summ["warnings"]:
        print(f"  ⚠ {w}")


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Анализ корпуса комментариев")
    ap.add_argument("--corpus", default="./corpus", help="каталог с comments.jsonl")
    ap.add_argument("--comments", default=None, help="путь к comments.jsonl (по умолчанию <corpus>/comments.jsonl)")
    ap.add_argument("--results", default="./results")
    ap.add_argument("--theta", type=float, default=DEFAULT_THETA, help="порог overlap для referential (по умолчанию 0.5)")
    ap.add_argument("--max-per-repo", type=int, default=500, help="потолок комментариев на репозиторий")
    ap.add_argument("--min-per-repo", type=int, default=1, help="репозитории с меньшим числом комментариев отбрасываются")
    ap.add_argument("--balance", action="store_true", help="выровнять число комментариев по языкам между AI и human")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--boot", type=int, default=2000, help="итераций кластерного бутстрепа")
    ap.add_argument("--top", type=int, default=80, help="слов в облаке")
    ap.add_argument("--min-word-count", type=int, default=5, help="слова реже этого в характерных не учитываются")
    ap.add_argument("--examples", type=int, default=40, help="примеров на полосу overlap и источник")
    ap.add_argument("--file-stats", default=None, help="file_stats.jsonl для плотности комментариев (по умолчанию рядом с comments.jsonl)")
    ap.add_argument("--demo", action="store_true", help="пометить результат как демонстрационный")
    args = ap.parse_args(argv)

    path = Path(args.comments) if args.comments else Path(args.corpus) / "comments.jsonl"
    if not path.exists():
        raise SystemExit(f"Нет {path}. Сначала: python extract_comments.py --corpus {args.corpus}")
    out = Path(args.results)
    out.mkdir(parents=True, exist_ok=True)

    lex = metrics.load_lexicon()
    pool, composition = build_pool(path, args, lex)
    if not pool:
        raise SystemExit("После отбора не осталось комментариев. Проверьте corpus/extract_stats.json.")
    print(f"В анализе: {len(pool)} комментариев. Считаю признаки...")
    for r in pool:
        r["f"] = metrics.analyze(r["text"], r.get("code"), lex)
        r["cls"] = metrics.classify(r["f"], args.theta)
        r["cls_ext"] = metrics.classify(r["f"], args.theta, "overlap_ext")

    summ = summarize(pool, args, lex)
    fs_path = Path(args.file_stats) if args.file_stats else path.with_name("file_stats.jsonl")
    summ["density"] = compute_density(fs_path, args.boot, args.seed)
    summ.update({
        "schema": SCHEMA_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "demo": bool(args.demo), "theta": args.theta, "min_content_words": MIN_CONTENT_WORDS,
        "nlp_backend": metrics.nlp_backend(), "balanced": bool(args.balance), "composition": composition,
        "params": {k: getattr(args, k) for k in ("max_per_repo", "min_per_repo", "seed", "boot", "code_window") if hasattr(args, k)},
    })
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "examples.json").write_text(json.dumps(make_examples(pool, args.theta, args.examples, lex, args.seed), ensure_ascii=False),
                                       encoding="utf-8")

    with open(out / "pool.jsonl", "w", encoding="utf-8") as f:  # для validate.py: слепая выборка на ручную разметку
        for r in pool:
            f.write(json.dumps({"id": r["id"], "source": r["source"], "lang": r["lang"], "kind": r["kind"], "repo": r["repo"],
                                "file": r["file"], "line": r["start_line"], "text": r["text"], "code": (r.get("code") or "")[:800],
                                "overlap": r5(r["f"]["overlap"]), "cue": r["f"]["explanatory_cue"], "cls": r["cls"]},
                               ensure_ascii=False) + "\n")

    write_csv(out / "idioms.csv", summ["idioms"], ["label", "category", "category_ru", "group", "connotation", "ai_n", "human_n", "ai_per1k", "human_per1k", "odds_ratio", "p", "q", "note"])
    write_csv(out / "markers.csv", summ["markers"], ["label", "label_ru", "group", "explanatory", "ai_n", "human_n", "ai_per1k", "human_per1k", "odds_ratio", "p", "q", "note"])
    write_csv(out / "overlap_sensitivity.csv", summ["referential"]["sensitivity"], ["theta", "ai", "human", "ai_ext", "human_ext"])
    for s in SOURCES:
        write_csv(out / f"words_{s}.csv", [{"word": w, "count": c} for w, c in summ["words"]["freq"][s]], ["word", "count"])
    dn = summ["density"]
    if dn["available"]:
        drows = [{"scope": "все языки", "source": s, **dn["per_source"][s]} for s in SOURCES]
        drows += [{"scope": lang, "source": s, **d[s]} for lang, d in dn["by_lang"].items() for s in SOURCES]
        drows += [{"scope": f"tool:{t}", "source": "ai", **d} for t, d in dn["by_tool"].items()]
        write_csv(out / "density.csv", drows, ["scope", "source", "files", "repos", "added_lines", "added_nonblank", "comments",
                                               "comment_lines", "per100", "per100_nonblank", "line_share", "main_per100", "doc_per100",
                                               "repo_median_per100"])

    print_report(summ, composition)
    print(f"\nРезультаты: {out}/summary.json, examples.json, *.csv")


if __name__ == "__main__":
    main()
