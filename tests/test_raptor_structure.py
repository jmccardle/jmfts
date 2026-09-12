"""`raptor_summarize` consumes the tree it summarises.

`docs/SPRINT_0_5_0.md` Part 1.2 — the entry condition for Block C. Part 1.1 states the
property the whole of Part 3 rests on: **a derived tree links to the source leaves and
does not own them.** Owning them is what `reparent` does, and `_summarize_cluster`
(`jmfts_core/summarization.py:406`-`:424`) does both on the same loop iteration:

1. create a summary node under ``root_parent_id`` with ``usetype="summary"``;
2. ``repo.reparent(doc_id, summary_doc.id)`` for every member (`:422`);
3. ``repo.create_link(..., link_type="summarizes")`` for every member (`:424`).

Steps 2 and 3 record the same relation and only 3 can record it correctly — the link is
many-to-many, carries provenance, and is not destructive. `reparent` rewrites
``Document.path`` for the node and every descendant, which is the same column three
different subsystems read as containment:

* subtree search — ``Document.path @> jsonb_build_array(parent_id)``
  (`repositories/search.py:187`, `:271`);
* subtree RBAC — ``_within_any`` over the principal's ACRs (`access.py:78`);
* the entity roots' access key — ``[D.path] + [D.id]`` (`access.py:189`).

These three tests are the ground truth for that. They are written to the shape Part 1.2
specifies and they assert what Part 1.2 says to assert; where the observed result differs
from the prediction, the test is left as written and the docstring on it says so. Per
`SPRINT_0_4_0.md` Part 0, an open defect is a numbered step with a failing test as its
entry condition — so a test that passes is a finding about the plan, not a licence to
change the assertion until it goes red.

**Measured 2026-09-05, one run, empty `jmfts_test`: 1 failed, 2 passed.**

| test | Part 1.2 predicted | observed |
|---|---|---|
| `..._does_not_reparent_its_members` | fails | **fails** — all three members moved |
| `..._search_after_rollup_still_finds_its_leaves` | fails | **passes** |
| `..._does_not_widen_access` | unknown | **passes** — no widening |

Tests 2 and 3 pass for ONE shared reason, and it is the reason Part 1.2 could not have
predicted from the call site alone: ``root_parent_id`` is the node RAPTOR was asked to
summarise, and that node is necessarily the members' own parent (see below). So the
summary is created as a SIBLING of the members and they are then moved one level down,
underneath it. The path GAINS an element and loses none — ``[root, chapter]`` becomes
``[root, chapter, summary]`` — so every ``path @>`` containment that held before the
roll-up still holds after it. Part 1.2's stated mechanism for test 2, "the summary node is
created under root_parent_id rather than under the chapter", does not arise: those are the
same node. The corollary is that test 3's severity question resolves the safe way. RAPTOR
does not lift a node out of its governing ACR, so `SPRINT_0_3_0.md` 13.9 is NOT reached by
this route.

What remains true is the whole of Part 1.1, which is about ownership and not about paths:
the derivation takes the members as children, one parent per member drops the many-to-many
Leiden can produce, and the previous parent is recorded nowhere — so the as-written
structure is unrecoverable and a second derivation over the same leaves cannot coexist
with the first. Test 1 is the entry condition for that and it is red.

**The tree, and why RAPTOR is run on the chapter.** `_get_embedded_child_ids` calls
``repo.get_children(parent_id, depth=1)`` (`summarization.py:429`), and depth 1 is
``Document.parent_id == parent_id`` (`repositories/document.py:708`) — IMMEDIATE children
only. So the only node in a ``root -> chapter -> leaf`` tree whose children are the leaves
is the chapter, and ``root_parent_id`` inside `_summarize_cluster` is therefore the chapter
itself. Running RAPTOR on the root instead clusters nothing: the root has one embedded
child, which trips the ``len(current_ids) < 2`` guard at `summarization.py:293` and returns
an empty `RaptorResult` without writing a row.

**Two test doubles, and the line between them.** The LLM is stubbed at
`jmfts_core.summarization.complete` — the outbound HTTP seam, `llm_client.complete`, whose
only job is to reach an OpenAI-compatible endpoint. `settings.require_llm` still runs for
real against a configured URL, so the "no LLM configured" path is exercised rather than
bypassed (config.py:325 raises `LlmNotConfiguredError`, and nothing here suppresses it).
The EMBEDDINGS are real: `nomic-ai/modernbert-embed-base` on CPU over three short leaves,
because Leiden clusters what the model actually produces and a mock vector would make the
clustering a property of the mock.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import select

from jmfts_core.access import can_read, readable_filter
from jmfts_core.config import get_settings
from jmfts_core.llm_client import LlmCompletion
from jmfts_core.models.document import Document
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import (
    OWNER,
    CurrentPrincipal,
    reset_principal,
    set_principal,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.summarization import raptor_summarize
from tests.conftest import requires_db

import pytest

#: Three leaves is the minimum that clusters at all: `_leiden_cluster` returns early for
#: n <= 1 (`summarization.py:123`) and `raptor_summarize` refuses fewer than 2 embedded
#: children (`:293`). Every node lands in some cluster whatever Leiden decides — an
#: undersized community becomes an orphan and is merged into the nearest centroid
#: (`:159`-`:176`), so all three leaves are reparented on any partition.
LEAVES = [
    (
        "Photosynthesis in maize",
        "Maize fixes carbon through the C4 pathway, concentrating carbon dioxide in the "
        "bundle sheath cells before the Calvin cycle runs. The mesophyll and bundle "
        "sheath divide the labour between them.",
    ),
    (
        "Photosynthesis in rice",
        "Rice uses the ancestral C3 pathway, which loses carbon to photorespiration when "
        "stomata close in the heat. Engineering a C4 pathway into rice is a long-running "
        "programme in plant biology.",
    ),
    (
        "Stomatal regulation",
        "Guard cells open and close the stomatal pore in response to light, humidity and "
        "internal carbon dioxide. The trade-off between water loss and carbon gain is set "
        "at that pore.",
    ),
]

#: The text the stubbed endpoint returns for every cluster. Nothing asserts on it; it
#: exists so the summary node has content to embed.
STUB_SUMMARY = (
    "The passages describe carbon fixation pathways in crop plants and the stomatal "
    "control of gas exchange that constrains them."
)


def _run(coro):
    """Run a coroutine on a private loop — the pattern `tests/test_raptor.py:29` uses."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@contextmanager
def _as(principal):
    """Bind ``principal`` as the current request principal for the block."""
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def stub_llm(monkeypatch):
    """Point the settings at an LLM and stub the outbound call, nothing else.

    `_llm_summarize` resolves ``(base_url, model)`` through ``settings.require_llm``
    (`summarization.py:213`), which raises `LlmNotConfiguredError` on the blank default.
    So the endpoint has to be configured for the code path under test to be reached at
    all, and then the ONE thing that would leave this machine — `llm_client.complete` —
    is replaced. `tests/test_raptor.py` patches `_llm_summarize` itself; patching one
    level lower keeps prompt assembly, the token budget and `require_llm` in the test.
    """
    monkeypatch.setenv("JMFTS_LLM_BASE_URL", "http://llm.invalid:9999/v1")
    monkeypatch.setenv("JMFTS_LLM_MODEL", "stub-model")
    get_settings.cache_clear()

    async def _stub_complete(**kwargs):
        return LlmCompletion(text=STUB_SUMMARY, model="stub-model", usage={})

    with patch("jmfts_core.summarization.complete", new=_stub_complete):
        yield
    # The env vars are monkeypatch's to restore; the LRU cache in front of them is not.
    get_settings.cache_clear()


def _tree(session):
    """``root -> chapter -> three leaves``, with real embeddings on the leaves.

    ``auto_embed=True`` because `_get_embedded_child_ids` skips any child whose ``embed``
    is NULL (`summarization.py:433`). ``embed_tokens=False`` because MaxSim is not in
    question here and the token pass is the expensive half.
    """
    repo = DocumentRepository(session)
    root = repo.create(title="Crop physiology", content="A book about crops.", auto_embed=False)
    chapter = repo.create(
        title="Chapter 1 — Carbon fixation",
        content="How crop plants fix carbon.",
        parent_id=root.id,
        auto_embed=False,
    )
    leaves = [
        repo.create(
            title=title,
            content=content,
            parent_id=chapter.id,
            usetype="chunk",
            auto_embed=True,
            embed_tokens=False,
        )
        for title, content in LEAVES
    ]
    session.flush()
    return repo, root, chapter, leaves


def _principal(session, name: str) -> CurrentPrincipal:
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name)


@requires_db
def test_raptor_does_not_reparent_its_members(db_session, stub_llm):
    """`SPRINT_0_5_0.md` 1.2 test 1. Predicted to fail at `summarization.py:422`.

    **FAILS, as predicted, and only on half of what it asserts.** Observed: all three
    members' ``parent_id`` moved from the chapter to the summary node; the chapter stayed
    on all three ``path``\\ s. Two facts, not the same one twice — ``parent_id`` is the
    edge, ``path`` is the ancestor chain every containment query in the appliance reads —
    and the second one is why tests 2 and 3 below pass.
    """
    repo, root, chapter, leaves = _tree(db_session)
    leaf_ids = [leaf.id for leaf in leaves]

    result = _run(raptor_summarize(chapter.id, db_session))
    assert result.total_summaries >= 1, (
        "RAPTOR wrote no summary at all, so this test asserted nothing about reparenting; "
        f"layers={result.layers}"
    )

    db_session.expire_all()
    moved = {}
    lifted = {}
    for leaf_id in leaf_ids:
        leaf = repo.get(leaf_id)
        if leaf.parent_id != chapter.id:
            moved[leaf_id] = leaf.parent_id
        if chapter.id not in (leaf.path or []):
            lifted[leaf_id] = leaf.path

    # ONE assertion over both facts, so the failure report carries both. Splitting them
    # would let the first fire and hide the second, and which of the two is violated is
    # the whole distinction Part 1.2's tests 2 and 3 turn on.
    assert (moved, lifted) == ({}, {}), (
        f"raptor_summarize moved {len(moved)} of {len(leaf_ids)} members out from under "
        f"the chapter ({chapter.id}) — new parents {moved} — and dropped the chapter from "
        f"{len(lifted)} of their paths: {lifted}. The 'summarizes' link written on the "
        "next line (summarization.py:424) records the same relation without moving "
        "anything."
    )


@requires_db
def test_subtree_search_after_rollup_still_finds_its_leaves(db_session, stub_llm):
    """`SPRINT_0_5_0.md` 1.2 test 2 — subtree retrieval after a roll-up.

    **PASSES today, against Part 1.2's prediction.** All three leaves come back. The
    reparent moves them one level DOWN, under a summary that is itself a child of the
    chapter, so ``path`` still contains the chapter and the scoping predicate still
    matches. This is therefore a guard rather than an entry condition: it is what turns
    red if a later derived-tree design puts the summary node somewhere other than inside
    the subtree it summarises, which is exactly what Part 3's separate derived root does.

    ``parent_id`` scoping is ``Document.path @> jsonb_build_array(parent_id)``
    (`repositories/search.py:187`), so this asks the reparent's effect on the path in the
    voice of a caller rather than of a column read. The summary nodes themselves are held
    out of the result set by ``search_exclude_usetypes`` (`config.py:205` lists
    ``summary``), so a shortfall here is the leaves going missing and not the summaries
    crowding them out.
    """
    repo, root, chapter, leaves = _tree(db_session)
    leaf_ids = {leaf.id for leaf in leaves}

    result = _run(raptor_summarize(chapter.id, db_session))
    assert result.total_summaries >= 1, f"RAPTOR wrote no summary; layers={result.layers}"

    db_session.expire_all()
    search = SearchRepository(db_session)
    hits = search.vector_search_text(
        "how do crop plants fix carbon",
        limit=50,
        parent_id=chapter.id,
    )
    found = {hit.document.id for hit in hits}

    assert leaf_ids <= found, (
        f"subtree search under the chapter ({chapter.id}) lost "
        f"{sorted(leaf_ids - found)} of {sorted(leaf_ids)} after the roll-up; it returned "
        f"{sorted(found)}"
    )


@requires_db
def test_raptor_does_not_widen_access(db_session, stub_llm):
    """`SPRINT_0_5_0.md` 1.2 test 3 — the one whose status the plan calls inferred.

    **PASSES today. RAPTOR does not widen access, and `SPRINT_0_3_0.md` 13.9 is not
    reached by this route.** Both spellings agree: after the roll-up an ungranted
    principal still sees none of the three leaves. The mechanism is the same one that
    saves test 2 — the summary node is a child of the ACR, so the members stay inside it.
    Part 1.3's severity claim therefore rests on tests 1 and 2, and test 2 is green too,
    which leaves test 1 carrying it alone. Per Part 1.2 this stays as the regression guard
    on step 13: it is what fails if the fix moves the members to a derived root that does
    not inherit the source's grants, which is the hazard `sql/migrations/014_entity_roots
    .sql` opens by naming.

    The chapter is the access-control root. ``P`` holds read on it; ``Z`` holds no grant
    anywhere, so before the roll-up ``Z`` can see the root (ungoverned) and nothing under
    the chapter. `access.py:78` resolves that set with the same
    ``path @> jsonb_build_array(root)`` containment `reparent` rewrites, so IF a member
    were lifted out of the chapter's subtree it would land under no ACR at all — and
    `access.py:161` returns True for an ungoverned document. That is the widening this
    asserts against.

    Both spellings of the check are made, because the appliance has two: `can_read` is the
    point check the direct-get verbs use, `readable_filter` is the predicate every search
    leaf ANDs in. A divergence between them would itself be the finding.

    RAPTOR runs as the OWNER, which bypasses every check (`principal_context.py:37`), so
    nothing here is a test of whether the roll-up was ALLOWED — it is a test of what the
    roll-up left behind for somebody else to read.
    """
    repo, root, chapter, leaves = _tree(db_session)
    leaf_ids = {leaf.id for leaf in leaves}

    p = _principal(db_session, "P-holds-read-on-the-chapter")
    z = _principal(db_session, "Z-holds-nothing")
    db_session.add(AccessGrant(document_id=chapter.id, principal_id=p.id, level="read"))
    db_session.flush()

    # Precondition: the grant governs the leaves BEFORE the roll-up. Without this the
    # test could pass on a tree that was never protected in the first place.
    with _as(z):
        assert not any(can_read(db_session, repo.get(i)) for i in leaf_ids), (
            "the leaves were already readable by an ungranted principal before RAPTOR "
            "ran; the fixture is not testing what it claims to"
        )

    with _as(OWNER):
        result = _run(raptor_summarize(chapter.id, db_session))
    assert result.total_summaries >= 1, f"RAPTOR wrote no summary; layers={result.layers}"

    db_session.expire_all()
    with _as(z):
        readable_now = {i for i in leaf_ids if can_read(db_session, repo.get(i))}
        pred = readable_filter(db_session, z)
        visible = set(
            db_session.execute(
                select(Document.id).where(pred) if pred is not None else select(Document.id)
            ).scalars()
        )

    assert readable_now == set(), (
        f"can_read: RAPTOR made {sorted(readable_now)} readable by a principal holding no "
        f"grant. The chapter ({chapter.id}) is the ACR and the roll-up moved them out "
        "from under it."
    )
    assert leaf_ids & visible == set(), (
        f"readable_filter: {sorted(leaf_ids & visible)} are now inside the readable set of "
        "a principal holding no grant."
    )
