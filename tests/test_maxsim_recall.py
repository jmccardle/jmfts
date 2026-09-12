"""`maxsim_search` returns a ranked list, and it has to be the list a caller reads it as.

**This file was the ENTRY condition and is now the EXIT condition, and not one assertion
changed to make that true.** `docs/SPRINT_0_4_0.md` Block A and `docs/ANN_INDEX_HEALTH.md`
4.3 recorded the same candidate in the same words: nothing in `jmfts_core/` set
`ivfflat.probes`, so every MaxSim ANN query ran at pgvector's default of one probe over
`lists = 1024`. Part 0's rule is that **an open defect is a numbered step whose entry
condition is a failing test**, an argument is not a step, and the entry condition was *"a
failing test through `maxsim_search`, and `scripts/maxsim_recall.py` is a throwaway-table
harness rather than that test."* This file supplied it, at 0.4542 against a threshold of
0.90 (`ANN_INDEX_HEALTH.md` 5.5). Migration 022 makes `embed_256` an HNSW index and 5.8 is
the decision; what closes the step is this same threshold, asserted on the same corpus by
the same three tests, going green.

It runs against the appliance's own `documents` and `token_embeddings`, the
`idx_token_embed_256_hnsw` that `jmfts_core/sql/schema.sql` builds, real
`nomic-ai/modernbert-embed-base` vectors, and a real non-owner principal whose grant makes
`readable_sql` emit a fragment. `scripts/maxsim_probes.py` sweeps the ANN setting over the
same fixture and is where the numbers behind the threshold come from; both import
`tests/maxsim_corpus.py`, so the assertion and the measurement cannot drift apart.

**Why 0.90, and why not 0.5.** The number is the answer to "what would a caller be wrong
to assume", and three readings bound it.

* `maxsim_search` hands back a list and says nothing about approximation. The one signal
  that could carry that meaning is `AppliedFilters.truncated`, and `ANN_INDEX_HEALTH.md`
  1.5 measured it firing on 44 of 51 ANN pages in a suite run where nothing was wrong —
  86.3% — so it cannot. A caller therefore reads the list as the top N by MaxSim.
* It cannot be 1.0. ANN is approximate by construction, and `docs/STRESS_CORPUS.md` 6.1
  records even a clean HNSW index on real vectors at 0.9767 with 10 of 60 queries losing a
  neighbour. That is the floor any recall claim about this appliance reads against.
* 0.90 was reachable four separate ways when it was chosen, which is why it did not
  prejudge the repair. `STRESS_CORPUS.md` 6.2 measured the old index on 1.37M real token
  rows at 0.9433 (`probes = 10`) and 0.9767 (`probes = 50`) against 0.7533 at the shipped
  default, and HNSW on the same rows at 0.9667 for the same 0.65 ms. The threshold survived
  the decision unchanged, so it is still a statement about what a caller may assume rather
  than a restatement of what the current index happens to deliver.

0.5 would encode the opposite: that a search returning half the documents it ranked is
working. `4dadb62` already measured 0.51 and it is recorded as a defect, not as a spec.

**The two controls are the point.** Ground truth is the same `maxsim_search`, same rows,
same transaction, same query embedding, with every index path denied so the planner sorts —
so a shortfall is ANN-against-exact and never a judgement about what the right answer was.
And the `open` arm asks as the owner, for whom no ACL fragment is built at all, because
`4dadb62`'s finding was that the ungated scan was already losing about half its true
neighbours BEFORE any filter existed. `test_the_read_gate_is_not_what_is_lost` is that
control as an assertion, and it passes: attributing this to RBAC would be wrong by the
width of the whole measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import text as sa_text

from jmfts_core.principal_context import reset_principal, set_principal
from jmfts_core.repositories.search import SearchRepository
from tests.conftest import DB_READY
from tests.maxsim_corpus import (
    EF_SEARCH,
    K_PER_TOKEN,
    TOKEN_INDEX,
    ann_maxsim,
    ann_plan_reaches_the_index,
    exact_maxsim,
    query_texts,
    recall,
    seed_split_corpus,
)

pytestmark = pytest.mark.skipif(not DB_READY, reason="test database not provisioned")

#: What a caller would be wrong to assume is not true. See the module docstring for the
#: three readings that bound it; the short version is that 1.0 is unreachable for any ANN
#: index and `STRESS_CORPUS.md` 6.2 read 0.9667 for HNSW on the appliance's own 1.37M
#: token rows.
MINIMUM_RECALL = 0.90

#: Chunks to seed. Two constraints, from opposite directions. Below about 2,000 token rows
#: the planner prefers a sort and reads no index at all, and a recall number off a
#: sequential scan measures nothing — `ann_plan_reaches_the_index` is the guard, and 945
#: rows was measured on the wrong side of it. Above that, `limit = 10` has to be selective,
#: which is a fact about the DOCUMENT count rather than the row count: at 48 documents a
#: near-random page still scores 0.43 because ten of forty-eight is not a narrow target.
#: 200 chunks is roughly 15,000 token rows over 200 documents and clears both.
CHUNKS = 200
#: Queries per arm. One draw is not a measurement — where a single query enters the index,
#: and what it reaches from there, is close to arbitrary, and `scripts/maxsim_recall.py`
#: says so at `--queries`.
QUERIES = 24
#: What a caller asks for. Recall is over the documents `maxsim_search` hands back.
LIMIT = 10


@pytest.fixture(scope="module")
def corpus_session():
    """One session, one outer transaction, rolled back — but module-scoped.

    `conftest.db_session` is per-test, and this fixture costs a model pass per chunk; paying
    that for every assertion below would make the file minutes slower to say the same thing.
    The containment is identical: an outer connection-level transaction that is rolled back
    at teardown, so nothing here reaches the database it ran against.
    """
    from jmfts_core.database import get_engine, get_session_factory

    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    session = get_session_factory()(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@dataclass(frozen=True)
class Measured:
    """What one pass over the fixture found, so three assertions cost one pass.

    Every number below costs a model call per query and an exact scan of the whole table
    per query per arm. Both assertions in this file read the same two arms, so they are
    measured once here rather than twice in the tests — and that also makes them the SAME
    numbers, which matters when one of them is the control for the other.
    """

    corpus: object
    session: object
    gated: list[float]
    opened: list[float]

    @property
    def gated_mean(self) -> float:
        return sum(self.gated) / len(self.gated)

    @property
    def open_mean(self) -> float:
        return sum(self.opened) / len(self.opened)


@pytest.fixture(scope="module")
def measured(corpus_session):
    """Seed the corpus, take ground truth per arm, then ask the shipped configuration.

    Truth is computed once per arm: it is a property of the query and the rows, and no ANN
    setting can move it, so recomputing it per assertion would be an exact scan of the whole
    table for every line below. `ann_maxsim` is called with `ef_search=None`, which issues
    no `SET hnsw.ef_search` at all — the appliance issues none either, and that is the
    configuration under measurement. `maxsim_search`'s own `SET LOCAL hnsw.iterative_scan`
    is left alone for the same reason: it IS the shipped configuration.
    """
    built = seed_split_corpus(corpus_session, CHUNKS)
    repo = SearchRepository(corpus_session)
    queries = query_texts(QUERIES)
    scores = {}
    for arm, principal in built.arms.items():
        token = set_principal(principal)
        try:
            truth = {q: exact_maxsim(repo, q, LIMIT) for q in queries}
            scores[arm] = [
                recall(ann_maxsim(repo, q, LIMIT, ef_search=None), truth[q]) for q in queries
            ]
        finally:
            reset_principal(token)
    return Measured(
        corpus=built.corpus,
        session=corpus_session,
        gated=scores["gated"],
        opened=scores["open"],
    )


@pytest.fixture(autouse=True)
def _no_leaked_probes(corpus_session):
    """Undo any `SET LOCAL hnsw.ef_search` a previous test in this module issued.

    Every test here shares one transaction, and `SET LOCAL` lives until that transaction
    ends. Without this, "the shipped default" would mean "whatever the last sweep set",
    which is the one thing this file must not get wrong.

    `hnsw.iterative_scan` is deliberately NOT reset. `maxsim_search` sets it on every call
    (`repositories/search.py`, migration 022), so resetting it here would only widen the
    window in which the session carries a value no measurement below runs under.
    """
    corpus_session.execute(sa_text("RESET hnsw.ef_search"))
    yield


def _diagnosis(measured):
    """What was measured, printed at the point of failure rather than looked up later."""
    corpus = measured.corpus
    effective = measured.session.execute(sa_text("SHOW hnsw.ef_search")).scalar()
    scan_mode = measured.session.execute(sa_text("SHOW hnsw.iterative_scan")).scalar()
    return (
        f"\n  corpus         {corpus.token_rows} token rows over "
        f"{len(corpus.document_ids)} documents"
        f"\n  index          {TOKEN_INDEX} (schema.sql:537, migration 022)"
        f"\n  requested      {K_PER_TOKEN} rows per query token (search.py:1018)"
        f"\n  hnsw.ef_search {effective} — pgvector's default ({EF_SEARCH}), and nothing in "
        "jmfts_core issues a SET"
        f"\n  iterative_scan {scan_mode} — this one maxsim_search DOES set, every call"
        f"\n  recall@{LIMIT}      {measured.gated_mean:.4f} gated, {measured.open_mean:.4f} "
        "ungated — the control says the read gate is not what is lost"
        f"\n  degraded       {sum(1 for r in measured.gated if r < 1.0)}/{len(measured.gated)}"
        " queries"
        f"\n  see            docs/ANN_INDEX_HEALTH.md Part 5"
    )


def test_the_fixture_reaches_the_index(corpus_session, measured):
    """Nothing below is a measurement of the index unless the planner actually reads it.

    This is the `plan` column `scripts/maxsim_recall.py` prints and tells its reader to
    check before concluding anything: a row that says seq+sort was never a test of the
    index, and a recall of 1.0 off a sequential scan would have this file assert that the
    defect is absent. It is the first assertion in the file on purpose.
    """
    assert ann_plan_reaches_the_index(corpus_session, measured.corpus), (
        f"the planner does not read {TOKEN_INDEX} at {measured.corpus.token_rows} token "
        "rows, so every recall number in this file would be measuring a sequential scan. "
        "Raise CHUNKS."
    )


def test_a_shortfall_here_is_never_explained_by_the_read_gate(measured):
    """The control: removing the gate entirely does not recover the loss.

    `4dadb62` is why this exists. It measured a gated recall of 0.51 beside an ungated 0.50,
    which says the loss was already there before a principal was involved; a harness
    carrying one control would have handed RBAC the blame for the index's approximation.

    It is written as an implication rather than as a flat assertion on the ungated number,
    and that is deliberate. A test that asserted *"the ungated arm is below 0.90"* would be
    asserting that the defect is present, and would go red on the day somebody fixes it —
    which is the shape `ANN_INDEX_HEALTH.md` 1.3 got wrong in the other direction. What is
    durable is the attribution: while the gated arm falls short, the ungated arm falls short
    too, so the gate is never the account of it. When the shortfall is gone there is nothing
    left to attribute and this passes with nothing to say.
    """
    if measured.gated_mean >= MINIMUM_RECALL:
        return
    assert measured.open_mean < MINIMUM_RECALL, (
        f"the gated arm reads {measured.gated_mean:.4f} and falls short, but the UNGATED arm "
        f"reads {measured.open_mean:.4f} and clears the threshold. The read gate would then "
        "be the account of the shortfall, and this file's conclusion — that the loss is the "
        "index's own approximation — would be the wrong one."
    )


def test_maxsim_returns_the_documents_it_ranked(measured):
    """THE step condition: `maxsim_search` hands back the documents it ranked.

    It read 0.4542 against this threshold on the IVFFlat index built by an empty table
    (`ANN_INDEX_HEALTH.md` 5.5), which is what made the defect a numbered step, and
    migration 022 is what answers it.

    No `SET hnsw.ef_search` is issued anywhere in this file's measurement path, because the
    appliance issues none: this is the configuration a caller gets. `maxsim_search`'s own
    `SET LOCAL hnsw.iterative_scan` is left standing for the same reason. Ground truth is
    the same call on the same rows with every index path denied.
    """
    assert measured.gated_mean >= MINIMUM_RECALL, (
        f"maxsim_search returned {measured.gated_mean:.4f} of the documents an exact scan "
        f"ranks in its top {LIMIT}, against a threshold of {MINIMUM_RECALL}." + _diagnosis(measured)
    )
