"""A real-embedding corpus for the MaxSim ANN path, and the recall instrument over it.

`docs/ANN_INDEX_HEALTH.md` 4.3 and `docs/SPRINT_0_4_0.md` Block A both recorded that nothing
in `jmfts_core/` set `ivfflat.probes`, so every MaxSim ANN query ran at pgvector's default
of one probe over `lists = 1024`. Both also said what was missing: a failing test through
the real `maxsim_search`. This module is the fixture that test stands on, and
`scripts/maxsim_probes.py` sweeps the same knob over the same fixture, so the assertion and
the measurement cannot describe different corpora.

**MIGRATION 022 CHANGED WHAT THAT KNOB IS.** `embed_256` is an HNSW index
(`sql/schema.sql:537`), the setting is `hnsw.ef_search`, and `maxsim_search` now issues a
`SET LOCAL hnsw.iterative_scan` of its own. `ANN_INDEX_HEALTH.md` 5.8 is the decision and
5.7 is the measurement behind it; 5.3's `probes` table is the earlier index and is history.
The two controls below did not change, because they are about the harness rather than about
the index.

**Every vector here comes from the model.** That is not fastidiousness; it is the lesson of
`ANN_INDEX_HEALTH.md` Part 0, and a first version of this file repeated the second of the
two faults recorded there. Clustered Gaussian vectors with a per-component spread of 0.25
in 256 dimensions have a noise norm of ``0.25 * sqrt(256) = 4.0`` around unit-norm cluster
centres, which is not a cluster but isotropic noise; measured against them the shipped
index read recall 0.0155 and a k-means build read barely better, which is a fact about the
fixture. `scripts/maxsim_recall.py` avoids the same trap a different way, by drawing its
queries from the corpus it generated. Here the geometry is the model's, so there is nothing
to get wrong: the corpus is topical English, the queries are terms from the same topics,
and `nomic-ai/modernbert-embed-base` decides where any of it lands.

The text is generated rather than read from a file so the fixture is deterministic, needs
no dataset, and ships. The topics are deliberately unrelated to each other — twenty-four
of them, from marine biology to cryptography — because a corpus with no topical structure
is a corpus where every neighbour is equally far away, and an ANN index that reads badly on
it is being measured against its own fixture rather than against the appliance. That was
written against IVFFlat, whose k-means had nothing to partition; migration 022 makes the
index HNSW and the requirement does not relax, because a graph walk with no structure to
follow is the same failure with a different mechanism.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from sqlalchemy import text as sa_text

#: `token_embeddings.embed_256` (`models/token_embedding.py:41`), and the only dimension
#: `maxsim_search` accepts (`repositories/search.py:1007`).
DIM = 256
#: The index under measurement (`sql/schema.sql:537`, migration 022). Partial on nothing —
#: `token_embeddings` has no `settled` column, so unlike `idx_documents_embed` this one
#: cannot keep a rewritten chunk's rows out of the graph.
TOKEN_INDEX = "idx_token_embed_256_hnsw"
#: pgvector's default `hnsw.ef_search`, which is what the appliance runs at: nothing in
#: `jmfts_core/` issues that SET. `maxsim_search` DOES set `hnsw.iterative_scan`, which is
#: a different knob — it decides whether the scan re-enters the graph when a filter empties
#: a batch, not how many candidates one entry takes.
EF_SEARCH = 40
#: `repositories/search.py:1018`. How many token rows ONE query token asks the index for.
K_PER_TOKEN = 100
#: What the ingest path stores per chunk (`settings.token_top_percent` is 0.5 by default).
TOP_PERCENT = 0.5

#: Twenty-four topics with disjoint vocabularies. The model puts each one somewhere
#: different, which is what gives an ANN index over them something real to follow.
TOPICS: dict[str, str] = {
    "marine biology": (
        "coral reef polyp bleaching symbiont zooxanthellae larvae spawning atoll lagoon "
        "tidal salinity plankton krill baleen cetacean migration estuary mangrove seagrass"
    ),
    "railway signalling": (
        "interlocking semaphore block section token absolute permissive aspect shunt "
        "points catch siding gradient axle counter track circuit relay lever frame"
    ),
    "bread baking": (
        "levain autolyse hydration gluten crumb crust proof banneton scoring steam oven "
        "spring sourdough starter rye spelt ferment bulk shaping bake"
    ),
    "orbital mechanics": (
        "apoapsis periapsis inclination eccentricity transfer Hohmann burn delta-v "
        "perturbation precession node ascending retrograde insertion capture aerobraking"
    ),
    "medieval manuscripts": (
        "vellum parchment quire gathering rubric illuminator scriptorium colophon gloss "
        "marginalia palimpsest binding gilding pigment lapis minium scribe hand uncial"
    ),
    "compiler design": (
        "lexer parser grammar production terminal reduction register allocation liveness "
        "dominator basic block inlining unrolling escape analysis intermediate representation"
    ),
    "volcanology": (
        "caldera magma chamber pyroclastic tephra lahar fumarole basalt andesite viscosity "
        "effusive plinian dome collapse seismic swarm tremor degassing sulphur"
    ),
    "textile weaving": (
        "warp weft heddle shuttle loom selvedge twill satin dobby jacquard sett reed "
        "denting draft treadle tabby worsted carding spinning fibre"
    ),
    "epidemiology": (
        "incidence prevalence cohort confounding stratification attack rate serial interval "
        "reproduction number contact tracing seroprevalence case definition surveillance"
    ),
    "jazz harmony": (
        "tritone substitution voicing rootless comping modal interchange turnaround cadence "
        "chromatic mediant altered dominant upper structure passing diminished bebop"
    ),
    "soil science": (
        "horizon podzol loam clay silt cation exchange capacity humus mycorrhiza tilth "
        "compaction leaching pedogenesis parent material drainage aggregate porosity"
    ),
    "typography": (
        "kerning tracking leading counter aperture x-height descender ligature hinting "
        "grotesque humanist serif bracket stress axis optical size masters interpolation"
    ),
    "hydrology": (
        "catchment baseflow hydrograph infiltration aquifer recharge confined unconfined "
        "transmissivity evapotranspiration gauging weir stage discharge rating curve"
    ),
    "chess endgames": (
        "opposition zugzwang triangulation fortress tablebase rook pawn Lucena Philidor "
        "underpromotion stalemate corresponding squares outside passed majority"
    ),
    "antique clocks": (
        "escapement verge anchor deadbeat pendulum suspension spring fusee mainspring "
        "barrel pallet arbor pinion wheel train remontoire strike repeater regulator"
    ),
    "mycology": (
        "mycelium hyphae sporocarp basidium ascus spore print lamellae volva annulus "
        "saprotroph ectomycorrhizal fruiting substrate inoculation colonisation primordia"
    ),
    "naval architecture": (
        "displacement metacentre righting arm freeboard sheer camber bilge keel frame "
        "bulkhead scantling hull form block coefficient prismatic wetted surface"
    ),
    "lexicography": (
        "headword lemma citation slip corpus attestation etymology sense division gloss "
        "run-on entry cross-reference pronunciation respelling label register obsolete"
    ),
    "beekeeping": (
        "brood comb foundation super queen excluder swarm nuc varroa forage nectar flow "
        "capped honey propolis requeening apiary hive inspection frame"
    ),
    "cartography": (
        "projection graticule datum ellipsoid grid convergence contour hypsometric "
        "generalisation hachure relief shading gazetteer sheet index scale bar"
    ),
    "fermentation chemistry": (
        "lactobacillus brine osmosis anaerobic acidity titratable pellicle kahm yeast "
        "mash wort attenuation flocculation ester phenol diacetyl conditioning"
    ),
    "structural engineering": (
        "moment shear bending stiffness buckling slenderness reinforcement rebar prestress "
        "camber deflection serviceability ultimate limit state redundancy load path"
    ),
    "ornithology": (
        "plumage moult primaries coverts rectrices eclipse juvenile ringing recovery "
        "philopatry irruption vagrant passage migrant territory song flight call"
    ),
    "cryptography": (
        "cipher keystream nonce initialisation vector mode of operation authenticated "
        "padding oracle collision preimage lattice reduction key schedule round function"
    ),
}

#: Sentence frames. Prose rather than a word list, because the model's token embeddings for
#: a bag of nouns are not the ones the ingest path stores for a document.
_FRAMES = (
    "The {a} governs how the {b} behaves once the {c} is established.",
    "Practitioners disagree about whether {a} or {b} should be recorded before the {c}.",
    "A survey of the {a} found that {b} accounts for most of the variation in {c}.",
    "When the {a} fails, the {b} is usually the first thing to change, and the {c} follows.",
    "Notes on the {a}: its relationship to {b} is indirect, and the {c} mediates it.",
    "Measuring the {a} without disturbing the {b} requires the {c} to be held constant.",
    "In the older literature the {a} is described as a property of the {b}, not of the {c}.",
    "Field work suggests the {a} and the {b} vary together, which the {c} does not explain.",
)

#: One seed for the corpus and one for the queries, both fixed: a recall number that moves
#: between runs because the text moved is not a measurement of the index.
CORPUS_SEED = 20260910
QUERY_SEED = 7


def chunk_texts(
    count: int, start: int = 0, seed: int = CORPUS_SEED, sentences: int = 8
) -> list[str]:
    """`count` chunks of topical prose, round-robin over `TOPICS`, from ordinal `start`.

    Eight sentences is about 125 words, which lands inside
    `settings.embedding_token_window` with room to spare and yields roughly 80 stored
    token rows per chunk at `TOP_PERCENT`.

    `start` exists so two calls produce two DIFFERENT halves of one corpus: the read-gate
    control below needs a readable half and an unreadable half, and two calls that both
    began at zero would put the same text in both.
    """
    rng = random.Random(seed + start)
    names = sorted(TOPICS)
    out = []
    for i in range(start, start + count):
        topic = names[i % len(names)]
        words = TOPICS[topic].split()
        body = [f"Notes on {topic}."]
        for _ in range(sentences):
            a, b, c = (rng.choice(words) for _ in range(3))
            body.append(rng.choice(_FRAMES).format(a=a, b=b, c=c))
        out.append(" ".join(body))
    return out


def query_texts(count: int, seed: int = QUERY_SEED) -> list[str]:
    """`count` queries, each four terms from one topic's vocabulary.

    Drawn from the corpus's own vocabulary for the reason `scripts/maxsim_recall.py`
    states at `_queries`: a query from outside the corpus distribution lands between cells
    and measures the geometry of the probe rather than the index.
    """
    rng = random.Random(seed)
    names = sorted(TOPICS)
    return [" ".join(rng.sample(TOPICS[names[i % len(names)]].split(), 4)) for i in range(count)]


@dataclass(frozen=True)
class Corpus:
    """What `seed_corpus` wrote, so a caller can report the fixture it measured."""

    document_ids: list[int]
    token_rows: int
    #: The subtree the chunks hang under — the node a caller grants on to make the corpus
    #: an access-control root. `seed_corpus` creates one when given no `parent_id`, and
    #: without this the caller would have no way to name it.
    root_id: int
    seconds: float

    # `rows_per_probe` lived here until migration 022 and is deliberately not replaced.
    # It computed `token_rows / LISTS`, which was the whole arithmetic of the old defect:
    # IVFFlat at `probes = 1` scans exactly one of `lists` cells, so a scan touched about
    # that many tuples whatever the query was, and below `K_PER_TOKEN * lists` rows one
    # probe could not hold the `LIMIT 100` `maxsim_search` asks for. HNSW has no counterpart
    # — a walk's cost is `ef_search` candidates out of a graph and does not divide the table
    # — so a number of the same shape would be an invention rather than a translation.


def seed_corpus(session, count: int, *, parent_id: int | None = None, start: int = 0) -> Corpus:
    """Write `count` real-embedding chunks into `documents` and `token_embeddings`.

    The appliance's own tables, the appliance's own index. `DocumentRepository.create`
    writes the document rows (settled, so the retrieval predicates admit them) and the
    token rows go in with one `executemany`, carrying the SAME 256-dim truncation of the
    SAME model output that `EmbeddingService` hands the ingest path.

    ``auto_embed=False`` because this loop truncates and writes the token rows itself; the
    document-level `embed` column plays no part in MaxSim and paying for it would double
    the fixture's cost.
    """
    from jmfts_core.embedding import get_embedding_service
    from jmfts_core.repositories.document import DocumentRepository

    service = get_embedding_service()
    repo = DocumentRepository(session)
    started = time.monotonic()

    if parent_id is None:
        root = repo.create(title="maxsim recall corpus", content="root", auto_embed=False)
        session.flush()
        parent_id = root.id

    document_ids: list[int] = []
    rows: list[dict] = []
    for i, body in enumerate(chunk_texts(count, start=start), start=start):
        doc = repo.create(
            title=f"chunk {i}",
            content=body,
            parent_id=parent_id,
            usetype="chunk",
            auto_embed=False,
        )
        session.flush()
        document_ids.append(doc.id)
        result = service.embed_with_tokens(
            body, top_percent=TOP_PERCENT, prefix="search_document: "
        )
        for j, token in enumerate(result.token_embeddings):
            vector = service.truncate_embedding(token.embedding, DIM)
            rows.append(
                {
                    "document_id": doc.id,
                    "token_idx": j,
                    "token_text": token.token_text,
                    "importance_score": float(token.importance_score),
                    "tier": 10,
                    "embed": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                }
            )

    session.execute(
        sa_text(
            "INSERT INTO token_embeddings "
            "  (document_id, token_idx, token_text, importance_score, tier, embed_256) "
            "VALUES (:document_id, :token_idx, :token_text, :importance_score, :tier, "
            "        CAST(:embed AS halfvec(256)))"
        ),
        rows,
    )
    session.flush()
    # The planner will not choose an ANN scan over a table it believes is empty, and
    # `ANN_INDEX_HEALTH.md` 1.1 is the record of what a stale `reltuples` does here. Nothing
    # is being measured until statistics exist.
    session.execute(sa_text("ANALYZE token_embeddings"))
    session.execute(sa_text("ANALYZE documents"))
    return Corpus(
        document_ids=document_ids,
        token_rows=len(rows),
        root_id=parent_id,
        seconds=time.monotonic() - started,
    )


@dataclass(frozen=True)
class Fixture:
    """A corpus in two halves and the two principals that see different amounts of it."""

    corpus: Corpus
    reader: object
    owner: object

    @property
    def arms(self) -> dict:
        """`{name: principal}`, in the order a report should print them."""
        return {"gated": self.reader, "open": self.owner}


def seed_split_corpus(session, count: int) -> Fixture:
    """`count` chunks in two halves, one of them behind an access-control root.

    The two-control discipline is `scripts/maxsim_recall.py`'s and it is the reason
    `4dadb62`'s reading survived contact with the appliance: an ANN index is approximate
    BEFORE any filter — at one probe the IVFFlat that stood here until migration 022 badly
    so — and a gated recall of 0.5 cannot be told apart from the index's own loss unless the
    ungated arm is measured beside it.

    For that control to control anything the gate has to do real work, which means the
    readable half must be a proper subset. A grant to a principal is what makes a document
    an access-control root (`access.py`), so the unreadable half is rooted under a node
    granted to somebody else: `readable_sql`'s `OR NOT within(...)` arm admits a document
    under NO root, and without that second grant the "hidden" half would be ungoverned and
    therefore readable, which is the gate being a no-op while appearing to be a gate.
    """
    from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
    from jmfts_core.principal_context import CurrentPrincipal
    from jmfts_core.repositories.document import DocumentRepository

    repo = DocumentRepository(session)
    open_root = repo.create(title="maxsim corpus (readable)", content="root", auto_embed=False)
    hidden_root = repo.create(title="maxsim corpus (hidden)", content="root", auto_embed=False)
    session.flush()

    def principal(name, is_owner):
        row = PrincipalModel(name=name, is_owner=is_owner)
        session.add(row)
        session.flush()
        return row

    reader = principal("maxsim-reader", False)
    stranger = principal("maxsim-stranger", False)
    owner = principal("maxsim-owner", True)
    session.add(AccessGrant(document_id=open_root.id, principal_id=reader.id, level="read"))
    session.add(AccessGrant(document_id=hidden_root.id, principal_id=stranger.id, level="read"))
    session.flush()

    half = count // 2
    first = seed_corpus(session, half, parent_id=open_root.id, start=0)
    second = seed_corpus(session, count - half, parent_id=hidden_root.id, start=half)
    corpus = Corpus(
        document_ids=first.document_ids + second.document_ids,
        token_rows=first.token_rows + second.token_rows,
        root_id=open_root.id,
        seconds=first.seconds + second.seconds,
    )
    return Fixture(
        corpus=corpus,
        reader=CurrentPrincipal(id=reader.id, name=reader.name, is_owner=False),
        owner=CurrentPrincipal(id=owner.id, name=owner.name, is_owner=True),
    )


def exact_maxsim(repo, query: str, limit: int) -> list[int]:
    """Ground truth: the same `maxsim_search`, on the same rows, with no index to use.

    Every index path is denied for the duration, so the planner sorts, and a sort is exact
    by construction. Computed through the shipped method rather than beside it: the query
    embedding, the token filter, the read gate and the per-token aggregation are then
    identical in both arms, and the only difference left is the scan.
    """
    gucs = ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan")
    for guc in gucs:
        repo.session.execute(sa_text(f"SET LOCAL {guc} = off"))
    try:
        return [r.document.id for r in repo.maxsim_search(query, limit=limit)]
    finally:
        for guc in gucs:
            repo.session.execute(sa_text(f"SET LOCAL {guc} = on"))


def ann_maxsim(repo, query: str, limit: int, ef_search: int | None = None) -> list[int]:
    """`maxsim_search` as the appliance runs it, optionally under an explicit `ef_search`.

    `ef_search=None` is the shipped configuration — nothing in `jmfts_core/` issues this
    SET, so pgvector's default of `EF_SEARCH` stands. Passing a number is the measurement
    arm and is NOT what the appliance does.

    This does not touch `hnsw.iterative_scan`, and must not: `maxsim_search` sets that
    itself (`repositories/search.py`, migration 022), so overriding it here would measure a
    configuration no caller can get.
    """
    if ef_search is not None:
        repo.session.execute(sa_text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    return [r.document.id for r in repo.maxsim_search(query, limit=limit)]


def recall(got: list[int], truth: list[int]) -> float:
    """Fraction of the exact top-N a caller actually received. 1.0 when there is no truth."""
    if not truth:
        return 1.0
    return len(set(got) & set(truth)) / len(set(truth))


def ann_plan_reaches_the_index(session, corpus: Corpus) -> bool:
    """Does an ANN scan shaped like `maxsim_search`'s actually read `TOKEN_INDEX`?

    A recall number collected from a sequential scan measures nothing at all — it is the
    `plan` column `scripts/maxsim_recall.py` prints and tells the reader to check first —
    so every assertion below this file is conditioned on it.
    """
    zero = "[" + ",".join(["0.01"] * DIM) + "]"
    plan = session.execute(
        sa_text(
            f"EXPLAIN (FORMAT JSON) SELECT te.document_id FROM token_embeddings te "
            f"JOIN documents d ON te.document_id = d.id "
            f"WHERE te.embed_256 IS NOT NULL AND d.settled = 'settled' "
            f"ORDER BY te.embed_256 <=> CAST('{zero}' AS vector) LIMIT {K_PER_TOKEN}"
        )
    ).scalar()
    return TOKEN_INDEX in str(plan)
