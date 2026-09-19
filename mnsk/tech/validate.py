#!/usr/bin/env python3
"""
validate.py - проверка самой метрики на ручной разметке.

Зачем. Вся работа держится на утверждении «overlap >= θ означает, что комментарий пересказывает
код». Это операционализация, и жюри вправе спросить, а совпадает ли она с тем, что человек
называет пересказом. Ответ измеряется, а не заявляется:

  1. `sample` выгружает СЛЕПУЮ выборку (источник AI/human скрыт, порядок перемешан), поровну из
     трёх полос overlap и из двух источников. Я (а лучше ещё один человек независимо) размечаю её
     как referential / explanatory / other.
  2. `eval` сравнивает правило с разметкой: точность, F1, κ Коэна (правило против человека,
     а при двух разметчиках - и человек против человека), и подбирает θ, при котором правило
     лучше всего совпадает с людьми.

Слепота важна: зная источник, разметчик невольно увидит «AI-стиль» и подгонит ответ под гипотезу.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from pathlib import Path

import stats
from config import DEFAULT_THETA, setup_console

LABELS = ("referential", "explanatory", "other")
ALIASES = {"r": "referential", "ref": "referential", "referential": "referential", "пересказ": "referential", "п": "referential",
           "e": "explanatory", "expl": "explanatory", "explanatory": "explanatory", "объяснение": "explanatory", "о": "explanatory",
           "o": "other", "other": "other", "другое": "other", "д": "other"}
BANDS = (("low", 0.0, 0.25), ("mid", 0.25, 0.6), ("high", 0.6, 1.01))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def sample(results: Path, n: int, seed: int, out: Path | None, key_out: Path | None) -> tuple[Path, Path]:
    pool_p = results / "pool.jsonl"
    if not pool_p.exists():
        raise SystemExit(f"Нет {pool_p}. Сначала: python counter.py")
    pool = [r for r in _read_jsonl(pool_p) if r["kind"] != "doc" and r["code"] and r["cls"] is not None and r["overlap"] is not None]
    rng = random.Random(seed)
    per = max(1, n // (len(BANDS) * 2))
    picked: list[dict] = []
    for src in ("ai", "human"):
        for name, lo, hi in BANDS:
            cand = [r for r in pool if r["source"] == src and lo <= r["overlap"] < hi]
            rng.shuffle(cand)
            picked += cand[:per]
    rng.shuffle(picked)   # порядок не выдаёт ни источник, ни полосу

    out = out or results / "gold_sample.csv"
    key_out = key_out or results / "gold_key.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "lang", "comment", "code", "label", "note"])
        for r in picked:
            w.writerow([r["id"], r["lang"], r["text"], r["code"][:600], "", ""])
    with open(key_out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "source", "overlap", "cue", "cls", "file", "line"])
        for r in picked:
            w.writerow([r["id"], r["source"], r["overlap"], int(r["cue"]), r["cls"], r["file"], r["line"]])
    return out, key_out


def read_labels(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            lab = ALIASES.get((row.get("label") or "").strip().lower())
            if lab:
                out[row["id"]] = lab
    return out


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def rule_label(row: dict, theta: float) -> str:
    if int(row["cue"]):
        return "explanatory"
    return "referential" if float(row["overlap"]) >= theta - 1e-12 else "other"


def evaluate(results: Path, labels_p: Path, labels2_p: Path | None, theta: float, key_p: Path | None) -> dict:
    key_p = key_p or results / "gold_key.csv"
    with open(key_p, encoding="utf-8-sig", newline="") as f:
        key = {r["id"]: r for r in csv.DictReader(f)}
    man = read_labels(labels_p)
    ids = [i for i in man if i in key]
    if len(ids) < 20:
        raise SystemExit(f"Размечено только {len(ids)} комментариев - для выводов нужно хотя бы 100 (лучше 200).")

    y = [man[i] for i in ids]
    pred = [rule_label(key[i], theta) for i in ids]
    conf = {a: {b: 0 for b in LABELS} for a in LABELS}
    for m, p in zip(y, pred):
        conf[m][p] += 1
    acc = sum(m == p for m, p in zip(y, pred)) / len(y)
    per_class = {c: prf(conf[c][c], sum(conf[o][c] for o in LABELS if o != c), sum(conf[c][o] for o in LABELS if o != c)) for c in LABELS}

    # Основная ось гипотезы - referential против всего остального: по ней ищу θ.
    y_bin = [m == "referential" for m in y]
    scan = []
    for i in range(20, 91, 2):
        t = i / 100
        pb = [rule_label(key[j], t) == "referential" for j in ids]
        tp = sum(a and b for a, b in zip(y_bin, pb)); fp = sum((not a) and b for a, b in zip(y_bin, pb)); fn = sum(a and (not b) for a, b in zip(y_bin, pb))
        scan.append({"theta": t, **prf(tp, fp, fn)})
    best = max(scan, key=lambda r: (r["f1"], -abs(r["theta"] - DEFAULT_THETA)))

    res = {"n": len(ids), "theta": theta, "accuracy": round(acc, 3), "kappa_rule_vs_human": round(stats.cohen_kappa(y, pred) or 0, 3),
           "confusion_human_rows_rule_cols": conf, "per_class": per_class, "best_theta_for_referential": best,
           "manual_distribution": dict(Counter(y)), "theta_scan": scan}
    if labels2_p:
        m2 = read_labels(labels2_p)
        both = [i for i in ids if i in m2]
        res["annotators"] = {"n": len(both), "kappa": round(stats.cohen_kappa([man[i] for i in both], [m2[i] for i in both]) or 0, 3),
                             "agreement": round(sum(man[i] == m2[i] for i in both) / len(both), 3) if both else None}
    return res


KAPPA_WORDS = [(0.8, "почти полное согласие"), (0.6, "существенное"), (0.4, "умеренное"), (0.2, "слабое"), (-1, "случайное или хуже")]


def kappa_word(k: float) -> str:
    return next(w for lim, w in KAPPA_WORDS if k >= lim)


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Слепая выборка на ручную разметку и калибровка порога θ")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="выгрузить слепую выборку для разметки")
    s.add_argument("--results", default="./results"); s.add_argument("--n", type=int, default=200)
    s.add_argument("--seed", type=int, default=42); s.add_argument("--out"); s.add_argument("--key")
    e = sub.add_parser("eval", help="сравнить правило с ручной разметкой")
    e.add_argument("--results", default="./results"); e.add_argument("--labels", required=True, help="CSV с заполненной колонкой label")
    e.add_argument("--labels2", help="CSV второго разметчика (для κ между людьми)")
    e.add_argument("--theta", type=float, default=DEFAULT_THETA); e.add_argument("--key")
    args = ap.parse_args(argv)

    if args.cmd == "sample":
        out, key = sample(Path(args.results), args.n, args.seed, Path(args.out) if args.out else None, Path(args.key) if args.key else None)
        print(f"Выборка для разметки: {out}\nКлюч (НЕ открывать до конца разметки): {key}\n"
              "В колонке label поставьте: referential (r) - комментарий пересказывает код; explanatory (e) - объясняет причину, "
              "цель, ограничение; other (o) - остальное.")
        return
    res = evaluate(Path(args.results), Path(args.labels), Path(args.labels2) if args.labels2 else None, args.theta, Path(args.key) if args.key else None)
    (Path(args.results) / "validation.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    b = res["best_theta_for_referential"]
    print(f"Размечено: {res['n']}. Правило при θ={res['theta']}: точность {res['accuracy']:.1%}, κ (правило-человек) = "
          f"{res['kappa_rule_vs_human']} ({kappa_word(res['kappa_rule_vs_human'])}).")
    for c, m in res["per_class"].items():
        print(f"  {c:12} precision {m['precision']:.2f}  recall {m['recall']:.2f}  F1 {m['f1']:.2f}")
    print(f"θ, лучше всего совпадающий с людьми по referential: {b['theta']} (F1 {b['f1']}).")
    if "annotators" in res:
        a = res["annotators"]
        print(f"Согласие разметчиков: κ = {a['kappa']} ({kappa_word(a['kappa'])}), совпало {a['agreement']:.1%} на {a['n']} комментариях.")
    print(f"Подробности: {Path(args.results) / 'validation.json'}")


if __name__ == "__main__":
    main()
