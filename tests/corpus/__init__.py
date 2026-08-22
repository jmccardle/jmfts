"""The fidelity corpus harness — ``docs/OFFICE_SPEC.md`` Part 10.

Four modules, and the split between them is the point:

``vocabulary``
    One table of pattern names, each with a status saying who knows it today.
    ``jmfts_core.probe`` is imported and interrogated, never restated.

``manifest``
    The per-file record — bytes, provenance, feature tags, expected behaviour — and
    a loader that refuses a tag the vocabulary does not carry.

``fixtures``
    The generator. Every corpus file this repository ships is built here, in code,
    from ``zipfile`` and string literals, so no binary is committed.

``roundtrip``
    Tiers 1 and 2 of the fidelity table: what changed between two packages, at the
    granularity that tells a swapped part from a re-authored document.

Nothing here needs a database, a model, or an optional dependency.
"""
