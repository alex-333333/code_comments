#!/usr/bin/env python3
"""
dashboard.py - results/summary.json + examples.json -> один автономный dashboard.html.

Один файл, а не приложение: страницу можно открыть двойным кликом, приложить к отчёту
МНСК, отправить научному руководителю. Данные вшиты внутрь, внешних запросов нет
(ни CDN, ни шрифтов), поэтому она работает без интернета и не зависит от чужих серверов.
Шаблон лежит в dashboard_template.html: интерфейс правится без знания Python.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from config import HUMAN_CUTOFF, ROOT, setup_console

MARKER = "/*__DATA__*/null"


def build(results: Path, out: Path, template: Path | None = None) -> Path:
    summary_p, examples_p = results / "summary.json", results / "examples.json"
    if not summary_p.exists():
        raise SystemExit(f"Нет {summary_p}. Сначала: python counter.py")
    summary = json.loads(summary_p.read_text(encoding="utf-8"))
    summary.setdefault("human_cutoff", HUMAN_CUTOFF)
    examples = json.loads(examples_p.read_text(encoding="utf-8")) if examples_p.exists() else {"ai": [], "human": []}

    html = (template or ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    if MARKER not in html:
        raise SystemExit("В шаблоне нет маркера данных /*__DATA__*/null")
    payload = json.dumps({"summary": summary, "examples": examples}, ensure_ascii=False)
    # Данные лежат внутри <script>: последовательность "</" преждевременно закрыла бы тег,
    # а U+2028/2029 - валидный JSON, но перенос строки в JavaScript.
    payload = payload.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html.replace(MARKER, payload, 1), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Сборка dashboard.html из результатов анализа")
    ap.add_argument("--results", default="./results")
    ap.add_argument("--out", default="./dashboard.html")
    ap.add_argument("--template", default=None)
    args = ap.parse_args(argv)
    out = build(Path(args.results), Path(args.out), Path(args.template) if args.template else None)
    size = out.stat().st_size / 1024
    print(f"Дашборд: {out} ({size:.0f} КБ). Откройте файл в браузере.")


if __name__ == "__main__":
    main()
