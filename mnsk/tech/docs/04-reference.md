# 4. Справочник

Каждую команду можно запускать двумя способами: `python mnsk.py <команда> …` или сразу скриптом
(`python parser.py …`), это одно и то же. У каждой команды есть `--help`.

## Команды и флаги

### `mnsk.py`

| Команда | Скрипт | Назначение |
|---|---|---|
| `setup` | нет | скачать грамматики tree-sitter, затем `doctor` |
| `doctor` | нет | диагностика окружения; код возврата 1, если есть `[ERR]` |
| `discover` | `discover_repos.py` | поиск репозиториев |
| `collect` | `parser.py` | сбор корпуса |
| `extract` | `extract_comments.py` | комментарии из корпуса |
| `analyze` | `counter.py` | анализ |
| `dashboard` | `dashboard.py` | сборка `dashboard.html` |
| `run` | нет | `extract`, потом `analyze`, потом `dashboard` |
| `check` | `checker.py` | чекер комментариев |
| `install-hook` | `install_hook.py` | pre-commit хук |
| `validate` | `validate.py` | выборка для разметки, калибровка θ |
| `demo` | `demo.py` | сквозное демо на синтетике |

`run` принимает `--corpus`, `--results`, `--out`. Флаги `--all-comments`, `--code-window`, `--limit` уходят в
`extract`, все остальные в `analyze`.

### `discover_repos.py`

Токен задаётся так: `$env:GITHUB_TOKEN="ghp_..."`. Без него скрипт не падает, а печатает понятное сообщение.

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--mode` | `ai` | `ai`: репозитории с AI-трейлерами; `human`: старые популярные репозитории; `paired`: AI-репозитории, у которых есть история до cutoff |
| `--outdir` | `.` | куда писать списки |
| `--since`, `--until` | `2024-01-01`, сегодня | период поиска AI-коммитов (`ai`, `paired`) |
| `--window-hours` | `6` | ширина окна поиска. 6 часов укладываются в потолок Search API (1000 результатов на запрос), а сутки нет: на реальных данных Claude делает около 3700 коммитов в сутки |
| `--pages` | `2` | сколько страниц по 100 коммитов брать из окна |
| `--max-repos` | `300` | целевое число репозиториев |
| `--min-stars` | `100` | `human`: минимум звёзд |
| `--max-repo-mb` | `300` | пропускать репозитории больше этого размера (клон гигантов занимает часы) |
| `--match` | нет | `human`: файл `ai_repos.jsonl`, по которому подгоняются квоты языков |
| `--exclude-file` | нет | `human`: список репозиториев, которые брать нельзя |
| `--cutoff` | `2020-11-01` | граница human-корпуса |
| `--seed` | `42` | воспроизводимость |

Как работает `ai`: окна обходятся в **случайном** порядке, и поиск останавливается, когда набрано
`3 × max-repos` репозиториев. Обход по возрастанию дат остановился бы на самых ранних окнах и дал бы выборку из
одного месяца. Метаданные (язык, звёзды, размер, `created_at`) кэшируются в `repo_meta.json`.

Выходные файлы: `ai_repos.txt`, `ai_repos.jsonl`, `human_repos.txt`, `human_repos.jsonl`, `paired_repos.txt`.

### `parser.py` (`mnsk.py collect`)

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--repos` | обязателен | файл, по репозиторию в строке: `owner/repo`, полный URL или путь к локальному репозиторию; пустые строки и `# комментарии` игнорируются |
| `--source` | обязателен | `ai` или `human` |
| `--outdir` | `./corpus` | каталог корпуса |
| `--gb` | `5.0` | потолок суммарного размера за запуск (1 GB = 1024³ байт); проверяется **после** сохранения файла, поэтому превышение не больше одного файла |
| `--seed` | `42` | детерминированное перемешивание коммитов |
| `--max-files-per-repo` | `2000` | потолок файлов на репозиторий |
| `--max-files-per-commit` | `200` | коммиты с большим числом релевантных файлов пропускаются |
| `--max-file-kb` | `512` | пропуск больших файлов |
| `--delete-clones` | выключено | удалять bare-клон после обработки (экономит диск) |

Как ведёт себя парсер:
- Клонирует во временный каталог `*.part` и переименовывает после успеха; при сетевых сбоях делает до 3 попыток.
  Недоклон никогда не принимается за готовый клон.
- Сбой одного репозитория пишется в `errors.log`, остальные обрабатываются дальше.
- Повторный запуск идемпотентен: `(source, repo, sha, file)`, которые уже есть в манифесте, пропускаются.
- Пропускает `node_modules/`, `vendor/`, `third_party/`, `dist/`, `build/`, `*.min.js`, `*_pb2.py`,
  `*.pb.go`, `package-lock.json`, `generated/` и другое (`config.SKIP_PATH_PATTERNS`).
- Коммиты идут в случайном порядке (по `--seed`), иначе лимит на репозиторий брал бы только самые свежие.
- Файлы сохраняются **побайтово** (без преобразования `\n` в `\r\n`, которое делает `Path.write_text` на
  Windows); BOM в начале срезается.
- Глубокие пути: каталог ревизии называется `sha[:10]`, а если полный путь длиннее 240 символов, имя файла
  заменяется на `hash8_basename`.

### `extract_comments.py` (`mnsk.py extract`)

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--corpus` | `./corpus` | каталог с `manifest.jsonl` |
| `--out` | `<corpus>/comments.jsonl` | куда писать |
| `--all-comments` | выключено | брать **все** комментарии файла, а не только добавленные коммитом (методологически слабее, нужно для сравнения) |
| `--code-window` | `12` | сколько строк кода-цели учитывать |
| `--limit` | нет | обработать только первые N записей манифеста (для отладки) |

Один битый файл не роняет прогон: ошибка идёт в stderr, а счётчик `files_failed` растёт.

### `counter.py` (`mnsk.py analyze`)

| Флаг | По умолчанию | Смысл |
|---|---|---|
| `--corpus` / `--comments` | `./corpus` | где искать `comments.jsonl` |
| `--results` | `./results` | куда писать |
| `--theta` | `0.5` | порог overlap для referential |
| `--max-per-repo` | `500` | потолок комментариев на репозиторий |
| `--min-per-repo` | `1` | репозитории с меньшим числом отбрасываются |
| `--balance` | выключено | выровнять число комментариев по языкам между AI и human |
| `--seed` | `42` | воспроизводимость |
| `--boot` | `2000` | итераций бутстрепа по репозиториям |
| `--top` | `80` | слов в облаке |
| `--min-word-count` | `5` | «характерные» слова реже этого не показываются |
| `--examples` | `40` | примеров на полосу overlap и источник |
| `--file-stats` | `file_stats.jsonl` рядом с `comments.jsonl` | источник для плотности комментариев; если файла нет, блок `density` помечается `available: false`, а остальной анализ не страдает |
| `--demo` | выключено | пометить результат как демонстрационный (баннер на дашборде) |

Выход: `summary.json`, `examples.json`, `pool.jsonl`, `idioms.csv`, `markers.csv`, `overlap_sensitivity.csv`,
`density.csv`, `words_ai.csv`, `words_human.csv`. CSV пишутся в UTF-8 с BOM, чтобы Excel на Windows читал
кириллицу.

### `dashboard.py`

`--results ./results`, `--out ./dashboard.html`, `--template dashboard_template.html`.

### `checker.py` (`mnsk.py check`)

| Флаг | Смысл |
|---|---|
| `--staged` | (по умолчанию) индекс git: проверяются только добавленные в индекс комментарии |
| `--file A B …` | все комментарии указанных файлов |
| `--text "…" --code "…"` | один комментарий и код-цель (для демонстрации) |
| `--repo` | каталог репозитория (по умолчанию текущий) |
| `--theta` | порог (по умолчанию 0.5, как в корпусном анализе) |
| `--strict` | код возврата 1, если найдены пересказы; то же делает переменная `MNSK_CHECKER_STRICT=1` |
| `--include-doc` | проверять и докстринги (по умолчанию нет) |
| `--json` | машиночитаемый вывод |

Сбой самого чекера (не git-репозиторий, нечитаемый файл) **не блокирует** коммит: код возврата 0 и сообщение в
stderr.

### `install_hook.py` (`mnsk.py install-hook`)

`--repo <путь>` (по умолчанию текущий), `--strict`, `--uninstall`.

### `validate.py` (`mnsk.py validate`)

- `sample [--results] [--n 200] [--seed 42] [--out] [--key]`: слепая стратифицированная выборка.
- `eval --labels file.csv [--labels2 second.csv] [--theta 0.5] [--results] [--key]`: сравнение правила с
  разметкой. В колонке `label` допустимы `referential` / `r` / `ref` / `пересказ`, `explanatory` / `e` /
  `expl` / `объяснение`, `other` / `o` / `другое`.

## Форматы файлов

### `corpus/manifest.jsonl`: одна строка на файл

```json
{"repo": "pallets/itsdangerous", "sha": "0994288880cf…", "file": "itsdangerous.py", "lang": ".py",
 "source": "human", "tool": null, "commit_date": "2013-05-18T22:46:40+02:00",
 "added_ranges": [[281, 281], [328, 328], [454, 455]], "size": 26505,
 "local_path": "human/pallets__itsdangerous/0994288880/itsdangerous.py"}
```

| Поле | Смысл |
|---|---|
| `added_ranges` | строки **нового** файла, добавленные коммитом: `[[с, по], …]`, нумерация с 1, границы включительно |
| `tool` | `claude` / `copilot` / `cursor` для AI; `null` для human |
| `local_path` | путь **относительно** `corpus/`, поэтому корпус можно переносить |

### `corpus/comments.jsonl`: одно наблюдение на строку

```json
{"id": "a3f9…", "source": "ai", "tool": "claude", "repo": "o/r", "sha": "…", "file": "src/a.py", "lang": "python",
 "commit_date": "2025-06-01T12:00:00+00:00", "kind": "leading", "form": "line", "start_line": 10, "end_line": 11,
 "text": "Retry because the API drops idle connections", "code": "for attempt in range(3):\n    …",
 "target_type": "for_statement", "attached": true, "exclude": null}
```

| Поле | Значения |
|---|---|
| `kind` | `inline`, `leading`, `block`, `detached`, `doc` |
| `form` | `line`, `block`, `docstring` |
| `exclude` | `null` (идёт в анализ) или `license`, `directive`, `code`, `empty` |
| `code` | код-цель (до 12 строк, без комментариев и докстрингов); `null` у `detached` и у докстринга модуля |

### `corpus/file_stats.jsonl`: одна строка на **файл** (для плотности комментариев)

```json
{"source": "ai", "tool": "claude", "repo": "o/r", "sha": "…", "file": "src/a.py", "lang": "python",
 "added_lines": 14, "added_nonblank": 11, "comments": 6, "comment_lines": 6,
 "by_kind": {"doc": 1, "leading": 4, "inline": 1}}
```

| Поле | Смысл |
|---|---|
| `added_lines` | строки, добавленные коммитом в этом файле (при `--all-comments` все строки файла) |
| `added_nonblank` | из них непустые |
| `comments` | комментарии, написанные автором и целиком добавленные коммитом: **без** лицензий, директив, закомментированного кода и разделителей; язык не фильтруется |
| `comment_lines` | сколько строк эти комментарии занимают |
| `by_kind` | разбивка `comments` по видам (`inline`, `leading`, `block`, `detached`, `doc`) |

Файл без комментариев тоже получает строку (`comments = 0`): он входит в знаменатель плотности. Пишется рядом с
`comments.jsonl`.

### `corpus/extract_stats.json`

Счётчики по источникам: `files`, `comments`, `parse_error`, `dropped_not_added` (комментарии, которые коммит не
добавлял), `dropped_mixed` (блок, затронутый частично), `raw_<kind>`, `excluded_<reason>`.

### `results/summary.json` (схема версии 1)

| Ключ | Содержимое |
|---|---|
| `demo`, `generated_at`, `theta`, `nlp_backend`, `balanced`, `params` | служебное |
| `composition` | по источникам: сколько наблюдений, сколько исключено и почему, дубли, потолок на репозиторий, сколько попало в анализ |
| `sizes`, `langs` | число комментариев, докстрингов, репозиториев |
| `overlap.hist[src][lang]` | гистограммы на 100 корзин (шаг 0.01): `hist`, `hist_nocue`, `hist_ext`, `hist_nocue_ext`, `classified`, `cue`, `n` |
| `overlap.describe`, `overlap.mann_whitney` | описательные статистики, критерий Манна-Уитни, Cliff's δ |
| `referential` | `mix`, `by_lang`, `sensitivity` (θ от 0.20 до 0.80), `bootstrap` (разница долей и 95 % ДИ) |
| `kinds` | распределение типов, χ², Cramér's V, по языкам |
| `markers`, `idioms` | таблицы с частотами, OR, p, q (BH); плюс `group`, `label_ru`/`category_ru` - научная группа и понятное название, см. [05-lexicons.md](05-lexicons.md#научная-классификация-категорий-marker_categoriescsv-idiom_categoriescsv-groupscsv) |
| `groups` | group → `{label_ru, gloss, source}` - расшифровка и академическая ссылка для каждой группы маркеров/идиом |
| `connotation`, `idiom_categories`, `sentiment`, `style`, `verb_form`, `docs` | остальные блоки |
| `density` | плотность комментариев: `available`, `per_source`, `by_lang`, `by_tool`, `bootstrap` (`all` и `main`, то есть без doc). В блоке: `per100` (комментариев на 100 добавленных строк), `per100_nonblank`, `line_share`, `main_per100`, `doc_per100`, `by_kind_per100`, `repo_median_per100`; в `bootstrap` лежат `diff` (на 100 строк), `ratio` (AI / human) и их 95 % интервалы по репозиториям |
| `words` | частоты, «характерные» слова, биграммы |
| `warnings` | предупреждения (мало репозиториев или комментариев) |

`hist_nocue` - распределение overlap у комментариев **без** маркеров «почему». По нему дашборд пересчитывает долю
referential при любом θ, не обращаясь к исходным данным.

## Переменные окружения

| Переменная | Где | Смысл |
|---|---|---|
| `GITHUB_TOKEN` | `discover_repos.py` | токен GitHub API |
| `MNSK_NO_SPACY=1` | `metrics.py` | принудительно эвристика вместо spaCy |
| `MNSK_CHECKER_STRICT=1` | `checker.py` | то же, что `--strict` |
| `NO_COLOR` | `checker.py` | отключить цвета |
