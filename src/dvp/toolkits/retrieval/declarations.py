"""Tokenization for local JSONL declaration retrieval."""

from __future__ import annotations

import re


WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*|\d+")


SYMBOL_WORDS = {
    "ℕ": " Nat ",
    "ℤ": " Int ",
    "ℚ": " Rat ",
    "ℝ": " Real ",
    "ℂ": " Complex ",
    "∀": " forall ",
    "∃": " exists ",
    "→": " implies ",
    "↔": " iff ",
    "≤": " le ",
    "≥": " ge ",
    "≠": " ne ",
    "∈": " mem ",
    "∑": " sum ",
    "∏": " prod ",
}



def _normalize_text(text: str) -> str:
    for symbol, word in SYMBOL_WORDS.items():
        text = text.replace(symbol, word)
    return text


def _split_identifier(word: str) -> list[str]:
    parts: list[str] = []
    for dotted in word.replace("'", "").split("."):
        if not dotted:
            continue
        camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", dotted)
        camel = camel.replace("_", " ")
        parts.extend(part.lower() for part in camel.split() if part)
    return parts


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in WORD_RE.findall(_normalize_text(text)):
        tokens.extend(_split_identifier(word))
    return [token for token in tokens if len(token) > 1 or token.isdigit()]
