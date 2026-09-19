import csv
import json
import random

import pytest

import validate


def make_pool(tmp_path, n=400):
    rng = random.Random(5)
    rows = []
    for i in range(n):
        src = "ai" if i % 2 else "human"
        ov = round(rng.random(), 3)
        cue = rng.random() < 0.2
        rows.append({"id": f"id{i}", "source": src, "lang": "python", "kind": "leading", "repo": "r", "file": "f.py", "line": i,
                     "text": f"comment {i}", "code": "x = 1", "overlap": ov, "cue": cue,
                     "cls": "explanatory" if cue else ("referential" if ov >= 0.5 else "other")})
    (tmp_path / "pool.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def label_file(sample_path, key_path, out, noise=0.0, seed=1):
    rng = random.Random(seed)
    key = {r["id"]: r for r in csv.DictReader(open(key_path, encoding="utf-8-sig"))}
    rows = list(csv.DictReader(open(sample_path, encoding="utf-8-sig")))
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "lang", "comment", "code", "label", "note"])
        for r in rows:
            k = key[r["id"]]
            lab = validate.rule_label(k, 0.5)
            if rng.random() < noise:
                lab = rng.choice(list(validate.LABELS))
            w.writerow([r["id"], r["lang"], r["comment"], r["code"], lab, ""])
    return out


def test_sample_is_blind_and_stratified(tmp_path):
    make_pool(tmp_path)
    out, key = validate.sample(tmp_path, 60, 1, None, None)
    header = out.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "source" not in header and "overlap" not in header      # разметчик не видит источник и метрику
    rows = list(csv.DictReader(open(key, encoding="utf-8-sig")))
    assert len(rows) == 60
    by = {(r["source"], "low" if float(r["overlap"]) < .25 else "mid" if float(r["overlap"]) < .6 else "high") for r in rows}
    assert len(by) == 6                                              # все шесть страт представлены
    order = [r["id"] for r in csv.DictReader(open(out, encoding="utf-8-sig"))]
    assert order == [r["id"] for r in rows]                          # ключ и выборка согласованы построчно


def test_perfect_labels_give_perfect_agreement(tmp_path):
    make_pool(tmp_path)
    out, key = validate.sample(tmp_path, 120, 1, None, None)
    lab = label_file(out, key, tmp_path / "lab.csv")
    res = validate.evaluate(tmp_path, lab, None, 0.5, key)
    assert res["accuracy"] == 1.0 and res["kappa_rule_vs_human"] == 1.0
    assert res["best_theta_for_referential"]["f1"] == 1.0


def test_noisy_labels_lower_agreement_and_second_annotator_kappa(tmp_path):
    make_pool(tmp_path)
    out, key = validate.sample(tmp_path, 180, 1, None, None)
    a = label_file(out, key, tmp_path / "a.csv", noise=0.25, seed=1)
    b = label_file(out, key, tmp_path / "b.csv", noise=0.25, seed=2)
    res = validate.evaluate(tmp_path, a, b, 0.5, key)
    assert 0.4 < res["accuracy"] < 0.95
    assert -0.2 < res["annotators"]["kappa"] < 0.95


def test_eval_recovers_shifted_threshold(tmp_path):
    """Если люди считают пересказом всё от 0.7, подобранный θ должен уйти к 0.7."""
    make_pool(tmp_path)
    out, key = validate.sample(tmp_path, 180, 1, None, None)
    keyrows = {r["id"]: r for r in csv.DictReader(open(key, encoding="utf-8-sig"))}
    lab = tmp_path / "shift.csv"
    with open(lab, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "lang", "comment", "code", "label", "note"])
        for r in csv.DictReader(open(out, encoding="utf-8-sig")):
            w.writerow([r["id"], r["lang"], r["comment"], r["code"], validate.rule_label(keyrows[r["id"]], 0.7), ""])
    res = validate.evaluate(tmp_path, lab, None, 0.5, key)
    assert abs(res["best_theta_for_referential"]["theta"] - 0.7) <= 0.04


def test_too_few_labels_is_refused(tmp_path):
    make_pool(tmp_path)
    out, key = validate.sample(tmp_path, 60, 1, None, None)
    lab = label_file(out, key, tmp_path / "l.csv")
    rows = list(csv.reader(open(lab, encoding="utf-8-sig")))
    with open(lab, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows[:6])
    with pytest.raises(SystemExit, match="Размечено только"):
        validate.evaluate(tmp_path, lab, None, 0.5, key)


def test_label_aliases_are_accepted(tmp_path):
    p = tmp_path / "l.csv"
    p.write_text("id,label\na,r\nb,E\nc,Other\nd,\ne,unknown\n", encoding="utf-8")
    assert validate.read_labels(p) == {"a": "referential", "b": "explanatory", "c": "other"}
