"""Generate the browser client in ``jmfts-web/jmfts_web/static/client/`` from the OpenAPI
document.

``docs/SPRINT_0_6_0.md`` Block F step 23, IC-6. The sibling of
``scripts/generate_client.py``, which renders ``jmfts-client/jmfts_client/_verbs.py`` from
the route table, and of ``scripts/generate_reference.py``, whose ``--check`` mode and
``generate() -> {filename: text}`` shape this copies verbatim so the three behave the same
way::

    python -m scripts.generate_ts_client            # rewrite the files
    python -m scripts.generate_ts_client --check    # exit 1 if any is stale

``tests/test_ts_client_codegen.py`` runs the check, so a route cannot land without the
front end gaining it.

**A branch that adds a route now regenerates TWO clients, not one.** ``_verbs.py`` and this
directory are both checked-in output of the same surface, so ``docs/SPRINT_0_6_0.md`` 5.3's
rule for ``_verbs.py`` — regenerated, never edited, by one worktree at a time — covers these
four files too. Two worktrees regenerating in parallel is a guaranteed conflict in a
generated file, and ``verbs.d.ts`` is the largest one in the tree.

Why the OPENAPI DOCUMENT and not the route table
------------------------------------------------
``generate_client.py`` reads the route table because the Python client needs resolved
parameter binding that ``ExposeSpec`` does not record. This generator needs the same
binding, and the document records it — ``parameters[].in`` and ``requestBody.content`` are
exactly that information, published. Reading the document instead has three consequences
worth having:

* ``jmfts_core/rest/main.py`` corrects the generated ``security`` block to match the gate
  that actually runs, so the document knows which of the two credentials each operation
  takes and this client does too. The route table does not carry that correction.
* The document covers the whole mounted surface — 115 operations, of which 108 are
  ``@expose``'d and seven (``/health``, ``/health/llm``, ``/``, ``/config`` and the three
  ``/runner`` routes) are hand-written FastAPI routes with no ``ExposeSpec`` and therefore
  no ``_verbs.py`` method. A front end that could not call ``/health`` because it generated
  from ``REGISTRY`` would be a front end that cannot draw its own connection state.
* :func:`generate` is a pure function of a document, so a route shape this tree does not
  have yet can be tested against a synthetic one. That is how the binary path below is
  covered before ``wt/bytes`` merges.

The three facts OpenAPI cannot carry
------------------------------------
:func:`document` stamps each operation with ``x-jmfts-route`` before handing the document
to :func:`generate`, and :func:`generate` REQUIRES it. It holds what the route table knows
and the document format has no way to say:

``name``
    The operation's identity — ``SearchService.hybrid_search``, which is ``ExposeSpec.name``
    and ``route.name``. OpenAPI has ``operationId``, but FastAPI's default mangles the name
    together with the path and the method (``SearchService_hybrid_search_search_hybrid_post``),
    and recovering the two halves means inverting a formula rather than reading a field.
    This is the ``op_id`` of the IC-6 call event.
``path_template``
    The route's own path, with Starlette's converters intact. ``compile_path`` strips them,
    so the document says ``/usetype-presentations/{usetype}`` where the route is
    ``{usetype:path}`` — and a client that percent-encodes the slash in a value for that
    parameter sends a request the server will not match. Three routes are affected today.
``python_verb``
    The ``RemoteJmftsClient`` method name, or ``null`` for the seven routes that have none.
    Copy-out prints a Python call and must not invent a method that is not there.

A generator that guessed any of the three would be a generator whose output is wrong in a
way nothing fails on, which is the drift this whole arrangement exists to prevent.

What lands in the browser, and why it is ``.js`` plus ``.d.ts``
--------------------------------------------------------------
There is no build step in this tree and step 23 does not add one — step 24 does
(``docs/SPRINT_0_6_0.md`` Block F phase F2), and taking its scope here would leave the
front end unable to load until that step ran. So the generated client is an ES module the
browser executes directly, with a generated ``.d.ts`` beside it carrying the TypeScript
types for all 150 component schemas and every operation signature. TypeScript consumers get
full checking through the declarations; the page gets working code today; and when step 24
introduces a compiler, the declarations are already what its views type-check against.
Nothing about that decision has to be revisited then.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
TARGET_DIR = REPO / "jmfts-web" / "jmfts_web" / "static" / "client"

#: The extension :func:`document` stamps and :func:`generate` requires. See the module
#: docstring for why each of its three fields exists.
ROUTE_EXTENSION = "x-jmfts-route"

#: The files this generator owns. Everything else in ``TARGET_DIR`` is the hand-written
#: runtime, listed in ``tests/test_ts_client_codegen.py::RUNTIME_FILES`` — that test refuses
#: a file that is in neither set, because the directory ships wholesale inside the wheel.
GENERATED_FILES = ("operations.js", "operations.d.ts", "verbs.js", "verbs.d.ts")

BANNER_JS = "// GENERATED BY scripts/generate_ts_client.py — DO NOT EDIT."


def _self_types(module: str) -> str:
    """The banner plus the pragma that points a type checker at this module's declarations.

    The bundle is loose ES modules with no ``package.json``, so nothing tells a tool that
    ``transport.js`` is typed by ``transport.d.ts``. TypeScript infers it from the filename;
    Deno does not, and reads this pragma instead. It is a comment in a browser, and it is
    what lets ``tests/test_ts_client_codegen.py`` type-check the 3900 generated declaration
    lines that nothing else looks at.
    """
    return f'{BANNER_JS}\n// @ts-self-types="./{module}.d.ts"'


_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

#: A response declared under a non-JSON media type with this schema is BYTES. It is the
#: literal shape ``jmfts_core/rest/wiring.py::_BINARY_SCHEMA`` writes for an ``@expose``
#: carrying a ``media_type``, and that constant's own comment names this generator as the
#: reason it exists: without it the document would describe an empty body for a route that
#: sends a PNG, and this file would render a JSON-parsing call for it.
BINARY_SCHEMA = {"type": "string", "format": "binary"}

_TS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: JSON Schema keywords that say what a value IS. Everything else in a node — ``title``,
#: ``description``, ``minimum``, ``format`` and the rest — annotates it without narrowing
#: the TypeScript type, so a node carrying none of these constrains nothing.
_TYPE_KEYS = frozenset(
    {
        "$ref",
        "const",
        "enum",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "type",
        "properties",
        "items",
        "additionalProperties",
    }
)


class GenerationError(RuntimeError):
    """A document this generator cannot represent honestly. Never worked around."""


# --------------------------------------------------------------------------- the document


def document() -> dict:
    """The live OpenAPI document, stamped with what the route table knows and it does not.

    Importing the app is what makes this the LIVE surface rather than a snapshot: the route
    table is built at import, ``app.openapi()`` runs the security correction in
    ``rest/main.py``, and the stamp below joins each operation back to the route FastAPI
    built for it on ``(path, method)``.

    **Deep-copied before anything is written into it.** ``app.openapi()`` memoises into
    ``app.openapi_schema`` and hands back that same dict, so stamping it in place would put
    ``x-jmfts-route`` into the document this process then SERVES at ``/openapi.json`` — a
    generator changing the appliance's published contract as a side effect of running.
    Whether the served document should carry the stamp is a real question and a separate one;
    it is not something a build tool gets to decide by accident.
    """
    from jmfts_core.registry import REGISTRY
    from jmfts_core.rest.main import app
    from jmfts_core.rest.wiring import iter_mounted_api_routes

    verbs = {spec.name: spec.func.__name__ for spec in REGISTRY}
    by_wire: dict[tuple[str, str], Any] = {}
    for route in iter_mounted_api_routes(app):
        if len(route.methods) != 1:
            raise GenerationError(
                f"{route.name} is mounted for {sorted(route.methods)}; one operation is one "
                "method, and a multi-method route has no single identity to stamp."
            )
        (method,) = route.methods
        by_wire[(route.path_format, method.upper())] = route

    schema = copy.deepcopy(app.openapi())
    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            route = by_wire.get((path, method.upper()))
            if route is None:
                raise GenerationError(
                    f"the document declares {method.upper()} {path} and no mounted route "
                    "answers it; the join this stamp rests on is not total."
                )
            bodies = [f.name for f in route.dependant.body_params]
            if len(bodies) > 1 and not _is_multipart(operation):
                raise GenerationError(
                    f"{route.name} binds {bodies} into one request body; one request has one "
                    "body, and the client cannot name two."
                )
            operation[ROUTE_EXTENSION] = {
                "name": route.name,
                "path_template": route.path,
                "body_param": bodies[0] if len(bodies) == 1 else None,
                "python_verb": verbs.get(route.name),
            }
    return schema


def _is_multipart(operation: dict) -> bool:
    content = (operation.get("requestBody") or {}).get("content") or {}
    return "multipart/form-data" in content


# --------------------------------------------------------------------------- reading it


@dataclass(frozen=True)
class Operation:
    """One mounted operation, read out of the document. The unit this generator emits."""

    op_id: str
    operation_id: str
    python_verb: Optional[str]
    method: str
    path: str
    path_params: tuple[str, ...]
    #: Path parameters declared with Starlette's ``:path`` converter, whose values keep
    #: their slashes. Recovered from ``path_template``; see the module docstring.
    path_slash_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body_param: Optional[str]
    body_media_type: Optional[str]
    form_params: tuple[str, ...]
    file_params: tuple[str, ...]
    binary: bool
    response_media_type: Optional[str]
    success_status: int
    security_scheme: Optional[str]
    tags: tuple[str, ...]
    summary: str

    @property
    def member(self) -> str:
        """The client method name. Identical to ``python_verb`` where there is one."""
        return self.op_id.split(".")[-1]

    @property
    def arg_names(self) -> tuple[str, ...]:
        return (
            self.path_params
            + self.query_params
            + tuple(n for n in (self.body_param,) if n)
            + self.form_params
            + self.file_params
        )


def _operations(doc: dict) -> list[Operation]:
    found: list[Operation] = []
    for path, item in doc.get("paths", {}).items():
        for method, operation in item.items():
            if method.lower() in _HTTP_METHODS:
                found.append(_operation(path, method.upper(), operation, doc))
    found.sort(key=lambda op: op.op_id)

    members = [op.member for op in found]
    clashing = sorted({name for name in members if members.count(name) > 1})
    if clashing:
        raise GenerationError(
            f"two operations want the client method name(s) {clashing}. The client is one "
            "flat namespace, as ``_verbs.py`` is; the generator must not emit the second "
            "over the first."
        )
    return found


def _operation(path: str, method: str, operation: dict, root: dict) -> Operation:
    stamp = operation.get(ROUTE_EXTENSION)
    if not isinstance(stamp, dict) or "name" not in stamp or "path_template" not in stamp:
        raise GenerationError(
            f"{method} {path} carries no usable {ROUTE_EXTENSION!r}. This generator reads the "
            "document, but three facts about a route are not expressible in one — see the "
            "module docstring. scripts.generate_ts_client.document() stamps them."
        )

    parameters = operation.get("parameters", [])
    for parameter in parameters:
        if parameter["in"] not in ("path", "query"):
            raise GenerationError(
                f"{method} {path} declares a {parameter['in']!r} parameter "
                f"({parameter['name']!r}); this client sends path and query parameters only."
            )
    path_params = tuple(p["name"] for p in parameters if p["in"] == "path")
    query_params = tuple(p["name"] for p in parameters if p["in"] == "query")

    declared = set(re.findall(r"\{([A-Za-z0-9_]+)\}", path))
    if declared != set(path_params):
        raise GenerationError(
            f"{method} {path} has placeholders {sorted(declared)} and path parameters "
            f"{sorted(path_params)}; the two must agree or the URL cannot be built."
        )
    slash_params = tuple(re.findall(r"\{([A-Za-z0-9_]+):path\}", stamp["path_template"]))

    body_param, body_media_type, form_params, file_params = _body(
        path, method, operation, stamp, root
    )
    binary, response_media_type, success_status = _response(path, method, operation)
    schemes = root.get("components", {}).get("securitySchemes", {})

    return Operation(
        op_id=stamp["name"],
        operation_id=operation["operationId"],
        python_verb=stamp.get("python_verb"),
        method=method,
        path=path,
        path_params=path_params,
        path_slash_params=slash_params,
        query_params=query_params,
        body_param=body_param,
        body_media_type=body_media_type,
        form_params=form_params,
        file_params=file_params,
        binary=binary,
        response_media_type=response_media_type,
        success_status=success_status,
        security_scheme=_security_scheme(path, method, operation, schemes),
        tags=tuple(operation.get("tags", [])),
        summary=(operation.get("summary") or "").strip(),
    )


def _body(
    path: str, method: str, operation: dict, stamp: dict, root: dict
) -> tuple[Optional[str], Optional[str], tuple[str, ...], tuple[str, ...]]:
    """``(body_param, media_type, form_params, file_params)`` for one operation."""
    content = (operation.get("requestBody") or {}).get("content") or {}
    if not content:
        return None, None, (), ()
    if len(content) > 1:
        raise GenerationError(
            f"{method} {path} declares {sorted(content)} for its request body; one request "
            "is sent under one media type and the client cannot choose."
        )
    ((media_type, media),) = content.items()

    if media_type == "multipart/form-data":
        # The parts are the properties of the generated ``Body_*`` schema, and a part whose
        # schema says ``contentMediaType: application/octet-stream`` is the file. That is
        # what FastAPI writes for an ``UploadFile``; everything else beside it is a form
        # field carrying JSON text, which is what ``jmfts_client.transport`` sends too.
        schema = media.get("schema") or {}
        properties = _resolve(schema, root).get("properties") or {}
        files = tuple(n for n, s in properties.items() if _is_file_part(s))
        fields = tuple(n for n in properties if n not in files)
        if not files:
            raise GenerationError(
                f"{method} {path} is multipart and declares no file part; the client has no "
                "reason to build a multipart body for it."
            )
        return None, media_type, fields, files

    name = stamp.get("body_param")
    if not name:
        raise GenerationError(
            f"{method} {path} has a {media_type} request body and its {ROUTE_EXTENSION!r} "
            "names no parameter for it. The document cannot name a body, so the stamp must."
        )
    return name, media_type, (), ()


def _is_file_part(schema: dict) -> bool:
    if schema.get("contentMediaType") == "application/octet-stream":
        return True
    return schema.get("type") == "string" and schema.get("format") == "binary"


def _response(path: str, method: str, operation: dict) -> tuple[bool, Optional[str], int]:
    """``(binary, media_type, status)`` for the success response."""
    responses = operation.get("responses") or {}
    success = sorted(code for code in responses if code.startswith("2"))
    if len(success) != 1:
        raise GenerationError(
            f"{method} {path} declares success responses {success}; the client returns one "
            "shape per operation and cannot branch on which arrived."
        )
    status = int(success[0])
    content = responses[success[0]].get("content") or {}
    if not content:
        return False, None, status
    if len(content) > 1:
        raise GenerationError(
            f"{method} {path} declares {sorted(content)} for its {status} response; this "
            "client sends no Accept negotiation and so cannot pick one."
        )
    ((media_type, media),) = content.items()
    schema = media.get("schema") or {}
    if media_type == "application/json":
        return False, media_type, status
    if schema == BINARY_SCHEMA:
        return True, media_type, status
    raise GenerationError(
        f"{method} {path} answers {status} with {media_type} and schema {schema!r}. A "
        f"non-JSON response is bytes and must declare {BINARY_SCHEMA!r}; anything else is a "
        "shape this client would have to guess how to read."
    )


#: Security scheme names this client knows how to satisfy, checked against the document's
#: own ``securitySchemes`` rather than assumed. Both are ``Authorization: Bearer`` and they
#: are deliberately disjoint credentials — ``jmfts_core/rest/auth.py``, "Two credentials".
_BEARER = {"type": "http", "scheme": "bearer"}


def _security_scheme(path: str, method: str, operation: dict, schemes: dict) -> Optional[str]:
    security = operation.get("security")
    if security is None:
        raise GenerationError(
            f"{method} {path} declares no security block. ``rest/main.py`` writes one for "
            "every operation — an empty list for a public path — so an absent block means "
            "the document was not produced by this appliance."
        )
    if not security:
        return None
    if len(security) > 1:
        raise GenerationError(
            f"{method} {path} lists {len(security)} alternative credentials; the correction "
            "in rest/main.py exists to leave exactly one, and the client would have to guess."
        )
    ((name, scopes),) = security[0].items()
    if scopes:
        raise GenerationError(f"{method} {path} requires scopes {scopes}; this API has none.")
    declared = schemes.get(name)
    if declared is None:
        raise GenerationError(f"{method} {path} names security scheme {name!r}, undeclared.")
    if {k: declared.get(k) for k in _BEARER} != _BEARER:
        raise GenerationError(
            f"security scheme {name!r} is {declared!r}; this client sends bearer credentials "
            "in an Authorization header and cannot honour another kind."
        )
    return name


def _resolve(schema: dict, root: dict) -> dict:
    """Follow a ``$ref`` into the component table, once.

    Only the multipart body needs this: its parts are the properties of the generated
    ``Body_*`` schema, and the client has to know which of them is the file. Everything else
    keeps its ``$ref`` and becomes a named TypeScript type.
    """
    ref = schema.get("$ref")
    if not ref:
        return schema
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        raise GenerationError(f"$ref {ref!r} points outside the component table")
    target = root.get("components", {}).get("schemas", {}).get(ref[len(prefix) :])
    if target is None:
        raise GenerationError(f"$ref {ref!r} names a schema the document does not declare")
    if "$ref" in target:
        raise GenerationError(f"$ref {ref!r} resolves to another $ref; this follows one hop")
    return target


# --------------------------------------------------------------------------- TypeScript


def _ts_name(name: str) -> str:
    if not _TS_IDENTIFIER.match(name):
        raise GenerationError(
            f"component schema {name!r} is not a TypeScript identifier. Renaming the model "
            "is the fix; a sanitised alias would make the type's name differ from the one "
            "the OpenAPI document publishes."
        )
    return name


def _ts_type(schema: Any, *, where: str) -> str:
    """Render one JSON Schema node as a TypeScript type.

    Raises rather than degrading to ``any``. A node this does not understand means the API
    grew a shape the front end cannot express, and ``any`` there types nothing while looking
    like it does.
    """
    if schema is True or schema == {}:
        return "unknown"
    if schema is False:
        return "never"
    if not isinstance(schema, dict):
        raise GenerationError(f"{where}: {schema!r} is not a schema")

    # A node carrying only annotations — ``{"title": "Input"}``, which pydantic writes for a
    # field typed ``Any`` — constrains nothing, and ``unknown`` is what that MEANS in
    # TypeScript. It is the one place ``unknown`` is correct rather than a shrug: every other
    # shape this function does not recognise raises.
    if not _TYPE_KEYS & set(schema):
        return "unknown"
    if "not" in schema:
        raise GenerationError(f"{where}: `not` has no TypeScript equivalent this can render")

    if "$ref" in schema:
        ref = schema["$ref"]
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            raise GenerationError(f"{where}: $ref {ref!r} points outside the component table")
        return _ts_name(ref[len(prefix) :])
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    if "anyOf" in schema:
        return _union(schema["anyOf"], where=where)
    if "oneOf" in schema:
        return _union(schema["oneOf"], where=where)
    if "allOf" in schema:
        parts = [_ts_type(s, where=where) for s in schema["allOf"]]
        return " & ".join(dict.fromkeys(parts))

    kind = schema.get("type")
    if kind is None:
        raise GenerationError(f"{where}: schema {schema!r} declares no type and no combinator")
    if isinstance(kind, list):
        return _union([{**schema, "type": one} for one in kind], where=where)
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        items = schema.get("items")
        if items is None:
            raise GenerationError(f"{where}: an array with no items type")
        inner = _ts_type(items, where=where)
        return f"Array<{inner}>"
    if kind == "object":
        return _ts_object(schema, where=where)
    raise GenerationError(f"{where}: unknown JSON Schema type {kind!r}")


def _union(options: list, *, where: str) -> str:
    parts = [_ts_type(option, where=where) for option in options]
    return " | ".join(dict.fromkeys(parts))


def _ts_object(schema: dict, *, where: str) -> str:
    properties = schema.get("properties")
    additional = schema.get("additionalProperties")
    if not properties:
        if additional in (None, True):
            return "Record<string, unknown>"
        return f"Record<string, {_ts_type(additional, where=where)}>"
    required = set(schema.get("required", []))
    lines = ["{"]
    for name, child in properties.items():
        lines.extend(_jsdoc(child.get("description"), indent="  "))
        key = name if _TS_IDENTIFIER.match(name) else json.dumps(name)
        mark = "" if name in required else "?"
        lines.append(f"  {key}{mark}: {_ts_type(child, where=f'{where}.{name}')};")
    if additional not in (None, False):
        inner = "unknown" if additional is True else _ts_type(additional, where=where)
        lines.append(f"  [key: string]: {inner};")
    lines.append("}")
    return "\n".join(lines)


def _jsdoc(text: Optional[str], *, indent: str) -> list[str]:
    if not text:
        return []
    body = text.strip().replace("*/", "*\\/")
    lines = [f"{indent}/**"]
    lines += [f"{indent} * {line}".rstrip() for line in body.splitlines()]
    lines.append(f"{indent} */")
    return lines


def _render_types(doc: dict) -> list[str]:
    """Every component schema as an exported TypeScript type."""
    schemas = doc.get("components", {}).get("schemas", {})
    out: list[str] = []
    for name in sorted(schemas):
        schema = schemas[name]
        out.extend(_jsdoc(schema.get("description"), indent=""))
        rendered = _ts_type({k: v for k, v in schema.items() if k != "description"}, where=name)
        if rendered.startswith("{"):
            out.append(f"export interface {_ts_name(name)} {rendered}")
        else:
            out.append(f"export type {_ts_name(name)} = {rendered};")
        out.append("")
    return out


def _arg_type(op: Operation, name: str, doc: dict) -> str:
    """The TypeScript type of one argument of one operation."""
    for parameter in _lookup(op, doc).get("parameters", []):
        if parameter["name"] == name:
            return _ts_type(parameter["schema"], where=f"{op.op_id}.{name}")
    if name in op.file_params:
        # ``File`` extends ``Blob``, so this accepts what an ``<input type=file>`` yields
        # and also a Blob the page built itself.
        return "Blob"
    content = (_lookup(op, doc).get("requestBody") or {}).get("content") or {}
    ((media_type, media),) = content.items()
    if name == op.body_param:
        if media_type == "application/json":
            return _ts_type(media["schema"], where=f"{op.op_id}.{name}")
        # A body the route declared under a media type of its own is sent verbatim.
        return "string"
    schema = _resolve(media.get("schema") or {}, doc)
    part = (schema.get("properties") or {})[name]
    return _ts_type(_unwrap_json_text(part), where=f"{op.op_id}.{name}")


def _unwrap_json_text(schema: dict) -> dict:
    """A multipart field declared as JSON-carrying TEXT, typed as the value it carries.

    ``POST /ingest/file`` takes its ``options`` as a form field whose content is a JSON
    document, and FastAPI declares that as ``type: string`` with ``contentMediaType:
    application/json`` and the real shape under ``contentSchema``. The client serialises the
    value on the way out — ``jmfts_client.transport`` does the same with ``json.dumps`` — so
    the caller hands over the object, and typing the parameter ``string`` would describe the
    wire rather than the call.
    """
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:
        return {**schema, "anyOf": [_unwrap_json_text(one) for one in schema["anyOf"]]}
    if schema.get("contentMediaType") == "application/json":
        return schema.get("contentSchema", {"title": schema.get("title", "")})
    return schema


def _lookup(op: Operation, doc: dict) -> dict:
    return doc["paths"][op.path][op.method.lower()]


def _required_args(op: Operation, doc: dict) -> set[str]:
    operation = _lookup(op, doc)
    required = {p["name"] for p in operation.get("parameters", []) if p.get("required")}
    body = operation.get("requestBody") or {}
    if body.get("required"):
        if op.body_param:
            required.add(op.body_param)
        else:
            content = next(iter(body["content"].values()))
            schema = _resolve(content.get("schema") or {}, doc)
            required |= set(schema.get("required", []))
    return required


# --------------------------------------------------------------------------- emit


def _table(op: Operation) -> dict:
    """One operation as the plain data ``operations.js`` carries.

    Strict JSON on purpose: ``tests/test_ts_client_codegen.py`` reads this table back with
    ``json.loads`` and compares it against the live registry, which it could not do against
    a table written as arbitrary JavaScript.
    """
    return {
        "op_id": op.op_id,
        "operation_id": op.operation_id,
        "python_verb": op.python_verb,
        "method": op.method,
        "path": op.path,
        "path_params": list(op.path_params),
        "path_slash_params": list(op.path_slash_params),
        "query_params": list(op.query_params),
        "body_param": op.body_param,
        "body_media_type": op.body_media_type,
        "form_params": list(op.form_params),
        "file_params": list(op.file_params),
        "binary": op.binary,
        "response_media_type": op.response_media_type,
        "success_status": op.success_status,
        "security_scheme": op.security_scheme,
        "tags": list(op.tags),
        "summary": op.summary,
    }


def _render_operations_js(ops: list[Operation]) -> str:
    table = {op.op_id: _table(op) for op in ops}
    body = json.dumps(table, indent=2, ensure_ascii=False, sort_keys=False)
    return f"""{_self_types("operations")}
//
// The operation table: every mounted JMFTS operation, as data. ``transport.js`` reads it to
// place arguments on the wire, ``verbs.js`` names one method per entry, and ``copyout.js``
// reads ``python_verb`` to print a RemoteJmftsClient call.
//
// Regenerate with ``python -m scripts.generate_ts_client`` in the jmfts repository;
// ``tests/test_ts_client_codegen.py`` fails if this file falls behind the surface.

// The object literal below is strict JSON and starts and ends on lines of its own, so
// ``tests/test_ts_client_codegen.py`` can read the table back and compare it against the
// live registry. A table it could not parse would be a table nothing checks.

/** @type {{Readonly<Record<string, import("./operations.js").Operation>>}} */
export const OPERATIONS = Object.freeze(
{body}
);

/** Every operation id, in the order the document declares them. */
export const OP_IDS = Object.freeze(Object.keys(OPERATIONS));
"""


def _render_operations_dts(ops: list[Operation]) -> str:
    ids = "\n".join(f"  | {json.dumps(op.op_id)}" for op in ops)
    return f"""{BANNER_JS}

/** Every mounted operation, by id. A union rather than ``string``, so a typo is a type error. */
export type OpId =
{ids};

/** One operation, as ``operations.js`` carries it. */
export interface Operation {{
  /** ``ServiceClass.method`` for an ``@expose``'d operation, the route name otherwise. */
  op_id: OpId;
  /** The document's own ``operationId``, mangled by FastAPI out of the name, path and method. */
  operation_id: string;
  /** The ``RemoteJmftsClient`` method name, or ``null`` where the route is hand-written. */
  python_verb: string | null;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** The path template, with ``{{name}}`` placeholders. */
  path: string;
  path_params: ReadonlyArray<string>;
  /** Path parameters whose values keep their slashes (Starlette's ``:path`` converter). */
  path_slash_params: ReadonlyArray<string>;
  query_params: ReadonlyArray<string>;
  /** The single JSON or raw body argument, or ``null`` for none and for multipart. */
  body_param: string | null;
  body_media_type: string | null;
  /** Multipart fields that travel beside a file part, sent as JSON text. */
  form_params: ReadonlyArray<string>;
  file_params: ReadonlyArray<string>;
  /** ``true`` when the response is bytes: the call resolves to a ``Blob``, not parsed JSON. */
  binary: boolean;
  response_media_type: string | null;
  success_status: number;
  /** The security scheme the corrected document says this operation takes, or ``null``. */
  security_scheme: string | null;
  tags: ReadonlyArray<string>;
  summary: string;
}}

export declare const OPERATIONS: Readonly<Record<OpId, Operation>>;
export declare const OP_IDS: ReadonlyArray<OpId>;
"""


def _method_js(op: Operation) -> str:
    doc = ["  /**", f"   * {op.summary}" if op.summary else "   * (no summary)", "   *"]
    doc.append(f"   * `{op.method} {op.path}` — {op.op_id}")
    if op.binary:
        doc.append("   *")
        doc.append(f"   * Answers `{op.response_media_type}`; resolves to a Blob, not JSON.")
    if op.python_verb is None:
        doc.append("   *")
        doc.append("   * Hand-written FastAPI route: no RemoteJmftsClient method exists for it.")
    doc.append("   */")
    return "\n".join(
        doc
        + [
            f"  {op.member}(args = {{}}) {{",
            f"    return this.call({json.dumps(op.op_id)}, args);",
            "  }",
        ]
    )


def _render_verbs_js(ops: list[Operation]) -> str:
    methods = "\n\n".join(_method_js(op) for op in ops)
    return f"""{_self_types("verbs")}
//
// One method per mounted operation, each doing nothing but naming its operation id. The
// request path lives in ``transport.js`` and the argument binding in ``operations.js``, so
// regenerating this file cannot lose behaviour — the same split ``_verbs.py`` and
// ``jmfts_client/transport.py`` have, for the same reason.
//
// Regenerate with ``python -m scripts.generate_ts_client``.

import {{ JmftsTransport }} from "./transport.js";

export class JmftsClient extends JmftsTransport {{
{methods}
}}
"""


def _render_verbs_dts(ops: list[Operation], doc: dict) -> str:
    lines = [
        BANNER_JS,
        "",
        'import { JmftsTransport } from "./transport.js";',
        'import type { CallEvent } from "./transport.js";',
        'import type { OpId } from "./operations.js";',
        "",
        "// ------------------------------------------------------------- component schemas",
        "",
    ]
    lines.extend(_render_types(doc))
    lines.append("// ------------------------------------------------------------------ operations")
    lines.append("")

    members: list[str] = []
    for op in ops:
        required = _required_args(op, doc)
        fields = []
        for name in op.arg_names:
            mark = "" if name in required else "?"
            fields.append(f"    {name}{mark}: {_arg_type(op, name, doc)};")
        args = "{\n" + "\n".join(fields) + "\n  }" if fields else "Record<string, never>"
        returns = "Blob" if op.binary else _return_type(op, doc)
        members.extend(_jsdoc(_signature_doc(op), indent="  "))
        optional = "" if required else "?"
        members.append(f"  {op.member}(args{optional}: {args}): Promise<{returns}>;")
        members.append("")

    lines.append("export declare class JmftsClient extends JmftsTransport {")
    lines.extend(members[:-1])  # every member is followed by a blank line; drop the last
    lines.append("}")
    lines.append("")
    lines.append("export type { CallEvent, OpId };")
    lines.append("")
    return "\n".join(lines)


def _signature_doc(op: Operation) -> str:
    parts = [op.summary or "(no summary)", "", f"`{op.method} {op.path}` — {op.op_id}"]
    if op.python_verb:
        parts.append(f"RemoteJmftsClient method: `{op.python_verb}`.")
    else:
        parts.append("Hand-written route: no RemoteJmftsClient method.")
    return "\n".join(parts)


def _return_type(op: Operation, doc: dict) -> str:
    operation = _lookup(op, doc)
    responses = operation["responses"]
    content = responses[str(op.success_status)].get("content") or {}
    if not content:
        return "null"
    schema = next(iter(content.values()))["schema"]
    return _ts_type(schema, where=f"{op.op_id}.response")


def generate(doc: Optional[dict] = None) -> dict[str, str]:
    """``{filename: text}`` for every generated file. Pure in ``doc``."""
    doc = document() if doc is None else doc
    ops = _operations(doc)
    if not ops:
        raise GenerationError("the document declares no operations")
    return {
        "operations.js": _render_operations_js(ops),
        "operations.d.ts": _render_operations_dts(ops),
        "verbs.js": _render_verbs_js(ops),
        "verbs.d.ts": _render_verbs_dts(ops, doc),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any generated file differs from the current surface",
    )
    args = parser.parse_args(argv)

    pages = generate()
    if args.check:
        stale = [
            name
            for name, text in pages.items()
            if not (TARGET_DIR / name).exists()
            or (TARGET_DIR / name).read_text(encoding="utf-8") != text
        ]
        if stale:
            print(
                f"stale: {', '.join(sorted(stale))}. Run: python -m scripts.generate_ts_client",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET_DIR} is current ({len(pages)} files).")
        return 0

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        (TARGET_DIR / name).write_text(text, encoding="utf-8")
        print(f"wrote {TARGET_DIR / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
