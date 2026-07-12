"""
Identifier-segment utilities — Python translation of codegraph/src/search/identifier-segments.ts.

Splits symbol names into lowercase word segments for the name_segment_vocab table,
so natural-language prompt words can be verified against the graph.
"""

from __future__ import annotations

import re
import unicodedata

MIN_SEGMENT_CHARS = 2
MAX_SEGMENT_CHARS = 32
MAX_SEGMENTS_PER_NAME = 12

MAX_PROSE_CANDIDATES = 16
MIN_PROSE_CHARS = 4
MAX_PROSE_CHARS = 24

# Unicode property escapes (Python re doesn't support \p{L} natively, use regex module or patterns)
# We use the `regex` third-party module for full Unicode property support.
# Fallback: use str.isalpha() based splitting for ASCII-heavy codebases.
try:
    import regex as _re
    _HAS_REGEX = True
except ImportError:
    import re as _re
    _HAS_REGEX = False


def _word_runs(name: str) -> list[str]:
    """Extract alphanumeric runs from a string using Unicode-aware regex."""
    if _HAS_REGEX:
        return _re.findall(r"[\p{L}\p{N}]+", name)
    else:
        # Fallback: ASCII + common Unicode via \w (includes underscore, strip later)
        return [r for r in _re.findall(r"[\w]+", name) if any(c.isalnum() for c in r)]


def _split_camel_case(run: str) -> list[str]:
    """
    Split a single alphanumeric run into camelCase / PascalCase / acronym parts.

    Splits before an Upper that follows lower/digit (camelCase hump), and
    before the last Upper of an acronym run when a lowercase follows
    ("HTMLParser" -> HTML | Parser).
    """
    if _HAS_REGEX:
        parts = _re.split(
            r"(?<=[\p{Ll}\p{N}])(?=\p{Lu})|(?<=\p{Lu})(?=\p{Lu}\p{Ll})",
            run,
        )
    else:
        # Fallback: manual camelCase split
        parts = []
        current = ""
        for i, ch in enumerate(run):
            if ch.isupper() and current:
                prev = run[i - 1] if i > 0 else ""
                next_ch = run[i + 1] if i + 1 < len(run) else ""
                if prev.islower() or prev.isdigit():
                    parts.append(current)
                    current = ch
                elif next_ch and next_ch.islower() and prev.isupper():
                    parts.append(current)
                    current = ch
                else:
                    current += ch
            else:
                current += ch
        if current:
            parts.append(current)
    return [p for p in parts if p]


def split_identifier_segments(name: str) -> list[str]:
    """
    Split a symbol or file name into lowercase word segments (identifier-segments.ts:30-47).

    "OrderStateMachine" -> ["order", "state", "machine"]
    "HTMLParser" -> ["html", "parser"]
    "base64Encode" -> ["base64", "encode"]
    """
    if not name:
        return []

    out: set[str] = set()
    for run in _word_runs(name):
        parts = _split_camel_case(run)
        for part in parts:
            if len(out) >= MAX_SEGMENTS_PER_NAME:
                return list(out)
            seg = part.lower()
            if len(seg) < MIN_SEGMENT_CHARS or len(seg) > MAX_SEGMENT_CHARS:
                continue
            if _is_digit_only(seg):
                continue
            out.add(seg)

    return list(out)


def _is_digit_only(s: str) -> bool:
    """Check if a string is all digits (Unicode-aware)."""
    if _HAS_REGEX:
        return bool(_re.match(r"^\p{N}+$", s))
    return s.isdigit()


def normalize_prose_word(word: str) -> str:
    """
    Normalize a prose word for segment lookup (identifier-segments.ts:56-58).

    Lowercase + strip diacritics (NFD, drop combining marks), so "références"
    matches the segment "references".
    """
    nfd = unicodedata.normalize("NFD", word)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return stripped.lower()


# English prompt words that are never evidence a symbol was NAMED
# (identifier-segments.ts:81-104).
_ENGLISH_PROSE_STOPWORDS = frozenset([
    "about", "above", "actually", "after", "again", "against", "almost", "along", "also", "always",
    "another", "anything", "around", "away", "back", "because", "been", "before", "behind", "being",
    "below", "best", "better", "between", "both", "cannot", "come", "could", "does", "doing", "done",
    "down", "each", "either", "else", "even", "ever", "every", "everything", "fine", "first", "from",
    "getting", "give", "goes", "going", "gone", "good", "great", "have", "having", "help", "here",
    "inside", "instead", "into", "just", "keep", "know", "last", "least", "less", "like", "likely",
    "little", "look", "looking", "made", "make", "making", "many", "maybe", "mind", "more", "most",
    "much", "must", "need", "needs", "never", "next", "nice", "none", "nothing", "okay", "only",
    "onto", "other", "otherwise", "over", "please", "pretty", "probably", "quite", "rather", "really",
    "right", "same", "seem", "seems", "should", "show", "since", "some", "someone", "something",
    "somewhere", "soon", "still", "such", "sure", "take", "than", "thank", "thanks", "that", "their",
    "them", "then", "there", "these", "they", "thing", "things", "think", "this", "those", "though",
    "tried", "tries", "trying", "under", "until", "upon", "very", "want", "wants", "well", "went",
    "were", "what", "when", "which", "while", "will", "wish", "with", "within", "without", "would",
    "wrong", "your", "yours",
    "again", "change", "changes", "check", "class", "classes", "code", "detail", "details",
    "directory", "error", "errors", "example", "examples", "file", "files", "folder", "function",
    "functions", "issue", "issues", "line", "lines", "method", "methods", "name", "names", "problem",
    "problems", "project", "question", "questions", "rename", "test", "tests", "type", "types",
    "update", "value", "values", "warning", "warnings", "work", "working", "write", "writing",
])


def extract_prose_candidates(prompt: str) -> list[str]:
    """
    Candidate words from a prompt for segment-vocabulary lookup (identifier-segments.ts:114-127).
    """
    if not prompt:
        return []

    seen: set[str] = set()
    for run in _word_runs(prompt):
        if len(seen) >= MAX_PROSE_CANDIDATES:
            break
        if len(run) > MAX_PROSE_CHARS:
            continue
        w = normalize_prose_word(run)
        if len(w) < MIN_PROSE_CHARS or len(w) > MAX_PROSE_CHARS:
            continue
        if _is_digit_only(w):
            continue
        if w in _ENGLISH_PROSE_STOPWORDS:
            continue
        seen.add(w)

    return list(seen)


def segment_lookup_variants(word: str) -> list[str]:
    """
    Lookup variants for a prose word: the word itself plus light plural folding
    (identifier-segments.ts:147-160).
    """
    variants = [word]
    can_strip_2 = len(word) >= MIN_PROSE_CHARS + 2
    can_strip_1 = len(word) >= MIN_PROSE_CHARS + 1

    if re.search(r"(?:x|sh|ss|zz)es$", word):
        if can_strip_2:
            variants.append(word[:-2])
    elif re.search(r"(?:ch|s|z|o)es$", word):
        if can_strip_2:
            variants.append(word[:-2])
        if can_strip_1:
            variants.append(word[:-1])
    elif word.endswith("s") and not word.endswith("ss"):
        if can_strip_1:
            variants.append(word[:-1])

    return variants
