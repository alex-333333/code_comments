"""Тесты metrics.py: определение overlap, маркеры, идиомы, границы применимости."""

import os

os.environ["MNSK_NO_SPACY"] = "1"   # одинаковый результат на любой машине, со spaCy и без

import pytest

import metrics as m

LEX = m.load_lexicon()


def test_split_identifiers():
    assert m.split_identifiers("getUserName") == ["get", "user", "name"]
    assert m.split_identifiers("HTTPServer_v2") == ["http", "server", "v2"]
    assert m.split_identifiers("x + 1") == []          # односимвольные и числа отбрасываются


def test_overlap_full_restatement():
    o = m.compute_overlap("Return the user list", "def get_users():\n    return user_list", LEX)
    assert o.overlap == 1.0 and o.n_content == 3


def test_overlap_explanatory_comment_is_low():
    o = m.compute_overlap("Retry because the upstream API drops idle connections",
                          "for attempt in range(3):\n    client.get(url)", LEX)
    assert o.overlap is not None and o.overlap < 0.2


def test_overlap_none_when_too_short_or_no_code():
    assert m.compute_overlap("Loop items", "for item in items: pass", LEX).overlap is None
    assert m.compute_overlap("Loop over each item in the list", None, LEX).overlap is None


def test_overlap_none_when_code_has_no_words():
    # '{' - единственное, что в коде на строке с trailing-комментарием: делить не на что
    assert m.compute_overlap("this opens the main block here", "{", LEX).overlap is None


def test_stemming_matches_inflections():
    o = m.compute_overlap("Initializes counters and returns totals", "def initialize(counter): return total", LEX)
    assert o.overlap == 1.0


def test_paraphrase_overlap_is_at_least_strict_and_catches_syntax_words():
    text, code = "Loop over each item in the list", "for item in list:\n    pass"
    o = m.compute_overlap(text, code, LEX)
    assert o.overlap_ext >= o.overlap
    assert o.overlap_ext == 1.0 and o.overlap < 1.0     # 'loop' объясняется только через for -> loop


def test_urls_and_issue_refs_do_not_count_as_words():
    o = m.compute_overlap("See https://github.com/x/y/issues/1 and #412 for details about parsing", "def parse(): pass", LEX)
    assert "github" not in o.novel and "412" not in o.novel


def test_highlight_marks_matched_and_novel_words():
    hl = dict((w, s) for w, s in m.highlight("Return the user quickly", "return user", LEX))
    assert hl["Return"] == 1 and hl["user"] == 1 and hl["quickly"] == 3 and hl["the"] == 0


@pytest.mark.parametrize("text,cat", [
    ("Retry because the API drops connections", "cause"),
    ("HACK: pad the empty lines", "tag"),
    ("TODO fix later", "tag"),
    ("Careful, this mutates the list", "warning"),
    ("See #412 for the background", "reference"),
    ("Probably fine for now", "hedge"),
    ("We should not touch this", "first_person"),
])
def test_marker_categories(text, cat):
    assert cat in m.marker_hits(text, LEX)


@pytest.mark.parametrize("text", [
    "review the diff before merging",     # 'review' - тег только заглавными
    "reads from stdin and I/O buffers",   # 'I/O' - не местоимение
    "Initialize the counter",
])
def test_marker_false_positives_avoided(text):
    hits = m.marker_hits(text, LEX)
    assert "tag" not in hits and "first_person" not in hits


def test_explanatory_cue_flag():
    assert m.has_explanatory_cue(m.marker_hits("Skip this because it is slow", LEX), LEX)
    assert not m.has_explanatory_cue(m.marker_hits("Probably fine", LEX), LEX)    # хедж - не «почему»


def test_idioms_longest_form_wins_and_dedup():
    assert m.idiom_hits("This is quick and dirty, a real hack, such a hack", LEX) == ["quick and dirty", "hack"]
    assert m.idiom_hits("off-by-one and a race condition", LEX) == ["off by one", "race condition"]
    assert m.idiom_hits("Initialize the counter", LEX) == []


def test_verb_form_heuristic():
    assert m.verb_form("Return the value", LEX) == "imperative"
    assert m.verb_form("Returns the value", LEX) == "third_person"
    assert m.verb_form("Initializing the state", LEX) == "gerund"
    assert m.verb_form("The value is returned", LEX) is None
    assert m.nlp_backend() == "heuristic"


def test_is_english():
    assert m.is_english("Retry because the server drops idle connections", LEX)
    assert m.is_english("Convert x", LEX)
    assert not m.is_english("Повторяем запрос, потому что сервер обрывает соединение", LEX)
    assert not m.is_english("这是一个测试注释", LEX)
    assert not m.is_english("caché pour éviter de recharger les données du serveur à chaque fois", LEX)


def test_classify_order_of_rules():
    f = m.analyze("Loop over each item in the list because order matters", "for item in list: pass", LEX, False)
    assert m.classify(f) == "explanatory"                 # маркер «почему» побеждает высокий overlap
    g = m.analyze("Loop over each item in the list", "for item in list: pass", LEX, False)
    assert m.classify(g, 0.5) == "referential"
    assert m.classify(g, 0.99) == "other"                 # порог меняет результат ровно как задумано
    h = m.analyze("ok", None, LEX, False)
    assert m.classify(h) is None                          # решить нечем -> в доли не входит


def test_analyze_has_stable_keys():
    f = m.analyze("Returns the user.", "def get_user(): pass", LEX)
    for key in ("overlap", "overlap_ext", "cues", "explanatory_cue", "idioms", "verb_form", "sentiment", "ends_with_period", "starts_upper"):
        assert key in f
    assert f["ends_with_period"] and f["starts_upper"]
