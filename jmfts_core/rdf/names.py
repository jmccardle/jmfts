"""Naming: what a JMFTS integer is called out loud, and how a vocabulary term reads back.

``docs/SPRINT_0_3_0.md`` Part 5. This module is string work and nothing else — it imports
neither ``rdflib`` nor ``pyshacl``, so it lives on the base install's import path and both
sides of the seam can use it. Serialising needs to MINT an IRI for a document node and for
a local predicate; parsing needs to read a LOCAL NAME back out of somebody else's IRI.
Those are the same vocabulary question asked in two directions, so they live together.

**Why a ``urn:`` and not an ``http://``.** Part 12 item 5 records "IRI minting policy" as
an open question, and it is: what a locally-created entity should be called, and whether
that name survives a re-ingest, are decisions this sprint does not get to make. What it
CAN refuse to do is guess an ``http://`` namespace. An HTTP IRI is a claim that something
answers at that address, and nothing does — a JMFTS appliance has no public namespace
until an operator gives it one. ``urn:jmfts:`` claims nothing: it is syntactically an IRI,
it is stable inside one database, and it is visibly not dereferenceable. Pass ``base_iri``
to say otherwise when the appliance has a real namespace to mint under.

``jmfts`` is not an IANA-registered URN namespace identifier, and this does not pretend it
is. Registering one is a decision to take when these IRIs start leaving the appliance for
somewhere that has to resolve them.

**Why TWO namespaces and not one.** A serialiser can only write ``doc:42`` if the
namespace bound to ``doc:`` ends exactly where the local name begins. Measured against
rdflib 7.6: binding ``jmfts: <urn:jmfts:>`` and emitting ``urn:jmfts:document:42`` produces
``<urn:jmfts:document:42>`` on every line — rdflib splits an IRI at its own trailing-name
boundary and will not use a bound prefix that stops short of it. Binding
``doc: <urn:jmfts:document:>`` produces ``doc:42``. That is the difference between twenty
readable lines and twenty unreadable ones, which is the entire argument for doing Turtle
out early (5.3), so the namespaces are split at the separator rather than the prefix being
one name for everything.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

#: What a locally-minted IRI hangs off when the caller does not say. See the module
#: docstring for why this is a URN.
DEFAULT_BASE_IRI = "urn:jmfts:"

#: The two namespaces under a base IRI, and the prefixes bound to them in output. Split at
#: the separator for the reason the module docstring measures.
DOCUMENT_SEGMENT = "document:"
PREDICATE_SEGMENT = "predicate:"
DOCUMENT_PREFIX = "doc"
PREDICATE_PREFIX = "prop"

#: Namespaces bound on every graph this package writes, so the output reads in the names
#: an RDF reader already knows rather than in rdflib's invented ``ns1:``. ``sh:`` is here
#: because the SHACL subset (5.1) is the vocabulary shapes are written in.
STANDARD_PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "sh": "http://www.w3.org/ns/shacl#",
}

#: ``predicates.name`` is free text (a plain ``String(200)``) and neither a URN nor
#: Turtle's ``PN_LOCAL`` admits a space, so the minted IRI percent-encodes everything
#: outside the unreserved set rather than trusting that nobody ever names a predicate
#: ``works at``. ``%20`` is itself writable in a prefixed name — Turtle's ``PLX``
#: production is exactly the percent escape — so encoding does not cost the readability
#: the two namespaces above bought.
_SAFE_IN_LOCAL_NAME = ""


def document_namespace(base_iri: str = DEFAULT_BASE_IRI) -> str:
    """The namespace document IRIs hang off, ending at the separator."""
    return f"{base_iri}{DOCUMENT_SEGMENT}"


def predicate_namespace(base_iri: str = DEFAULT_BASE_IRI) -> str:
    """The namespace locally-minted predicate IRIs hang off, ending at the separator."""
    return f"{base_iri}{PREDICATE_SEGMENT}"


def document_iri(document_id: int, base_iri: str = DEFAULT_BASE_IRI) -> str:
    """The IRI for a document node — an entity, a section, a file, any node in the tree."""
    return f"{document_namespace(base_iri)}{document_id}"


def predicate_iri(name: str, base_iri: str = DEFAULT_BASE_IRI) -> str:
    """The IRI for a predicate that no published vocabulary names.

    Only for a predicate whose ``iri`` column is NULL. A predicate that carries an IRI is
    called by it — that column exists precisely so an imported term keeps the name its
    vocabulary gave it, and re-minting one here under the local base would make the same
    property two different properties depending on which end of the wire read it.
    """
    return f"{predicate_namespace(base_iri)}{quote(name, safe=_SAFE_IN_LOCAL_NAME)}"


def local_name(iri: str) -> str:
    """The last segment of an IRI — what a predicate row should be NAMED after import.

    ``http://xmlns.com/foaf/0.1/knows`` → ``knows``;
    ``http://schema.org/Person#name`` → ``name``.

    Splits on the LAST ``#`` or ``/``, whichever is further right, which is the convention
    every published vocabulary follows. Returns the whole IRI when it holds neither, rather
    than an empty string: a term named after nothing is a predicate row nobody can find by
    name, and ``predicates.name`` is UNIQUE, so the second such term would collide with the
    first.

    This is a NAME, not an identity. Two vocabularies both defining ``name`` produce one
    string here and two distinct IRIs, and it is the IRI that decides whether they are the
    same predicate — see ``OntologyService`` for how that collision is reported rather than
    resolved.
    """
    cut = max(iri.rfind("#"), iri.rfind("/"))
    if cut == -1 or cut == len(iri) - 1:
        return iri
    return iri[cut + 1 :]


def iri_problem(value: str) -> Optional[str]:
    """Why this string cannot be written between ``<`` and ``>`` and still mean itself.

    Returns ``None`` when it can. Every caller raises on a reason and none of them repairs
    anything — repairing would mean guessing which document's prefix map was in the
    author's head.

    It exists because ``triples.object_datatype`` and ``predicates.iri`` are plain string
    columns with no CHECK on their form. Two things can be in one that must not be written
    out verbatim:

    * **A CURIE.** ``xsd:integer`` written between angle brackets becomes
      ``"128000"^^<xsd:integer>`` — a datatype no other RDF tool has heard of, silently
      different from the one that was meant. A CURIE and an absolute IRI cannot be told
      apart by a parser and this does not claim to: ``xsd:integer`` is syntactically a URI
      whose scheme is ``xsd`` and nothing forbids that. What separates them is SHAPE. An
      absolute IRI is hierarchical (``scheme://…``) or has a structured multi-part tail
      (``urn:isbn:0451450523``, ``tag:example.com,2020:thing``); a CURIE is one prefix and
      one local name and nothing else.
    * **A relative reference.** ``integer`` written as ``<integer>`` resolves against
      whatever base the READER happens to have, which is a different IRI for every reader.
    """
    if not value or any(c.isspace() for c in value):
        return "it is empty or contains whitespace"
    scheme, sep, rest = value.partition(":")
    if not sep or not scheme or not rest:
        return (
            "it has no scheme, so it is a RELATIVE reference — it would resolve against "
            "whatever base the reader happens to have"
        )
    if not scheme[0].isalpha() or not all(c.isalnum() or c in "+-." for c in scheme):
        return f"{scheme!r} is not a usable URI scheme"
    if not rest.startswith("//") and not any(c in rest for c in ":/#?"):
        return (
            "it is shaped like a CURIE (one prefix, one local name), which only means "
            "something inside the document that declared the prefix"
        )
    return None
