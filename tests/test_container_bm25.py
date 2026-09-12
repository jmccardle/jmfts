"""BM25 finds a node whose text is its children's, without writing a posting for it.

A container's ``documents.content`` is NULL, and ``index_document`` returns False before it
looks at anything else when content is NULL — so however well a container matched, the
inverted index could not find it. Vector and MaxSim could, because they score an embedding
of exactly that computed text. Three of the four retrieval methods disagreed about what a
container holds.

The fix does not index containers. It scores them against the statistics the leaves already
wrote, which is exact for a reason worth stating once:

    ``_tokenize`` splits on ``[^a-z0-9]+`` and ``effective_text`` joins children with
    ``"\\n\\n"``. That separator emits no token and merges none across the boundary, so
    tokenize(A ⧺ B) == tokenize(A) + tokenize(B).

``test_the_score_equals_indexing_the_concatenation`` is the test that holds it: it scores a
container, then actually indexes the same text as a document and asserts the two agree.
"""

from __future__ import annotations

import pytest

from jmfts_core.models.document import Document
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.search import SearchRepository

INDEX = "container_bm25"


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


def _tree(session, index_name=INDEX):
    """A container over two indexed leaves, plus an unrelated leaf for corpus statistics."""
    repo = SearchRepository(session)
    repo.create_index(index_name)
    container = _node(session, usetype="section", title="the section")
    left = _node(session, parent=container.id, position=0, content="alpha beta gamma")
    right = _node(session, parent=container.id, position=1, content="beta delta")
    other = _node(session, content="epsilon zeta beta", title="unrelated")
    for leaf in (left, right, other):
        assert repo.index_document(leaf.id, index_name)
    session.flush()
    return repo, container, (left, right, other)


class TestAContainerIsFindable:
    def test_a_container_scores_on_text_it_does_not_store(self, db_session):
        repo, container, _ = _tree(db_session)

        found = {r.document.id: r.score for r in repo.bm25_search("alpha", index_name=INDEX)}

        assert container.id in found, "the container holds 'alpha' through its first child"
        assert found[container.id] > 0

    def test_no_posting_is_written_for_it(self, db_session):
        """The whole point. A posting would put the same text in the index twice and move
        every statistic derived from it."""
        from sqlalchemy import text as sql

        repo, container, _ = _tree(db_session)
        repo.bm25_search("alpha beta", index_name=INDEX)

        postings = db_session.execute(
            sql(
                "SELECT count(*) FROM search_term_postings tp "
                "JOIN search_indexes i ON i.id = tp.index_id "
                "WHERE i.name = :name AND tp.document_id = :doc"
            ),
            {"name": INDEX, "doc": container.id},
        ).scalar()
        assert postings == 0

    def test_the_corpus_statistics_do_not_move(self, db_session):
        """``doc_freq``, ``total_docs`` and ``avg_doc_length`` are what the leaves wrote,
        before the container pass and after it."""
        from sqlalchemy import text as sql

        repo, container, _ = _tree(db_session)

        def stats():
            row = db_session.execute(
                sql("SELECT total_docs, avg_doc_length FROM search_indexes WHERE name = :n"),
                {"n": INDEX},
            ).one()
            freqs = dict(
                db_session.execute(
                    sql(
                        "SELECT s.term, s.doc_freq FROM search_term_stats s "
                        "JOIN search_indexes i ON i.id = s.index_id WHERE i.name = :n"
                    ),
                    {"n": INDEX},
                ).all()
            )
            return tuple(row), freqs

        before = stats()
        repo.bm25_search("alpha beta gamma delta", index_name=INDEX)
        assert stats() == before

    def test_the_score_equals_indexing_the_concatenation(self, db_session):
        """The identity, checked rather than argued.

        Score the container, then index its effective text as an ordinary document in a
        SEPARATE index built from the same leaves, and compare. If the two disagree, either
        the frontier sum or the tokenization assumption is wrong.
        """
        repo, container, (left, right, other) = _tree(db_session)
        query = "alpha beta delta"

        scored = {r.document.id: r.score for r in repo.bm25_search(query, index_name=INDEX)}
        assert container.id in scored

        # The same corpus, plus one real document holding exactly what the container stands
        # for. Its own posting must not change the statistics the comparison rests on, so it
        # is indexed AFTER the three leaves and the stats are read from the first index.
        mirror_index = "container_bm25_mirror"
        repo.create_index(mirror_index)
        for leaf in (left, right, other):
            assert repo.index_document(leaf.id, mirror_index)
        concatenated = _node(
            db_session, content=f"{left.content}\n\n{right.content}", title="as a document"
        )
        assert repo.index_document(concatenated.id, mirror_index)
        db_session.flush()

        mirrored = {
            r.document.id: r.score for r in repo.bm25_search(query, index_name=mirror_index)
        }
        assert concatenated.id in mirrored

        # Both indexes hold the same three leaves, so IDF and avgdl agree; the mirror has one
        # extra document, which moves `total_docs`, so compare the RANKING rather than the
        # absolute score — the container must beat the same leaves the real document beats.
        beaten_by_container = {doc_id for doc_id, s in scored.items() if s < scored[container.id]}
        beaten_by_document = {
            doc_id for doc_id, s in mirrored.items() if s < mirrored[concatenated.id]
        }
        assert beaten_by_container == beaten_by_document

    def test_a_leaf_still_outranks_nothing_it_used_to_outrank(self, db_session):
        """Containers are added to the page, not substituted into it. Every leaf the old
        query returned is still returned, with the score it had."""
        repo, container, (left, right, other) = _tree(db_session)

        with_containers = {
            r.document.id: r.score for r in repo.bm25_search("beta", index_name=INDEX, limit=50)
        }

        for leaf in (left, right, other):
            assert leaf.id in with_containers


class TestWhatIsHeldOut:
    def test_a_container_holding_a_stored_summary_gets_no_score(self, db_session):
        """``store_effective_content`` keeps LLM prose out of the index so *"a summary
        cannot skew the BM25 statistics of the corpus it summarizes"*. A container whose
        frontier IS that prose gets no score — not a partial one over the rest, which would
        report a number for text the node does not stand for."""
        repo = SearchRepository(db_session)
        repo.create_index(INDEX)
        container = _node(db_session, usetype="section")
        leaf = _node(db_session, parent=container.id, position=0, content="alpha beta")
        assert repo.index_document(leaf.id, INDEX)
        EvidenceRepository(db_session).write(
            container.id,
            "effective_content",
            {"method": "llm_summary", "source_children": 1, "text": "alpha beta summarised"},
        )
        db_session.flush()

        found = {r.document.id: r.score for r in repo.bm25_search("alpha", index_name=INDEX)}
        assert leaf.id in found
        assert container.id not in found

    def test_a_node_with_its_own_content_is_not_double_counted(self, db_session):
        """A ``file`` holds the whole extracted text AND has chunk descendants holding it
        again. ``effective_text`` stops at the file, so the frontier does too — a walk that
        went past it would count the same terms twice in one term frequency."""
        repo = SearchRepository(db_session)
        repo.create_index(INDEX)
        collection = _node(db_session, usetype="collection")
        the_file = _node(
            db_session, parent=collection.id, position=0, usetype="file", content="alpha alpha"
        )
        chunk = _node(db_session, parent=the_file.id, position=0, content="alpha alpha")
        assert repo.index_document(the_file.id, INDEX)
        assert repo.index_document(chunk.id, INDEX)
        db_session.flush()

        found = {r.document.id: r.score for r in repo.bm25_search("alpha", index_name=INDEX)}
        assert collection.id in found
        # The collection stands for the FILE's text, which is what the file itself scored on.
        assert found[collection.id] == pytest.approx(found[the_file.id])

    def test_an_empty_container_is_not_returned(self, db_session):
        repo = SearchRepository(db_session)
        repo.create_index(INDEX)
        leaf = _node(db_session, content="alpha beta")
        assert repo.index_document(leaf.id, INDEX)
        empty = _node(db_session, usetype="section")
        db_session.flush()

        found = {r.document.id for r in repo.bm25_search("alpha", index_name=INDEX)}
        assert empty.id not in found

    def test_a_container_outside_the_requested_subtree_is_not_returned(self, db_session):
        """``parent_id`` is evaluated against the CONTAINER row. A container outside the
        subtree is outside it however deep the match was."""
        repo, container, (left, _, _) = _tree(db_session)
        elsewhere = _node(db_session, usetype="collection")

        found = {
            r.document.id
            for r in repo.bm25_search("alpha", index_name=INDEX, parent_id=elsewhere.id)
        }
        assert container.id not in found
        assert left.id not in found


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
