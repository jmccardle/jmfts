"""Text chunking strategies for document decomposition.

Splits raw text into smaller pieces (sentences, paragraphs, or fixed
token-count windows) for tree-building pipelines.  Step 2 of the 4-step
pipeline: structural split → **chunk** → PELT group → summarize.

Pure text operations — no database or model dependencies.
"""

import re
from dataclasses import dataclass
from enum import Enum

from jmfts_core.config import get_settings


class ChunkStrategy(str, Enum):
    sentence = "sentence"
    sentence_packed = "sentence_packed"
    paragraph_packed = "paragraph_packed"
    paragraph = "paragraph"
    token_count = "token_count"


@dataclass
class Chunk:
    """A text chunk with position metadata."""

    text: str
    index: int  # ordinal position in the chunk sequence
    char_start: int  # start character offset in original text
    char_end: int  # end character offset in original text


#: Words whose trailing dot ends an abbreviation rather than a sentence.
_ABBREVS = (
    r"Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|Inc|Ltd|Corp"
    r"|Ave|Blvd|Dept|Est|Fig|Gen|Gov|Rev|Sgt|Spc"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    r"|approx|dept|est|govt|misc|tech|temp|vol"
)

#: A candidate sentence boundary: end punctuation, whitespace, then something that can
#: open a sentence. The lookahead is what keeps `et al. 2017.` together — a digit cannot
#: start a sentence — and it does the work no abbreviation list can enumerate.
_SENTENCE_BOUNDARY = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'\(\[])')

#: The same list, anchored to the end of the text preceding a candidate boundary.
_ENDS_IN_ABBREV = re.compile(rf"\b({_ABBREVS})\.$", re.IGNORECASE)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on end punctuation, skipping known abbreviations.

    Each candidate boundary is REJECTED rather than the text being rewritten. The
    previous implementation substituted abbreviation dots for a NUL placeholder and
    substituted them back afterwards, which silently turned any NUL already in the input
    into a period — the corrupting-in-place failure that an in-band placeholder always
    risks, and reachable from every non-PDF ingest path.
    """
    parts: list[str] = []
    last = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        head = text[last : match.start()]
        if _ENDS_IN_ABBREV.search(head):
            continue  # "Dr." is not the end of a sentence
        parts.append(head)
        last = match.end()
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def _split_sentences_packed(text: str, max_tokens: int = 120) -> list[str]:
    """Fill consecutive sentences up to ``max_tokens`` words. Never splits a sentence.

    The `sentence` strategy emits one chunk per sentence, so a 12-word sentence and a
    90-word sentence become equally weighted siblings, and roughly a fifth of the nodes
    over a paper corpus are too short to carry a retrievable idea. Packing to a budget is
    the one thing a boundary-based splitter cannot do on its own.

    The budget is a word count, matching `_split_token_count`'s proxy. It is a target and
    not a cap: a single sentence longer than the budget is emitted whole, because cutting
    mid-sentence is the thing this strategy exists to avoid. ``chunk_text``'s
    ``max_chars`` still bounds the result, which is where a runaway sentence — a
    reference list with no end punctuation — actually gets cut.
    """
    packed: list[str] = []
    current: list[str] = []
    words = 0
    for sentence in _split_sentences(text):
        length = len(sentence.split())
        if current and words + length > max_tokens:
            packed.append(" ".join(current))
            current, words = [], 0
        current.append(sentence)
        words += length
    if current:
        packed.append(" ".join(current))
    return packed


def _split_paragraphs_packed(text: str, max_tokens: int = 120) -> list[str]:
    """Pack sentences to a word budget, but NEVER across a paragraph boundary.

    `sentence_packed` fills its budget from the section body as one stream, so a blank
    line — the one boundary the author actually wrote — is invisible to it. Measured on a
    three-paragraph section of five, six and three sentences (ABCDE / FGHIJK / LMN), it
    returns ``ABCDEFGHI`` and ``JKLMN``: two chunks, neither of which is a paragraph, and
    both boundaries placed where the word budget ran out rather than where the author
    stopped. The pieces are a reasonable SIZE (593 and 329 characters) and cut in the
    wrong PLACE, which is why a size histogram does not show the problem.

    A paragraph break is a claim by the author that what follows is a separate unit.
    Packing across it fuses two claims and splits one; this splits per paragraph first and
    packs only within one, so a paragraph that fits the budget comes back whole and is its
    own chunk. Paragraphs are NOT merged when they are short: merging is what
    ``min_chunk_length`` decides in :func:`chunk_text`, and doing it here would reintroduce
    the same fusion by another route.

    The budget still applies inside an oversized paragraph, where a boundary has to be
    invented because the document declares none. That is the only case where this differs
    from "one chunk per paragraph".
    """
    packed: list[str] = []
    for paragraph in _split_paragraphs(text):
        packed.extend(_split_sentences_packed(paragraph, max_tokens=max_tokens))
    return packed


def _split_paragraphs(text: str) -> list[str]:
    """Split text on paragraph boundaries (double newlines or more)."""
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _split_token_count(text: str, max_tokens: int = 200, overlap: int = 0) -> list[str]:
    """Split text into windows of approximately max_tokens words.

    Uses whitespace tokenisation (word count) as a fast proxy for
    subword token count.
    """
    words = text.split()
    if not words:
        return []

    step = max(1, max_tokens - overlap)
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + max_tokens, len(words))
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += step

    return chunks


def _enforce_max_chars(text: str, max_chars: int) -> list[str]:
    """Split a chunk that is still over budget, whatever the strategy produced.

    Boundary-based strategies (sentence, paragraph) split on *boundaries* and so
    have no size bound at all: one long paragraph — which is what most assistant
    messages are — comes back whole.  And a word-based splitter cannot split text
    that contains no whitespace: a base64 blob, a line of minified JS, a data URI
    are each a single "word" no amount of `text.split()` will divide.

    So the last resort is a character boundary.  Splitting mid-word is ugly;
    embedding 8% of a document and calling it done is worse.
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    current = ""

    for word in text.split():
        if len(word) > max_chars:
            # No whitespace to split on — cut the word itself (D4).
            if current:
                pieces.append(current)
                current = ""
            for i in range(0, len(word), max_chars):
                pieces.append(word[i : i + max_chars])
            continue

        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            pieces.append(current)
            current = word

    if current:
        pieces.append(current)

    return pieces


#: Below this character budget, halving cannot converge — the budget approaches the
#: length of the model's own prefix and special tokens, so a piece that still does not
#: fit is not going to start fitting. Reaching it is a real failure and :func:`chunk_text`
#: raises. In practice it is unreachable: no tokenizer turns 64 characters into more than
#: 64 tokens, and every window in this appliance is larger than that.
_MIN_FIT_CHARS = 64


def _enforce_fit(text: str, max_chars: int, fits) -> list[str]:
    """Split until every piece MEASURABLY fits the window, halving the budget as it goes.

    The character cap is a proxy for the token window, and the exchange rate is not a
    constant: English prose runs about five characters to a subword token, while tables,
    code, citations and CJK run two or three. So a piece inside ``chunk_max_chars`` can
    still be over the window — which is the shape of KNOWN-DEFECTS D7, where a 1,799-char
    markdown paragraph tokenised to 557 and failed a 512-token embed permanently.

    Measuring is the only way to know, so the caller passes a predicate that measures.
    Halving rather than computing a target length is deliberate: the ratio that made the
    first guess wrong would make a computed second guess wrong too, and halving converges
    without needing to know it.
    """
    if fits(text):
        return [text]
    if max_chars <= _MIN_FIT_CHARS:
        raise ValueError(
            f"a {len(text)}-character piece does not fit the embedding window even at a "
            f"{max_chars}-character budget; halving further cannot converge"
        )

    # Halve from whichever is smaller. When the budget is already larger than the text,
    # halving the budget alone would return the same piece unchanged and recurse without
    # progress.
    budget = max(min(max_chars, len(text)) // 2, _MIN_FIT_CHARS)
    pieces: list[str] = []
    for piece in _enforce_max_chars(text, budget):
        pieces.extend(_enforce_fit(piece, budget, fits))
    return pieces


def chunk_text(
    text: str,
    strategy: ChunkStrategy = ChunkStrategy.sentence,
    max_tokens: int = 200,
    overlap: int = 0,
    min_chunk_length: int = 1,
    max_chars: int | None = None,
    fits=None,
) -> list[Chunk]:
    """Split text into chunks using the specified strategy.

    Every chunk returned is guaranteed to be at most `max_chars` characters.
    That guarantee is the point of the function: chunks exist to be embedded, and
    an over-long chunk is embedded as a truncated prefix (see KNOWN-DEFECTS D1).
    Before the cap existed, `chunk_text` could return the document unchanged and
    report success — chunking that defeats its own purpose.

    A character cap is a PROXY for the window the chunk has to fit, and a proxy is only
    as good as its exchange rate (KNOWN-DEFECTS D7). Pass `fits` to replace the proxy
    with a measurement: the caller supplies the predicate that knows the real window —
    `EmbeddingService.fits_token_window` is that predicate — and every returned chunk
    then satisfies it, whatever the text tokenises at.

    Args:
        text: The text to chunk.
        strategy: Chunking strategy (sentence, sentence_packed, paragraph, token_count).
        max_tokens: Target word count per chunk (token_count and sentence_packed only).
        overlap: Word overlap between consecutive chunks (token_count only).
        min_chunk_length: Minimum character length for a chunk.  Shorter
            chunks are merged with the previous chunk, never past `max_chars`.
        max_chars: Hard upper bound on chunk size, enforced for *every* strategy
            after splitting and after merging.  Defaults to settings.chunk_max_chars.
        fits: Optional `(str) -> bool` predicate saying whether a piece fits the window
            it is being chunked FOR. Applied after merging, so a merge cannot put a
            chunk back over it. `min_chunk_length` yields to it: a piece may come back
            shorter than the preference, because fitting the window is a requirement and
            the minimum length is not.

    Returns:
        List of Chunk objects with text and position metadata.

    Raises:
        ValueError: If text is empty, strategy is unknown, max_chars < 1, or `fits`
            rejects a piece that can no longer be halved.
    """
    if not text or not text.strip():
        raise ValueError("Cannot chunk empty text")

    if max_chars is None:
        max_chars = get_settings().chunk_max_chars
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")

    if strategy == ChunkStrategy.sentence:
        raw_chunks = _split_sentences(text)
    elif strategy == ChunkStrategy.sentence_packed:
        raw_chunks = _split_sentences_packed(text, max_tokens=max_tokens)
    elif strategy == ChunkStrategy.paragraph_packed:
        raw_chunks = _split_paragraphs_packed(text, max_tokens=max_tokens)
    elif strategy == ChunkStrategy.paragraph:
        raw_chunks = _split_paragraphs(text)
    elif strategy == ChunkStrategy.token_count:
        raw_chunks = _split_token_count(text, max_tokens=max_tokens, overlap=overlap)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    # Bound every chunk, regardless of strategy.  `max_tokens` was honoured by
    # token_count only — its own docstring admitted as much — so sentence and
    # paragraph had no size bound whatsoever (D2a), and no word-based strategy
    # could bound text with no word boundaries (D4).
    raw_chunks = [piece for chunk in raw_chunks for piece in _enforce_max_chars(chunk, max_chars)]

    # Merge chunks shorter than min_chunk_length into the previous chunk —
    # but never past the cap.  Unbounded, this loop glued every below-threshold
    # chunk onto the first one forever: 90 short sentences merged into a single
    # 6,840-char chunk, i.e. the document chunked into itself (D2b).
    if min_chunk_length > 1 and len(raw_chunks) > 1:
        merged = [raw_chunks[0]]
        for chunk in raw_chunks[1:]:
            if len(chunk) < min_chunk_length and len(merged[-1]) + 1 + len(chunk) <= max_chars:
                merged[-1] = merged[-1] + " " + chunk
            else:
                merged.append(chunk)
        raw_chunks = merged

    # After the merge, not before: two pieces that each fit can merge into one that does
    # not, and the merge above is bounded in characters, which is exactly the unit that
    # cannot answer the question.
    if fits is not None:
        raw_chunks = [
            piece for chunk in raw_chunks for piece in _enforce_fit(chunk, max_chars, fits)
        ]

    # If everything merged into nothing, bail
    if not raw_chunks:
        raise ValueError("Text produced no chunks with the given strategy")

    # Build Chunk objects with character offsets
    chunks = []
    search_start = 0
    for i, chunk_text_str in enumerate(raw_chunks):
        pos = text.find(chunk_text_str, search_start)
        if pos >= 0:
            char_start = pos
            char_end = pos + len(chunk_text_str)
            search_start = char_end
        else:
            # Token-count strategy re-joins words so exact match may fail;
            # approximate from current position
            char_start = search_start
            char_end = char_start + len(chunk_text_str)
            search_start = char_end

        chunks.append(
            Chunk(
                text=chunk_text_str,
                index=i,
                char_start=char_start,
                char_end=char_end,
            )
        )

    return chunks
