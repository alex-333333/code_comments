# 2. Быстрый старт

Все команды рассчитаны на **PowerShell** и запускаются в каталоге проекта:

```powershell
cd C:\Users\Admin\newfolder\mnsk\tech
```

## 2.1. Установка (один раз)

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Что делает скрипт:
1. создаёт виртуальное окружение `.venv`;
2. ставит зависимости из `requirements.txt` (`tree-sitter`, `tree-sitter-language-pack`, `requests`, `nltk`,
   `vaderSentiment`, `pytest`). Ни numpy, ни scipy там нет, поэтому установка лёгкая;
3. скачивает грамматики tree-sitter (`mnsk.py setup`), для этого интернет нужен один раз;
4. запускает диагностику (`mnsk.py doctor`).

Если сеть медленная и pip обрывается, достаточно запустить скрипт ещё раз: то, что уже скачано, кэшируется.

То же самое руками:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --timeout 180 --retries 8
.\.venv\Scripts\python.exe mnsk.py setup
```

Необязательно можно поставить spaCy: он точнее определяет, начинается ли комментарий с глагола
(`powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Optional`). Без него признак считается эвристикой по
списку `lexicon/code_verbs.txt`. Основные метрики от spaCy **не зависят**.

Дальше в примерах `python` означает `.\.venv\Scripts\python.exe`. Можно и активировать окружение:
`.\.venv\Scripts\Activate.ps1`.

## 2.2. Проверка окружения

```powershell
python mnsk.py doctor
```

`[OK ]` значит всё хорошо, `[-- ]` - необязательное предупреждение, `[ERR]` - надо исправить (рядом написано,
как). Один раз стоит выполнить `git config --global core.longpaths true`.

## 2.3. Демо (без сети и токена)

```powershell
python mnsk.py demo
```

В `demo_work\` создаются пять синтетических git-репозиториев: три «парных» (человеческая история 2019 года и
AI-коммиты 2025 года), один только человеческий и один только AI. Дальше собирается корпус, извлекаются
комментарии, считается статистика и строится `demo_work\dashboard.html`. Всё занимает секунд пятнадцать.

**Демо доказывает только то, что конвейер работает.** Комментарии в нём я написал вручную под гипотезу, поэтому
любой результат на нём - артефакт этих данных, и цитировать его нельзя. В `corpus\` демо ничего не пишет.

## 2.4. Настоящий прогон

### Шаг 0. Токен GitHub (нужен только для `discover`)

Токен создаётся на https://github.com/settings/tokens (для чтения публичных данных права не нужны). Потом:

```powershell
$env:GITHUB_TOKEN = "ghp_..."
```

Переменная живёт, пока открыто окно PowerShell. Токен не должен попадать ни в файлы проекта, ни в чат.

### Шаг 1. Найти репозитории

```powershell
# AI-кандидаты: репозитории, где GitHub нашёл коммиты с AI-трейлерами
python mnsk.py discover --mode ai --since 2024-01-01 --max-repos 300

# Human-кандидаты, вариант А: популярные репозитории, созданные до 2020-11,
# с языковыми квотами, повторяющими AI-выборку
python mnsk.py discover --mode human --match ai_repos.jsonl --exclude-file ai_repos.txt --max-repos 300

# Human-кандидаты, вариант Б («парный» дизайн): те же репозитории до и после cutoff
python mnsk.py discover --mode paired --since 2024-01-01 --max-repos 300
```

Как выбирать между А и Б, написано в [03-methodology.md, §3](03-methodology.md#3-откуда-берутся-human-репозитории-два-режима-рабочее).
Использовать можно оба режима сразу и сравнить результаты.

Про время: поиск ограничен 30 запросами в минуту (с токеном), так что 300 репозиториев занимают около часа.
Прогон можно прервать и запустить заново, метаданные кэшируются в `repo_meta.json`.

### Шаг 2. Собрать корпус

```powershell
python mnsk.py collect --repos ai_repos.txt    --source ai    --outdir corpus --gb 3
python mnsk.py collect --repos human_repos.txt --source human --outdir corpus --gb 3
# для парного дизайна paired_repos.txt идёт дважды: как ai и как human
python mnsk.py collect --repos paired_repos.txt --source ai    --outdir corpus --gb 3
python mnsk.py collect --repos paired_repos.txt --source human --outdir corpus --gb 3
```

- Дольше всего идёт клонирование (`git clone --bare`). Клоны кэшируются в `corpus\_clones`; после сбора
  каталог можно удалить или сразу передать `--delete-clones`.
- Повторный запуск безопасен: уже собранные `(source, repo, sha, file)` пропускаются.
- `--gb` - общий потолок размера корпуса за один запуск. `--max-files-per-repo` (по умолчанию 2000) не даёт
  одному проекту занять всю квоту.
- Диагностика: `corpus\errors.log` (репозитории, которые упали) и `corpus\suspects.log` (коммиты, похожие на
  сгенерированные, но без известного трейлера; это сигнал дополнить `AI_TRAILERS`).

### Шаги 3-5. Комментарии, анализ, дашборд

```powershell
python mnsk.py run --corpus corpus --results results --out dashboard.html
```

То же по отдельности: `mnsk.py extract`, `mnsk.py analyze`, `mnsk.py dashboard`.

Полезные флаги анализа (через `run` они тоже проходят): `--balance` выравнивает число комментариев по языкам
между AI и human, `--theta 0.6` меняет порог, `--max-per-repo 300` ограничивает вклад одного репозитория.

### Шаг 6. Проверить метрику на людях (очень рекомендую)

```powershell
python mnsk.py validate sample --n 200                 # results\gold_sample.csv, слепая выборка
# в колонку label вручную пишется: referential / explanatory / other (лучше двум людям независимо)
python mnsk.py validate eval --labels results\gold_sample.csv --labels2 results\gold_sample_second.csv
```

`gold_key.csv` нельзя открывать до конца разметки. На выходе получаются точность, κ Коэна и рекомендуемый θ.

## 2.5. Сколько это занимает

| Шаг | Оценка | От чего зависит |
|---|---|---|
| Поиск AI-кандидатов | 30-90 мин | `--max-repos`, лимит 30 запросов в минуту |
| Клонирование | от минут до часов | размер репозиториев и скорость сети |
| Извлечение комментариев | около 0.3 с на 100 файлов | процессор; 100 тыс. файлов примерно 5 мин |
| Анализ | 1-3 мин на 100 тыс. комментариев | `--boot` |

## 2.6. Что дальше

- Словари (`lexicon\`) надо заморозить **до** просмотра результатов, подробности в
  [05-lexicons.md](05-lexicons.md#правило-заморозки-словарей).
- Черновик раздела «Ограничения» лежит в [08-limitations.md](08-limitations.md).
- К защите: [09-defense-qa.md](09-defense-qa.md).
