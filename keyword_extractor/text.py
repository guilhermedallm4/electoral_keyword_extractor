"""Normalização de texto usada para comparar (nunca para exibir: a forma original mantém acentos)."""
import html
import re
import unicodedata

PARTICLES = {"da", "de", "do", "das", "dos", "e", "d"}
STOPWORDS = PARTICLES | {
    "a", "o", "as", "os", "em", "no", "na", "nos", "nas", "um", "uma", "para", "por", "com",
    "sem", "que", "ao", "aos", "the", "of", "and",
}
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[#\w]+(?:[-'][\w]+)*", re.UNICODE)


def strip_html(s: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def fold(s: str) -> str:
    """Minúsculas e sem acentos: 'JOÃO' -> 'joao'."""
    return strip_accents(s.casefold())


def stem(tok: str) -> str:
    """Stemming mínimo para plural: 'propostas' -> 'proposta', 'eleições' -> 'eleicao'."""
    if len(tok) > 4 and tok.endswith("oes"):
        return tok[:-3] + "ao"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def tokens(s: str, drop_stopwords: bool = False) -> list[str]:
    out = [stem(t) for t in _TOKEN.findall(fold(s))]
    return [t for t in out if t not in STOPWORDS] if drop_stopwords else out


_ORDINALS = {"1o": "primeiro", "1a": "primeira", "2o": "segundo", "2a": "segunda"}


def key_tokens(s: str) -> list[str]:
    """Tokens de comparação: sem caixa, acentos, plural e palavras vazias; '1º turno' = 'primeiro turno'."""
    return [_ORDINALS.get(t, t) for t in tokens(s.replace('"', " "), drop_stopwords=True)]


def entity_key(s: str) -> str:
    """Chave de identidade: 'João da Silva', 'JOAO DA SILVA', 'João Silva' -> 'joao silva';
    'debate na Globo' e 'debate da Globo' -> 'debate globo'."""
    return " ".join(key_tokens(s))


def contains_phrase(text_toks: list[str], phrase_toks: list[str]) -> bool:
    """`phrase_toks` aparece contígua em `text_toks` (ambos via key_tokens)."""
    n = len(phrase_toks)
    if n == 0:
        return False
    return any(text_toks[i:i + n] == phrase_toks for i in range(len(text_toks) - n + 1))


def term_regexes(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    """Compila termos para busca com fronteira de palavra no texto já 'folded'."""
    return [(t, re.compile(r"(?<![\w])" + re.escape(fold(t)) + r"(?![\w])")) for t in terms]
