"""discover_repos.py на подменённой сессии: настоящий GitHub API (и токен) не нужны."""

import json
import re
from datetime import date, datetime, timedelta

import pytest

import discover_repos as dr


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Отвечает функцией handler(url, params) -> Resp и запоминает все запросы."""

    def __init__(self, handler):
        self.headers, self.handler, self.calls = {}, handler, []

    def get(self, url, params=None, timeout=None):
        assert timeout, "запрос без таймаута зависнет навсегда"
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})


def gh(handler):
    sleeps = []
    return dr.GitHub("tok", FakeSession(handler), sleep=sleeps.append), sleeps


def items(prefix, n):
    return [{"repository": {"full_name": f"{prefix}/r{i}", "fork": False}} for i in range(n)]


def test_rate_limit_retries_same_page_and_waits():
    state = {"n": 0}

    def handler(url, p):
        state["n"] += 1
        if state["n"] == 1:
            return Resp(403, text="API rate limit exceeded", headers={"Retry-After": "7"})
        return Resp(200, {"total_count": 1, "items": items("a", 1)})

    client, sleeps = gh(handler)
    data = client.get("/search/commits", {"q": "x", "page": 3})
    assert data["items"][0]["repository"]["full_name"] == "a/r0"
    assert sleeps == [8.0]                                   # Retry-After + 1
    pages = [c[1]["page"] for c in client.session.calls]
    assert pages == [3, 3]                                    # повтор ТОЙ ЖЕ страницы, а не пропуск


def test_server_errors_are_retried_then_give_up():
    client, sleeps = gh(lambda u, p: Resp(502, text="bad gateway"))
    with pytest.raises(RuntimeError):
        client.get("/x")
    assert len(sleeps) == dr.MAX_RETRIES


def test_validation_error_is_explained():
    client, _ = gh(lambda u, p: Resp(422, text='{"message":"Validation Failed"}'))
    with pytest.raises(ValueError, match="Validation Failed"):
        client.get("/search/commits", {"q": "bad"})


def test_search_phrase_is_quoted_and_window_has_time():
    seen = []

    def handler(url, p):
        seen.append(p["q"])
        return Resp(200, {"total_count": 0, "items": []})

    client, _ = gh(handler)
    dr.search_window(client, "Co-authored-by: Claude", datetime(2025, 1, 1, 6), datetime(2025, 1, 1, 11, 59, 59), {}, 2)
    assert seen[0] == '"Co-authored-by: Claude" committer-date:2025-01-01T06:00:00Z..2025-01-01T11:59:59Z'


def test_windows_cover_range_without_overlap():
    w = dr.make_windows(date(2025, 3, 1), date(2025, 3, 2), 6)
    assert len(w) == 8                                        # 2 суток по 6 часов
    assert w[0][0] == datetime(2025, 3, 1) and w[-1][1] == datetime(2025, 3, 2, 23, 59, 59)
    assert all(a[1] < b[0] for a, b in zip(w, w[1:]))         # окна не пересекаются: один коммит не считается дважды
    assert all(b[0] - a[1] == timedelta(seconds=1) for a, b in zip(w, w[1:]))   # и между ними нет дыр


def test_pagination_stops_on_short_page():
    def handler(url, p):
        page = p["page"]
        return Resp(200, {"total_count": 150, "items": items(f"p{page}", 100 if page == 1 else 50)})

    found = {}
    client, _ = gh(handler)
    dr.search_window(client, "t", datetime(2025, 1, 1), datetime(2025, 1, 1, 5), found, max_pages=5)
    assert len(found) == 150 and [c[1]["page"] for c in client.session.calls] == [1, 2]


def test_pagination_respects_max_pages():
    client, _ = gh(lambda u, p: Resp(200, {"total_count": 900, "items": items(f"p{p['page']}", 100)}))
    found = {}
    dr.search_window(client, "t", datetime(2025, 1, 1), datetime(2025, 1, 1, 5), found, max_pages=2)
    assert len(found) == 200 and len(client.session.calls) == 2


def test_over_cap_windows_are_counted_not_fatal():
    client, _ = gh(lambda u, p: Resp(200, {"total_count": 5000, "items": items("x", 100)}))
    found, stat = dr.discover_ai(client, date(2025, 1, 1), date(2025, 1, 1), 24, 1, target=10**6, seed=1)
    assert stat["windows_over_cap"] == len(dr.AI_TRAILERS) and found


def test_discover_ai_stops_when_target_reached_and_is_reproducible():
    counter = {"n": 0}

    def handler(url, p):
        counter["n"] += 1
        return Resp(200, {"total_count": 20, "items": items(f"w{counter['n']}", 20)})   # каждый запрос - 20 новых репозиториев

    client, _ = gh(handler)
    found, stat = dr.discover_ai(client, date(2025, 1, 1), date(2025, 1, 31), 6, 1, target=250, seed=7)
    assert len(found) >= 250 and stat["windows_used"] < stat["windows_total"]        # остановился рано
    used = [re.search(r"committer-date:(\S+?)\.\.", c[1]["q"]).group(1) for c in client.session.calls]
    client2, _ = gh(handler)
    dr.discover_ai(client2, date(2025, 1, 1), date(2025, 1, 31), 6, 1, target=250, seed=7)
    used2 = [re.search(r"committer-date:(\S+?)\.\.", c[1]["q"]).group(1) for c in client2.session.calls]
    assert used[:len(used2)] == used2[:len(used)]                                     # тот же seed -> те же окна
    days = {u[:10] for u in used}
    assert len(days) > 1                                                              # окна разбросаны по периоду, а не подряд с 1 января


def test_select_paired_filters():
    meta = {
        "a/old": {"full_name": "a/old", "created_at": "2018-01-01T00:00:00Z", "size_kb": 100},
        "a/new": {"full_name": "a/new", "created_at": "2023-01-01T00:00:00Z", "size_kb": 100},
        "a/fork": {"full_name": "a/fork", "created_at": "2018-01-01T00:00:00Z", "fork": True},
        "a/huge": {"full_name": "a/huge", "created_at": "2018-01-01T00:00:00Z", "size_kb": 900 * 1024},
        "a/err": {"full_name": "a/err", "error": "404"},
    }
    got = [r["full_name"] for r in dr.select_paired(meta, "2020-11-01", 300)]
    assert got == ["a/old"]


def test_language_quotas_follow_ai_sample(tmp_path):
    p = tmp_path / "ai.jsonl"
    p.write_text("\n".join(json.dumps({"language": l}) for l in ["Python"] * 6 + ["Go"] * 3 + ["Rust"]), encoding="utf-8")
    q = dr.language_quotas(p, 100)
    assert q == {"Python": 60, "Go": 30, "Rust": 10}
    assert set(dr.language_quotas(None, 80)) == set(dr.GITHUB_LANGS)


def test_human_query_uses_cutoff_and_excludes_forks_and_giants():
    seen = []

    def handler(url, p):
        seen.append(p["q"])
        return Resp(200, {"items": [{"full_name": "o/r", "language": "Python", "stargazers_count": 500, "size": 10, "created_at": "2018-01-01"}]})

    client, _ = gh(handler)
    rows = dr.discover_human(client, "2020-11-01", 3, 100, 300, None, set(), 1)
    q = seen[0]
    assert "created:<2020-11-01" in q and "fork:false" in q and "size:<307200" in q and "language:" in q
    assert rows and rows[0]["full_name"] == "o/r"


def test_human_exclude_list_is_respected():
    handler = lambda u, p: Resp(200, {"items": [{"full_name": "x/keep"}, {"full_name": "x/skip"}]})
    client, _ = gh(handler)
    rows = dr.discover_human(client, "2020-11-01", 8, 100, 300, None, {"x/skip"}, 1)
    assert all(r["full_name"] != "x/skip" for r in rows) and rows


def test_missing_token_gives_actionable_message(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        dr.main(["--mode", "ai"])
    assert "GITHUB_TOKEN" in str(exc.value) and "$env:" in str(exc.value)


def test_module_imports_without_token(monkeypatch):
    """Раньше os.environ['GITHUB_TOKEN'] на уровне модуля ронял сам import."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    import importlib
    importlib.reload(dr)
