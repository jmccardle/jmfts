"""Turtle in, Turtle out, and shapes. The ``rdf`` extra, and the only door to it.

``docs/SPRINT_0_3_0.md`` Part 5 is the design and Part 9 is the packaging decision. This
package is to ``rdflib``/``pyshacl`` exactly what :mod:`jmfts_core.office` is to
``python-docx``: the boundary, and the only place either name may be imported — through a
``require_*`` function below, at the point of use, never at module scope.

**The claim being made is the same one the office seam makes.** An install that cannot
read or write Turtle is a supported deployment, not a broken one. The triple store works
without it: facts are asserted, queried, superseded and invalidated through
``TripleRepository`` with no RDF library anywhere near them, because the store is an RDF
store with integers for names and always was. What the extra buys is the ability to say
those integers out loud in somebody else's vocabulary, and to be told a vocabulary in
return. A storage-side worker does neither.

So the failure has to say which deployment you are in rather than read as a missing
dependency — which is what :class:`RdfStackNotInstalled` is for.

**On the two libraries being separate functions.** ``pyshacl`` depends on ``rdflib``, so
in practice one extra installs both, and a single guard would be simpler. They are split
because they answer different questions: ``require_rdflib()`` is "can this process parse
or serialise RDF at all", and ``require_pyshacl()`` is "can it validate data against a
shape". The second is a strictly later capability — no validator ships in 0.3.0 (5.1: no
reasoner, and validation waits for the shapes to have data to run against) — and a guard
that names the wrong library in its traceback is a guard that costs a reader ten minutes.

``tests/test_rdf_packaging.py`` asserts that nothing on the app's import path reaches for
either library, and ``scripts/check_base_install.sh`` asserts it against a real base venv.
"""

from __future__ import annotations


class RdfStackNotInstalled(ImportError):
    """This install holds triples but cannot speak Turtle, and something asked it to.

    Raised instead of letting a bare ``ModuleNotFoundError: No module named 'rdflib'``
    reach the caller. That message describes a broken environment, and this one usually is
    not: the triple store, the graph measures and fact extraction all work without an RDF
    library, so a worker that never imports or exports a vocabulary is correctly installed
    and correctly has no ``rdflib``.

    Classified PERMANENT by :mod:`jmfts_core.task_errors`, along with every other
    ``ImportError`` (``task_errors.py``): a package that is not installed does not appear
    on the third attempt, and spending the retry budget on it only delays the moment
    somebody reads this.
    """


#: What to do about it. One string, because the message is the whole value of the exception
#: and two copies of it would be free to drift.
#:
#: It names the extra and nothing else — there is no remote equivalent of
#: ``JMFTS_RUNNER_URL`` here. Parsing Turtle is not work you can send somewhere else the
#: way embedding is; it is a library, and the answer is to install it.
_INSTALL_HINT = (
    "This JMFTS was installed without the RDF stack, so it can hold triples but cannot "
    "read or write Turtle. Install it:\n"
    "    pip install 'jmfts[rdf]'\n"
    "A worker that neither imports a vocabulary nor exports one does not need it; see "
    "jmfts_core/rdf/__init__.py."
)


def require_rdflib():
    """The ``rdflib`` module, or say what is missing and what to do."""
    try:
        import rdflib
    except ImportError as exc:
        raise RdfStackNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return rdflib


def require_pyshacl():
    """The ``pyshacl`` module, or say what is missing and what to do.

    Nothing calls this yet. 0.3.0 stores shapes and binds them to scopes; running a shape
    against data is the step after, and it lands here rather than anywhere else because
    this is the only door. Declared now so that the extra which carries ``pyshacl`` is the
    extra whose absence has a name — the alternative is a validator landing later and
    quietly adding a top-level import.
    """
    try:
        import pyshacl
    except ImportError as exc:
        raise RdfStackNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return pyshacl


__all__ = [
    "RdfStackNotInstalled",
    "require_pyshacl",
    "require_rdflib",
]
