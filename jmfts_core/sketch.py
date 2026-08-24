"""Bounded set sketches. The ``sketch`` extra, and the only door to it.

``INGEST_SPEC.md`` 8.3 gives ``profile:sheet`` a measurement table, and 8.6 asks a
workbook-level task to find columns whose value sets are equal or nested. 8.5 is what makes
that hard: it stores ``values`` only for a column whose distinct set is small enough to
retain, so a **high-cardinality column has a count and no values** — and a set comparison
over stored values cannot see it at all. A MinHash sketch is bounded whatever the
cardinality is, so it makes those columns comparable rather than invisible.

That is why this is a dependency and not an optimisation. The cheaper thing — compare the
value sets we already store — is not a slower version of this; it is a version that cannot
answer the question for the columns most worth asking about.

**The packaging shape is the ``office`` extra's, copied deliberately.**
:class:`SketchStackNotInstalled` subclasses ``ImportError``, so
:mod:`jmfts_core.task_errors` classifies it PERMANENT; the import happens inside
:func:`require_datasketch`, at the point of use, never at module scope; and
``tests/test_office_packaging.py`` fails if it reaches the application's import path. The
claim is the same one: **an install that cannot sketch a column is a supported deployment**
— it can still probe, extract, chunk, embed and search — so the failure has to say which
deployment you are in.

Nothing here computes a similarity. Comparing two sketches is ``propose:links``' business
(8.6), and it is a later step; what this module does is produce the sketch and write it
down in a form that survives a round trip through JSONB.
"""

from __future__ import annotations

from typing import Iterable, Optional

#: How many permutations a column sketch uses. datasketch's own default, kept rather than
#: tuned because it is the number every published error figure for MinHash is quoted
#: against: 128 permutations give a standard error of about 1/sqrt(128) ≈ 0.088 on the
#: Jaccard estimate. It is a SIZE, not a threshold — it decides how much space a sketch
#: costs (128 unsigned integers per column) and how precisely it can be compared, and
#: nothing about it decides whether two columns are related.
#:
#: Two sketches built with different values CANNOT be compared, which is why the number
#: travels with the sketch (:meth:`SketchBuilder.finish`) instead of being read from
#: configuration at comparison time.
MINHASH_PERMUTATIONS = 128

#: The permutation seed. datasketch's default. Recorded beside the permutation count for
#: the same reason: it is half of what makes two sketches comparable, and a sketch that did
#: not say which seed produced it would be silently incomparable with the next one.
MINHASH_SEED = 1

#: The permutation SCHEME, when the installed datasketch has one. Discovered the hard way:
#: datasketch 2.0.0 refuses to rebuild a MinHash from stored hash values unless the caller
#: names the scheme, and its reason is the same one this module already gives for carrying
#: ``num_perm`` and ``seed`` — *"hash values carry no trace of the scheme that produced
#: them (legacy values fit the affine32 range), so a default here would silently mislabel
#: pre-2.0.0 state"*. Under datasketch 1.x the same rebuild succeeded and could compare a
#: sketch with one built from a different permutation family, reporting a number. So the
#: scheme is recorded beside the other two, read off the object that produced it rather
#: than hard-coded, and ``None`` on an install whose datasketch predates the concept.
#:
#: This is exactly why 8.6 needs the whole recipe stored with the sketch: the point of a
#: sketch is to compare a workbook profiled today against one profiled months ago, and
#: months is long enough for a library to change how it permutes.
SKETCH_SCHEME_UNKNOWN = None

#: The name written into a stored sketch's ``kind``. A stored sketch is data with a long
#: life — the point of 8.6 is to compare a workbook profiled today against one profiled
#: months ago — so it says what algorithm produced it rather than leaving a reader to infer
#: it from the shape of the array.
SKETCH_MINHASH = "minhash"


class SketchStackNotInstalled(ImportError):
    """This install can profile a sheet but not sketch its columns, and something asked.

    Raised instead of letting a bare ``ModuleNotFoundError: No module named 'datasketch'``
    reach the caller. That message describes a broken environment, and this one usually is
    not: every measurement ``INGEST_SPEC.md`` 8.3 names except the per-column sketch is
    computed from the standard library and the office readers, so an appliance that never
    runs ``propose:links`` is correctly installed and correctly has no ``datasketch``.

    Classified PERMANENT by :mod:`jmfts_core.task_errors`, along with every other
    ``ImportError``: a package that is not installed does not appear on the third attempt.
    """


#: What to do about it. One string, for the reason ``jmfts_core.office._INSTALL_HINT`` is
#: one string: the message is the whole value of the exception, and two copies of it would
#: be free to drift.
#:
#: It names the task parameter as well as the extra, because there are genuinely two ways
#: out and only one of them is an install. A `profile:sheet` run with
#: ``sketch_columns: false`` produces every measurement 8.3 names; what it does not produce
#: is the thing that makes a high-cardinality column findable by 8.6.
_INSTALL_HINT = (
    "This JMFTS was installed without the sketch stack, so it can measure a worksheet but "
    "cannot sketch its columns for the containment search of INGEST_SPEC.md 8.6. Install "
    "it:\n"
    "    pip install 'jmfts[sketch]'\n"
    "Or run profile:sheet with the `sketch_columns` parameter set to false, which measures "
    "the sheet and leaves its high-cardinality columns undiscoverable by propose:links. "
    "See jmfts_core/sketch.py."
)


def require_datasketch():
    """The ``datasketch`` module, or say what is missing and what to do."""
    try:
        import datasketch
    except ImportError as exc:
        raise SketchStackNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return datasketch


class SketchBuilder:
    """A MinHash fed one value at a time, and written out as JSON.

    Incremental rather than a function over a collection, because the caller
    (:func:`jmfts_core.office.sheets.measure_sheet`) is a single streaming pass over a
    sheet that may hold a million rows. A function taking an iterable would either force
    that pass to materialise the column or force a second pass over the part.

    ``values`` are the canonical strings :func:`jmfts_core.office.sheets.canonical`
    produces, and they are fed to the sketch as UTF-8. Canonicalisation is the caller's job
    and it is the whole correctness of the comparison: two columns holding the same
    identifiers agree only if ``128000`` and ``128000.0`` reached the sketch as one string.
    """

    def __init__(self, *, num_perm: int = MINHASH_PERMUTATIONS, seed: int = MINHASH_SEED):
        datasketch = require_datasketch()
        self.num_perm = num_perm
        self.seed = seed
        self.count = 0
        self._minhash = datasketch.MinHash(num_perm=num_perm, seed=seed)

    def update(self, value: str) -> None:
        self._minhash.update(value.encode("utf-8"))
        self.count += 1

    def finish(self, *, partial: bool = False) -> dict:
        """The stored form: the hashes, and everything needed to compare them later.

        ``num_perm`` and ``seed`` sit beside the hash values because a MinHash is only
        comparable with another built from the same permutations. Reading them off
        configuration at comparison time would compare a sketch made under one setting with
        one made under another and report a number, which is the class of quiet wrong
        answer the project's Fail Early rule exists to stop.

        ``count`` is how many distinct values went in. LSH Ensemble needs it — a
        containment estimate is a Jaccard estimate scaled by the ratio of set sizes — and
        the sketch itself cannot supply it.

        ``partial`` says the caller stopped feeding values before the column ran out
        (``DISTINCT_TRACKED_MAX``). The sketch is then a sketch of a PREFIX, and a
        containment estimate computed from it is about that prefix. Recording it is what
        stops 8.6 treating the two kinds of sketch as one.
        """
        return {
            "kind": SKETCH_MINHASH,
            "num_perm": self.num_perm,
            "seed": self.seed,
            "count": self.count,
            # `getattr`, not an attribute access: the concept arrived in datasketch 2.0.0
            # and an older install has no `scheme`. `None` then travels with the sketch and
            # `load_sketch` passes nothing, which is the 1.x call that worked.
            "scheme": getattr(self._minhash, "scheme", SKETCH_SCHEME_UNKNOWN),
            "partial": partial,
            # `int()` per element: datasketch returns a numpy array of unsigned integers,
            # and psycopg2 has no adapter for numpy scalars — a JSONB write of the raw
            # array fails at the driver rather than at the point the value was produced.
            "hashvalues": [int(value) for value in self._minhash.hashvalues],
        }


def column_sketch(
    values: Iterable[str],
    *,
    num_perm: int = MINHASH_PERMUTATIONS,
    seed: int = MINHASH_SEED,
) -> dict:
    """:class:`SketchBuilder` over a collection already in hand, for callers that have one."""
    builder = SketchBuilder(num_perm=num_perm, seed=seed)
    for value in values:
        builder.update(value)
    return builder.finish()


def load_sketch(stored: dict) -> Optional[object]:
    """Rebuild a ``datasketch.MinHash`` from what :meth:`SketchBuilder.finish` wrote.

    ``None`` for a stored sketch this appliance does not know how to read — a ``kind`` from
    a later version — because the alternative is constructing a MinHash out of an array that
    means something else and comparing it with a straight face.
    """
    if stored.get("kind") != SKETCH_MINHASH:
        return None
    datasketch = require_datasketch()
    import numpy

    scheme = stored.get("scheme")
    return datasketch.MinHash(
        num_perm=stored["num_perm"],
        seed=stored["seed"],
        hashvalues=numpy.array(stored["hashvalues"], dtype=numpy.uint64),
        # Only when the sketch recorded one. Passing `scheme=None` to a 2.0.0 MinHash is
        # what raises, and passing the keyword at all to a 1.x one is a TypeError; the
        # stored value is what says which world the sketch came from.
        **({"scheme": scheme} if scheme is not None else {}),
    )


__all__ = [
    "MINHASH_PERMUTATIONS",
    "SKETCH_SCHEME_UNKNOWN",
    "MINHASH_SEED",
    "SKETCH_MINHASH",
    "SketchBuilder",
    "SketchStackNotInstalled",
    "column_sketch",
    "load_sketch",
    "require_datasketch",
]
