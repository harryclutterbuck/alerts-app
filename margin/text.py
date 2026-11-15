import re
import unicodedata

_WORD = re.compile(r"\w+", re.UNICODE)


def fold(s: str) -> str:
    """Lowercase and strip accents so 'Pérez' matches 'perez'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def tokens(s: str) -> list[str]:
    return _WORD.findall(fold(s))
