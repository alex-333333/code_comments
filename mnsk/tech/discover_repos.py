#!/usr/bin/env python3
"""
discover_repos.py - поиск репозиториев-кандидатов для корпусов.

Только обнаружение (какие репозитории вообще стоит клонировать), не финальный
источник данных: индекс Search API неполный и может отставать от реального
состояния GitHub. Финальная фильтрация коммитов всё равно происходит локально
в parser.py через git log.

Три режима:

  ai      репозитории, где GitHub нашёл коммиты с AI-трейлером (config.AI_TRAILERS).
          -> ai_repos.txt + ai_repos.jsonl (с языком, звёздами, размером, датой создания)

  human   популярные репозитории, СОЗДАННЫЕ до HUMAN_CUTOFF, по языкам - с квотами,
          повторяющими языковой состав AI-выборки (--match ai_repos.jsonl).
          -> human_repos.txt

  paired  подмножество AI-репозиториев, у которых есть история до cutoff. Человеческие и
          AI-комментарии тогда берутся из ОДНОГО проекта - тот же стиль, команда, язык, -
          и различие нельзя списать на «разные проекты». Самый сильный дизайн, но выборка
          меньше: многие AI-репозитории созданы уже после 2020.
          -> paired_repos.txt

Токен: переменная окружения GITHUB_TOKEN. В PowerShell:  $env:GITHUB_TOKEN="ghp_..."
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from config import AI_TRAILERS, HUMAN_CUTOFF, LANG_LABELS, setup_console

API = "https://api.github.com"
REQUEST_TIMEOUT = 60            # без таймаута зависший сокет останавливает многочасовой поиск навсегда
SEARCH_PAUSE = 2.1              # Search API: 30 запросов/мин с токеном, 10/мин без
MAX_RETRIES = 6
SEARCH_CAP = 1000               # Search API отдаёт не более 1000 результатов на запрос

# Языки GitHub для human-режима: соответствуют расширениям из config.EXT_TO_LANG.
GITHUB_LANGS = ["Python", "JavaScript", "TypeScript", "Java", "C", "C++", "Go", "Rust"]


class GitHub:
    """Минимальный клиент: заголовки, таймаут, ретраи по лимитам."""

    def __init__(self, token: str | None, session: requests.Session | None = None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.sleep = sleep
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET с повтором. Ключевое: при 403/429 повторяется ТА ЖЕ страница.
        В прежней версии `continue` внутри `for page in range(...)` переходил к следующей
        странице, и страница, на которой сработал лимит, терялась навсегда."""
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout):
                self.sleep(min(60, 2 ** attempt * 2))
                continue
            if r.status_code in (403, 429) and ("rate limit" in r.text.lower() or r.headers.get("Retry-After")
                                                 or r.headers.get("X-RateLimit-Remaining") == "0"):
                wait = self._retry_delay(r, attempt)
                print(f"  лимит запросов, жду {wait:.0f} с...", file=sys.stderr)
                self.sleep(wait)
                continue
            if r.status_code >= 500:
                self.sleep(min(60, 2 ** attempt * 2))
                continue
            if r.status_code == 422:
                raise ValueError(f"GitHub отклонил запрос: {r.text[:300]}")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"не удалось получить {url} за {MAX_RETRIES} попыток")

    @staticmethod
    def _retry_delay(r: requests.Response, attempt: int) -> float:
        if r.headers.get("Retry-After", "").isdigit():
            return float(r.headers["Retry-After"]) + 1
        reset = r.headers.get("X-RateLimit-Reset", "")
        if reset.isdigit():
            return max(1.0, min(3600.0, int(reset) - time.time() + 1))
        return min(120.0, 15.0 * 2 ** attempt)


# --- AI-режим -------------------------------------------------------------------

def _phrase(trailer: str) -> str:
    # Кавычки нужны: без них Search API ищет слова по отдельности, а не фразу трейлера.
    return f'"{trailer}"'


def make_windows(since: date, until: date, hours: int) -> list[tuple[datetime, datetime]]:
    """Окна [начало, конец] шириной `hours` часов, подряд, без пересечений.

    Почему часы, а не дни: на реальных данных трейлер Claude даёт ~11 000 коммитов за 3 дня, а
    один поисковый запрос отдаёт не более 1000. Шестичасовое окно укладывается в потолок
    (~700 коммитов), сутки - нет."""
    out = []
    cur = datetime(since.year, since.month, since.day)
    end = datetime(until.year, until.month, until.day) + timedelta(days=1)
    step = timedelta(hours=hours)
    while cur < end:
        out.append((cur, min(cur + step, end) - timedelta(seconds=1)))
        cur += step
    return out


def _stamp(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def search_window(gh: GitHub, trailer: str, start: datetime, end: datetime, found: dict[str, dict],
                  max_pages: int) -> int:
    """Репозитории коммитов с трейлером в окне. Возвращает total_count окна.

    Все коммиты окна не перечисляются: мне нужны РЕПОЗИТОРИИ, а не коммиты, и первых страниц
    хватает. Если total_count больше потолка, окно просто недоступно целиком - это нормально
    для выборки, но фиксируется в статистике."""
    q = f"{_phrase(trailer)} committer-date:{_stamp(start)}..{_stamp(end)}"
    total = 0
    for page in range(1, max_pages + 1):
        data = gh.get("/search/commits", {"q": q, "per_page": 100, "page": page})
        gh.sleep(SEARCH_PAUSE)
        got = data.get("items", [])
        total = data.get("total_count", 0)
        for item in got:
            repo = item["repository"]
            found.setdefault(repo["full_name"], {"full_name": repo["full_name"], "fork": repo.get("fork", False)})
        if len(got) < 100 or page * 100 >= min(total, SEARCH_CAP):
            break
    return total


def discover_ai(gh: GitHub, since: date, until: date, window_hours: int, pages: int, target: int,
                seed: int) -> tuple[dict[str, dict], dict]:
    """Окна обходятся в СЛУЧАЙНОМ порядке и поиск останавливается, как только набрано достаточно
    репозиториев. Обход по порядку остановился бы на самых ранних окнах и дал бы выборку из
    одного месяца; случайный порядок даёт равномерное покрытие всего периода.
    По каждому окну опрашиваются все трейлеры по очереди."""
    windows = make_windows(since, until, window_hours)
    random.Random(seed).shuffle(windows)
    found: dict[str, dict] = {}
    stat = {"windows_total": len(windows), "windows_used": 0, "windows_over_cap": 0}
    for i, (a, b) in enumerate(windows, 1):
        for trailer in AI_TRAILERS:
            if search_window(gh, trailer, a, b, found, pages) > SEARCH_CAP:
                stat["windows_over_cap"] += 1
        stat["windows_used"] = i
        if i % 10 == 0:
            print(f"  окон обработано: {i}/{len(windows)}, репозиториев найдено: {len(found)}")
        if len(found) >= target:
            break
    return found, stat


# --- метаданные -----------------------------------------------------------------

def fetch_meta(gh: GitHub, names: list[str], cache_path: Path) -> dict[str, dict]:
    """Метаданные репозиториев (язык, звёзды, размер, дата создания) с кэшем на диске:
    прерванный прогон продолжается с того же места, а не начинается заново."""
    cache: dict[str, dict] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    for i, name in enumerate(names, 1):
        if name in cache:
            continue
        try:
            d = gh.get(f"/repos/{name}")
        except Exception as exc:  # удалённый/приватный репозиторий: помечаю и иду дальше
            cache[name] = {"full_name": name, "error": str(exc)[:120]}
            continue
        cache[name] = {
            "full_name": name, "language": d.get("language"), "stars": d.get("stargazers_count", 0),
            "size_kb": d.get("size", 0), "created_at": d.get("created_at"), "fork": d.get("fork", False),
            "archived": d.get("archived", False),
        }
        if i % 25 == 0:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            print(f"  метаданные: {i}/{len(names)}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


# --- human-режим ------------------------------------------------------------------

def language_quotas(match_path: Path | None, total: int) -> dict[str, int]:
    """Квоты репозиториев по языкам. Если дан ai_repos.jsonl, повторяю его языковой состав:
    иначе различие AI/human смешалось бы с различием «TypeScript vs C»."""
    if match_path and match_path.exists():
        rows = [json.loads(x) for x in match_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        counts = Counter(r.get("language") for r in rows if r.get("language") in GITHUB_LANGS)
        if counts:
            s = sum(counts.values())
            return {lang: max(1, round(total * n / s)) for lang, n in counts.items()}
    per = max(1, total // len(GITHUB_LANGS))
    return {lang: per for lang in GITHUB_LANGS}


def discover_human(gh: GitHub, cutoff: str, total: int, min_stars: int, max_repo_mb: int,
                   match_path: Path | None, exclude: set[str], seed: int) -> list[dict]:
    quotas = language_quotas(match_path, total)
    rng = random.Random(seed)
    picked: list[dict] = []
    for lang, quota in quotas.items():
        # Диапазон звёзд разбит на полосы, чтобы выборка не состояла из одних мегапроектов.
        bands = [(min_stars, min_stars * 3), (min_stars * 3 + 1, min_stars * 20), (min_stars * 20 + 1, "*")]
        per_band = max(1, -(-quota // len(bands)))
        got: list[dict] = []
        for lo, hi in bands:
            stars = f"{lo}..{hi}" if hi != "*" else f">={lo}"
            q = (f"language:{lang} created:<{cutoff} stars:{stars} fork:false archived:false "
                 f"size:<{max_repo_mb * 1024}")
            data = gh.get("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 100})
            gh.sleep(SEARCH_PAUSE)
            items = [it for it in data.get("items", []) if it["full_name"] not in exclude]
            rng.shuffle(items)
            got.extend(items[:per_band])
        for it in got[:quota]:
            picked.append({"full_name": it["full_name"], "language": it.get("language"),
                           "stars": it.get("stargazers_count"), "size_kb": it.get("size"),
                           "created_at": it.get("created_at")})
        print(f"  {lang}: {min(len(got), quota)}/{quota}")
    return picked


# --- paired-режим -------------------------------------------------------------------

def select_paired(meta: dict[str, dict], cutoff: str, max_repo_mb: int) -> list[dict]:
    """AI-репозитории, у которых есть шанс иметь историю до cutoff.
    created_at - лишь приближение (репозиторий мог быть импортирован с историей);
    окончательно решает git log в parser.py."""
    out = []
    for m in meta.values():
        if m.get("error") or m.get("fork") or m.get("archived"):
            continue
        if not m.get("created_at") or m["created_at"][:10] >= cutoff:
            continue
        if m.get("size_kb", 0) > max_repo_mb * 1024:
            continue
        out.append(m)
    return sorted(out, key=lambda x: x["full_name"])


# --- вывод ----------------------------------------------------------------------------

def write_list(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(r["full_name"] for r in rows) + ("\n" if rows else ""), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Поиск репозиториев-кандидатов (ai / human / paired)")
    ap.add_argument("--mode", choices=["ai", "human", "paired"], default="ai")
    ap.add_argument("--outdir", default=".", help="куда писать списки")
    ap.add_argument("--since", default="2024-01-01", help="ai/paired: начало окна поиска коммитов (YYYY-MM-DD)")
    ap.add_argument("--until", default=None, help="ai/paired: конец окна (по умолчанию сегодня)")
    ap.add_argument("--window-hours", type=int, default=6, help="ai: ширина окна поиска в часах (6 ч укладывается в потолок 1000 результатов)")
    ap.add_argument("--pages", type=int, default=2, help="ai: сколько страниц по 100 коммитов брать из окна")
    ap.add_argument("--max-repos", type=int, default=300, help="сколько репозиториев оставить (ai/paired: потолок; human: цель)")
    ap.add_argument("--min-stars", type=int, default=100, help="human: минимум звёзд")
    ap.add_argument("--max-repo-mb", type=int, default=300, help="пропускать репозитории больше этого размера")
    ap.add_argument("--match", default=None, help="human: ai_repos.jsonl, по которому подгоняются квоты языков")
    ap.add_argument("--exclude-file", default=None, help="human: список репозиториев, которые нельзя брать (например, ai_repos.txt)")
    ap.add_argument("--cutoff", default=HUMAN_CUTOFF)
    ap.add_argument("--seed", type=int, default=42)
    return ap


def main(argv: list[str] | None = None) -> None:
    setup_console()
    args = build_parser().parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        # Проверка здесь, а не при импорте: модуль можно импортировать и тестировать без токена.
        raise SystemExit(
            "Не задан GITHUB_TOKEN. Search API без токена даёт 10 запросов в минуту, а поиск по коммитам "
            "часто требует авторизации.\nPowerShell:  $env:GITHUB_TOKEN=\"ghp_...\"   "
            "(токен: https://github.com/settings/tokens, права на чтение публичных данных достаточно)")

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    gh = GitHub(token)
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else date.today()

    if args.mode in ("ai", "paired"):
        found, stat = discover_ai(gh, since, until, args.window_hours, args.pages, args.max_repos * 3, args.seed)
        print(f"Окон использовано: {stat['windows_used']}/{stat['windows_total']}, окон выше потолка API: {stat['windows_over_cap']}")
        names = sorted(n for n, r in found.items() if not r.get("fork"))
        meta = fetch_meta(gh, names, out / "repo_meta.json")
        ai_rows = [meta[n] for n in names if not meta[n].get("error") and meta[n].get("size_kb", 0) <= args.max_repo_mb * 1024]
        random.Random(args.seed).shuffle(ai_rows)
        ai_rows = sorted(ai_rows[:args.max_repos], key=lambda r: r["full_name"])
        write_list(out / "ai_repos.txt", ai_rows)
        write_jsonl(out / "ai_repos.jsonl", ai_rows)
        langs = Counter(r.get("language") for r in ai_rows)
        print(f"\nAI-кандидатов: {len(ai_rows)} -> ai_repos.txt. Языки: {dict(langs.most_common(6))}")
        if args.mode == "paired":
            paired = select_paired({r["full_name"]: r for r in ai_rows}, args.cutoff, args.max_repo_mb)
            write_list(out / "paired_repos.txt", paired)
            print(f"Paired (создан до {args.cutoff}): {len(paired)} из {len(ai_rows)} -> paired_repos.txt")
            if len(paired) < 30:
                print("  Мало для статистики. Расширьте --since/--max-repos либо дополните human-корпус режимом --mode human.")
    else:
        exclude: set[str] = set()
        if args.exclude_file:
            exclude = {ln.strip() for ln in Path(args.exclude_file).read_text(encoding="utf-8").splitlines() if ln.strip()}
        rows = discover_human(gh, args.cutoff, args.max_repos, args.min_stars, args.max_repo_mb,
                              Path(args.match) if args.match else None, exclude, args.seed)
        write_list(out / "human_repos.txt", rows)
        write_jsonl(out / "human_repos.jsonl", rows)
        print(f"\nHuman-кандидатов: {len(rows)} -> human_repos.txt")


if __name__ == "__main__":
    main()
