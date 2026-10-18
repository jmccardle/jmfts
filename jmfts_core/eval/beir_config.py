"""BEIR dataset table and data location.

Ported from ``benchmarks/beir/config.py`` on the pre-0.1.0 history, with two things removed
on purpose:

- **The ``.env.beir`` side-load.** The old module rewrote ``os.environ`` from a file at the
  repository root as an import side effect, so that the experiment database would be
  selected before pydantic-settings read the environment. A wheel has no repository root,
  and an import that edits the environment is the opposite of least surprise. Point the
  normal ``JMFTS_DB_*`` variables at the experiment database instead (or source a file of
  them in the shell that runs ``jmfts-eval``).
- **The default data directory.** It was a hard-coded absolute path on one machine. The data directory is
  ``--data-dir`` or ``JMFTS_EVAL_DATA_DIR``, and with neither the run stops and says so.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: The public BEIR mirror. ``{name}`` is the dataset's ``beir_name``.
BEIR_URL_TEMPLATE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{name}.zip"

#: The environment variable ``--data-dir`` falls back to. Read by the CLI only; it is not an
#: appliance setting, so it is not a field of ``jmfts_core.config.Settings``.
DATA_DIR_ENV = "JMFTS_EVAL_DATA_DIR"

# `beir_name` is the slug in the download URL and the directory the zip unpacks to.
# `corpus_size` is descriptive only (it is printed by --list), never checked.
BEIR_DATASETS: dict[str, dict[str, str]] = {
    "scifact": {
        "beir_name": "scifact",
        "description": "Scientific claim verification",
        "split": "test",
        "corpus_size": "~5K",
    },
    "nfcorpus": {
        "beir_name": "nfcorpus",
        "description": "Nutrition/medical retrieval",
        "split": "test",
        "corpus_size": "~3.6K",
    },
    "fiqa": {
        "beir_name": "fiqa",
        "description": "Financial QA",
        "split": "test",
        "corpus_size": "~57K",
    },
    "trec-covid": {
        "beir_name": "trec-covid",
        "description": "Biomedical (COVID-19)",
        "split": "test",
        "corpus_size": "~171K",
    },
    "nq": {
        "beir_name": "nq",
        "description": "Natural Questions (factoid QA)",
        "split": "test",
        "corpus_size": "~2.68M",
    },
    "msmarco": {
        "beir_name": "msmarco",
        "description": "Web passage retrieval",
        "split": "dev",
        "corpus_size": "~8.8M",
    },
}

#: The README's table is BEIR_FULL_SUITE.
BEIR_SUITES: dict[str, list[str]] = {
    "quick": ["scifact", "nfcorpus", "fiqa"],
    "full": ["scifact", "nfcorpus", "fiqa", "trec-covid"],
    "extended": ["scifact", "nfcorpus", "fiqa", "trec-covid", "nq", "msmarco"],
}


class DataDirNotConfigured(RuntimeError):
    """Neither ``--data-dir`` nor ``JMFTS_EVAL_DATA_DIR`` names where the datasets live."""


def resolve_data_dir(explicit: Optional[str]) -> Path:
    """The directory datasets are read from and downloaded into.

    ``explicit`` (the ``--data-dir`` argument) wins; then ``JMFTS_EVAL_DATA_DIR``. There is
    no third answer: a guessed directory is either a path on somebody else's machine or a
    fresh download into wherever the shell happened to be.
    """
    chosen = explicit or os.environ.get(DATA_DIR_ENV)
    if not chosen:
        raise DataDirNotConfigured(
            f"No data directory. Pass --data-dir DIR or set {DATA_DIR_ENV}=DIR; datasets are "
            "read from DIR/<name>/ and downloaded there when absent."
        )
    return Path(chosen).expanduser()


def dataset_info(name: str) -> dict[str, str]:
    """The table row for ``name``, or a ValueError listing the names that exist."""
    try:
        return BEIR_DATASETS[name]
    except KeyError:
        raise ValueError(
            f"Unknown BEIR dataset {name!r}. Available: {', '.join(BEIR_DATASETS)}"
        ) from None
