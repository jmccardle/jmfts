"""The seal on the presets ``schema.sql`` seeds into ``search_contexts``.

**It seeds none today**, and that is asserted here rather than assumed. This file is
therefore two things at once: the record that the table is deliberately empty, and a gate
armed for the next attempt to fill it.

``ROADMAP.md`` known gap 5. A named search-context preset is DATA and nothing validates
it: ``SearchContextRepository.resolve_params`` reads ``config`` out of JSONB and hands it
to the search methods, so a preset whose ``usetype`` names something this appliance never
writes is accepted at every layer and produces an empty page with no error to say why. The
2026-09-05 attempt at this gap stopped for exactly that reason and the gap entry records
it; this file is what makes the next attempt fail loudly instead.

**A gate over an empty table passes vacuously, and two tests below say so in their own
names.** What keeps this file from being worthless in the meantime is
:class:`TestTheGateWouldCatchAnUnseedablePreset`, which puts a preset through the same
check on a row it creates itself — so the gate is exercised on every run whether or not
anything is seeded.

**The producible set is READ OUT OF THE SOURCE, not copied from it.**
:func:`producible_usetypes` walks ``jmfts_core`` with ``ast`` and collects every string a
``usetype=`` keyword argument is given and every module constant whose name contains
``USETYPE``. A checked-in list would be a copy of the thing under test, and a copy can only
confirm what it was copied from — the same objection ``tests/test_usetype_filter.py``
records against the inline helpers it replaced.

The scan is deliberately WIDE: it also picks up usetypes that are only read (``repo.find(
usetype="entity")``), which is harmless because it can only make the gate more permissive.
What tightens it is :data:`NOT_A_DOCUMENT_USETYPE`, and every entry there is a fact stated
in the source rather than a judgement made here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jmfts_client.contracts.search import usetype_globs
from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import _apply_usetype_filter, _usetype_matches
from jmfts_core.repositories.search_context import SearchContextRepository

#: Two of the three presets ``docs/archive/ROADMAP_HISTORY.md:697`` specified, with the
#: globs they were specified with. Asserted below to match NOTHING this appliance can
#: produce, so this is the executable form of gap 5's finding rather than a sentence
#: somebody has to trust. The third is not named here: it is expressible and withheld for
#: a reason no assertion can carry, and ``jmfts_core/sql/schema.sql`` states that reason
#: where the absent row would have been.
UNSEEDABLE_SPEC = {
    "personal-notes": ["transcript:*", "obsidian:*"],
    "hardware": ["kicad:*", "freecad:*"],
}

#: Strings the scan finds that are NOT values of ``documents.usetype``. Each is excluded on
#: the strength of what its own source says, and excluding it TIGHTENS this gate — leaving
#: one in would let a preset name it and pass.
NOT_A_DOCUMENT_USETYPE = {
    # `repositories/usetype_presentation.py:15` — the catch-all KEY of the
    # `usetype_presentations` lookup, which is a rendering rule and not a node.
    "*": "usetype_presentation._DEFAULT_FALLBACK_USETYPE: a presentation lookup key",
    # `services/conversation_service.py:50` calls it "the entry point a transcript is
    # ingested under", i.e. a key of INGEST_USETYPES. `conversation_tasks.py:9` — "Why a
    # transcript stopped being a usetype" — is the other half: the root node of an ingested
    # conversation is `file` (`services/ingest_service.py:249`) and its turns are `chunk`.
    "conversation": "conversation_service.USETYPE_CONVERSATION: an ingest entry point",
}

_JMFTS_CORE = Path(__file__).resolve().parent.parent / "jmfts_core"


def _scan_usetype_literals() -> dict[str, list[str]]:
    """Every ``usetype`` string literal in ``jmfts_core``, mapped to where it is written.

    Two shapes, and the reason it is ``ast`` rather than ``grep``: the string
    ``usetype="conversation"`` appears in ``conversation_tasks.py``'s module docstring, in a
    paragraph explaining that it is no longer written, and a textual scan would read that
    prose as a declaration.

    * a ``usetype=`` keyword argument whose value is a literal — ``repo.create(
      usetype="summary")``;
    * a module-level assignment whose target name contains ``USETYPE`` and whose value is a
      literal — the ``USETYPE_*`` block on the model, ``ENTITY_USETYPE``,
      ``TEMPLATE_USETYPE``, ``DERIVED_ROOT_USETYPE``.

    A ``usetype=`` argument given a NAME rather than a literal (``usetype=USETYPE_SECTION``)
    is not collected here and does not need to be: the constant it names is.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(_JMFTS_CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg != "usetype":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.setdefault(value.value, []).append(f"{path.name}:{value.lineno}")
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if not isinstance(node.value.value, str):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and "USETYPE" in target.id:
                        found.setdefault(node.value.value, []).append(f"{path.name}:{node.lineno}")
    return found


def producible_usetypes() -> dict[str, list[str]]:
    """What ``jmfts_core`` can write into ``documents.usetype``, and where each is written.

    Raises rather than returning a short set if the scan comes back implausible. An empty
    or tiny result would silently pass every preset in this file — the gate would report
    success while measuring nothing, which is the one failure mode a gate cannot have.
    """
    found = {
        usetype: sites
        for usetype, sites in _scan_usetype_literals().items()
        if usetype not in NOT_A_DOCUMENT_USETYPE
    }
    # The seven `USETYPE_*` constants on `models/document.py` alone. If the scan cannot see
    # those it is not seeing the tree, and every assertion below is vacuous.
    for anchor in ("file", "section", "chunk", "segment", "sheet", "summary", "record"):
        if anchor not in found:
            raise AssertionError(
                f"the usetype scan did not find {anchor!r}, which "
                f"jmfts_core/models/document.py declares as a USETYPE_* constant; the scan "
                f"is broken and every assertion resting on it would pass vacuously. "
                f"Found: {sorted(found)}"
            )
    return found


def _unmatched(globs: tuple[str, ...], producible: dict[str, list[str]]) -> list[str]:
    """The globs in ``globs`` that no producible usetype satisfies."""
    return [g for g in globs if not any(_usetype_matches((g,), u) for u in producible)]


def _seeded_presets(db_session):
    """Every row ``schema.sql`` seeded into ``search_contexts``, read back from the DB.

    Read from the database rather than parsed out of the file, because what has to be true
    is that the seed LANDED — a statement that never ran, or ran into a column that does not
    take it, is exactly the failure a file-level assertion cannot see.
    """
    return SearchContextRepository(db_session).list_all()


class TestProducibleUsetypes:
    """The scan the rest of this file rests on."""

    def test_an_ingest_entry_point_is_not_a_producible_usetype(self):
        """`conversation` names an entry point, and a preset naming it would match nothing.

        The exclusion is what makes this gate able to fail. Without it the scan would offer
        `conversation` as producible and a preset filtering on it would pass here and
        return an empty page in production, which is gap 5's exact defect.
        """
        assert "conversation" not in producible_usetypes()
        assert "conversation" in _scan_usetype_literals()


class TestSeededPresets:
    """The gate: every seeded preset names usetypes this appliance can produce."""

    def test_schema_seeds_no_preset_at_all(self, db_session):
        """The table is deliberately empty, and this is the only place that says so.

        An absent row leaves no trace, so a later reader cannot tell "decided against"
        from "nobody got to it". ``jmfts_core/sql/schema.sql`` carries the reasoning where
        the row would have been; this asserts the state that reasoning describes, and
        fails the day somebody seeds one without reading it.
        """
        names = sorted(ctx.name for ctx in _seeded_presets(db_session))
        assert names == [], (
            f"schema.sql now seeds {names}. Seeding a preset is a decision, not a "
            f"convenience: see the comment above search_contexts in schema.sql and "
            f"ROADMAP.md known gap 5, then update this test with the reasoning."
        )

    def test_every_seeded_preset_resolves_to_a_non_empty_usetype_set(self, db_session):
        """No seeded preset carries a filter that normalises to nothing.

        ``usetype_globs`` raises on ``""``, ``","`` and ``[]``, so this also proves the
        stored blob is a filter the search path will accept rather than 400 on.

        VACUOUS while nothing is seeded, deliberately. The same check is exercised on a
        real row by :class:`TestTheGateWouldCatchAnUnseedablePreset`.
        """
        presets = _seeded_presets(db_session)
        for ctx in presets:
            raw = ctx.config.get("usetype")
            assert raw is not None, f"preset {ctx.name!r} carries no usetype filter"
            globs = usetype_globs(raw)
            assert globs, f"preset {ctx.name!r} normalises to no glob at all"

    def test_every_seeded_preset_names_a_usetype_this_appliance_produces(self, db_session):
        """The assertion the next person seeding a preset has to get past.

        Not "it returns results" — a fresh database holds no documents. What is asserted is
        that each glob COULD match: some usetype `jmfts_core` writes satisfies it. A glob
        that matches nothing producible can only ever return an empty page.

        VACUOUS while nothing is seeded, deliberately; see the class below.
        """
        producible = producible_usetypes()
        for ctx in _seeded_presets(db_session):
            globs = usetype_globs(ctx.config.get("usetype"))
            assert globs is not None
            unmatched = _unmatched(globs, producible)
            assert not unmatched, (
                f"search context {ctx.name!r} names usetype glob(s) {unmatched} that no "
                f"usetype jmfts_core writes can satisfy, so it returns an empty page on "
                f"every install. Producible today: {sorted(producible)}. See ROADMAP.md "
                f"known gap 5."
            )

    def test_the_two_unseeded_presets_could_not_have_been_expressed(self):
        """Gap 5's finding, executable.

        Every glob `personal-notes` and `hardware` were specified with matches nothing this
        appliance produces. If that stops being true — a handler starts writing
        `obsidian:*`, say — this test fails and the preset can be seeded.
        """
        producible = producible_usetypes()
        for name, globs in UNSEEDABLE_SPEC.items():
            unmatched = _unmatched(tuple(globs), producible)
            assert unmatched == globs, (
                f"{name!r} was specified as {globs} and at least one of those now matches "
                f"a usetype jmfts_core writes; seed it and update ROADMAP.md known gap 5"
            )


class TestTheGateWouldCatchAnUnseedablePreset:
    """The two assertions above run over an empty table. These run them over a row.

    Without this class the gate could rot unnoticed — every one of its assertions would
    keep passing on a table with nothing in it, including a gate that had stopped being
    able to fail at all. Each test here creates the row it checks, so the machinery is
    exercised on every run and the day something IS seeded the check is known to work.
    """

    def test_the_gate_flags_a_preset_naming_a_usetype_nothing_writes(self, db_session):
        """`personal-notes` as specified, written into the table and put through the gate.

        Not a seeded row: created here, checked, and rolled back with the fixture. What is
        asserted is that the gate reports it, which is what `test_every_seeded_preset_
        names_a_usetype_this_appliance_produces` would have done had it been in schema.sql.
        """
        SearchContextRepository(db_session).create(
            name="personal-notes",
            config={"usetype": UNSEEDABLE_SPEC["personal-notes"]},
        )
        ctx = SearchContextRepository(db_session).get_by_name("personal-notes")
        globs = usetype_globs(ctx.config["usetype"])
        assert _unmatched(globs, producible_usetypes()) == list(globs)

    def test_the_gate_passes_a_preset_whose_globs_are_producible(self, db_session):
        """The other direction, so the gate is not one that refuses everything.

        `se*` is a glob over usetypes `models/document.py` declares as `USETYPE_*`
        constants, chosen because `producible_usetypes` already refuses to run unless it
        can see those seven.
        """
        SearchContextRepository(db_session).create(
            name="a-producible-preset",
            config={"usetype": ["se*"]},
        )
        ctx = SearchContextRepository(db_session).get_by_name("a-producible-preset")
        globs = usetype_globs(ctx.config["usetype"])
        assert _unmatched(globs, producible_usetypes()) == []


class TestTheStoredBlobReachesSQL:
    """The blob passes through no contract, so this drives it into a real query."""

    def test_resolve_params_carries_a_stored_filter_into_the_query(self, db_session):
        """JSONB -> `resolve_params` -> `_apply_usetype_filter` -> SQL, end to end.

        The one path a preset takes in production, with nothing stubbed. `se*` selects
        `section` and `segment` and must not reach `chunk` or `sheet`; the last of those
        is the case a `LIKE` built from the wrong glob translation would get wrong.
        """
        SearchContextRepository(db_session).create(
            name="a-producible-preset",
            config={"usetype": ["se*"]},
        )
        repo = DocumentRepository(db_session)
        section = repo.create(title="A section", usetype="section", auto_embed=False)
        segment = repo.create(title="A segment", usetype="segment", auto_embed=False)
        chunk = repo.create(title="A chunk", usetype="chunk", auto_embed=False)
        sheet = repo.create(title="A sheet", usetype="sheet", auto_embed=False)
        db_session.flush()

        params = SearchContextRepository(db_session).resolve_params("a-producible-preset", {})
        query = _apply_usetype_filter(db_session.query(Document), params["usetype"])
        selected = {doc.id for doc in query.all()}

        assert {section.id, segment.id} <= selected
        assert chunk.id not in selected
        assert sheet.id not in selected

    def test_an_unseedable_glob_would_have_returned_the_empty_page(self, db_session):
        """What seeding `personal-notes` as specified would have shipped.

        The red baseline for this lane, kept in the suite: the filter is well-formed, the
        query runs, and the page is empty — no error, nothing to debug from.
        """
        repo = DocumentRepository(db_session)
        repo.create(title="A conversation turn", usetype="chunk", auto_embed=False)
        repo.create(title="A markdown section", usetype="section", auto_embed=False)
        db_session.flush()

        query = _apply_usetype_filter(db_session.query(Document), UNSEEDABLE_SPEC["personal-notes"])
        assert query.all() == []


def test_an_empty_usetype_in_a_preset_is_refused_rather_than_widened():
    """A seeded preset cannot silently become "no filter".

    ``resolve_params`` would hand ``""`` straight to the search path, where it used to be
    falsy and take the same branch ``None`` takes — an unfiltered page from a request that
    named a filter. That is the hazard a seeded preset makes permanent, so it is asserted
    here next to the seed rather than only in the filter's own tests.
    """
    with pytest.raises(ValueError):
        usetype_globs("")
    with pytest.raises(ValueError):
        usetype_globs([])
