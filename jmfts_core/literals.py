"""Validation of a literal triple object before it is stored.

``triples.object_datatype`` is a plain string column with no CHECK on its form, and
``object_literal`` is unbounded TEXT, so the schema will store a "not a number" typed
``xsd:integer``. This module is the write-side gate for the REST surface: a literal that is
not a valid value of its declared datatype is REFUSED, never coerced or stored as a string.

What is checked, and what deliberately is not:

* ``object_datatype`` must be an absolute IRI (:func:`~jmfts_core.rdf.names.iri_problem`)
  that fits ``triples.object_datatype``. A CURIE such as ``xsd:integer`` is refused for the
  reason ``iri_problem`` gives: written out as ``<xsd:integer>`` it is a different datatype.
* A datatype in the XSD namespace must be one of :data:`XSD_LEXICAL`, and the literal must
  be a valid lexical form of it. An XSD name this table does not know is refused — it is
  unknown to this appliance, and storing it would defer the failure to whoever serialises
  it.
* A datatype outside the XSD namespace is a vocabulary's own; its lexical space cannot be
  known here, so it is accepted as an opaque absolute IRI.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from jmfts_core.models.triple import Triple
from jmfts_core.rdf.names import iri_problem

XSD = "http://www.w3.org/2001/XMLSchema#"
DATATYPE_WIDTH: int = Triple.object_datatype.type.length


class InvalidLiteralError(ValueError):
    """The datatype is unknown or malformed, or the literal is not a value of it (→ 422)."""


_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_FLOAT = re.compile(r"([+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?|[+-]?INF|NaN)\Z")
_TZ = r"(Z|[+-](0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)?"
_DATE = re.compile(r"-?[0-9]{4,}-[0-9]{2}-[0-9]{2}" + _TZ + r"\Z")
_DATETIME = re.compile(
    r"-?[0-9]{4,}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?" + _TZ + r"\Z"
)


def _bounded(lo: int, hi: int) -> Callable[[str], bool]:
    return lambda v: bool(_INTEGER.match(v)) and lo <= int(v) <= hi


def _decimal(v: str) -> bool:
    if not re.fullmatch(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)", v):
        return False
    try:
        Decimal(v)
    except InvalidOperation:
        return False
    return True


def _ymd_ok(v: str) -> bool:
    m = re.match(r"(-?[0-9]{4,})-([0-9]{2})-([0-9]{2})", v)
    year = int(m.group(1))
    try:
        date(max(1, min(abs(year), 9999)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return True


def _is_date(v: str) -> bool:
    return bool(_DATE.match(v)) and _ymd_ok(v)


def _is_datetime(v: str) -> bool:
    if not (_DATETIME.match(v) and _ymd_ok(v)):
        return False
    t = v[v.index("T") + 1 :]
    h, mi, sec = int(t[0:2]), int(t[3:5]), int(t[6:8])
    return (h < 24 and mi < 60 and sec < 60) or (h == 24 and mi == 0 and sec == 0)


#: local name in the XSD namespace -> "is this string a valid lexical form".
XSD_LEXICAL: dict[str, Callable[[str], bool]] = {
    "string": lambda v: True,
    "boolean": lambda v: v in ("true", "false", "1", "0"),
    "integer": lambda v: bool(_INTEGER.match(v)),
    "nonNegativeInteger": lambda v: bool(_INTEGER.match(v)) and int(v) >= 0,
    "positiveInteger": lambda v: bool(_INTEGER.match(v)) and int(v) > 0,
    "long": _bounded(-(2**63), 2**63 - 1),
    "int": _bounded(-(2**31), 2**31 - 1),
    "short": _bounded(-(2**15), 2**15 - 1),
    "decimal": _decimal,
    "double": lambda v: bool(_FLOAT.match(v)),
    "float": lambda v: bool(_FLOAT.match(v)),
    "date": _is_date,
    "dateTime": _is_datetime,
    "anyURI": lambda v: bool(v) and not any(c.isspace() for c in v),
}


def check_literal(object_literal: str, object_datatype: Optional[str]) -> None:
    """Raise :class:`InvalidLiteralError` unless the literal can be stored under its datatype.

    ``object_datatype=None`` is ``xsd:string`` and accepts any text.
    """
    if object_datatype is None:
        return
    problem = iri_problem(object_datatype)
    if problem is not None:
        raise InvalidLiteralError(
            f"object_datatype {object_datatype!r} is not a usable datatype IRI: {problem}."
        )
    if len(object_datatype) > DATATYPE_WIDTH:
        raise InvalidLiteralError(
            f"object_datatype is {len(object_datatype)} characters; "
            f"triples.object_datatype holds {DATATYPE_WIDTH}."
        )
    if not object_datatype.startswith(XSD):
        return
    local = object_datatype[len(XSD) :]
    valid = XSD_LEXICAL.get(local)
    if valid is None:
        raise InvalidLiteralError(
            f"object_datatype {object_datatype!r} is in the XSD namespace but is not one of "
            f"the datatypes this appliance knows: {', '.join(sorted(XSD_LEXICAL))}."
        )
    if not valid(object_literal):
        raise InvalidLiteralError(
            f"object_literal {object_literal!r} is not a valid xsd:{local} value."
        )
