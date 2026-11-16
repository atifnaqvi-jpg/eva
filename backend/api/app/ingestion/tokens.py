"""Token counting.

The chunker's budget is in tokens (ARCHITECTURE 10.2: 400-800), but no tokenizer
is installed: ``tiktoken``/``transformers`` are not among the granted runtime
dependencies. Counting is therefore an estimate — which is fine here, because the
budget is a quality heuristic, not a billing or context-limit guarantee.

The estimate is calibrated conservatively (English runs ~1.3 tokens/word). It
splits on whitespace and charges an extra token for punctuation-heavy words,
which keeps us from overshooting on dense technical text.
"""

import re

_WORD = re.compile(r"\S+")
_PUNCT = re.compile(r"[^\w\s]")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    words = _WORD.findall(text)
    # ~1.3 tokens/word, plus one extra per 4 punctuation marks.
    return max(1, round(len(words) * 1.3) + len(_PUNCT.findall(text)) // 4)


def fits(text: str, limit: int) -> bool:
    return count_tokens(text) <= limit
