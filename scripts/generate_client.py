"""Generate ``jmfts-client/jmfts_client/_verbs.py`` from the built route table.

Why the ROUTE TABLE and not ``REGISTRY`` directly
-------------------------------------------------
``ExposeSpec`` records method, path, response model, error map and summary. It
deliberately does NOT record which parameters are body, query or path: ``rest/wiring.py``
leaves that to FastAPI's own inference, because that inference is what reproduces the wire
contract. A generator reading ``REGISTRY`` alone would have to reimplement it, and every
disagreement between the two implementations would be a silent wire bug — the exact drift
the registry exists to prevent.

So this reads the routes FastAPI actually built (``route.dependant`` carries the resolved
``path_params`` / ``query_params`` / ``body_params``) and joins each one back to its
``ExposeSpec`` on ``route.name``, which ``wiring.py`` sets to ``ServiceClass.method``.
``tests/test_api_parity.py`` already proves that join is total.

Run it from the repository root::

    python -m scripts.generate_client            # rewrite the file
    python -m scripts.generate_client --check    # exit 1 if the file is stale

``tests/test_client_codegen.py`` runs the check, so a new verb cannot land without the
client gaining it.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import typing
from pathlib import Path
from typing import Any, Optional

from fastapi.datastructures import UploadFile
from fastapi.routing import APIRoute

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "jmfts-client" / "jmfts_client" / "_verbs.py"

CONTRACTS_ROOT = "jmfts_client.contracts"


class GenerationError(RuntimeError):
    """A route this generator cannot represent honestly. Never worked around."""


# --------------------------------------------------------------------------- types


def _render_type(annotation: Any, imports: set[str]) -> str:
    """Render one annotation as client-side source text, recording contract imports.

    Raises rather than degrading to ``Any``. An annotation this does not understand means
    the surface grew a shape the client cannot express, and emitting ``Any`` there would
    hand the caller a verb that type-checks and lies.
    """
    if annotation is type(None):
        return "None"
    if annotation is Any:
        return "Any"

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Annotated:  # wire-behaviour metadata is a server concern
        return _render_type(args[0], imports)
    if origin is typing.Union:
        parts = [_render_type(a, imports) for a in args if a is not type(None)]
        rendered = " | ".join(dict.fromkeys(parts))
        return f"Optional[{rendered}]" if type(None) in args else rendered
    if origin in (list, set, tuple, dict):
        name = origin.__name__
        inner = ", ".join(_render_type(a, imports) for a in args)
        return f"{name}[{inner}]" if inner else name

    if annotation in (int, str, bool, float, bytes, dict, list):
        return annotation.__name__
    if annotation is datetime.datetime:
        imports.add("import datetime")
        return "datetime.datetime"
    if annotation is UploadFile:
        return "UploadedFile"

    module = getattr(annotation, "__module__", "")
    if module.startswith(CONTRACTS_ROOT):
        imports.add(f"from {module} import {annotation.__name__}")
        return annotation.__name__

    raise GenerationError(
        f"cannot render annotation {annotation!r} (module {module!r}). "
        "Move the type into jmfts_client.contracts, or the client cannot express it."
    )


class _NonLiteralDefault(Exception):
    """A default this generator will not copy into client source. See ``_default_for``."""


def _render_default(default: Any) -> str:
    """Render a parameter default as source text.

    Only literals are reproduced. A model instance (``chunk: ChunkRequest = ChunkRequest()``)
    is refused deliberately: writing its field values here would make the client a SECOND
    home for the server's defaults, free to disagree with it after any server-side change.
    ``_default_for`` turns that refusal into ``Optional[T] = None``, which sends no body and
    lets the server apply the one default that exists.
    """
    if isinstance(default, bool) or default is None:
        return repr(default)
    if isinstance(default, (str, int, float)):
        return repr(default)
    raise _NonLiteralDefault(repr(default))


def _default_for(field: Any, type_text: str) -> tuple[str, Optional[str]]:
    """Return ``(type_text, default_text)`` for one optional-or-required parameter."""
    if field.field_info.is_required():
        return type_text, None
    try:
        return type_text, _render_default(field.field_info.default)
    except _NonLiteralDefault:
        optional = type_text if type_text.startswith("Optional[") else f"Optional[{type_text}]"
        return optional, "None"


# --------------------------------------------------------------------------- params


class _Param:
    """One client-method parameter and where it belongs on the wire."""

    def __init__(
        self,
        name: str,
        type_text: str,
        where: str,
        default: Optional[str],
        media_type: Optional[str] = None,
    ) -> None:
        self.name = name
        self.type_text = type_text
        self.where = where  # "path" | "query" | "body" | "raw_body" | "file" | "form_json"
        self.default = default
        #: Only for "raw_body": the media type the route declared, sent verbatim.
        self.media_type = media_type

    @property
    def required(self) -> bool:
        return self.default is None


def _collect_params(route: APIRoute, imports: set[str]) -> list[_Param]:
    dependant = route.dependant
    params: list[_Param] = []

    for field in dependant.path_params:
        params.append(
            _Param(field.name, _render_type(field.field_info.annotation, imports), "path", None)
        )

    for field in dependant.body_params:
        annotation = field.field_info.annotation
        if annotation is UploadFile:
            imports.add(f"from {CONTRACTS_ROOT}.upload import UploadedFile")
            params.append(_Param(field.name, "UploadedFile", "file", None))
            continue
        # A body the route declared under a media type of its own is sent VERBATIM, not
        # wrapped in JSON. `text/turtle` works either way — FastAPI would parse a JSON
        # string back to the same str — but a client that posts `application/json` to a
        # route documented as `text/turtle` is a client whose requests do not look like the
        # ones the OpenAPI document tells everyone else to send, and the first proxy or
        # gateway that content-negotiates would tell them apart.
        media_type = getattr(field.field_info, "media_type", None)
        if media_type and media_type != "application/json" and not _has_file_part(route):
            rendered, default = _default_for(field, _render_type(annotation, imports))
            params.append(_Param(field.name, rendered, "raw_body", default, media_type))
            continue
        # A body param beside a file part is a multipart form field carrying JSON text.
        where = "form_json" if _has_file_part(route) else "body"
        rendered, default = _default_for(field, _render_type(annotation, imports))
        params.append(_Param(field.name, rendered, where, default))

    for field in dependant.query_params:
        rendered, default = _default_for(field, _render_type(field.field_info.annotation, imports))
        params.append(_Param(field.name, rendered, "query", default))

    return params


def _has_file_part(route: APIRoute) -> bool:
    return any(f.field_info.annotation is UploadFile for f in route.dependant.body_params)


# --------------------------------------------------------------------------- emit


def _response_text(spec: Any, imports: set[str]) -> tuple[str, str]:
    """Return ``(return_annotation, response_argument)`` for one operation."""
    if spec.media_type:
        # A binary operation has no response_model by construction — ``@expose`` refuses
        # the pair — so this branch comes first and the None below means "JSON, unmodelled"
        # rather than "nothing came back".
        imports.add("from jmfts_client.contracts.binary import BinaryPayload")
        return "BinaryPayload", "BinaryPayload"
    model = spec.response_model
    if model is None:
        return "Any", "None"
    rendered = _render_type(model, imports)
    return rendered, rendered


def _docstring(spec: Any, route: APIRoute) -> list[str]:
    lines: list[str] = []
    summary = (spec.summary or "").strip()
    if summary:
        lines.append(summary)
        lines.append("")
    method = sorted(route.methods)[0]
    lines.append(f"``{method} {route.path}`` — {spec.name}")
    if spec.errors:
        lines.append("")
        by_status: dict[int, list[str]] = {}
        for exc, status in spec.errors.items():
            by_status.setdefault(status, []).append(exc.__name__)
        for status in sorted(by_status):
            names = ", ".join(sorted(by_status[status]))
            lines.append(f"Raises on {status} (server: {names}).")
    if spec.media_type:
        lines.append("")
        lines.append(
            f"Returns a ``BinaryPayload``; the route declares ``{spec.media_type}`` and the "
            "payload carries what actually arrived."
        )
    elif spec.response_model is None:
        lines.append("")
        lines.append("The route declares no response model, so the parsed JSON is returned.")
    return lines


def _emit_method(route: APIRoute, spec: Any, imports: set[str]) -> str:
    params = _collect_params(route, imports)
    return_text, response_arg = _response_text(spec, imports)

    required = [p for p in params if p.required]
    optional = [p for p in params if not p.required]

    sig = ["self"]
    sig += [f"{p.name}: {p.type_text}" for p in required]
    if optional:
        sig.append("*")
        sig += [f"{p.name}: {p.type_text} = {p.default}" for p in optional]

    method = sorted(route.methods)[0]
    lines = [f"    def {spec.func.__name__}("]
    for item in sig:
        lines.append(f"        {item},")
    lines.append(f"    ) -> {return_text}:")

    doc = _docstring(spec, route)
    lines.append(f'        """{doc[0]}')
    for extra in doc[1:]:
        lines.append(f"        {extra}" if extra else "")
    lines.append('        """')

    call = [
        "        return self._call(",
        f'            "{method}",',
        f'            "{route.path}",',
    ]
    for where, key in (("path", "path"), ("query", "query"), ("form_json", "form_json")):
        selected = [p for p in params if p.where == where]
        if selected:
            entries = ", ".join(f'"{p.name}": {p.name}' for p in selected)
            call.append(f"            {key}={{{entries}}},")
    files = [p for p in params if p.where == "file"]
    if files:
        entries = ", ".join(f'"{p.name}": {p.name}' for p in files)
        call.append(f"            files={{{entries}}},")
    raw = [p for p in params if p.where == "raw_body"]
    if len(raw) > 1:
        raise GenerationError(f"{spec.name} has {len(raw)} raw body params; expected at most 1")
    if raw:
        call.append(f"            content={raw[0].name},")
        call.append(f'            content_type="{raw[0].media_type}",')
    body = [p for p in params if p.where == "body"]
    if len(body) > 1:
        raise GenerationError(f"{spec.name} has {len(body)} JSON body params; expected at most 1")
    if body and raw:
        raise GenerationError(
            f"{spec.name} has both a JSON body and a raw body; one request has one body"
        )
    if body:
        call.append(f"            body={body[0].name},")
    call.append(f"            response={response_arg},")
    call.append("        )")

    return "\n".join(lines + call)


def render() -> str:
    """Build the full text of ``_verbs.py``."""
    from jmfts_core.registry import REGISTRY
    from jmfts_core.rest.main import app
    from jmfts_core.rest.wiring import iter_mounted_api_routes

    by_name = {spec.name: spec for spec in REGISTRY}
    routes = [r for r in iter_mounted_api_routes(app) if r.name in by_name]
    if len(routes) != len(REGISTRY):
        raise GenerationError(
            f"joined {len(routes)} routes against {len(REGISTRY)} registry entries; "
            "test_api_parity should have caught this first"
        )

    imports: set[str] = set()
    bodies = [
        _emit_method(route, by_name[route.name], imports)
        for route in sorted(routes, key=lambda r: by_name[r.name].name)
    ]

    header = '''"""Generated verb table — DO NOT EDIT.

Every method here is one ``@expose``'d JMFTS operation, rendered from the route FastAPI
built for it. Regenerate with ``python -m scripts.generate_client`` in the jmfts
repository; ``tests/test_client_codegen.py`` fails if this file falls behind the surface.

The request logic lives in ``jmfts_client.transport``, not here, so regenerating this file
cannot lose behaviour.
"""

from __future__ import annotations

from typing import Any, Optional

'''
    typing_lines = sorted(i for i in imports if i.startswith("import "))
    contract_lines = sorted(i for i in imports if i.startswith("from "))
    import_block = "\n".join(typing_lines + ([""] if typing_lines else []) + contract_lines)

    class_block = (
        "\n\nfrom jmfts_client.transport import _VerbTransport\n\n\n"
        "class _GeneratedVerbs(_VerbTransport):\n"
        '    """Every exposed JMFTS operation, as a method. Mixed into ``RemoteJmftsClient``."""\n'
    )

    raw = header + import_block + class_block + "\n" + "\n\n".join(bodies) + "\n"
    return _blacken(raw)


def _blacken(text: str) -> str:
    """Run Black over the generated source, at the line length the repo uses.

    The generated file is checked in and every other checked-in file is Black-formatted, so
    ``black --check .`` would fail on it otherwise — and a developer running ``black .`` to
    fix that would rewrite the file, making it differ from ``render()`` and failing
    ``test_generated_client_is_current`` instead. Formatting HERE is what makes those two
    checks agree. Black is a dev dependency and generation is a dev-time action, so the
    import is allowed to raise rather than emitting unformatted source.
    """
    import black

    return black.format_str(text, mode=black.Mode(line_length=100))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the generated file differs from the current surface",
    )
    args = parser.parse_args(argv)

    text = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET} is stale. Run: python -m scripts.generate_client", file=sys.stderr)
            return 1
        print(f"{TARGET} is current.")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
