#!/usr/bin/env bash
# Does `pip install jmfts` — with no extras — actually work?
#
# The test suite cannot answer this. It runs in an environment that HAS torch, so the most
# it can do is assert that nothing on the worker's import path reaches for it
# (tests/test_thin_worker.py) and simulate the absence by poisoning sys.modules. Neither
# catches the failure that matters here: a base dependency that quietly pulls torch back
# in transitively, or a module-level import added to a file every process loads.
#
# So this builds a real, empty virtualenv, installs base JMFTS into it, and runs the
# assertions there. It needs network the first time and takes a couple of minutes.
#
#   ./scripts/check_base_install.sh
#
# What it proves: the API and the worker import, the tokenizer half works, and asking for
# a vector raises ModelStackNotInstalled naming both ways out. What it does NOT prove is
# that a remote embedder can reach a runner — that needs two processes and is
# tests/test_thin_worker.py's job.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="${JMFTS_BASE_CHECK_VENV:-$(mktemp -d)/basevenv}"
PYTHON="${PYTHON:-python3}"

echo ">> building an empty venv at $VENV"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

echo ">> installing base jmfts (no extras)"
# jmfts-client first, from this tree. `jmfts` pins `jmfts-client==<this version>`, a name
# that is not on PyPI yet, so the server install would fail resolving it. Installing the
# local copy is also the only honest check: this script asks what THIS source tree
# installs as, and the pin means the local client is the only one that can satisfy it.
"$VENV/bin/pip" install --quiet ./jmfts-client
"$VENV/bin/pip" install --quiet .

echo ">> checking nothing dragged the model stack in"
if "$VENV/bin/pip" list 2>/dev/null | grep -iqE "^(torch|sentence-transformers|nvidia-)"; then
  echo "FAIL: the base install contains the model stack:" >&2
  "$VENV/bin/pip" list | grep -iE "^(torch|sentence-transformers|nvidia-)" >&2
  exit 1
fi
echo "   no torch, no sentence-transformers, no nvidia-*"

# Reuse the developer's Hugging Face cache if there is one, so this does not re-download
# the tokenizer on every run. It is a cache, not a dependency: an empty one just downloads.
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"

echo ">> running the assertions in that venv"
"$VENV/bin/python" - <<'PY'
import sys

import jmfts_core.rest.main      # the whole API, every router
import jmfts_core.worker         # the CLI and the loop
import jmfts_core.ingest_tasks   # every registered task handler, embed included
import jmfts_core.embedder       # the remote embedder

import jmfts_core.probe          # format detection, which must stay stdlib-only
import jmfts_core.office         # the office seam: importing it imports no reader

heavy = sorted(m for m in ("torch", "sentence_transformers") if m in sys.modules)
assert not heavy, f"importing the app pulled in {heavy}"
readers = sorted(m for m in ("docx", "pptx", "openpyxl") if m in sys.modules)
assert not readers, f"importing the app pulled in {readers}"
print("   api + worker + task registry import, with no model stack and no office readers")

import numpy as np

from jmfts_core.config import get_settings
from jmfts_core.embedder import RemoteEmbedder, get_embedder
from jmfts_core.embedding import EmbeddingService, ModelStackNotInstalled
from jmfts_core.task_errors import ErrorType, classify_exception

service = EmbeddingService()

# The half that is base JMFTS: measuring text, and matryoshka truncation.
fit = service.check_fit("Late interaction scores each query token against every token.")
assert fit.token_count > 0 and not fit.truncated
assert service.fits_token_window("hello world") is True
assert len(service.chunk_to_fit("A sentence. " * 60)) >= 1
assert service.truncate_embedding(np.arange(768, dtype=np.float32), 256).shape == (256,)
print(f"   check_fit / chunker / truncate: OK ({fit.token_count} tokens, limit {fit.limit})")

# The half that is not, and how it says so.
for label, call in (
    ("model", lambda: service.model),
    ("embed_text", lambda: service.embed_text("hello")),
    ("embed_with_tokens", lambda: service.embed_with_tokens("hello")),
):
    try:
        call()
    except ModelStackNotInstalled as exc:
        assert "jmfts[embed]" in str(exc), f"{label}: message does not name the extra"
        assert "JMFTS_RUNNER_URL" in str(exc), f"{label}: message does not name the runner"
        assert classify_exception(exc) is ErrorType.PERMANENT, f"{label}: not PERMANENT"
    else:
        raise AssertionError(f"{label} produced a vector with no model installed")
print("   embedding refuses by name, names both ways out, classified PERMANENT")

# And the way out actually selects.
get_settings().runner_url = "http://runner:8100"
get_settings().runner_key = "shared-secret"
embedder = get_embedder()
assert isinstance(embedder, RemoteEmbedder)
assert embedder.fits_token_window("hello world") is True
assert embedder.device == "remote:http://runner:8100"
embedder.close()
print("   JMFTS_RUNNER_URL selects a RemoteEmbedder that still measures locally")

# ---------------------------------------------------------------------------
# The office tiers. docs/OFFICE_SPEC.md Part 1.
# ---------------------------------------------------------------------------

from jmfts_core.office import (
    OfficeStackNotInstalled,
    require_docx,
    require_openpyxl,
    require_pptx,
)
from jmfts_core.probe import detect_format

# Tier 1 works with no readers at all: a base install can still say WHAT a file is.
# These are the ZIP-manifest and OLE2 paths, which is the whole reason the split works.
assert detect_format(b"%PDF-1.7 trailer", filename="a.pdf").format == "pdf"
assert detect_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 32, filename="a.doc").format == "ole2"
print("   probe detects formats with no office reader installed")

# Tier 2 refuses by name.
for label, guard in (("docx", require_docx), ("pptx", require_pptx), ("openpyxl", require_openpyxl)):
    try:
        guard()
    except OfficeStackNotInstalled as exc:
        assert "jmfts[office]" in str(exc), f"{label}: message does not name the extra"
        assert classify_exception(exc) is ErrorType.PERMANENT, f"{label}: not PERMANENT"
    else:
        raise AssertionError(f"{label} imported with no office extra installed")
print("   office readers refuse by name, classified PERMANENT")

print("\nBASE INSTALL OK")
PY

echo ">> installed size"
du -sh "$VENV/lib"/python*/site-packages
