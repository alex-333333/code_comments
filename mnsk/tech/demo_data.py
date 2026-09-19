"""
demo_data.py - синтетический код для demo.py.

ВСЁ ЗДЕСЬ ВЫМЫШЛЕНО. Комментарии «человеческого» и «AI» стиля написаны вручную под
ожидаемую гипотезу, поэтому демо доказывает только работоспособность конвейера, а не
саму гипотезу. Результаты демо нельзя цитировать.

Структура:
  paired_a/b/c  - история и до 2020 (человек), и после (AI-коммиты с трейлерами): «парный» дизайн.
                  AI-коммит дописывает функции в СТАРЫЙ файл - так проверяется атрибуция:
                  старые человеческие комментарии не должны попасть в AI-корпус.
  legacy_human  - только человеческая история 2019.
  new_ai        - только AI-история 2025.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

NOUNS = ["user", "order", "item", "record", "task", "message", "config", "session", "token", "invoice",
         "node", "event", "job", "entry", "report", "ticket", "account", "payment", "product", "request"]

TRAILERS = [
    "Co-Authored-By: Claude <noreply@anthropic.com>",          # заглавные, как пишет Claude Code
    "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
    "Co-authored-by: Cursor Agent <cursoragent@cursor.com>",
    "Co-authored-by: Claude Opus 4 <noreply@anthropic.com>",
]

# --- пулы комментариев -------------------------------------------------------------------

HUMAN_WHY = [
    "Retry because the upstream API drops idle connections after about 30 seconds.",
    "HACK: the parser chokes on empty lines, so we pad them. See #412.",
    "TODO: remove this once we drop python 2 support.",
    "Not thread safe, callers must hold the lock first.",
    "Workaround for a Windows path quirk, otherwise the {n} never resolves.",
    "This is O(n^2) but n is tiny in practice, so leave it alone for now.",
    "Edge case: the {n} can be empty when the job is cancelled mid-flight.",
    "Sanity check, the scheduler sometimes hands us stale {n}s.",
    "We cap this at 10 since the vendor rate limits us, see the ticket.",
    "Order matters here: the {n} has to be validated before we touch the cache.",
    "Careful, this mutates the caller's list.",
    "Don't use a set, we rely on the insertion order.",
    "Unfortunately the legacy client returns strings, not ints.",
    "Kludge to keep old {n}s working until the migration finishes.",
    "Probably fine, but I haven't tested it with unicode names.",
    "Needed for backwards compatibility with the v1 export format.",
    "Skip these on purpose, they are handled by the nightly batch instead.",
    "Magic number from the spec, section 4.2.",
    "Temporary fix, the real solution needs the new storage layer.",
    "Off by one here because the header row is counted too.",
    "Not pretty, but it avoids a second database round trip.",
    "The docs say this is optional but the server rejects the request without it.",
    "FIXME: this breaks when two {n}s share a name, nobody noticed yet.",
    "We used to sort here, but it made the report page painfully slow.",
]
HUMAN_REF = [
    "increment the counter", "loop over the {n}s", "return the result", "get the {n} from the list",
    "set up the {n}", "check the {n}",
]
AI_WHY = [
    "Retry because the request may fail transiently.",
    "Skip invalid entries to avoid crashing later on.",
]

# --- формы функций -------------------------------------------------------------------------
# @@k@@ - слот комментария (строка целиком или хвост строки); {D} - место докстринга/дока.
# Для каждого слота: AI-формулировка (пересказ) и набор человеческих вариантов.

PY = [
    {"code": "def process_{n}s({n}s, limit=10):\n{D}    @@0@@\n    result = []\n    @@1@@\n    for {n} in {n}s:\n        @@2@@\n"
             "        if {n}.is_valid():\n            result.append({n})  @@3@@\n    @@4@@\n    return result\n",
     "doc": "Process the {n}s and return the list of valid {n}s.",
     "ai": ["Initialize the result list to store the valid {n}s", "Loop through each {n} in the {n}s list",
            "Check if the {n} is valid before adding it", "Append the {n} to the result list", "Return the result list"],
     "slots": ["Ensure the {n}s are properly validated", "Initialize the result list", "Iterate over each {n}",
               "Check whether the {n} is valid", "Add the {n} to the result", "Return the result"]},
    {"code": "def load_{n}(path):\n{D}    @@0@@\n    with open(path) as handle:\n        @@1@@\n        data = json.load(handle)\n"
             "    @@2@@\n    return {N}(**data)\n",
     "doc": "Load a {n} from the JSON file at the given path.",
     "ai": ["Open the file at the given path", "Load the JSON data from the file handle", "Return a new {N} instance built from the data"]},
    {"code": "def count_{n}s({n}s):\n{D}    @@0@@\n    total = 0\n    @@1@@\n    for {n} in {n}s:\n        total += {n}.size  @@2@@\n    return total\n",
     "doc": "Count the total size of all {n}s.",
     "ai": ["Initialize the total counter to zero", "Loop through the {n}s and add each size to the total", "Add the {n} size to the total"]},
    {"code": "def fetch_{n}(client, {n}_id, retries=3):\n{D}    for attempt in range(retries):\n        try:\n            @@0@@\n"
             "            return client.get({n}_id)\n        except TimeoutError:\n            @@1@@\n            time.sleep(2 ** attempt)\n"
             "    @@2@@\n    raise RuntimeError('giving up on {n}')\n",
     "doc": "Fetch a {n} from the client, retrying on timeouts.",
     "ai": ["Try to fetch the {n} using the client", "Handle the timeout error and wait before retrying", "Raise an error if all retries fail"]},
]
JS = [
    {"code": "{D}function filter{N}s({n}s, limit = 10) {\n  @@0@@\n  const result = [];\n  @@1@@\n  for (const {n} of {n}s) {\n"
             "    @@2@@\n    if ({n}.isValid()) {\n      result.push({n}); @@3@@\n    }\n  }\n  return result;\n}\n",
     "doc": "Filters the {n}s and returns only the valid {n}s.",
     "ai": ["Initialize the result array", "Loop through each {n} in the {n}s array", "Check if the {n} is valid", "Push the {n} to the result array"]},
    {"code": "{D}async function load{N}(url) {\n  @@0@@\n  const response = await fetch(url);\n  @@1@@\n  const data = await response.json();\n"
             "  @@2@@\n  return new {N}(data);\n}\n",
     "doc": "Loads a {n} from the given URL.",
     "ai": ["Fetch the response from the URL", "Parse the JSON data from the response", "Return a new {N} instance with the data"]},
]
GO = [
    {"code": "{D}func Count{N}s({n}s []{N}) int {\n\t@@0@@\n\ttotal := 0\n\t@@1@@\n\tfor _, {n} := range {n}s {\n"
             "\t\ttotal += {n}.Size @@2@@\n\t}\n\treturn total\n}\n",
     "doc": "Count{N}s returns the total size of all {n}s.",
     "ai": ["Initialize the total counter", "Loop through the {n}s and sum the sizes", "Add the {n} size to the total"]},
]
JAVA = [
    {"code": "public class {N}Service {\n{D}    public List<{N}> filter(List<{N}> {n}s) {\n        @@0@@\n        List<{N}> result = new ArrayList<>();\n"
             "        @@1@@\n        for ({N} {n} : {n}s) {\n            @@2@@\n            if ({n}.isValid()) {\n"
             "                result.add({n});\n            }\n        }\n        return result;\n    }\n}\n",
     "doc": "Filters the {n}s and returns the valid ones.",
     "ai": ["Create a new list to hold the valid {n}s", "Loop through each {n} in the list", "Check if the {n} is valid and add it to the result"]},
]

LANGS = {
    "py": {"ext": ".py", "shapes": PY, "prefix": "#", "dir": "pkg"},
    "js": {"ext": ".js", "shapes": JS, "prefix": "//", "dir": "src"},
    "go": {"ext": ".go", "shapes": GO, "prefix": "//", "dir": "internal"},
    "java": {"ext": ".java", "shapes": JAVA, "prefix": "//", "dir": "src/main"},
}


def _fill(text: str, noun: str) -> str:
    return text.replace("{n}", noun).replace("{N}", noun.capitalize())


def render(lang: str, shape: dict, noun: str, style: str, rng: random.Random) -> str:
    """Один фрагмент кода в «человеческом» или «AI» стиле комментирования."""
    spec = LANGS[lang]
    prefix = spec["prefix"]
    code = shape["code"]
    # {D} всегда отдельная строка: докстринг стоит сразу под def / перед function, а не «хвостом» кода
    code = re.sub(r"\{D\}(?=[^\n])", "{D}\n", code)
    slots = sorted({int(x) for x in re.findall(r"@@(\d)@@", code)})

    texts: dict[int, str | None] = {}
    if style == "ai":
        for k in slots:
            base = shape["ai"][k] if k < len(shape["ai"]) else None
            if base and rng.random() < 0.85:
                texts[k] = base + ("." if rng.random() < 0.6 else "")
            if rng.random() < 0.08:
                texts[k] = rng.choice(AI_WHY)
        # старательные слоты «Ensure ...», характерные для AI-регистра, если форма их предусматривает
        doc = shape["doc"] if rng.random() < 0.95 else None
    else:
        pick = rng.sample(slots, k=min(len(slots), rng.choice([0, 1, 1, 2])))
        for k in pick:
            texts[k] = rng.choice(HUMAN_WHY) if rng.random() < 0.8 else rng.choice(HUMAN_REF)
        doc = shape["doc"] if rng.random() < 0.3 else None

    def doc_block(indent: str) -> str:
        if doc is None:
            return ""
        d = _fill(doc, noun)
        if lang == "py":
            return f'{indent}"""{d}"""\n'
        if lang == "go":
            return f"// {d}\n"
        return f"{indent}/**\n{indent} * {d}\n{indent} */\n"

    indent_doc = "    " if lang in ("py", "java") else ""
    out = []
    for line in code.split("\n"):
        stripped = line.strip()
        if stripped == "{D}":
            if doc:
                out.append(doc_block(indent_doc).rstrip("\n"))
            continue
        m = re.fullmatch(r"@@(\d)@@", stripped)
        if m:                                                   # слот-строка
            t = texts.get(int(m.group(1)))
            if t:
                out.append(f"{line[:len(line) - len(line.lstrip())]}{prefix} {_fill(t, noun)}")
            continue
        m2 = re.search(r"\s*@@(\d)@@\s*$", line)               # слот-хвост
        if m2:
            t = texts.get(int(m2.group(1)))
            line = line[:m2.start()] + (f"  {prefix} {_fill(t, noun)}" if t else "")
        out.append(line)
    return _fill("\n".join(out), noun)


def make_file(lang: str, style: str, rng: random.Random, n_funcs: int, used: set) -> str:
    spec = LANGS[lang]
    shapes = spec["shapes"]
    parts = []
    if lang == "py":
        parts.append("import json\nimport time\n")
    if lang == "go":
        parts.append("package internal\n")
    for _ in range(n_funcs):
        for _try in range(20):
            noun, shape = rng.choice(NOUNS), rng.choice(shapes)
            key = (lang, noun, id(shape))
            if key not in used:
                used.add(key)
                parts.append(render(lang, shape, noun, style, rng))
                break
    return "\n\n".join(parts) + "\n"


def build_repos(work: Path, GitRepo) -> dict:
    rng = random.Random(2026)
    repos = work / "repos"
    old, new = "2019-05-20T10:00:00", "2025-08-12T10:00:00"
    langs = list(LANGS)

    def files_for(style, n_files, prefix, lang_bias):
        out, used = {}, set()
        for i in range(n_files):
            lang = lang_bias[i % len(lang_bias)]
            spec = LANGS[lang]
            out[f"{spec['dir']}/{prefix}{i}{spec['ext']}"] = make_file(lang, style, rng, rng.choice([2, 3]), used)
        return out

    made = {}
    # парные репозитории: человек (2019) -> AI (2025)
    for name, bias in [("paired_a", ["py", "js"]), ("paired_b", ["py", "go"]), ("paired_c", ["js", "java", "py"])]:
        r = GitRepo(repos / name)
        human_files = files_for("human", 8, "mod", bias)
        r.commit(human_files, "initial import", old)
        r.commit(files_for("human", 3, "extra", bias), "more modules", "2019-09-02T10:00:00")

        # AI-коммит 1: новые файлы
        ai_new = files_for("ai", 6, "gen", bias)
        r.commit(ai_new, "add generated helpers", new, [TRAILERS[len(made) % len(TRAILERS)]])

        # AI-коммит 2: ДОПИСЫВАЕТ функцию в старый человеческий файл - проверка атрибуции
        target = sorted(human_files)[0]
        lang = next(k for k, v in LANGS.items() if target.endswith(v["ext"]))
        if lang in ("py", "js"):
            appended = human_files[target] + "\n\n" + render(lang, LANGS[lang]["shapes"][0], "invoice", "ai", rng) + "\n"
            r.commit({target: appended}, "extend module", "2025-08-20T10:00:00", [TRAILERS[(len(made) + 1) % len(TRAILERS)]])

        # AI-коммит 3: другим инструментом
        r.commit(files_for("ai", 3, "auto", bias), "more generated code", "2025-09-01T10:00:00",
                 [TRAILERS[(len(made) + 2) % len(TRAILERS)]])
        made[name] = r.path

    r = GitRepo(repos / "legacy_human")
    r.commit(files_for("human", 10, "lib", langs), "initial", old)
    r.commit(files_for("human", 4, "util", langs), "utils", "2019-11-11T10:00:00")
    made["legacy_human"] = r.path

    r = GitRepo(repos / "new_ai")
    r.commit(files_for("ai", 8, "svc", langs), "scaffold", new, [TRAILERS[0]])
    r.commit(files_for("ai", 4, "api", langs), "api layer", "2025-08-30T10:00:00", [TRAILERS[2]])
    made["new_ai"] = r.path

    ai_list = work / "ai_repos.txt"
    human_list = work / "human_repos.txt"
    ai_list.write_text("\n".join(str(made[k]) for k in ("paired_a", "paired_b", "paired_c", "new_ai")) + "\n", encoding="utf-8")
    human_list.write_text("\n".join(str(made[k]) for k in ("paired_a", "paired_b", "paired_c", "legacy_human")) + "\n", encoding="utf-8")
    return {"ai": ai_list, "human": human_list}
