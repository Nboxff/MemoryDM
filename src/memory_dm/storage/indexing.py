"""Shared text preparation for FTS writes and literal keyword queries."""

import re

_QUESTION_STOP_WORDS = frozenset(
    "a an the i me my we our us you your he his she her it its they their "
    "what which who where when why how do does did is are am was were be been "
    "can could would should will have has had to of in on at for with and or but "
    "user users s".split()
)


def search_terms(text: str) -> list[str]:
    # Add CJK bigrams for unsegmented text; keep Latin words intact for unicode61.
    terms = []
    for token in re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", text.lower()):
        if all("\u3400" <= character <= "\u9fff" for character in token):
            terms.extend(
                token if len(token) == 1 else [token[i : i + 2] for i in range(len(token) - 1)]
            )
        else:
            terms.append(token)
    return terms


def index_text(text: str) -> str:
    return " ".join(search_terms(text))


def query_terms(text: str, *, natural_language: bool = True) -> list[str]:
    """Normalize keywords without allowing shared pronouns to imply relevance."""
    if natural_language:
        # Atomic facts often share this subject. It is not evidence of topic relevance.
        text = re.sub(r"^\s*(?:the )?user(?:'s)?\b", "", text, flags=re.IGNORECASE)
    terms = search_terms(text)
    if natural_language:
        terms = [term for term in terms if term not in _QUESTION_STOP_WORDS]
    return list(dict.fromkeys(terms))


def match_query(text: str, *, natural_language: bool = False) -> str:
    return " OR ".join(
        '"' + term.replace('"', '""') + '"'
        for term in query_terms(text, natural_language=natural_language)
    )


def keyword_span(content: str, terms: list[str], *, window: int = 600) -> tuple[int, int] | None:
    """Locate a compact cluster of distinct keywords in the original character space."""
    if not terms:
        return None
    # Match the same word boundaries used by search_terms, including Latin words
    # adjacent to CJK text or underscores in the original source.
    word_character = r"[^\W_\u3400-\u9fff]"
    patterns = [
        re.escape(term)
        if re.search(r"[\u3400-\u9fff]", term)
        else rf"(?<!{word_character}){re.escape(term)}(?!{word_character})"
        for term in terms
    ]
    hits = list(re.finditer("|".join(patterns), content, flags=re.IGNORECASE))
    if not hits:
        return None
    counts, left, best, best_score = {}, 0, (hits[0].start(), hits[0].end()), (0, 0)
    for right, hit in enumerate(hits):
        term = hit.group().lower()
        counts[term] = counts.get(term, 0) + 1
        while hit.end() - hits[left].start() > window and left < right:
            previous = hits[left].group().lower()
            counts[previous] -= 1
            if not counts[previous]:
                del counts[previous]
            left += 1
        span = (hits[left].start(), hit.end())
        score = (len(counts), -(span[1] - span[0]))
        if score > best_score:
            best, best_score = span, score
    return best
