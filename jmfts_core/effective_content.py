"""The text a container node stands for, for many nodes at once.

:func:`~jmfts_core.rollup_tasks.effective_text` is the NORMATIVE definition and this module
does not replace it. That function answers for one node by walking the tree in Python, one
``session.get`` and one child query per node, which is right for an ingest task that
summarises a single node and wrong for a search response: a ``file`` node in the stress
corpus has 154 descendants, and a page of 100 results would be tens of thousands of
round-trips inside one request.

:func:`project_effective_content` answers the same question for a whole page in ONE query.
``tests/test_effective_projection.py::test_the_projection_agrees_with_effective_text``
compares the two over a tree that exercises every branch, so the fast path cannot drift
from the definition without a test failing.

**Why a projection is needed at all.** A container's ``documents.content`` is NULL by
design — ``rollup_tasks.store_effective_content`` embeds the concatenation and deliberately
does not store it, *"storing it at every level would duplicate the whole document once per
level for no fact that could not be recomputed"*. But the embedding IS stored, so the node
is a first-class retrieval target with nothing to display. Measured on 57,492 settled nodes
(``docs/STRESS_CORPUS.md`` 4.7): 26,870 nodes — 46.7% — carry a vector and no content, and
in a 10-query sweep the container outscored every descendant in its own subtree 53 times
out of 59. The nodes this appliance ranks best were the ones it could not render.

**What it costs, measured on that corpus 2026-09-07.** The worst realistic page is 100
results that are all containers; this query answers it in **4.9 ms**, reaching 619 frontier
nodes and returning 340 KB. Search itself is 25-50 ms, so the worst case is a fifth of one
search and the common case — a page of leaves — is one `IS NULL` check and no query at all.

Projected sizes for the 26,870 blank nodes, in characters:

===========  ======  ==========  ======  =======  =========
usetype       nodes   avg         median      p95        max
===========  ======  ==========  ======  =======  =========
``section``  12,179       1,547     575    6,395     37,306
``segment``     699       7,078   5,531   19,060     33,449
``sheet``        30       2,242   1,493    9,044     17,332
``file``         18       3,737   1,789   17,332     17,332
===========  ======  ==========  ======  =======  =========

Nothing here truncates. A prefix returned under the node's own name would be the node
misreporting what it stands for, and the largest projection is smaller than the 62,115
characters a ``file`` node with STORED content already returns from one row.
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

#: What :func:`~jmfts_core.rollup_tasks.effective_text` joins children with, and therefore
#: what this module must join frontier texts with. Uniform at every level, which is what
#: makes the flattened walk below equal to the nested one: joining is associative when the
#: separator does not vary, so ``(A ⧺ B) ⧺ C`` and ``A ⧺ (B ⧺ C)`` are the same string.
SEPARATOR = "\n\n"

#: The evidence row that holds a node's stored summary. Present on 147 source-tree nodes in
#: the stress corpus, and the reason the walk below has a second stop condition:
#: ``effective_text`` returns a stored summary INSTEAD of descending, so a walk that
#: descended past one would return text the node does not stand for.
EFFECTIVE_CONTENT = "effective_content"

#: How deep the walk may go before it stops. Documents form a tree and a cycle would be a
#: corrupt ``parent_id``, not a legal shape — but a recursive CTE meeting one does not
#: error, it runs until the connection dies, so the bound is here rather than absent. The
#: deepest node in the stress corpus is at depth 5.
MAX_DEPTH = 40

#: One level's contribution to the sort key, matching ``rollup_tasks.child_ids``'s ordering
#: contract exactly — ``position ASC NULLS LAST, created_at ASC, id ASC``.
#:
#: Text rather than a composite, because a recursive CTE's recursive term may not contain a
#: window function, so ``row_number()`` is unavailable and the key has to be computed from
#: the columns themselves. Each part is fixed-width and zero-padded so that lexicographic
#: order over the concatenation is the ordering above:
#:
#: * ``'0'``/``'1'`` puts a NULL position last, which is what ``NULLS LAST`` means.
#: * ``position + 2147483648`` is monotone over the whole of int4 INCLUDING negatives,
#:   which ``lpad`` on a bare ``-1`` would not be.
#: * microseconds on the timestamp, because two siblings written in one flush share a
#:   second and the tiebreak below is then the id.
_ORDER_KEY = """
    (CASE WHEN c.position IS NULL THEN '1' ELSE '0' END)
    || lpad((coalesce(c.position, 0) + 2147483648)::text, 20, '0')
    || '|' || to_char(c.created_at AT TIME ZONE 'UTC', 'YYYYMMDDHH24MISSUS')
    || '|' || lpad(c.id::text, 12, '0')
"""

#: The walk itself, as a ``WITH RECURSIVE`` clause two queries below both build on.
#:
#: It mirrors ``effective_text``'s three branches. A node with its own ``content`` is a
#: FRONTIER and is not descended past; so is a node carrying a stored summary; anything else
#: contributes its children. The two stop conditions are in the recursive term's ``WHERE``,
#: so a frontier row enters the result and produces no successors.
#:
#: **The frontier, and not the subtree, is what a container stands for.** A ``file`` node
#: holds the whole extracted text AND has chunk descendants that hold it again; a walk that
#: descended past the file would return that text twice, and — for :func:`frontier_members`
#: — would count it twice in a BM25 term frequency. The stop condition is the same fact
#: ``effective_text`` states by returning early.
#:
#: ``LEFT JOIN document_evidence`` rather than a scalar subquery: PostgreSQL rejects a
#: recursive self-reference that appears inside a subquery, and the join is what the
#: subquery would have been.
_WALK_CTE = f"""
    WITH RECURSIVE walk AS (
        SELECT
            d.id           AS root_id,
            d.id           AS node_id,
            0              AS depth,
            ARRAY[]::text[] AS ord,
            d.content      AS content,
            e.value->>'text' AS summary_text
        FROM documents d
        LEFT JOIN document_evidence e
               ON e.document_id = d.id AND e.name = :evidence_name
        WHERE d.id = ANY(:root_ids)

        UNION ALL

        SELECT
            w.root_id,
            c.id,
            w.depth + 1,
            w.ord || ({_ORDER_KEY}),
            c.content,
            e.value->>'text'
        FROM walk w
        JOIN documents c ON c.parent_id = w.node_id
        LEFT JOIN document_evidence e
               ON e.document_id = c.id AND e.name = :evidence_name
        WHERE w.content IS NULL
          AND w.summary_text IS NULL
          AND w.depth < :max_depth
    )
"""

#: Every text-bearing node reachable from each requested root, in document order.
_WALK = sql(_WALK_CTE + """
    SELECT root_id, coalesce(content, summary_text) AS body
    FROM walk
    WHERE content IS NOT NULL OR summary_text IS NOT NULL
    ORDER BY root_id, ord
    """)

#: Which nodes each root's text is made of, and whether any of them is a stored summary.
#:
#: The same rows as :data:`_WALK` without the bodies. ``summarised`` is per root and not per
#: node on purpose: a container is scorable by BM25 only if NONE of its frontier is LLM
#: prose, so the caller needs one flag per root rather than a set to subtract.
_FRONTIER = sql(_WALK_CTE + """
    SELECT root_id, node_id, (summary_text IS NOT NULL) AS is_summary
    FROM walk
    WHERE content IS NOT NULL OR summary_text IS NOT NULL
    """)


def project_effective_content(session: Session, node_ids: Iterable[int]) -> dict:
    """``{node_id: text}`` for every id in ``node_ids`` that stands for any text.

    An id whose subtree holds nothing is ABSENT from the mapping rather than present with
    ``""``. The distinction is the one ``effective_text`` erases by returning ``""`` for
    both, and a caller filling in a response needs it: a node with no text must keep a NULL
    ``content``, not acquire an empty string that reads as "measured, and empty".

    One query regardless of how many ids are asked for. Ids that do not exist are simply
    missing from the result, so a caller does not have to pre-filter.
    """
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return {}

    rows = session.execute(
        _WALK,
        {"root_ids": ids, "evidence_name": EFFECTIVE_CONTENT, "max_depth": MAX_DEPTH},
    ).all()

    parts: dict = {}
    for root_id, body in rows:
        # `strip()` and not `if body`: `effective_text` drops a part that is whitespace-only
        # at every level, so a subtree of blank leaves has to project to nothing here too.
        if body and body.strip():
            parts.setdefault(root_id, []).append(body)
    return {root_id: SEPARATOR.join(bodies) for root_id, bodies in parts.items()}


def project_one(session: Session, node_id: int) -> Optional[str]:
    """:func:`project_effective_content` for a single id. ``None`` when it stands for no text."""
    return project_effective_content(session, [node_id]).get(node_id)


def frontier_members(session: Session, node_ids: Iterable[int]) -> dict:
    """``{node_id: (frontier_ids, contains_summary)}`` — what each node's text is made of.

    The same walk :func:`project_effective_content` runs, reported as node ids instead of
    text. ``frontier_ids`` is the set whose contents, concatenated in document order, ARE
    that node's effective text; ``contains_summary`` says whether any of them is a stored
    LLM summary rather than text the corpus wrote.

    Why the flag rather than a filtered set: a caller scoring BM25 needs to refuse the whole
    node, not score the part of it that is not LLM prose. A score over a subset of a node's
    text is a number that misreports what was measured, which is worse than no number.

    A node whose subtree holds nothing is absent, exactly as in
    :func:`project_effective_content`.
    """
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return {}

    rows = session.execute(
        _FRONTIER,
        {"root_ids": ids, "evidence_name": EFFECTIVE_CONTENT, "max_depth": MAX_DEPTH},
    ).all()

    members: dict = {}
    for root_id, node_id, is_summary in rows:
        found, summarised = members.get(root_id, (None, False))
        if found is None:
            found = set()
        found.add(node_id)
        members[root_id] = (found, summarised or bool(is_summary))
    return members
