"""The page-at-a-time projection answers what the per-node walk answers.

``jmfts_core.effective_content.project_effective_content`` is a second implementation of
``jmfts_core.rollup_tasks.effective_text``, in SQL, because the Python one is one
``session.get`` and one child query per node and a page of 100 search results would be tens
of thousands of round-trips. Two implementations of one definition drift, so the first test
here is the one that stops them: it builds a tree exercising every branch and asserts the
two agree node for node.

The rest check what a search response needs and ``effective_text`` does not distinguish —
a node that stands for no text at all must stay NULL rather than becoming ``""``.
"""

from __future__ import annotations

import pytest

from jmfts_core.effective_content import (
    MAX_DEPTH,
    SEPARATOR,
    project_effective_content,
    project_one,
)
from jmfts_core.models.document import Document
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.rollup_tasks import effective_text


def _node(session, *, parent=None, content=None, position=None, usetype="chunk", title="n"):
    doc = Document(
        title=title,
        content=content,
        usetype=usetype,
        parent_id=parent,
        position=position,
        settled="settled",
    )
    session.add(doc)
    session.flush()
    return doc


def _summarize(session, doc, text):
    """Write the evidence row that makes a node stop being a concatenation of its children."""
    EvidenceRepository(session).write(
        doc.id, "effective_content", {"method": "llm_summary", "source_children": 2, "text": text}
    )
    session.flush()


class TestTheProjectionAgreesWithTheDefinition:
    def test_the_projection_agrees_with_effective_text(self, db_session):
        """One tree, every branch of ``effective_text``, compared node for node.

        * a leaf with content
        * a container with no content, which concatenates
        * a container of containers, which concatenates recursively
        * a container carrying a STORED SUMMARY, which must not be descended past
        * a sibling ordered by ``position``, out of insertion order
        """
        root = _node(db_session, usetype="file")
        left = _node(db_session, parent=root.id, position=0, usetype="section")
        _node(db_session, parent=left.id, position=1, content="second")
        _node(db_session, parent=left.id, position=0, content="first")

        summarized = _node(db_session, parent=root.id, position=1, usetype="section")
        _node(db_session, parent=summarized.id, position=0, content="ignored raw text")
        _summarize(db_session, summarized, "the summary that replaces it")

        right = _node(db_session, parent=root.id, position=2, content="own content")

        for node in (root, left, summarized, right):
            assert project_one(db_session, node.id) == effective_text(
                db_session, node.id
            ), f"node {node.id} ({node.usetype}) disagrees"

    def test_position_orders_the_concatenation_not_insertion(self, db_session):
        """The ordering contract is ``position ASC NULLS LAST, created_at ASC, id ASC``, and
        the projection builds its sort key from those columns rather than from a
        ``row_number()`` — which a recursive CTE's recursive term may not contain."""
        root = _node(db_session, usetype="section")
        _node(db_session, parent=root.id, position=2, content="C")
        _node(db_session, parent=root.id, position=0, content="A")
        _node(db_session, parent=root.id, position=1, content="B")

        assert project_one(db_session, root.id) == f"A{SEPARATOR}B{SEPARATOR}C"

    def test_a_null_position_sorts_last(self, db_session):
        root = _node(db_session, usetype="section")
        _node(db_session, parent=root.id, position=None, content="last")
        _node(db_session, parent=root.id, position=0, content="first")

        assert project_one(db_session, root.id) == f"first{SEPARATOR}last"

    def test_a_negative_position_still_sorts_first(self, db_session):
        """The sort key offsets by 2147483648 so it is monotone over the whole of int4.
        ``lpad`` on a bare ``-1`` would sort it after ``0``."""
        root = _node(db_session, usetype="section")
        _node(db_session, parent=root.id, position=0, content="zero")
        _node(db_session, parent=root.id, position=-5, content="negative")

        assert project_one(db_session, root.id) == f"negative{SEPARATOR}zero"

    def test_a_stored_summary_stops_the_descent(self, db_session):
        """``effective_text`` returns a stored summary INSTEAD of descending. A walk that
        went past one would return text the node does not stand for — and, for BM25, would
        put LLM prose into statistics the corpus never wrote."""
        root = _node(db_session, usetype="file")
        _node(db_session, parent=root.id, position=0, content="raw leaf text")
        _summarize(db_session, root, "the summary")

        assert project_one(db_session, root.id) == "the summary"
        assert "raw leaf" not in project_one(db_session, root.id)


class TestWhatASearchResponseNeeds:
    def test_a_node_standing_for_no_text_is_absent_rather_than_empty(self, db_session):
        """``effective_text`` returns ``""`` for this and for a node with an empty string of
        its own. A response filler needs them apart: an empty ``content`` reads as
        "measured, and empty", and this node was never measured."""
        empty = _node(db_session, usetype="section")

        assert effective_text(db_session, empty.id) == ""
        assert project_effective_content(db_session, [empty.id]) == {}
        assert project_one(db_session, empty.id) is None

    def test_whitespace_only_children_contribute_nothing(self, db_session):
        """``effective_text`` filters on ``part.strip()`` at every level, so a subtree of
        blank leaves projects to nothing rather than to a run of separators."""
        root = _node(db_session, usetype="section")
        _node(db_session, parent=root.id, position=0, content="   \n  ")
        _node(db_session, parent=root.id, position=1, content="\t")

        assert project_one(db_session, root.id) is None

    def test_one_query_answers_for_many_roots_at_once(self, db_session):
        """The reason this module exists. Each root gets its own text, keyed by its own id,
        and unrelated roots do not bleed into one another."""
        first = _node(db_session, usetype="section")
        _node(db_session, parent=first.id, position=0, content="alpha")
        second = _node(db_session, usetype="section")
        _node(db_session, parent=second.id, position=0, content="beta")
        leaf = _node(db_session, content="gamma")

        projected = project_effective_content(db_session, [first.id, second.id, leaf.id])
        assert projected == {first.id: "alpha", second.id: "beta", leaf.id: "gamma"}

    def test_an_unknown_id_is_simply_missing(self, db_session):
        assert project_effective_content(db_session, [-1]) == {}

    def test_no_ids_makes_no_query(self, db_session):
        assert project_effective_content(db_session, []) == {}

    def test_the_walk_is_depth_bounded(self, db_session):
        """A cycle in ``parent_id`` would be a corrupt row rather than a legal shape, but a
        recursive CTE that meets one does not error — it runs until the connection dies. The
        bound is asserted here so it cannot be removed as decoration."""
        assert MAX_DEPTH > 0
        chain = _node(db_session, usetype="section")
        top = chain
        for depth in range(MAX_DEPTH + 3):
            chain = _node(db_session, parent=chain.id, position=0, usetype="section")
        chain.content = "past the bound"
        db_session.flush()

        assert project_one(db_session, top.id) is None


class TestTheSearchResponseCarriesIt:
    """What the projection is FOR. Driven through ``SearchService`` rather than through a
    live query, because the thing under test is the response filler and not the ranking."""

    @staticmethod
    def _service_and_results(session, documents):
        from jmfts_core.repositories.search import SearchResult
        from jmfts_core.services.search_service import SearchService

        return (
            SearchService(session),
            [SearchResult(document=d, score=0.5, method="vector") for d in documents],
        )

    def test_a_container_result_renders_its_subtree(self, db_session):
        container = _node(db_session, usetype="section", title="a section")
        _node(db_session, parent=container.id, position=0, content="alpha")
        _node(db_session, parent=container.id, position=1, content="beta")

        service, results = self._service_and_results(db_session, [container])
        (projected,) = service._project(results)

        assert projected.content == f"alpha{SEPARATOR}beta"
        assert projected.content_source == "effective"

    def test_a_leaf_result_is_untouched_and_says_so(self, db_session):
        leaf = _node(db_session, content="stored text")

        service, results = self._service_and_results(db_session, [leaf])
        (projected,) = service._project(results)

        assert projected.content == "stored text"
        assert projected.content_source == "stored"

    def test_a_container_with_nothing_under_it_stays_null(self, db_session):
        """Not ``""``. A caller distinguishing "nothing to show" from "shown as empty"
        needs the NULL, and inventing a string here would be the fallback the pipeline
        spent `store_effective_content` avoiding."""
        empty = _node(db_session, usetype="section")

        service, results = self._service_and_results(db_session, [empty])
        (projected,) = service._project(results)

        assert projected.content is None
        assert projected.content_source == "stored"

    def test_the_page_keeps_its_order_and_its_scores(self, db_session):
        """``_project`` returns documents positionally and ``_build_response`` zips them
        back against the results. A reordering here would attach every score to the wrong
        document, silently."""
        first = _node(db_session, content="one", title="first")
        second = _node(db_session, usetype="section", title="second")
        _node(db_session, parent=second.id, position=0, content="two")
        third = _node(db_session, content="three", title="third")

        service, results = self._service_and_results(db_session, [first, second, third])
        projected = service._project(results)

        assert [d.title for d in projected] == ["first", "second", "third"]
        assert [d.content for d in projected] == ["one", "two", "three"]


class TestSynthesisSeesItToo:
    """The context handed to an LLM is built from a THIRD site, and it read the ORM row.

    A blank search result has a reader who can notice. A blank synthesis context does not:
    the container occupies a slot in a bounded window, contributes nothing, and the model
    answers from whatever ranked below it — or says it found nothing, about the node the
    ranking called the best answer.
    """

    def test_the_llm_context_carries_the_projected_text(self, db_session, monkeypatch):
        import asyncio
        from unittest.mock import MagicMock

        from jmfts_core.rest.schemas import SynthesizeRequest
        from jmfts_core.repositories.search import SearchResult
        from jmfts_core.services import search_service as module
        from jmfts_core.synthesis import SynthesisResult

        container = _node(db_session, usetype="section", title="the section")
        _node(db_session, parent=container.id, position=0, content="alpha")
        _node(db_session, parent=container.id, position=1, content="beta")

        repo = MagicMock()
        repo.hybrid_search.return_value = [
            SearchResult(document=container, score=0.9, method="hybrid")
        ]
        monkeypatch.setattr(module, "SearchRepository", lambda session: repo)
        monkeypatch.setattr(module.SearchService, "_resolve_context", lambda self, c, o: {})

        seen = {}

        async def _capture(*, query, documents, max_context_tokens, llm_model):
            seen["documents"] = documents
            return SynthesisResult(text="an answer", model="stub", usage={})

        monkeypatch.setattr(module, "synthesize", _capture)

        service = module.SearchService(db_session)
        asyncio.run(
            service.synthesize_search(
                request=SynthesizeRequest(query="alpha", search_method="hybrid", top_k=3),
                context=None,
            )
        )

        assert seen["documents"][0]["content"] == f"alpha{SEPARATOR}beta"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
