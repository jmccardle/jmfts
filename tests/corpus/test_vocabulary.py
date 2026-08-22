"""The claim that makes the corpus a measurement rather than a second opinion.

``docs/OFFICE_SPEC.md`` Part 10: *"The pattern vocabulary of Part 2 **is** the corpus's tag
vocabulary, and extending one extends the other. This means coverage is measurable with
shipped code, and a corpus file whose features no test exercises is a query, not an audit."*

That is an architectural claim, and it is the kind that decays silently. Nothing stops
somebody adding a pattern to ``probe`` and not to the corpus, or tagging a corpus file with
a word that sounds like a pattern and is not. Both leave every test passing and the number
that says "coverage" wrong. So the claim is tested here, in both directions, and it is
tested against the installed ``jmfts_core.probe`` rather than against a description of it.

Three things are held:

**Probe cannot grow a pattern in secret.** :func:`~tests.corpus.vocabulary.probe_vocabulary`
runs probe and reads the keys back; every key must be a declared term. Add a pattern to
``_probe_pdf`` and this file fails until ``vocabulary.py`` carries it.

**The spec cannot grow a pattern in secret.** ``OFFICE_SPEC.md`` Part 2's own tables are
parsed, and every name in them must be a term. Add a row to Part 2 and this file fails.

**A planned term cannot quietly become real.** A term declared ``PLANNED`` or ``PROPOSED``
that probe has started emitting must be promoted, because the status is what the coverage
report counts and a stale status makes the gap look bigger than it is.

The last of those is the one that will fire first: ``OFFICE_SPEC.md`` Part 11 step 4 is the
office prober, another lane is building it, and the day it lands ``has_slides`` moves from
planned to probed. That failure is the mechanism working.

**Where the claim does not survive contact with the code**, and this file is where to read
it: probe's vocabulary is a mix of flags and measurements, and only the flags can be tags —
``tags = ["page_count"]`` is not a feature vector. And Part 2's vocabulary describes what a
document *declares*, while half of Part 10's fixture generator produces container-level
attacks (zip-slip, duplicate members, a truncated archive) that Part 2 names no pattern
for. Those terms are carried as ``PROPOSED``, they are counted separately, and
``docs/CORPUS.md`` writes out the Part 2 rows that would close the gap.
"""

from __future__ import annotations

import pytest

from tests.corpus.vocabulary import (
    Kind,
    Status,
    VocabularyError,
    _PROBED_FORMATS,
    coverage,
    probe_vocabulary,
    spec_part,
    spec_patterns,
    tags,
    vocabulary,
)


def test_probe_emits_no_pattern_the_corpus_cannot_name():
    """The direction that matters: a new pattern in probe fails this suite.

    If it did not, a pattern could ship, the corpus would never tag anything with it, and
    the coverage report would keep saying 100% of a vocabulary that had grown.
    """
    live = probe_vocabulary()
    assert live, "probe emitted no patterns at all; the specimens are not reaching a prober"
    table = vocabulary()
    unknown = sorted(set(live) - set(table))
    assert not unknown, (
        f"probe emits {unknown} and tests/corpus/vocabulary.py does not carry them. Add a "
        "term, and a corpus fixture that has the feature."
    )
    for name, kind in live.items():
        assert table[name].kind is kind, f"{name} is a {kind.value} at runtime"
        assert table[name].status is Status.PROBED, (
            f"{name} is emitted by probe now and is still declared "
            f"{table[name].status.value}; promote it"
        )


def test_no_probed_term_describes_a_pattern_probe_stopped_emitting():
    """The other half: a pattern REMOVED from probe must not linger in the table."""
    live = probe_vocabulary()
    stale = sorted(set(_PROBED_FORMATS) - set(live))
    assert not stale, (
        f"_PROBED_FORMATS still claims {stale}; probe no longer emits them, so any corpus "
        "record tagged with one is claiming a feature nothing can measure"
    )


def test_every_pattern_the_spec_names_is_in_the_vocabulary():
    """``OFFICE_SPEC.md`` Part 2's tables, parsed, against the table."""
    named = spec_patterns()
    table = vocabulary()
    missing = sorted(named - set(table))
    assert not missing, (
        f"OFFICE_SPEC.md Part 2 names {missing} and the corpus vocabulary does not carry "
        "them. Part 10 says extending one extends the other."
    )


def test_every_planned_term_is_named_by_the_spec():
    """A planned term with no spec row is an invention, which is the drift itself."""
    named = spec_patterns()
    invented = sorted(
        name
        for name, term in vocabulary().items()
        if term.status is Status.PLANNED and name not in named
    )
    assert not invented, (
        f"{invented} are declared PLANNED and OFFICE_SPEC.md Part 2 does not name them. "
        "Either add the Part 2 row or mark them PROPOSED, which is what 'we want this and "
        "no spec section has agreed to it' means here."
    )


def test_proposed_terms_are_the_gap_and_are_named_as_such():
    """PROPOSED means: this corpus can label the file, and no shipped code can measure it.

    The assertion is that the status is honest in both directions — nothing PROPOSED is
    already probed, and nothing PROPOSED has quietly appeared in Part 2 without being
    promoted to PLANNED.
    """
    live = probe_vocabulary()
    named = spec_patterns()
    for name, term in vocabulary().items():
        if term.status is not Status.PROPOSED:
            continue
        assert name not in live, f"{name} is probed now; promote it out of PROPOSED"
        assert (
            name not in named
        ), f"OFFICE_SPEC.md Part 2 now names {name}; move it from PROPOSED to PLANNED"


def test_only_flags_can_be_tags():
    """``page_count`` is a measurement. A corpus tagged with a number means nothing."""
    flags = tags()
    table = vocabulary()
    assert set(flags) < set(table), "every term is a flag; the split is not being made"
    for name, term in flags.items():
        assert term.kind is Kind.FLAG, name
    measurements = sorted(n for n, t in table.items() if t.kind is Kind.MEASUREMENT)
    assert "page_count" in measurements and "char_count" in measurements, measurements


def test_a_specimen_exists_for_every_prober():
    """The ratchet on the vocabulary extractor itself.

    A prober with no specimen makes the reported vocabulary quietly incomplete, which is
    the same failure this whole module is about, one level down.

    This test is where that fails, and it is the ONLY place it fails. ``specimens()``
    used to raise instead, which fired during collection — three test modules build the
    vocabulary at module scope — so one missing specimen interrupted the whole run.
    Merging the office probers into this harness did exactly that: four new formats, and
    ~1500 unrelated tests stopped being collected.
    """
    from tests.corpus.vocabulary import uncovered_formats

    missing = uncovered_formats()
    assert not missing, (
        f"probe can look inside {missing} and tests/corpus/vocabulary.py has no specimen "
        "for it, so the vocabulary this harness reports is incomplete. Add one input per "
        "format to specimens()."
    )


def test_the_spec_parser_reads_the_pattern_column_and_not_the_page():
    """Part 2 is parsed by header, so a restructured table fails loudly rather than empty.

    Held against a table with the columns the other way round, because Part 2 really does
    have both shapes and a parser that assumed a position would read format names as
    pattern names.
    """
    text = "## Part 2 — x\n\n| Pattern | Formats |\n|---|---|\n| `a`, `b` | `docx` |\n\n## Part 3 — y\n"
    assert spec_patterns(text) == {"a", "b"}
    assert "x" in spec_part(2, text)

    with pytest.raises(VocabularyError):
        spec_patterns("## Part 2 — x\n\nprose with no table\n\n## Part 3 — y\n")


def test_the_coverage_report_counts_the_gap():
    """The number ``scripts/corpus_fixtures.py`` prints, asserted to be a real split."""
    grouped = coverage()
    assert grouped[Status.PROBED], "no probed tags: probe is not being read"
    assert grouped[Status.PLANNED], "no planned tags: OFFICE_SPEC Part 2 is not being read"
    assert grouped[Status.PROPOSED], (
        "no proposed tags. If Part 2 has grown rows for the container-level hazards, this "
        "is good news and the terms should have moved to PLANNED — see docs/CORPUS.md."
    )
