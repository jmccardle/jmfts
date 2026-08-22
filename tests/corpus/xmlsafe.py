"""Parsing XML that came from somewhere else.

``docs/OFFICE_SPEC.md`` Part 10: *"XML parsing at ingest uses ``defusedxml``, or ``lxml``
with ``resolve_entities=False`` and ``huge_tree=False``, from the first commit rather than
after the first finding."*

**Neither library is installed in this environment**, and this harness does not get to
install one, so the rule is met the third way: ``xml.parsers.expat`` driven directly, with
every declaration handler that could pull in outside content bound to a refusal. That is
not a weaker version of ``defusedxml`` — it is what ``defusedxml`` does — but it is more
code, and the moment either library is a dependency this module should become three lines
that call it.

The reason the handlers are set explicitly rather than left at their defaults is measured,
not assumed. ``xml.etree.ElementTree.fromstring`` **expands internal general entities**: a
three-level billion-laughs document parses to its full expansion without complaint, and
this repository's fixture generator produces exactly that file. External entities happen to
raise (expat has no external-entity handler unless one is installed), so the dangerous half
is the half that looks safe.

``ElementTree``'s C accelerator does not expose its expat parser (``XMLParser.parser`` was
removed), which is why the tree is built from ``expat`` plus ``TreeBuilder`` here rather
than by hardening ``XMLParser`` in place.

Four refusals and one limit:

* a ``DOCTYPE`` at all — an OOXML part never legitimately carries one, so the declaration
  is the finding and there is no need to reason about what is inside it;
* an entity declaration, an unparsed entity, a notation — reached only if a future caller
  relaxes the ``DOCTYPE`` rule, and left in place so relaxing it is not silently unsafe;
* an external entity reference;
* element nesting past ``MAX_DEPTH``, which is the recursion limit an office reader hits as
  a segfault rather than an exception.

Every one of them raises :class:`UnsafeXML`. A malformed document raises expat's own
``ExpatError``. Both are loud, and neither is caught here — a corpus harness that swallowed
a parse failure would be reporting coverage it does not have.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from xml.parsers import expat

#: How deep an element tree may be before it is called an attack rather than a document.
#: Word's own nesting (tables in cells in tables) reaches the low tens; 100 is comfortably
#: above anything an author produces and far below anything that threatens a C parser.
MAX_DEPTH = 100


class UnsafeXML(Exception):
    """The document declared something a corpus file is not allowed to declare."""


def _refuse(reason: str):
    """A handler that names what it refused. Bound to expat, so it takes anything."""

    def handler(*_args, **_kwargs):
        raise UnsafeXML(reason)

    return handler


def parse(data: bytes, *, max_depth: int = MAX_DEPTH) -> ElementTree.Element:
    """The root element of ``data``, or an exception. Never a partial tree.

    Raises :class:`UnsafeXML` for a declaration or a depth this harness refuses, and
    ``xml.parsers.expat.ExpatError`` for a document that is not well formed — which
    includes one whose bytes are not valid UTF-8, because expat decodes as it goes.
    """
    builder = ElementTree.TreeBuilder()
    # Namespace processing on, with expat's separator chosen so a name comes back as
    # ``uri}tag`` and needs only a leading brace to be the Clark notation ElementTree uses.
    # An OOXML part is nothing but namespaced elements, and a caller that had to match
    # ``w:document`` would be matching the PREFIX — which the author picks and can change
    # without changing the document's meaning.
    parser = expat.ParserCreate(namespace_separator="}")

    parser.StartDoctypeDeclHandler = _refuse("the document declares a DOCTYPE")
    parser.EntityDeclHandler = _refuse("the document declares an entity")
    parser.UnparsedEntityDeclHandler = _refuse("the document declares an unparsed entity")
    parser.NotationDeclHandler = _refuse("the document declares a notation")
    parser.ExternalEntityRefHandler = _refuse("the document references an external entity")

    depth = 0

    def clark(name: str) -> str:
        return "{" + name if "}" in name else name

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > max_depth:
            raise UnsafeXML(f"element nesting deeper than {max_depth} at <{name}>")
        builder.start(clark(name), {clark(key): value for key, value in attributes.items()})

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(clark(name))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data

    parser.Parse(data, True)
    return builder.close()
