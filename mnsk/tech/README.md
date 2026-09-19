# AI против человека: лингвистический анализ комментариев к коду

Это техническая часть моей работы для МНСК. Я сравниваю комментарии из коммитов с AI-соавтором (Claude,
Copilot, Cursor) с комментариями «доAI-эпохи»: коммитами до ноября 2020 года без AI-трейлеров.

**Гипотеза.** AI-комментарии в основном *референциальные*: они пересказывают то, что и так видно в коде
(`# Loop through each item`). Человеческие чаще *объясняющие*: они сообщают то, чего в коде нет, например
причину, ограничение или историю (`# retry: the API drops idle connections after 30 s`).

**Как я её проверяю.** Одной измеримой величиной, lexical overlap: какая доля содержательных слов комментария
уже встречается в соседнем коде. На ней держится всё остальное: корпусный анализ, дашборд и pre-commit чекер,
который применяет ту же линейку к новому коду.

---

## Быстрый старт (Windows / PowerShell)

```powershell
cd C:\Users\Admin\newfolder\mnsk\tech
powershell -ExecutionPolicy Bypass -File .\setup.ps1      # venv, зависимости, грамматики, проверка
.\.venv\Scripts\python.exe mnsk.py demo                    # сквозной прогон на синтетике, без сети и токена
```

После `demo` открывается `demo_work\dashboard.html`. Все данные в демо выдуманы: он показывает, что конвейер
работает, а не подтверждает гипотезу (на странице стоит красный баннер).

Настоящий прогон по шагам описан в [docs/02-quickstart.md](docs/02-quickstart.md).

## Как устроен конвейер

```
 discover_repos.py        parser.py            extract_comments.py        counter.py           dashboard.py
 ───────────────────  ─────────────────  ────────────────────────  ─────────────────  ─────────────────
 GitHub Search API →  git log + cat-file →  tree-sitter: комментарий →  метрики, критерии →  dashboard.html
 списки репозиториев  corpus/manifest.jsonl  + код-цель, только            results/summary.json  (один файл,
 (ai / human / paired) + файлы ревизий       ДОБАВЛЕННЫЕ коммитом строки   + CSV + примеры       без сервера)
                      + added_ranges         → corpus/comments.jsonl

 metrics.py: общая «линейка» для counter.py (корпус) и checker.py (pre-commit хук)
 lexicon/*.csv|txt: словари, правятся без кода, читает metrics.py
```

| Шаг | Команда | Что получается |
|---|---|---|
| 1. Кандидаты | `python mnsk.py discover --mode ai` | `ai_repos.txt`, `ai_repos.jsonl` |
| 2. Корпус | `python mnsk.py collect --repos ai_repos.txt --source ai` | `corpus/manifest.jsonl` и файлы |
| 3. Комментарии | `python mnsk.py extract` | `corpus/comments.jsonl` |
| 4. Анализ | `python mnsk.py analyze` | `results/summary.json`, CSV |
| 5. Дашборд | `python mnsk.py dashboard` | `dashboard.html` |
| 3-5 разом | `python mnsk.py run` | всё перечисленное выше |
| Чекер | `python mnsk.py install-hook --repo <путь>` | pre-commit хук |
| Проверка метрики | `python mnsk.py validate sample` / `eval` | выборка для ручной разметки, κ Коэна |

`python mnsk.py doctor` проверяет окружение, `python mnsk.py --help` показывает все команды.

## Четыре вещи, которые я держу в голове

1. **Считаются только комментарии, добавленные самим коммитом.** В файле на нужной ревизии лежат и старые
   человеческие комментарии, и без этого фильтра они попали бы в AI-корпус. На пробном реальном репозитории
   так отсеивается около 93 % комментариев файлов. Подробности в [docs/03-methodology.md](docs/03-methodology.md).
2. **Порог θ = 0.5 рабочий, а не истинный.** Вывод я строю на кривой чувствительности и калибрую разметкой
   вручную (`validate.py`), иначе на вопрос «почему 0.5?» нечего ответить.
3. **Словари в `lexicon/` пока черновик.** Их надо проверить с лингвистом и заморозить *до* того, как я посмотрю
   на результаты, иначе словарь неизбежно начнёт подгоняться под ответ.
4. **Токен GitHub нужен только для `discover`.** Остальные шаги работают без него.

## Документация

| Файл | О чём |
|---|---|
| [docs/01-overview.md](docs/01-overview.md) | цель, гипотеза, поток данных, устройство проекта |
| [docs/02-quickstart.md](docs/02-quickstart.md) | установка и полный прогон по шагам, сколько это занимает |
| [docs/03-methodology.md](docs/03-methodology.md) | **почему всё сделано именно так**: каждое методологическое решение с обоснованием |
| [docs/04-reference.md](docs/04-reference.md) | справочник: команды, флаги, форматы файлов |
| [docs/05-lexicons.md](docs/05-lexicons.md) | словари: что в них лежит и как их править |
| [docs/06-dashboard.md](docs/06-dashboard.md) | дашборд: блоки, как их читать, как показывать |
| [docs/07-checker.md](docs/07-checker.md) | чекер: pre-commit хук, режимы, демонстрация |
| [docs/08-limitations.md](docs/08-limitations.md) | готовый раздел «Ограничения» для отчёта |
| [docs/09-defense-qa.md](docs/09-defense-qa.md) | вероятные вопросы жюри и мои ответы |
| [docs/10-troubleshooting.md](docs/10-troubleshooting.md) | что ломается на Windows и как чинить |
| [docs/CHANGES.md](docs/CHANGES.md) | что и почему я поменял в исходных `parser.py` и `discover_repos.py` |

## Что где лежит

```
config.py            константы корпуса: трейлеры, граница human-корпуса, расширения, пропускаемые пути
parser.py            сбор корпуса из git (added_ranges, cat-file, идемпотентность)
discover_repos.py    поиск репозиториев: ai / human / paired
extract_comments.py  tree-sitter -> comments.jsonl (привязка комментарий→код, атрибуция коммиту)
metrics.py           overlap, прагматические маркеры, идиомы, классификация
stats.py             статистика на чистом Python (Манн-Уитни, χ², Фишер, бутстреп по репозиториям)
counter.py           анализ корпуса -> results/
validate.py          слепая выборка для ручной разметки, калибровка θ
dashboard.py         results/ -> dashboard.html   (шаблон: dashboard_template.html)
checker.py           pre-commit чекер;  install_hook.py ставит хук
demo.py, demo_data.py  синтетические репозитории и сквозной демо-прогон
mnsk.py              единая точка входа;  setup.ps1 - установка на Windows
lexicon/             словари (CSV/TXT)
tests/               134 теста (pytest), сеть и токен не нужны
docs/                документация
```

Тесты: `.\.venv\Scripts\python.exe -m pytest tests -q`
