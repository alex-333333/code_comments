"""
metrics.py - метрики комментария: пересказ кода (overlap) и прагматические признаки.

Этот модуль - общий знаменатель проекта. Его импортируют и counter.py (анализ
корпуса), и checker.py (pre-commit хук): чекер по построению применяет к новому
коду ту же линейку, которой измерен корпус. Если разнести реализации, чекер
перестанет быть «практическим следствием» исследования.

Главная величина - overlap:

    overlap = |стемы содержательных слов комментария ∩ стемы кода|
              / |стемы содержательных слов комментария|

Интерпретация: какая доля того, что сказано в комментарии, уже видна читателю
в самом коде. overlap ≈ 1 - комментарий пересказывает код (референциальная
функция), overlap ≈ 0 - комментарий сообщает то, чего в коде нет (обычно «почему»).

Почему стемминг, а не точное совпадение слов: «returns»/«return»,
«initializes»/«initialize» - один и тот же пересказ. Почему стемминг, а не
лемматизация: SnowballStemmer не требует скачивания данных и даёт одинаковый
результат на любой машине, а значит, результаты воспроизводимы.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from config import DEFAULT_THETA, MIN_CONTENT_WORDS, ROOT

LEXICON_DIR = ROOT / "lexicon"

# --- нормализация ----------------------------------------------------------

_CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "-": "-", "-": "-"})
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_ISSUE_RE = re.compile(r"(?<![\w&])#\d{1,7}\b|\b[A-Z][A-Z0-9]+-\d+\b|\b(?:gh|GH)-\d+\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9']*")
# Границы camelCase: fooBar -> foo|Bar, HTTPServer -> HTTP|Server
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def normalize(text: str) -> str:
    """Кавычки и тире к ASCII: словари написаны с прямым апострофом."""
    return text.translate(_CURLY)


def strip_refs(text: str) -> str:
    """Ссылки и номера тикетов - это признаки (has_url, has_issue_ref), но не слова
    для сравнения с кодом: 'github', 'com' и '123' не относятся к описанию поведения."""
    return _ISSUE_RE.sub(" ", _URL_RE.sub(" ", text))


# --- словари ----------------------------------------------------------------

def _form_regex(forms: list[str]) -> re.Pattern | None:
    """Одна альтернация на набор форм. Длинные формы идут первыми, иначе
    'for now' перехватывалось бы более короткой формой."""
    forms = sorted({_norm_form(f) for f in forms if f.strip()}, key=len, reverse=True)
    if not forms:
        return None
    body = "|".join(re.escape(f).replace(r"\ ", r"\s+") for f in forms)
    # / входит в набор границ, чтобы 'i/o' и 'and/or' не давали ложных срабатываний.
    return re.compile(rf"(?<![a-z0-9'/])(?:{body})(?![a-z0-9'/])")


def _norm_form(form: str) -> str:
    """Формы из CSV приводятся к тому же виду, что и текст: дефис и '&' разворачиваются."""
    return re.sub(r"\s+", " ", normalize(form).lower().replace("&", " and ").replace("-", " ")).strip()


def _match_view(text: str) -> str:
    """Вид текста для поиска фраз: нижний регистр, дефисы как пробелы."""
    return re.sub(r"\s+", " ", normalize(text).lower().replace("&", " and ").replace("-", " "))


@dataclass
class Marker:
    category: str
    explanatory: bool
    forms: list[str]
    note: str
    regex: re.Pattern | None = None
    group: str = ""       # ключ в Lexicon.groups: к какой научной категории относится маркер
    label_ru: str = ""    # короткое понятное название для дашборда, вместо технического category


@dataclass
class Idiom:
    lemma: str
    forms: list[str]
    category: str
    connotation: str
    note: str
    group: str = ""        # ключ в Lexicon.groups
    category_ru: str = ""  # короткое понятное название категории для дашборда


@dataclass
class Lexicon:
    stopwords: set[str]
    code_verbs: set[str]
    markers: list[Marker]
    idioms: list[Idiom]
    paraphrases: dict[str, set[str]]
    idiom_regex: re.Pattern | None = None
    idiom_lookup: dict[str, str] = field(default_factory=dict)
    # group -> {label_ru, gloss, source}: научное обоснование категорий маркеров и идиом
    # (lexicon/groups.csv), общее для marker_categories.csv и idiom_categories.csv.
    groups: dict[str, dict[str, str]] = field(default_factory=dict)


def _read_lines(path: Path) -> list[str]:
    return [
        ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _read_category_map(path: Path) -> dict[str, dict[str, str]]:
    """category -> строка (group, label_ru...) из маленького файла-классификатора."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {row["category"]: row for row in csv.DictReader(f)}


@lru_cache(maxsize=4)
def load_lexicon(directory: str | None = None) -> Lexicon:
    d = Path(directory) if directory else LEXICON_DIR

    stopwords = {normalize(w).lower() for w in _read_lines(d / "stopwords.txt")}
    code_verbs = {w.lower() for w in _read_lines(d / "code_verbs.txt")}

    groups: dict[str, dict[str, str]] = {}
    with open(d / "groups.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            groups[row["group"]] = {"label_ru": row["label_ru"], "gloss": row["gloss"], "source": row.get("source", "")}

    # Научная классификация категорий (group, label_ru) живёт отдельно от самих словарей
    # маркеров/идиом: так можно переклассифицировать без риска задеть формы или коннотацию.
    marker_cats = _read_category_map(d / "marker_categories.csv")
    idiom_cats = _read_category_map(d / "idiom_categories.csv")

    markers: list[Marker] = []
    with open(d / "pragmatic_markers.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            forms = [x for x in row["forms"].split("|") if x]
            mc = marker_cats.get(row["category"], {})
            markers.append(Marker(row["category"], row["explanatory"].strip() == "1", forms,
                                  row.get("note", ""), _form_regex(forms),
                                  mc.get("group", ""), mc.get("label_ru") or row["category"]))

    idioms: list[Idiom] = []
    lookup: dict[str, str] = {}
    with open(d / "idioms.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            forms = [x for x in row["forms"].split("|") if x]
            ic = idiom_cats.get(row["category"], {})
            idioms.append(Idiom(row["lemma"], forms, row["category"], row["connotation"], row.get("note", ""),
                                ic.get("group", ""), ic.get("label_ru") or row["category"]))
            for fm in forms:
                # При совпадении формы у двух лемм побеждает первая по файлу:
                # порядок в CSV - осознанный приоритет, а не случайность.
                lookup.setdefault(_norm_form(fm), row["lemma"])
    idiom_regex = _form_regex(list(lookup))

    paraphrases: dict[str, set[str]] = {}
    with open(d / "syntax_paraphrases.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            paraphrases.setdefault(row["code_token"].lower(), set()).update(
                w for w in row["comment_words"].split("|") if w)

    return Lexicon(stopwords, code_verbs, markers, idioms, paraphrases, idiom_regex, lookup, groups)


# --- стемминг и токенизация -------------------------------------------------

@lru_cache(maxsize=1)
def _stemmer():
    from nltk.stem.snowball import SnowballStemmer
    return SnowballStemmer("english")


@lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    return _stemmer().stem(word)


def split_identifiers(text: str) -> list[str]:
    """Разбор идентификаторов: snake_case и camelCase -> отдельные слова.

    Применяется и к коду, и к комментарию: авторы часто пишут в комментарии
    `userName` или get_user() прямо как в коде, и без разбиения это слово
    никогда не совпало бы с 'user' и 'name'."""
    out: list[str] = []
    for ident in _IDENT_RE.findall(text):
        for part in ident.split("_"):
            for piece in _CAMEL_RE.split(part):
                piece = piece.lower()
                if len(piece) >= 2 and not piece.isdigit():
                    out.append(piece)
    return out


def comment_words(text: str) -> list[str]:
    """Обычные слова комментария в нижнем регистре, без разбиения camelCase.
    Для облаков слов и частот: там нужны слова языка, а не куски идентификаторов."""
    text = normalize(strip_refs(text)).lower()
    words = []
    for w in _WORD_RE.findall(text):
        w = w[:-2] if w.endswith("'s") else w
        if len(w) >= 2:
            words.append(w)
    return words


def content_tokens(text: str, lex: Lexicon) -> list[str]:
    """Содержательные (не служебные) токены комментария для сравнения с кодом."""
    return [t for t in split_identifiers(normalize(strip_refs(text))) if t not in lex.stopwords]


def code_token_set(code: str) -> set[str]:
    return set(split_identifiers(code))


# --- overlap ---------------------------------------------------------------

@dataclass
class Overlap:
    overlap: float | None
    overlap_ext: float | None
    jaccard: float | None
    n_content: int
    matched: list[str]          # стемы комментария, найденные в коде
    novel: list[str]            # стемы комментария, которых в коде нет


def compute_overlap(text: str, code: str | None, lex: Lexicon | None = None) -> Overlap:
    lex = lex or load_lexicon()
    tokens = content_tokens(text, lex)
    comment_stems = {stem(t) for t in tokens}
    n = len(comment_stems)
    if not code or n < MIN_CONTENT_WORDS:
        # Без кода-цели (detached) или на 1-2 словах доля принимает только
        # значения 0, 1/2, 1 - считать её значит выдумывать сигнал.
        return Overlap(None, None, None, n, [], sorted(comment_stems))

    raw_code = code_token_set(code)
    if not raw_code:
        # Код-цель без единого слова (например, одна '{' у trailing-комментария):
        # overlap=0 выглядел бы как «объясняющий» комментарий чисто из-за артефакта.
        return Overlap(None, None, None, n, [], sorted(comment_stems))
    code_stems = {stem(t) for t in raw_code}
    matched = comment_stems & code_stems

    # Расширенный вариант: синтаксические слова кода (for, if, try...) считаются
    # присутствующими в коде вместе со своими естественно-языковыми пересказами
    # ('loop', 'check', 'handle'). Основная метрика остаётся чисто лексической;
    # ext показывает, насколько вывод зависит от такого допущения.
    ext_stems = set(code_stems)
    for tok in raw_code:
        for w in lex.paraphrases.get(tok, ()):
            ext_stems.add(stem(w))
    matched_ext = comment_stems & ext_stems

    union = comment_stems | code_stems
    return Overlap(
        overlap=len(matched) / n,
        overlap_ext=len(matched_ext) / n,
        jaccard=len(matched) / len(union) if union else None,
        n_content=n,
        matched=sorted(matched),
        novel=sorted(comment_stems - code_stems),
    )


def highlight(text: str, code: str, lex: Lexicon | None = None) -> list[list]:
    """Комментарий, разбитый по словам, с состоянием каждого слова:
    0 - служебное/не слово, 1 - есть в коде, 2 - есть только с учётом парафраз, 3 - нового нет в коде.
    Используется в дашборде, чтобы жюри видело, ЧТО именно совпало с кодом."""
    lex = lex or load_lexicon()
    raw_code = code_token_set(code)
    code_stems = {stem(t) for t in raw_code}
    ext_stems = set(code_stems)
    for tok in raw_code:
        for w in lex.paraphrases.get(tok, ()):
            ext_stems.add(stem(w))
    out = []
    for chunk in re.findall(r"\S+", text):
        toks = [t for t in split_identifiers(normalize(chunk)) if t not in lex.stopwords]
        if not toks:
            out.append([chunk, 0])
            continue
        stems = {stem(t) for t in toks}
        if stems & code_stems:
            out.append([chunk, 1])
        elif stems & ext_stems:
            out.append([chunk, 2])
        else:
            out.append([chunk, 3])
    return out


# --- прагматические признаки -----------------------------------------------

_UPPER_TAGS = re.compile(r"(?<![A-Za-z])(?:BUG|OPTIMI[SZ]E|REVIEW|HACK)(?![A-Za-z])")
_FIRST_I = re.compile(r"(?<![A-Za-z0-9/])I(?![A-Za-z0-9/])")


def marker_hits(text: str, lex: Lexicon | None = None) -> dict[str, list[str]]:
    """Категория -> найденные формы. Регистр нужен только для тегов и местоимения I."""
    lex = lex or load_lexicon()
    view = _match_view(strip_refs(text))
    hits: dict[str, list[str]] = {}
    for m in lex.markers:
        if m.regex is None:
            continue
        found = m.regex.findall(view)
        if m.category == "first_person":
            found = [f for f in found if f != "i"]
            found += _FIRST_I.findall(normalize(text))
        if m.category == "tag":
            found += [t.lower() for t in _UPPER_TAGS.findall(text)]
        if found:
            hits[m.category] = found
    return hits


def has_explanatory_cue(hits: dict[str, list[str]], lex: Lexicon | None = None) -> bool:
    lex = lex or load_lexicon()
    explanatory = {m.category for m in lex.markers if m.explanatory}
    return any(cat in explanatory for cat in hits)


def idiom_hits(text: str, lex: Lexicon | None = None) -> list[str]:
    """Леммы идиом, найденные в тексте (по одной на комментарий)."""
    lex = lex or load_lexicon()
    if lex.idiom_regex is None:
        return []
    view = _match_view(strip_refs(text))
    seen: list[str] = []
    for m in lex.idiom_regex.findall(view):
        lemma = lex.idiom_lookup.get(m)
        if lemma and lemma not in seen:
            seen.append(lemma)
    return seen


# --- форма первого слова (императив / 3-е лицо) -----------------------------

@lru_cache(maxsize=1)
def _spacy_nlp():
    """spaCy опционален: без него признак считается эвристикой по code_verbs.txt.
    MNSK_NO_SPACY=1 принудительно включает эвристику - так результаты одинаковы
    на машинах с spaCy и без."""
    if os.environ.get("MNSK_NO_SPACY") == "1":
        return None
    try:
        import spacy
        return spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"])
    except Exception:
        return None


def nlp_backend() -> str:
    return "spacy" if _spacy_nlp() is not None else "heuristic"


def verb_form(text: str, lex: Lexicon | None = None) -> str | None:
    """'imperative' (Return X), 'third_person' (Returns X), 'gerund' (Returning X) или None.

    Прагматически это различие важно: императив обращён к читателю/исполнителю
    (директив), 3-е лицо описывает код как объект (ассертив). Для проекта это
    признак пересказывающего стиля: комментарий начинается с описания действия кода."""
    lex = lex or load_lexicon()
    m = re.match(r"\W*([A-Za-z]+)", normalize(text))
    if not m:
        return None
    word = m.group(1).lower()

    nlp = _spacy_nlp()
    if nlp is not None:
        first = nlp(text[:200].strip())[0] if text.strip() else None
        if first is not None:
            return {"VB": "imperative", "VBZ": "third_person", "VBG": "gerund"}.get(first.tag_)
        return None

    verbs = lex.code_verbs
    if word in verbs:
        return "imperative"
    if word.endswith("ies") and word[:-3] + "y" in verbs:
        return "third_person"
    if word.endswith("es") and word[:-2] in verbs:
        return "third_person"
    if word.endswith("s") and word[:-1] in verbs:
        return "third_person"
    if word.endswith("ing"):
        base = word[:-3]
        if base in verbs or base + "e" in verbs or (len(base) > 2 and base[-1] == base[-2] and base[:-1] in verbs):
            return "gerund"
    return None


# --- язык -------------------------------------------------------------------

def is_english(text: str, lex: Lexicon | None = None) -> bool:
    """Грубый фильтр «английский ли комментарий».

    Словари и стоп-слова английские; русские, китайские и др. комментарии дали бы
    overlap=0 и выглядели бы «объясняющими» просто потому, что язык не тот.
    Такой артефакт легко принять за результат, поэтому не-английское отсекаю.
    Латиницей написанные испанский/французский отсекаются только по доле
    английских служебных слов - это эвристика, её погрешность описана в Limitations."""
    lex = lex or load_lexicon()
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    if sum(1 for c in letters if c.isascii()) / len(letters) < 0.9:
        return False
    words = comment_words(text)
    if len(words) >= 6:
        stop = sum(1 for w in words if w in lex.stopwords)
        if stop / len(words) < 0.12:
            return False
    return True


# --- тональность ------------------------------------------------------------

@lru_cache(maxsize=1)
def _vader():
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    return SentimentIntensityAnalyzer()


def sentiment(text: str) -> float:
    """VADER compound в [-1, 1]. VADER обучен на соцсетях, для комментариев к коду
    почти всё будет нейтральным - это ограничение, а не баг; поэтому основной
    инструмент коннотации у проекта - словарь идиом, а VADER - вспомогательный."""
    return _vader().polarity_scores(text)["compound"]


# --- сводный разбор ---------------------------------------------------------

def analyze(text: str, code: str | None, lex: Lexicon | None = None, with_sentiment: bool = True) -> dict:
    """Все признаки комментария одним словарём (сериализуется в JSON)."""
    lex = lex or load_lexicon()
    ov = compute_overlap(text, code, lex)
    hits = marker_hits(text, lex)
    stripped = text.strip()
    feat = {
        "n_words": len(comment_words(text)),
        "n_content": ov.n_content,
        "overlap": ov.overlap,
        "overlap_ext": ov.overlap_ext,
        "jaccard": ov.jaccard,
        "matched": ov.matched,
        "cues": {k: len(v) for k, v in hits.items()},
        "explanatory_cue": has_explanatory_cue(hits, lex),
        "idioms": idiom_hits(text, lex),
        "verb_form": verb_form(text, lex),
        "is_question": stripped.endswith("?"),
        "ends_with_period": stripped.endswith("."),
        "starts_upper": stripped[:1].isupper(),
        "has_url": bool(_URL_RE.search(text)),
        "has_issue_ref": bool(_ISSUE_RE.search(text)),
    }
    if with_sentiment:
        feat["sentiment"] = sentiment(text)
    return feat


def classify(feat: dict, theta: float = DEFAULT_THETA, key: str = "overlap") -> str | None:
    """Функция комментария: 'explanatory' | 'referential' | 'other' | None.

    Порядок правил важен: наличие маркера «почему» решает первым - комментарий
    «Loop over items because order matters» пересказывает код, но не только.
    None - когда решить нечем (мало слов и нет маркеров): такие комментарии
    не участвуют в долях, чтобы не искажать знаменатель."""
    if feat.get("explanatory_cue"):
        return "explanatory"
    ov = feat.get(key)
    if ov is None:
        return None
    return "referential" if ov >= theta else "other"
