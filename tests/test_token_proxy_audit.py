"""No character or word count may stand in for a token count unless it says so here (issue #9).

Why this exists: a token is whatever the model's tokenizer says it is, and the exchange rate
to characters or words is a property of the text, not a constant (``KNOWN-DEFECTS`` D7 --
code, citations and CJK run two or three characters per token, prose four). A count taken
with ``len(text)`` or ``len(text.split())`` and then called a token count is wrong by an
amount nobody can see, and wrong in the direction that silently truncates.

What it holds, by reading the syntax tree of everything a wheel ships (no database, no
tokenizer, no network):

1. A name containing ``token`` is not assigned ``len(...)`` or ``.split()`` of anything --
   unless it is the tokenizer's output or a real token list, which is an entry in
   ``MEASURED_TOKEN_COUNTS`` below (or not a model token at all: ``LEXICAL_TERMS``).
2. ``len(...)`` and anything named for chars, words or tokens is not multiplied or divided
   by a conversion constant (3, 4, 0.75, 1.3, 1.33, 1.5). There are none today; this is the
   standing guard.
3. Every definition of ``max_tokens`` is a real completion budget handed to a model API
   (``COMPLETION_BUDGETS``). The word-count one was renamed ``max_words`` (issue #1), so a
   new word budget named ``max_tokens`` fails here rather than repeating it.
4. Every definition of a ``max_chars``-style cap (``max_chars``, ``chunk_max_chars``,
   ``_MIN_FIT_CHARS``, ``..._MAX_CHARS_...``) is a deliberate character budget named as one,
   listed in ``CHARACTER_CAPS``.

The allowlists key on SYMBOLS -- ``module:qualified.name:identifier`` -- never on line
numbers, which drift with every edit. Each entry must still match something: a stale entry
fails the test, so deleting the code deletes its excuse.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Source roots that go into a wheel. ``_verbs.py`` is generated from the contracts and
#: carries no definition of its own.
SHIPPED_ROOTS = ("jmfts_core", "jmfts_batch", "jmfts-client/jmfts_client", "jmfts-web/jmfts_web")
GENERATED = {"_verbs.py"}

#: A token-named value taken with ``len(...)`` that IS a token measurement.
MEASURED_TOKEN_COUNTS = {
    # len(tokenizer(...)["input_ids"]) and len of the tokenizer's prefix ids
    "jmfts_core.embedding:EmbeddingService.check_fit:token_count",
    "jmfts_core.embedding:EmbeddingService.embed_with_tokens:prefix_token_count",
    "jmfts_core.embedding:EmbeddingService.embed_batch_with_tokens:prefix_token_count",
    # rows of stored token embeddings: tokens the model produced, counted
    "jmfts_core.services.document_service:DocumentService.get_document_tokens:token_count",
    # len(sorted_tokens): the document's real token embeddings, ranked, then tiered
    "jmfts_core.repositories.document:DocumentRepository.embed_document:n_tokens",
}

#: Not model tokens at all: lexical terms of the BM25 index, and a count of them.
LEXICAL_TERMS = {
    "jmfts_core.repositories.search:SearchRepository._tokenize:tokens",
}

#: There is no longer a word-count ``max_tokens``. The chunking budget was one, under eight
#: definitions, and issue #1 renamed it ``max_words`` in 0.7.0; this audit's stale-entry
#: check is what retired the allowlist that excused it. A new ``max_tokens`` has to be a
#: completion budget below, or be named for what it counts.
#:
#: ``max_tokens`` that is a real token budget, forwarded to an LLM endpoint as the
#: completion limit. The endpoint's tokenizer counts it, not us.
COMPLETION_BUDGETS = {
    "jmfts_core.config:Settings:synthesis_max_tokens",
    "jmfts_core.config:Settings:extraction_max_tokens",
    "jmfts_core.llm_client:build_model:max_tokens",
    "jmfts_batch.provider:BatchRequest:max_tokens",
    "jmfts_batch.providers.anthropic_api:_as_request:max_tokens",
    "jmfts_batch.providers.mock:local_llm_chat.chat:max_tokens",
    "jmfts_batch.providers.mock:_encode:max_tokens",
    "jmfts_core.llm_client:complete:max_tokens",
    "jmfts_core.llm_client:complete_sync:max_tokens",
    "jmfts_batch.summarize:prepare:max_tokens",
    "jmfts_batch.worker:BatchWorker.__init__:max_tokens",
}

#: Character budgets that remain on purpose. They say "chars" in the name, they bound a
#: piece's size, and none is read as a token count; where a token window matters
#: ``EmbeddingService.fits_token_window`` is the measurement (``chunking._enforce_fit``).
CHARACTER_CAPS = {
    "jmfts_core.config:Settings:chunk_max_chars",
    "jmfts_core.chunking:_enforce_max_chars:max_chars",
    "jmfts_core.chunking:_enforce_fit:max_chars",
    # Packing to the window (#2) holds chunk_text's character guarantee too; the window
    # itself is the `fits` measurement beside it, not this.
    "jmfts_core.chunking:_split_sentences_to_fit:max_chars",
    "jmfts_core.chunking:chunk_text:max_chars",
    # The ceiling EXPLAIN declares for a chunking run is a function of the same cap (#5).
    "jmfts_core.chunking:chunk_count_ceiling:max_chars",
    "jmfts_core.structure_tasks:_chunk_ceiling:max_chars",
    "jmfts_core.chunking:<module>:_MIN_FIT_CHARS",
    "jmfts_core.probe:<module>:SCANNED_MAX_CHARS_PER_PAGE",
    "jmfts_core.repositories.view:_snippet_around:max_chars",
    "jmfts_client.contracts.document:ChunkRequest:max_chars",
}

#: Multiplications by a ``_CONVERSION`` constant that are not a chars-per-token ratio.
#: ``chunk_count_ceiling``'s 3 is from its proof: adjacent pieces of one ``fits`` split hold
#: more than ``_MIN_FIT_CHARS`` together, so a length splits into at most
#: ``3 * length / _MIN_FIT_CHARS`` pieces. Characters in, a count of pieces out (#5).
NOT_CONVERSIONS = {
    "jmfts_core.chunking:chunk_count_ceiling:3 * characters",
}

_TOKEN_NAME = re.compile(r"token", re.I)
_MAX_TOKENS = re.compile(r"max_tokens$")
_CHAR_CAP = re.compile(r"(max|min)_(fit_)?chars", re.I)
_CONVERSION = {3, 4, 0.25, 0.75, 1.3, 1.33, 1.5}
_UNIT_WORDS = re.compile(r"char|word|token", re.I)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO).with_suffix("").parts)
    if parts[0] in ("jmfts-client", "jmfts-web"):
        parts = parts[1:]
    return ".".join(parts)


def _target_names(node: ast.AST):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _target_names(elt)


def _counts_text(value: ast.AST) -> bool:
    """Does this expression count something with ``len(...)`` or ``.split()``?"""
    for sub in ast.walk(value):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name) and sub.func.id == "len":
                return True
            if isinstance(sub.func, ast.Attribute) and sub.func.attr == "split":
                return True
    return False


class _Audit(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.scope: list[str] = []
        self.counted: set[str] = set()  # rule 1
        self.conversions: list[str] = []  # rule 2
        self.max_tokens: set[str] = set()  # rule 3
        self.char_caps: set[str] = set()  # rule 4

    def key(self, name: str) -> str:
        return f"{self.module}:{'.'.join(self.scope) or '<module>'}:{name}"

    def define(self, name: str):
        if _MAX_TOKENS.search(name):
            self.max_tokens.add(self.key(name))
        if _CHAR_CAP.search(name):
            self.char_caps.add(self.key(name))

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            self.define(arg.arg)
        self.scope.pop()
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _assigned(self, names, value):
        for name in names:
            self.define(name)
            if value is not None and _TOKEN_NAME.search(name) and _counts_text(value):
                self.counted.add(self.key(name))

    def visit_Assign(self, node):
        self._assigned([n for t in node.targets for n in _target_names(t)], node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._assigned(list(_target_names(node.target)), node.value)
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg and _TOKEN_NAME.search(node.arg) and _counts_text(node.value):
            self.counted.add(self.key(node.arg))
        self.generic_visit(node)

    def visit_Dict(self, node):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                self.define(k.value)
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, (ast.Mult, ast.Div, ast.FloorDiv)):
            sides = (node.left, node.right)
            const = any(
                isinstance(s, ast.Constant)
                and not isinstance(s.value, bool)
                and isinstance(s.value, (int, float))
                and s.value in _CONVERSION
                for s in sides
            )
            unit = any(_counts_text(s) or _UNIT_WORDS.search(ast.unparse(s)) for s in sides)
            if const and unit:
                self.conversions.append(self.key(ast.unparse(node)))
        self.generic_visit(node)


def _audit_all() -> list[_Audit]:
    audits = []
    for root in SHIPPED_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if path.name in GENERATED:
                continue
            audit = _Audit(_module_name(path))
            audit.visit(ast.parse(path.read_text(), filename=str(path)))
            audits.append(audit)
    return audits


def _union(attr: str) -> set[str]:
    out: set[str] = set()
    for audit in _audit_all():
        out |= getattr(audit, attr)
    return out


def _check(found: set[str], allowed: set[str], what: str):
    unexplained = sorted(found - allowed)
    stale = sorted(allowed - found)
    assert not unexplained, (
        f"{what}: not in the allowlist. If this is a real token count it must come from the "
        f"tokenizer; if it is deliberate, add it with a reason:\n  " + "\n  ".join(unexplained)
    )
    assert not stale, f"{what}: allowlist entries that match nothing now; delete them:\n  " + (
        "\n  ".join(stale)
    )


def test_roots_exist():
    for root in SHIPPED_ROOTS:
        assert any((REPO / root).rglob("*.py")), f"{root} not found; the audit would pass empty"


def test_no_token_named_value_is_a_text_length():
    _check(_union("counted"), MEASURED_TOKEN_COUNTS | LEXICAL_TERMS, "token count from len/split")


def test_no_chars_to_tokens_conversion_constant():
    found = {c for a in _audit_all() for c in a.conversions}
    unexplained = sorted(found - NOT_CONVERSIONS)
    stale = sorted(NOT_CONVERSIONS - found)
    assert not unexplained, "length scaled by a chars-per-token style constant:\n  " + (
        "\n  ".join(unexplained)
    )
    assert not stale, "NOT_CONVERSIONS entries that match nothing now; delete them:\n  " + (
        "\n  ".join(stale)
    )


def test_every_max_tokens_is_classified():
    _check(_union("max_tokens"), COMPLETION_BUDGETS, "max_tokens")


def test_character_caps_are_the_deliberate_ones():
    _check(_union("char_caps"), CHARACTER_CAPS, "character cap")


def test_allowlists_do_not_overlap():
    seen: set[str] = set()
    for s in (MEASURED_TOKEN_COUNTS, LEXICAL_TERMS, COMPLETION_BUDGETS):
        assert not (seen & s)
        seen |= s


def test_scanner_catches_the_patterns_it_exists_for():
    """The audit itself: feed it the bad shapes and see it flag them."""
    audit = _Audit("fake")
    audit.visit(
        ast.parse(
            "def f(text, max_tokens=5):\n"
            "    token_count = len(text.split())\n"
            "    budget_tokens = len(text) // 4\n"
            "    g(n_tokens=len(text))\n"
        )
    )
    assert "fake:f:token_count" in audit.counted
    assert "fake:f:budget_tokens" in audit.counted
    assert "fake:f:n_tokens" in audit.counted
    assert "fake:f:max_tokens" in audit.max_tokens
    assert any("len(text) // 4" in c for c in audit.conversions)
