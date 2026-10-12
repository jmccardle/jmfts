"""Find code nothing can reach, without drowning in the registry's false positives.

Run it::

    python -m scripts.deadcode_scan                  # what nothing in the repo calls
    python -m scripts.deadcode_scan --published-only # what nothing in the WHEEL calls
    python -m scripts.deadcode_scan --allowlist      # just the generated allowlist
    python -m scripts.deadcode_scan --strict         # non-zero exit if anything survives

WHY THIS EXISTS RATHER THAN `vulture jmfts_core`
------------------------------------------------

Plain vulture is unusable here, and not because it is wrong. This codebase is
registry-driven: ``@expose`` turns a service method into a REST route and
``@register_task_handler`` turns a function into a queue handler, so roughly a hundred
functions are reachable with NO in-repo caller. Add Pydantic contract fields, SQLAlchemy
mapped columns, and a generated client whose every method is public API, and a raw run
reports about 256 findings (2026-10-12) of which almost none are real. A report that is 95% noise is a
report nobody reads twice.

So this builds the allowlist from the LIVE registries rather than from a checked-in list
of names. It imports the app, walks ``REGISTRY``, ``TASK_HANDLERS``, every contract's
``model_fields``, every SQLAlchemy mapper, and the generated client's public surface.
Delete an ``@expose`` and its name leaves the allowlist the same day — which is the whole
point, because a stale allowlist hides exactly the code this is meant to find.

WHAT IT CANNOT TELL YOU
-----------------------

Three limits, stated because each one has already produced a wrong conclusion:

* **Unreferenced is not unreachable.** Anything dispatched by string, by entry point, or
  by a decorator this does not know about will be reported. ``jmfts-init-db`` and
  ``jmfts-batch-worker`` are console scripts; their modules have no importer.
* **Uncovered is not dead.** ``coverage`` is the other half of the picture and answers a
  different question: 0% covered means untested, not dead. A module reached only through
  a network fetch is the usual case, because no test makes a network call.
* **Reachable only from ``tests/`` or ``scripts/`` is a judgement call.** Both are in
  the public repository (``tests/test_readme_links.py::PUBLISHED``) and neither is in any
  WHEEL — the three ``[tool.setuptools.packages.find]`` tables decide that, and
  :data:`SCAN_TARGETS` is read from them. Run ``--published-only`` to see those; the
  answer is a decision about what the package is for, not something a tool can settle.

Findings are a starting point for reading the code, never a delete list.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import pkgutil
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The three distributions, by the directory holding each one's ``pyproject.toml``.
DISTRIBUTIONS = (".", "jmfts-client", "jmfts-web")


def _wheel_packages() -> tuple[str, ...]:
    """What ships: every top-level package each distribution's ``packages.find`` includes.

    Read rather than listed, so a fourth package (or a fourth distribution added to
    :data:`DISTRIBUTIONS`) is scanned from the edit that ships it.
    """
    import fnmatch
    import tomllib

    found = []
    for dist in DISTRIBUTIONS:
        root = REPO / dist
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        include = config["tool"]["setuptools"]["packages"]["find"]["include"]
        for child in sorted(root.iterdir()):
            if (child / "__init__.py").is_file() and any(
                fnmatch.fnmatch(child.name, pattern) for pattern in include
            ):
                found.append(str(child.relative_to(REPO)))
    if not found:
        raise SystemExit("no wheel packages found; the packages.find tables have moved")
    return tuple(found)


#: What ships, and therefore what a finding can be reported IN.
SCAN_TARGETS = _wheel_packages()

#: Handed to vulture as well, so a name used only here still counts as referenced — but
#: findings located in these paths are dropped. They are callers, not subjects.
#:
#: `--published-only` removes them from the reference set, which asks the other question:
#: what has no caller in the SHIPPED wheels? `tests/` and `scripts/` are in the public
#: repository and in no wheel, so a method whose only caller lives there has no caller in
#: an installed appliance. Both questions are legitimate; they are not the same question,
#: and answering one while claiming the other is how a helper survives for a year.
REFERENCE_ONLY = (
    "tests",
    "scripts",
)

#: Never reported, whatever vulture thinks. Every entry is a mechanism that makes a name
#: reachable without a reference, not a name we have decided to tolerate.
SUPPRESSED_PATHS = (
    # Generated from the server's route table; every method is the client's public API
    # and has no in-repo caller by construction. `test_client_codegen` guards its
    # contents, which is a stronger check than reachability.
    "jmfts-client/jmfts_client/_verbs.py",
)

#: Names that are reachable through a protocol or a framework rather than a call.
#: Pydantic reads `Config`/`model_config`; Python reads the dunders; `from_attributes`
#: and `env_prefix` are settings *values*, consumed by the library that owns them.
FRAMEWORK_NAMES = frozenset(
    {
        "Config",
        "model_config",
        "from_attributes",
        "env_prefix",
        "env_file",
        "populate_by_name",
        "use_enum_values",
        "exc_info",  # the `__exit__` protocol's signature
    }
)


def build_allowlist() -> set[str]:
    """Every name the running application makes reachable without a source reference."""
    import jmfts_core.rest.main  # noqa: F401  (imports the services, filling REGISTRY)

    from jmfts_core.database import Base
    from jmfts_core.ingest_tasks import TASK_HANDLERS
    from jmfts_core.registry import REGISTRY

    names: set[str] = set(FRAMEWORK_NAMES)

    # 1. `@expose` service methods. These ARE the REST API.
    for spec in REGISTRY:
        names.add(spec.func.__name__)
        names.add(spec.name.rsplit(".", 1)[-1])

    # 2. `@register_task_handler` functions, dispatched by task-type string.
    for handler in TASK_HANDLERS.values():
        name = getattr(handler, "__name__", "")
        if name:
            names.add(name)

    # 3. Contract classes and their fields — the wire shapes, read by Pydantic.
    from pydantic import BaseModel
    from pydantic_settings import BaseSettings

    import jmfts_client.contracts as contracts

    for module_info in pkgutil.iter_modules(contracts.__path__):
        module = importlib.import_module(f"jmfts_client.contracts.{module_info.name}")
        for attr_name, obj in vars(module).items():
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
                names.add(attr_name)
                names.update(obj.model_fields)

    # 3b. Every other model in our packages. Validators and serializers are called by
    # Pydantic, never by name, on contracts and on `Settings` alike. Fields are excused
    # everywhere EXCEPT a `BaseSettings`: a wire model outside the contracts package (the
    # health shapes in `rest/schemas.py`) is serialised by FastAPI, but a setting nothing
    # reads is an env var that silently does nothing, and that is a finding.
    def _subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from _subclasses(sub)

    for model in set(_subclasses(BaseModel)):
        if not model.__module__.startswith(("jmfts_core", "jmfts_client", "jmfts_batch")):
            continue
        decorators = model.__pydantic_decorators__
        for table in (
            decorators.validators,
            decorators.field_validators,
            decorators.root_validators,
            decorators.model_validators,
            decorators.field_serializers,
            decorators.model_serializers,
            decorators.computed_fields,
        ):
            names.update(table)
        if not issubclass(model, BaseSettings):
            names.add(model.__name__)
            names.update(model.model_fields)

    # 4. SQLAlchemy mapped classes, columns and relationships — and the members of any
    # Enum a column is typed with, which are reached by the stored VALUE (`FactType("static")`)
    # and never by attribute.
    import enum

    for mapper in Base.registry.mappers:
        names.add(mapper.class_.__name__)
        names.update(column.key for column in mapper.columns)
        names.update(rel.key for rel in mapper.relationships)
        for column in mapper.columns:
            enum_class = getattr(column.type, "enum_class", None)
            if isinstance(enum_class, type) and issubclass(enum_class, enum.Enum):
                names.update(enum_class.__members__)

    # 5. The generated client's public surface.
    from jmfts_client import RemoteJmftsClient

    names.update(n for n in dir(RemoteJmftsClient) if not n.startswith("_"))

    # 6. Hand-written route endpoints — the infra survivors in `main.py` and the whole of
    # `routers/runner.py`. FastAPI holds the function; nothing calls it by name. Read off
    # the mounted table rather than listed, so a route deleted from the app stops being
    # excused here on the same edit.
    from jmfts_core.rest.main import app
    from jmfts_core.rest.wiring import iter_mounted_api_routes

    for route in iter_mounted_api_routes(app):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            names.add(getattr(endpoint, "__name__", ""))

    # 7. Exception handlers, registered by exception TYPE and never called by name.
    for handler in getattr(app, "exception_handlers", {}).values():
        names.add(getattr(handler, "__name__", ""))

    # 8. Console-script entry points: a module nothing imports, called by name.
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    for target in pyproject.get("project", {}).get("scripts", {}).values():
        names.add(target.rsplit(":", 1)[-1])

    return {n for n in names if n}


def run_vulture(allowlist: set[str], min_confidence: int, published_only: bool) -> list[str]:
    """Vulture's findings, minus everything the allowlist or a caller explains."""
    # The interpreter's own bin/ first: `.venv/bin/python -m scripts.deadcode_scan` should
    # find the vulture installed beside it without the caller also fixing PATH.
    beside = Path(sys.executable).parent / "vulture"
    vulture = str(beside) if beside.is_file() else shutil.which("vulture")
    if vulture is None:
        raise SystemExit(
            "vulture is not installed. It is deliberately NOT a project dependency — this "
            "is an occasional audit, not part of the gate. Install it where you run this:\n"
            "    pip install vulture"
        )

    scanned = list(SCAN_TARGETS)
    if not published_only:
        scanned += [p for p in REFERENCE_ONLY if (REPO / p).is_dir()]
    targets = [str(REPO / t) for t in scanned]
    result = subprocess.run(
        [vulture, *targets, "--min-confidence", str(min_confidence)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    # vulture exits 3 when it has findings, 0 when it has none. Neither is an error;
    # anything else is.
    if result.returncode not in (0, 3):
        raise SystemExit(f"vulture failed ({result.returncode}):\n{result.stderr}")

    survivors = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        relative = line.replace(str(REPO) + "/", "")
        if any(relative.startswith(p) for p in SUPPRESSED_PATHS):
            continue
        # Findings inside the reference-only trees are not the subject of this audit.
        if any(relative.startswith(p + "/") for p in REFERENCE_ONLY):
            continue
        # "path:line: unused KIND 'NAME' (N% confidence)"
        if "'" in line:
            name = line.split("'")[1]
            if name in allowlist:
                continue
            # A dunder is a protocol slot. Python calls it; source never names it.
            if name.startswith("__") and name.endswith("__"):
                continue
        survivors.append(relative)
    return survivors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--allowlist",
        action="store_true",
        help="print the generated allowlist and exit, to see what is being excused",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=60,
        help="vulture's threshold (default 60, its floor for functions and classes)",
    )
    parser.add_argument(
        "--published-only",
        action="store_true",
        help=(
            "do not count tests/ or scripts/ as callers, which surfaces code that only "
            "something outside the shipped wheels still uses"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if anything survives, for a CI job that gates on it",
    )
    args = parser.parse_args()

    allowlist = build_allowlist()

    if args.allowlist:
        print("\n".join(sorted(allowlist)))
        return 0

    survivors = run_vulture(allowlist, args.min_confidence, args.published_only)

    callers = "the shipped packages only" if args.published_only else "the whole repository"
    print(f"{len(allowlist)} names excused by the live registries; callers read from {callers}.")
    print(f"{len(survivors)} findings survive at confidence >= {args.min_confidence}.\n")
    for line in survivors:
        print(f"  {line}")

    if survivors:
        print(
            "\nEach one is a question, not a verdict. Before deleting anything, grep the "
            "WHOLE tree — tests/, scripts/, plugin/, jmfts-web/static/ — and check whether the "
            "only caller lives outside the published subset."
        )

    return 1 if (args.strict and survivors) else 0


if __name__ == "__main__":
    sys.exit(main())
