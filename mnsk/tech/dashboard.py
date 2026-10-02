#!/usr/bin/env python3
"""
dashboard.py - results/summary.json + examples.json -> один автономный dashboard.html.

Один файл, а не приложение: страницу можно открыть двойным кликом, приложить к отчёту
МНСК, отправить научному руководителю. Данные вшиты внутрь, внешних запросов нет
(ни CDN, ни шрифтов с чужих серверов), поэтому она работает без интернета.
Шаблон лежит в dashboard_template.html: интерфейс правится без знания Python.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

from config import HUMAN_CUTOFF, ROOT, setup_console

MARKER = "/*__DATA__*/null"
FONTS_MARKER = "/*__FONTS__*/"
FONTS_DIR = ROOT / "assets" / "fonts"
FONT_MIME = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf"}


def font_css(fonts_dir: Path = FONTS_DIR) -> str:
    """assets/fonts/fonts.css с файлами шрифтов, вшитыми как data: URI.

    Список начертаний живёт в fonts.css, а не в коде: так можно поменять шрифт или добавить
    алфавит (unicode-range), не трогая Python. Если fonts.css или файла из него нет, страница
    берёт следующий шрифт из списка в CSS шаблона (Segoe UI, Consolas), остальное работает так же.
    """
    css_p = fonts_dir / "fonts.css"
    if not css_p.exists():
        return ""

    def inline(m: re.Match) -> str:
        name = m.group(1).strip().strip("\"'")
        if name.startswith("data:"):
            return m.group(0)
        path = fonts_dir / name
        if not path.is_file() or path.suffix.lower() not in FONT_MIME:
            # Пустой data: вместо ссылки на отсутствующий файл: иначе браузер полез бы за ним
            # рядом со страницей, а страница обещает ни одного запроса наружу.
            return "url(data:,)"
        return f"url(data:{FONT_MIME[path.suffix.lower()]};base64,{base64.b64encode(path.read_bytes()).decode('ascii')})"

    return re.sub(r"url\(([^)]+)\)", inline, css_p.read_text(encoding="utf-8"))


def build(results: Path, out: Path, template: Path | None = None) -> Path:
    summary_p, examples_p = results / "summary.json", results / "examples.json"
    if not summary_p.exists():
        raise SystemExit(f"Нет {summary_p}. Сначала: python counter.py")
    summary = json.loads(summary_p.read_text(encoding="utf-8"))
    summary.setdefault("human_cutoff", HUMAN_CUTOFF)
    # В результатах, посчитанных до научной классификации маркеров и идиом, её полей нет:
    # подставляю те же значения, что load_lexicon() даёт категории без классификатора.
    summary.setdefault("groups", {})
    for row in summary.get("markers", []):
        row.setdefault("group", "")
        row.setdefault("label_ru", row["label"])
    for row in summary.get("idioms", []):
        row.setdefault("group", "")
        row.setdefault("category_ru", row["category"])
    examples = json.loads(examples_p.read_text(encoding="utf-8")) if examples_p.exists() else {"ai": [], "human": []}

    html = (template or ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    if MARKER not in html:
        raise SystemExit("В шаблоне нет маркера данных /*__DATA__*/null")
    # Шрифты подставляю раньше данных: в данных (тексты комментариев) может встретиться что угодно,
    # в том числе строка, похожая на метку шрифтов.
    html = html.replace(FONTS_MARKER, font_css(), 1)
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
