"""The summary as a node in a parallel tree. ``SPRINT_0_5_0.md`` Block C step 11.

``summarize`` writes ``effective_content@self``: the summary is a FIELD on a structural
node, which makes it a property of the as-written tree and nothing a second tree can be
compared against. ``summarize:tree`` gives that same summary a NODE under the derived-tree
root (``derived_roots.py``, migration ``018``) with ``summarizes`` edges down to the members
it covers, which is the shape 3.1's leaf projection reads.

**The access tests come first in this file because they came first in the work.** A derived
tree hangs OUTSIDE the subtree it derives from, so the grants that governed the source do
not reach it, and the only thing between a restricted leaf and a world-readable summary of
it is the key the root was minted under. That is ``SPRINT_0_3_0.md`` 13.9 for the fourth
time — resolution, derivation, ``raptor_summarize``'s reparent, and now structure — and
``TestAccessIsNotWidened`` is the entry condition Part 0 of ``SPRINT_0_4_0.md`` asks for.

Every test drives the handler directly with a stand-in task row, the way
``tests/test_rollup_pelt.py`` does, and stages ``summarize``'s output by hand: an
``effective_content`` row and a vector. The real ``summarize`` is tested there; what is
under test here is what happens to that output afterwards, and running the embedding model
to produce a vector this handler only copies would test the model.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from jmfts_client.contracts.attempt import param_fingerprint
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.access import can_read
from jmfts_core.atoms import ATOMS, LOCI, Fact
from jmfts_core.config import get_settings
from jmfts_core.derived_roots import (
    DERIVED_ROOT_USETYPE,
    SUMMARY_TREE_KIND,
    get_or_create_derived_root,
    widening_descendants,
)
from jmfts_core.entity_roots import ENTITY_USETYPE, get_or_create_entities_root
from jmfts_core.ingest_tasks import TASK_SUMMARIZE, TASK_SUMMARIZE_TREE
from jmfts_core.models.derived_root import DerivedRoot
from jmfts_core.models.document import (
    SUMMARIZES_LINK_TYPE,
    USETYPE_SUMMARY,
    Document,
    DocumentLink,
)
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.rollup_tasks import (
    METHOD_CONCATENATED,
    METHOD_LLM_SUMMARY,
    NON_INGEST_ROOT_USETYPES,
    SOURCE_NODE_KEY,
    IngestRollupPlanner,
    child_ids,
    in_ingest_tree,
    run_summarize_tree,
)
from jmfts_core.services.ingest_service import IngestService
from tests.conftest import drain_ingest_queue

EMBED_DIM = 768


@dataclass
class _Task:
    """A claimed queue row, without the queue. The handler reads only these."""

    scope_document_id: int
    params: dict = field(default_factory=dict)
    task_type: str = TASK_SUMMARIZE_TREE


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name):
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


def _grant(session, doc, principal, level="read"):
    session.add(AccessGrant(document_id=doc.id, principal_id=principal.id, level=level))
    session.flush()


def _vector(seed: int) -> list[float]:
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 1.0
    return vec


def _chapter(session, *, leaves: int = 3, parent_id=None) -> Document:
    """A container with ``leaves`` text children, in document order."""
    repo = DocumentRepository(session)
    chapter = repo.create(title="chapter", content=None, parent_id=parent_id, auto_embed=False)
    for index in range(leaves):
        repo.create(
            title=f"leaf {index}",
            content=f"the body of leaf {index}",
            parent_id=chapter.id,
            auto_embed=False,
            sequential=True,
        )
    session.flush()
    return chapter


def _summarized(session, node: Document, *, method: str = METHOD_CONCATENATED, text=None):
    """Stage what a completed ``summarize`` leaves behind: the row, and the vector."""
    record = {"method": method, "source_children": len(node.children or []), "tokens": 12}
    if text is not None:
        record["text"] = text
    EvidenceRepository(session).write(node.id, "effective_content", record)
    node.embed = _vector(node.id)
    session.flush()
    return record


def _run(session, node: Document):
    return run_summarize_tree(session, _Task(scope_document_id=node.id))


def _derived_node(session, source_node_id: int) -> Document | None:
    return session.execute(
        select(Document).where(
            Document.structured_content[SOURCE_NODE_KEY].astext == str(source_node_id)
        )
    ).scalar_one_or_none()


def _links(session, node_id: int) -> list[DocumentLink]:
    return list(
        session.execute(
            select(DocumentLink)
            .where(
                DocumentLink.source_id == node_id,
                DocumentLink.link_type == SUMMARIZES_LINK_TYPE,
            )
            .order_by(DocumentLink.id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Access — written first, and the handler is not shippable without it
# ---------------------------------------------------------------------------


class TestAccessIsNotWidened:
    def test_a_stranger_cannot_read_the_summary_of_leaves_it_cannot_read(self, db_session):
        """`SPRINT_0_3_0.md` 13.9, reached by structure.

        The chapter is an access-control root granting alice. Its leaves are readable by
        alice and nobody else. The summary of those leaves goes into a tree OUTSIDE the
        chapter, so nothing about the chapter's grants reaches it by containment — the root
        it hangs under has to have been keyed by the chapter's access, and this is the test
        that it was.
        """
        alice = _principal(db_session, "alice")
        bob = _principal(db_session, "bob")
        chapter = _chapter(db_session)
        _grant(db_session, chapter, alice)
        _summarized(db_session, chapter)

        outcome = _run(db_session, chapter)
        assert outcome.status == "completed"

        summary = db_session.get(Document, outcome.detail["derived_node_id"])
        leaf = db_session.get(Document, chapter.children[0].id)
        with _as(bob):
            assert can_read(db_session, leaf) is False
            assert can_read(db_session, summary) is False
            assert can_read(db_session, db_session.get(Document, summary.parent_id)) is False

    def test_the_principal_who_can_read_the_leaves_can_read_the_summary(self, db_session):
        """The other half. A gate that denied everyone would pass the test above and be
        useless: the derived tree exists to be retrieved from."""
        alice = _principal(db_session, "alice")
        chapter = _chapter(db_session)
        _grant(db_session, chapter, alice)
        _summarized(db_session, chapter)

        summary = db_session.get(Document, _run(db_session, chapter).detail["derived_node_id"])
        with _as(alice):
            assert can_read(db_session, summary) is True

    def test_the_root_carries_the_grants_of_the_subtree_it_derives_from(self, db_session):
        alice = _principal(db_session, "alice")
        chapter = _chapter(db_session)
        _grant(db_session, chapter, alice, level="write")
        _summarized(db_session, chapter)

        root_id = _run(db_session, chapter).detail["derived_root_id"]
        grants = db_session.execute(
            select(AccessGrant.principal_id, AccessGrant.level).where(
                AccessGrant.document_id == root_id
            )
        ).all()
        assert grants == [(alice.id, "write")]

    def test_a_public_node_over_a_restricted_leaf_is_refused(self, db_session):
        """The one case the shipped access model actually admits, and it is the dangerous one.

        Grants are ADDITIVE, so a governed node's descendants are readable by at least its
        own readers and a root keyed by it is never wider than what it summarises. The
        exception is the ungoverned node: an empty key means PUBLIC, a governed leaf below
        it is readable by fewer people than its parent, and a summary keyed by the parent
        would publish it. The handler refuses, and refusing is the whole protection — there
        is no narrower root to fall back to.
        """
        alice = _principal(db_session, "alice")
        chapter = _chapter(db_session)
        _grant(db_session, chapter.children[0], alice)
        _summarized(db_session, chapter)

        outcome = _run(db_session, chapter)

        assert outcome.status == "skipped"
        assert outcome.detail["restricted_below"] == [chapter.children[0].id]
        assert "readable by principals who may not read" in outcome.detail["reason"]
        # Nothing was written on the way to the refusal: no node, and no root either.
        assert _derived_node(db_session, chapter.id) is None
        assert db_session.execute(select(DerivedRoot)).scalars().all() == []

    def test_the_check_is_empty_when_the_subtree_shares_the_node_s_access(self, db_session):
        alice = _principal(db_session, "alice")
        chapter = _chapter(db_session)
        _grant(db_session, chapter, alice)
        assert widening_descendants(db_session, chapter.id) == []

    def test_a_deeper_grant_that_only_widens_is_not_a_widening_of_the_summary(self, db_session):
        """alice governs the chapter; bob is granted one leaf as well. The leaf is readable
        by both, the summary by alice alone — narrower than its material, which is safe."""
        alice = _principal(db_session, "alice")
        bob = _principal(db_session, "bob")
        chapter = _chapter(db_session)
        _grant(db_session, chapter, alice)
        _grant(db_session, chapter.children[0], bob)
        _summarized(db_session, chapter)

        assert widening_descendants(db_session, chapter.id) == []
        summary = db_session.get(Document, _run(db_session, chapter).detail["derived_node_id"])
        with _as(bob):
            assert can_read(db_session, chapter.children[0]) is True
            assert can_read(db_session, summary) is False

    def test_a_missing_document_is_not_reported_as_nothing_widens(self, db_session):
        with pytest.raises(LookupError):
            widening_descendants(db_session, 10**9)


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


class TestTheDerivedNode:
    def test_it_hangs_under_the_derived_root_and_not_in_the_source_tree(self, db_session):
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)

        detail = _run(db_session, chapter).detail
        root = db_session.get(Document, detail["derived_root_id"])
        summary = db_session.get(Document, detail["derived_node_id"])

        assert root.usetype == DERIVED_ROOT_USETYPE
        assert root.parent_id is None
        assert summary.parent_id == root.id
        assert summary.usetype == USETYPE_SUMMARY
        assert summary.produced_by == TASK_SUMMARIZE_TREE
        assert summary.structured_content[SOURCE_NODE_KEY] == chapter.id
        assert summary.structured_content["tree_kind"] == SUMMARY_TREE_KIND

    def test_the_members_keep_their_parent_and_their_path(self, db_session):
        """Part 1.1's property, which is what the whole block is for: a derivation LINKS to
        the source leaves and does not OWN them."""
        chapter = _chapter(db_session)
        before = [(leaf.id, leaf.parent_id, list(leaf.path or [])) for leaf in chapter.children]
        _summarized(db_session, chapter)

        _run(db_session, chapter)

        for leaf_id, parent_id, path in before:
            leaf = db_session.get(Document, leaf_id)
            assert leaf.parent_id == parent_id
            assert list(leaf.path or []) == path

    def test_it_links_down_to_every_member_in_document_order(self, db_session):
        chapter = _chapter(db_session, leaves=4)
        _summarized(db_session, chapter)

        summary_id = _run(db_session, chapter).detail["derived_node_id"]
        links = _links(db_session, summary_id)

        assert [link.target_id for link in links] == [leaf.id for leaf in chapter.children]
        assert [link.link_metadata["position"] for link in links] == [0, 1, 2, 3]
        assert {link.derived_by for link in links} == {TASK_SUMMARIZE_TREE}

    def test_the_vector_is_the_one_summarize_computed(self, db_session):
        """Copied, not recomputed: it stands for exactly the text `summarize` embedded, so a
        second forward pass would spend the model to arrive at the same numbers."""
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)

        summary = db_session.get(Document, _run(db_session, chapter).detail["derived_node_id"])
        assert list(summary.embed) == list(chapter.embed)

    def test_a_concatenated_summary_gives_the_node_no_content(self, db_session):
        """The module's "nothing here writes content" rule, one node over. A concatenation
        is the leaves' own prose and copying it here indexes the same text twice."""
        chapter = _chapter(db_session)
        _summarized(db_session, chapter, method=METHOD_CONCATENATED)

        detail = _run(db_session, chapter).detail
        assert detail["has_content"] is False
        assert db_session.get(Document, detail["derived_node_id"]).content is None

    def test_an_llm_summary_is_new_text_and_becomes_the_node_s_content(self, db_session):
        chapter = _chapter(db_session)
        _summarized(db_session, chapter, method=METHOD_LLM_SUMMARY, text="what the span says")

        detail = _run(db_session, chapter).detail
        assert detail["has_content"] is True
        assert db_session.get(Document, detail["derived_node_id"]).content == "what the span says"

    def test_two_containers_of_one_access_share_a_root(self, db_session):
        """6.6's decision, exercised: one root per (access, tree kind), not one per node."""
        first = _chapter(db_session)
        second = _chapter(db_session)
        _summarized(db_session, first)
        _summarized(db_session, second)

        assert (
            _run(db_session, first).detail["derived_root_id"]
            == _run(db_session, second).detail["derived_root_id"]
        )
        assert len(db_session.execute(select(DerivedRoot)).scalars().all()) == 1


class TestRederivation:
    def test_a_second_run_rewrites_the_node_it_wrote_rather_than_adding_one(self, db_session):
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)
        first = _run(db_session, chapter).detail["derived_node_id"]

        DocumentRepository(db_session).create(
            title="leaf 3",
            content="a member that arrived later",
            parent_id=chapter.id,
            auto_embed=False,
            sequential=True,
        )
        db_session.flush()
        second = _run(db_session, chapter)

        assert second.detail["derived_node_id"] == first
        assert second.detail["created"] is False
        assert second.detail["members"] == 4
        summary = db_session.get(Document, first)
        assert summary.structured_content["member_count"] == 4
        # `child_ids` and not `chapter.children`: the relationship was loaded before the
        # fourth leaf existed, and the edge set under test is the one the handler read.
        assert [link.target_id for link in _links(db_session, first)] == child_ids(
            db_session, chapter.id
        )

    def test_a_member_that_left_loses_its_edge(self, db_session):
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)
        summary_id = _run(db_session, chapter).detail["derived_node_id"]
        departed = chapter.children[0].id

        DocumentRepository(db_session).delete(departed)
        db_session.flush()
        _run(db_session, chapter)

        assert departed not in {link.target_id for link in _links(db_session, summary_id)}


class TestRefusals:
    def test_a_node_summarize_wrote_nothing_for_is_skipped(self, db_session):
        chapter = _chapter(db_session)
        outcome = _run(db_session, chapter)
        assert outcome.status == "skipped"
        assert "no effective_content" in outcome.detail["reason"]
        assert _derived_node(db_session, chapter.id) is None

    def test_a_node_with_no_children_is_skipped(self, db_session):
        leaf = DocumentRepository(db_session).create(title="leaf", content="text", auto_embed=False)
        db_session.flush()
        assert _run(db_session, leaf).status == "skipped"

    def test_a_summary_with_no_vector_is_skipped_rather_than_written_unretrievable(
        self, db_session
    ):
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)
        chapter.embed = None
        db_session.flush()

        outcome = _run(db_session, chapter)
        assert outcome.status == "skipped"
        assert "no document vector" in outcome.detail["reason"]


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestThePlanner:
    def _attempt(self, session, node, task, params):
        EvidenceRepository(session).append(
            node.id,
            "attempts",
            [
                {
                    "task": task,
                    "status": "completed",
                    "param_fingerprint": param_fingerprint(params),
                }
            ],
        )
        session.flush()

    def test_the_third_rung_is_offered_once_summarize_has_written_something(self, db_session):
        chapter = _chapter(db_session)
        planner = IngestRollupPlanner()
        summarize = planner(db_session, chapter)[0]
        assert summarize.task_type == TASK_SUMMARIZE

        self._attempt(db_session, chapter, TASK_SUMMARIZE, summarize.params)
        _summarized(db_session, chapter)

        specs = planner(db_session, chapter)
        assert [spec.task_type for spec in specs] == [TASK_SUMMARIZE_TREE]
        assert specs[0].write_mode == "self"
        assert specs[0].params == {"child_count": 3}

    def test_a_node_whose_summarize_was_skipped_is_offered_nothing(self, db_session):
        """Gated on the evidence and not on the attempt. A `summarize` that reported
        `skipped` is attempted and wrote no summary, so a queue row here could only ever
        report `skipped` in turn — a plan claiming work where there is none."""
        chapter = _chapter(db_session)
        planner = IngestRollupPlanner()
        summarize = planner(db_session, chapter)[0]
        self._attempt(db_session, chapter, TASK_SUMMARIZE, summarize.params)

        assert planner(db_session, chapter) == ()

    def test_a_node_that_has_done_all_three_settles(self, db_session):
        chapter = _chapter(db_session)
        planner = IngestRollupPlanner()
        summarize = planner(db_session, chapter)[0]
        self._attempt(db_session, chapter, TASK_SUMMARIZE, summarize.params)
        _summarized(db_session, chapter)
        tree = planner(db_session, chapter)[0]
        self._attempt(db_session, chapter, TASK_SUMMARIZE_TREE, tree.params)

        assert planner(db_session, chapter) == ()


class TestThePlannerDeclinesTreesNobodyIngested:
    """``SPRINT_0_5_0.md`` Block C finding 2, and it is the reason a derived root can be a
    parent at all.

    The worker calls ``settle_after_task`` after every completed task and the walk asks the
    planner at every ancestor of that task's scope node. A derived root is an ancestor like
    any other, and its children are unrelated to each other by construction — they are
    whatever derivations have filed under one access key. Offering it ``summarize`` buys an
    LLM call over a container nobody assembled.

    THE RED BASELINE. Before :func:`~jmfts_core.rollup_tasks.in_ingest_tree` the first test
    here returned a ``summarize`` spec: the root has children, ``child_count`` is in the
    fingerprint, so every new derived node made a new fingerprint and a new call.
    """

    def test_a_shared_derived_root_is_offered_nothing(self, db_session):
        first = _chapter(db_session)
        second = _chapter(db_session)
        _summarized(db_session, first)
        _summarized(db_session, second)

        root_id = _run(db_session, first).detail["derived_root_id"]
        assert _run(db_session, second).detail["derived_root_id"] == root_id
        root = db_session.get(Document, root_id)
        assert root.usetype == DERIVED_ROOT_USETYPE
        # Two summaries of two unrelated chapters, sharing a root because they share an
        # access key. This is the container the walk would have summarised.
        assert len(child_ids(db_session, root_id)) == 2

        assert IngestRollupPlanner()(db_session, root) == ()

    def test_a_node_filed_under_a_shared_root_is_offered_nothing(self, db_session):
        """The case the finding names outright: a node produced OUTSIDE a rollup, filed
        under the derived root, with children of its own. Block A's validation report is
        parentless today precisely to avoid this (`validate_tasks.REPORT_PARENT_REASON`),
        and this is the check that makes the parent available."""
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)
        root_id = _run(db_session, chapter).detail["derived_root_id"]

        repo = DocumentRepository(db_session)
        filed = repo.create(title="a report", content=None, parent_id=root_id, auto_embed=False)
        repo.create(
            title="a violation", content="what was found", parent_id=filed.id, auto_embed=False
        )
        db_session.flush()
        assert len(child_ids(db_session, filed.id)) == 1

        assert IngestRollupPlanner()(db_session, filed) == ()

    def test_an_entities_root_is_offered_nothing_either(self, db_session):
        """One release earlier, the same shape: parentless, contentless, children keyed only
        by access. A summary of the entities a corpus mentions is the same unasked-for call
        as a summary of its summaries, so both usetypes are in the frozenset."""
        chapter = _chapter(db_session)
        root_id = get_or_create_entities_root(db_session, chapter.id)
        repo = DocumentRepository(db_session)
        repo.create(
            title="Ada Lovelace",
            content=None,
            parent_id=root_id,
            usetype=ENTITY_USETYPE,
            auto_embed=False,
        )
        db_session.flush()
        root = db_session.get(Document, root_id)

        assert root.usetype in NON_INGEST_ROOT_USETYPES
        assert IngestRollupPlanner()(db_session, root) == ()

    def test_an_ingest_tree_is_still_rolled_up(self, db_session):
        """The other half of the finding: this check must cost the ordinary path nothing.
        A hand-built container carries no ``produced_by`` and no usetype at all, and the
        planner offers it the same first rung it always did."""
        chapter = _chapter(db_session)
        assert chapter.produced_by is None
        assert chapter.usetype is None
        assert in_ingest_tree(db_session, chapter)
        assert IngestRollupPlanner()(db_session, chapter)[0].task_type == TASK_SUMMARIZE

    def test_a_leaf_of_an_ingest_tree_is_in_one(self, db_session):
        """The predicate reads the tree's ROOT, not the node, so it holds one level down."""
        chapter = _chapter(db_session)
        leaf = db_session.get(Document, child_ids(db_session, chapter.id)[0])
        assert leaf.path == [chapter.id]
        assert in_ingest_tree(db_session, leaf)

    def test_a_derived_node_is_not_in_an_ingest_tree(self, db_session):
        """And ``produced_by`` is not what says so. The derived node DOES carry
        ``summarize:tree`` (finding 3), but the root above it carries NULL — it is minted by
        ``DocumentRepository.create`` with no stamp — so the stamp cannot answer for the
        container, which is the node the finding is about."""
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)
        root_id = _run(db_session, chapter).detail["derived_root_id"]
        derived = _derived_node(db_session, chapter.id)

        assert derived.produced_by == TASK_SUMMARIZE_TREE
        assert db_session.get(Document, root_id).produced_by is None
        assert not in_ingest_tree(db_session, derived)


# ---------------------------------------------------------------------------
# The declaration, and the debt in it
# ---------------------------------------------------------------------------


class TestTheDeclaration:
    def test_it_reads_what_summarize_wrote_and_declares_no_product(self, db_session):
        atom = ATOMS[TASK_SUMMARIZE_TREE]
        assert set(atom.consumes) == {
            Fact("effective_content", "self"),
            Fact("embedding", "self"),
        }
        assert atom.produces == ()
        assert atom.write_mode == "self"
        assert atom.cost_class == "cpu"

    def test_the_locus_vocabulary_still_has_no_term_for_another_tree(self):
        """`SPRINT_0_5_0.md` open question 6.1, and the default step 11 took.

        The handler writes a node under the derived root, which is not `self`, not
        `children`, not `subtree` and not `ancestor`. It declares `self` anyway — the
        narrowest reservation available and true of everything it writes in its own tree,
        which is nothing — and the incompleteness is written down in `INGEST_SPEC.md` 5.3
        rather than papered over with a fifth locus that nothing would report on.

        This test fails on the day somebody adds that term, which is the day the record in
        5.3 and the comment on the declaration stop being true.
        """
        assert set(LOCI) == {"self", "children", "subtree", "ancestor"}


class TestTheHoldOut:
    def test_the_derived_root_usetype_is_held_out_of_both_default_lists(self):
        """A settings default and a module constant that must not drift apart. The root is
        contentless, so nothing can match its vector or its postings — what the exclusion
        holds out is a full-text match on the title "Derived: summary"."""
        settings = get_settings()
        assert DERIVED_ROOT_USETYPE in settings.search_exclude_usetypes
        assert DERIVED_ROOT_USETYPE in settings.bm25_exclude_usetypes

    def test_the_nodes_under_it_were_already_held_out(self, db_session):
        settings = get_settings()
        assert USETYPE_SUMMARY in settings.search_exclude_usetypes
        assert USETYPE_SUMMARY in settings.bm25_exclude_usetypes


class TestTheRootItself:
    def test_the_handler_is_the_first_caller_of_the_root_writer(self, db_session):
        """`derived_roots.py` shipped with migration 018 and no caller. This is it: the
        root the handler asks for is the one `get_or_create_derived_root` mints, and asking
        twice returns the same one."""
        chapter = _chapter(db_session)
        _summarized(db_session, chapter)
        root_id = _run(db_session, chapter).detail["derived_root_id"]
        assert get_or_create_derived_root(db_session, chapter.id, SUMMARY_TREE_KIND) == root_id


# ---------------------------------------------------------------------------
# The queue builds it
# ---------------------------------------------------------------------------


TWO_TOPIC_TEXT = (
    " ".join(
        f"Late interaction scores every query token against every document token, and "
        f"retrieval recall improved by {n} points against the sparse baseline."
        for n in range(1, 14)
    )
    + "\n\n"
    + " ".join(
        f"Braise the shallots in butter for {n} minutes, then deglaze the pan with white "
        f"wine and reduce the sauce until it coats the back of a spoon."
        for n in range(1, 14)
    )
)

INGEST_OPTIONS = {
    "structure": {"max_tokens": 20, "min_chunk_length": 5},
    "rollup": {"max_children": 4, "min_segment": 2, "penalty": 0.5},
}


class TestTheQueueBuildsIt:
    """Part 0.4's third column. ``raptor_summarize`` builds a tree the queue does not run,
    and the queued path built no summary node at all; this is the row that has all three —
    its own tree, links down, and the queue drives it."""

    @pytest.fixture
    def node(self, db_session) -> Document:
        response = IngestService(db_session).upload_file(
            UploadedFile(
                data=TWO_TOPIC_TEXT.encode("utf-8"),
                filename="notes.txt",
                content_type="text/plain",
            ),
            options=INGEST_OPTIONS,
        )
        drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=400)
        return DocumentRepository(db_session).get(response.document_id)

    def test_the_drain_minted_a_summary_tree(self, node, db_session):
        roots = db_session.execute(select(DerivedRoot)).scalars().all()
        assert [root.tree_kind for root in roots] == [SUMMARY_TREE_KIND]
        # The corpus is ungoverned, so the key is the empty one and the root carries no
        # grants — public, by the same rule that makes its source public.
        assert roots[0].access_key == ""

        derived = db_session.execute(
            select(Document).where(Document.parent_id == roots[0].document_id)
        ).scalars()
        by_source = {doc.structured_content[SOURCE_NODE_KEY]: doc for doc in derived}
        assert by_source, "the queue produced no derived summary node"
        for source_id, summary in by_source.items():
            assert summary.usetype == USETYPE_SUMMARY
            assert summary.embed is not None
            assert [link.target_id for link in _links(db_session, summary.id)] == child_ids(
                db_session, source_id
            )

    def test_the_source_tree_is_unchanged_by_having_been_summarised(self, node, db_session):
        """The whole of Part 1, end to end: every node the derivation covered is still
        where the structure rungs put it, and still settled."""
        rows = (
            db_session.execute(
                select(Document).where(Document.path.contains([node.id]))  # type: ignore[arg-type]
            )
            .scalars()
            .all()
        )
        assert rows
        assert all(row.settled == "settled" for row in rows)
        assert all(row.parent_id is not None for row in rows)

    def test_the_walk_stopped(self, node, db_session):
        pending = db_session.execute(
            select(TaskQueue).where(TaskQueue.status.in_(["pending", "claimed", "running"]))
        ).scalars()
        assert [task.task_type for task in pending] == []
