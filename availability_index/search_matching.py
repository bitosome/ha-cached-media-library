"""Conservative title matching shared by browsing and background prioritisation."""
import re
import unicodedata

ARTICLES = frozenset(('a', 'an', 'the'))


def normalized_title(value):
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    # Fold Latin accents, retaining meaningful marks on other alphabets (й).
    letters = []
    latin = False
    for char in unicodedata.normalize('NFKD', value):
        if unicodedata.combining(char):
            if not latin:
                letters.append(char)
        else:
            latin = 'a' <= char <= 'z'
            letters.append(char)
    value = unicodedata.normalize('NFC', ''.join(letters))
    return ' '.join(re.sub(r'[\W_]+', ' ', value).split())


def _without_articles(tokens):
    # A one-word title such as "A" or "The" must remain searchable.
    meaningful = tuple(t for t in tokens if t not in ARTICLES)
    return meaningful if len(tokens) > 1 and meaningful else tokens


def search_score(query, aliases):
    """Return an ascending relevance tuple, or None for no match.

    Words may be reordered and English articles may be omitted/added. The final
    word may be a prefix (normal search-as-you-type behaviour); no edit-distance
    guesses can turn an unrelated title into a match. Evaluate aliases separately.
    """
    query = normalized_title(query)
    if not query:
        return None
    qt = tuple(query.split())
    core = _without_articles(qt)
    if isinstance(aliases, str):
        aliases = aliases.splitlines()
    best = None
    for alias in aliases or ():
        name = normalized_title(alias)
        if not name:
            continue
        nt = tuple(name.split())
        nc = _without_articles(nt)
        # Numeric sequel/year terms identify a particular entry, never a prefix
        # of another number ("Toy Story 2" must not match "Toy Story 20").
        if any(token.isdigit() and token not in nt for token in qt):
            continue
        score = None
        if name == query:
            score = (0, 0, len(name))
        elif core == nc:
            score = (1, 0, len(name))
        elif re.search(r'(?<!\w)' + re.escape(query), name):
            score = (2, len(nt) - len(qt), len(name))
        elif core != qt and re.search(r'(?<!\w)' + re.escape(' '.join(core)), ' '.join(nc)):
            score = (3, len(nc) - len(core), len(name))
        elif all(token in nc for token in core):
            score = (4, len(nc) - len(core), len(name))
        elif len(core) > 1 and all(token in nc for token in core[:-1]) and any(t.startswith(core[-1]) for t in nc):
            score = (5, len(nc) - len(core), len(name))
        if score is not None and (best is None or score < best):
            best = score
    return best
