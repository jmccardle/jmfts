"""MetaService — what this appliance accepts, asked without sending it anything.

The gap this fills is stated in ``jmfts_client.contracts.meta``: ``GET /config`` returns
six tuning numbers, ``GET /ingest/pipelines`` returns entry points that are deliberately
not formats, and ``POST /ingest/explain`` needs the bytes first. Nothing answered "are the
office readers installed", "can this process produce a vector", or "will MaxSim rank
anything on this corpus".

It is an ``@expose``d service rather than a hand-written route beside ``/config`` for one
reason: an integrator should reach it through the generated ``RemoteJmftsClient`` like
every other operation. ``/config`` is infra and is not on that client, which is exactly why
it was not the place to add this.

**Every fact is read from a live registry or from settings.** The installed-extras answer
comes from :func:`importlib.util.find_spec`, the ingest entry points from
``INGEST_USETYPES``, the task types from ``TASK_HANDLERS``, the formats from ``probe``'s
own tables, and the retrieval vocabulary from the contracts both halves validate against.
There is no second list here to keep in step.
"""

from __future__ import annotations

from importlib.util import find_spec

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jmfts_client.contracts.meta import (
    CapabilitiesResponse,
    CorpusFacts,
    EmbeddingCapability,
    ExtraStatus,
    IngestCapability,
    LlmCapability,
    RetrievalCapability,
)
from jmfts_client.contracts.search import DEFAULT_HYBRID_METHODS, SEARCH_METHODS
from jmfts_core import __version__
from jmfts_core.config import get_settings
from jmfts_core.ingest_options import INGEST_USETYPES
from jmfts_core.ingest_tasks import TASK_HANDLERS
from jmfts_core.models.document import Document, SETTLED_SETTLED
from jmfts_core.models.search_index import SearchIndex
from jmfts_core.models.token_embedding import TokenEmbedding
from jmfts_core.probe import PROBERS_AVAILABLE, detectable_formats
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.search import TUNED_HYBRID_WEIGHTS

#: The document vector's width, from ``Document.embed``'s column type. Read off the mapper
#: rather than repeated, so a migration that changes the column changes this answer.
_DOC_DIMS = Document.__table__.c.embed.type.dim

#: Each optional dependency group: the extra's name, one import that proves it resolves,
#: what it buys, and how to install it. The import name is what is CHECKED — an extra is
#: installed if its imports resolve in this process, which is the only question a caller
#: can act on.
#:
#: ``convert`` names the client half only. Tier 3 is LibreOffice in a badged worker image
#: (``OFFICE_SPEC.md`` Part 1), so a true answer here means this process can talk to one,
#: not that one exists.
_EXTRAS: tuple[tuple[str, str, str, str], ...] = (
    (
        "embed",
        "sentence_transformers",
        "produce embeddings in this process (torch + sentence-transformers)",
        "pip install 'jmfts[embed]'",
    ),
    (
        "office",
        "docx",
        "open .docx/.pptx/.xlsx and extract their content; probing them works without it",
        "pip install 'jmfts[office]'",
    ),
    (
        "rdf",
        "rdflib",
        "Turtle in, Turtle out, SHACL shapes",
        "pip install 'jmfts[rdf]'",
    ),
    (
        "sketch",
        "datasketch",
        "MinHash per column, for propose:links",
        "pip install 'jmfts[sketch]'",
    ),
    (
        "convert",
        "unoserver",
        "drive a LibreOffice worker; the worker itself is a separate image",
        "pip install 'jmfts[convert]'",
    ),
)

#: What stops working with ``JMFTS_LLM_BASE_URL`` blank. Named rather than counted, because
#: "4 features" tells a caller nothing about whether their ingest will work.
_REQUIRES_LLM: tuple[str, ...] = (
    "summarize",
    "raptor",
    "extract:facts",
    "search/synthesize",
)


def _installed(import_name: str) -> bool:
    """Whether ``import_name`` would resolve here, WITHOUT importing it.

    ``find_spec`` rather than a ``try: import`` because this endpoint must not pull torch
    into a process that has so far avoided loading it. It does import the package's
    PARENT for a dotted name; every name in :data:`_EXTRAS` is top-level, so it does not.
    """
    try:
        return find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


@register_service
class MetaService:
    """Appliance metadata: capability discovery, and the health and config endpoints.

    The first line of this docstring is the `meta` tag's description in the OpenAPI
    document (``build_openapi_tags``), and that group also holds the hand-written
    ``/``, ``/health``, ``/health/llm`` and ``/config`` routes — which is why it names
    them rather than just this service.
    """

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/capabilities",
        response_model=CapabilitiesResponse,
        tags=["meta"],
        summary="What this appliance accepts and can do",
    )
    def capabilities(self, *, corpus: bool = False) -> CapabilitiesResponse:
        """What this appliance accepts and can do.

        ``corpus=true`` adds four counts over ``documents``, ``token_embeddings`` and
        ``search_indexes``. They are OPT-IN because each is an aggregate over a table that
        grows without bound, and a capability listing that becomes a table scan is one
        nobody puts in a startup check.

        Makes no network call. ``llm.configured`` reports that a URL is set, not that
        anything answers at it; ``GET /health/llm`` is the probe.
        """
        settings = get_settings()
        extras = [
            ExtraStatus(
                name=name,
                installed=_installed(import_name),
                provides=provides,
                install=install,
            )
            for name, import_name, provides, install in _EXTRAS
        ]
        local_model = _installed("sentence_transformers") and _installed("torch")

        return CapabilitiesResponse(
            version=__version__,
            extras=extras,
            embedding=EmbeddingCapability(
                model=settings.embedding_model,
                device=settings.embedding_device,
                document_dims=_DOC_DIMS,
                # The schema carries 384/512 columns too; `token_embed_dims` is the list
                # actually written, and only 256 is active.
                token_dims=settings.token_embed_dims[0],
                local_model_available=local_model,
                runner_url=settings.runner_url or None,
                can_embed=local_model or bool(settings.runner_url),
            ),
            retrieval=RetrievalCapability(
                methods=list(SEARCH_METHODS),
                default_methods=list(DEFAULT_HYBRID_METHODS),
                default_weights=dict(TUNED_HYBRID_WEIGHTS),
                search_exclude_usetypes=list(settings.search_exclude_usetypes),
                bm25_exclude_usetypes=list(settings.bm25_exclude_usetypes),
            ),
            ingest=IngestCapability(
                usetypes=list(INGEST_USETYPES),
                detectable_formats=list(detectable_formats()),
                probeable_formats=list(PROBERS_AVAILABLE),
                task_types=sorted(TASK_HANDLERS),
            ),
            llm=LlmCapability(
                configured=settings.llm_configured,
                base_url=settings.effective_llm_url,
                model=settings.effective_llm_model,
                requires_llm=list(_REQUIRES_LLM),
            ),
            corpus=self._corpus_facts() if corpus else None,
        )

    def _corpus_facts(self) -> CorpusFacts:
        """The four counts, each scoped the way the matching search path is scoped.

        ``documents_with_vectors`` carries the ``settled`` predicate because retrieval
        does: an in-flight node holds a vector and is invisible to search, so counting it
        would answer a question nobody asked.
        """
        session = self.session
        total = session.execute(select(func.count()).select_from(Document)).scalar_one()
        with_vectors = session.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.embed.isnot(None))
            .where(Document.settled == SETTLED_SETTLED)
        ).scalar_one()
        with_tokens = session.execute(
            select(func.count(func.distinct(TokenEmbedding.document_id)))
        ).scalar_one()
        indexes = (
            session.execute(select(SearchIndex.name).order_by(SearchIndex.name)).scalars().all()
        )
        return CorpusFacts(
            documents=total,
            documents_with_vectors=with_vectors,
            documents_with_token_vectors=with_tokens,
            bm25_indexes=list(indexes),
        )
