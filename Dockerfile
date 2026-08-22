# JMFTS development image — CPU only.
#
# Deliberately not a production build: embedding runs on CPU (slow, fine for dev),
# and the source tree is bind-mounted with `uvicorn --reload` by docker-compose so
# code edits reload live without a rebuild. See docker-compose.yml.
FROM python:3.11-slim

# libgomp1: the OpenMP runtime torch and python-igraph load at import time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch FIRST, from PyTorch's CPU wheel index, so the `pip install`
# below sees torch>=2.1.0 already satisfied and never drags in the multi-GB
# CUDA build (useless here — this image has no GPU).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Source is copied for the editable install to resolve at build time; at runtime
# docker-compose bind-mounts jmfts_core/ over this, so edits are live.
COPY pyproject.toml ./
COPY jmfts_core ./jmfts_core
# The second distribution, installed first. `jmfts` declares `jmfts-client>=0.1.0`, and
# that name is not on PyPI yet, so the install below would fail resolving it. Installing
# the copy in this tree satisfies the requirement before pip reads it, and is what you
# want regardless: the image should carry the contracts this source tree defines.
COPY jmfts-client ./jmfts-client
RUN pip install --no-cache-dir -e ./jmfts-client
# `[embed]` because a dev appliance both embeds and searches, and search embeds its query
# locally whatever JMFTS_RUNNER_URL says. Base JMFTS does NOT install torch any more — it
# assumes external embedding — so an image that serves /search or /runner has to ask for
# the model stack by name. See pyproject.toml.
RUN pip install --no-cache-dir -e ".[embed]"

EXPOSE 8100
# Overridden by compose (adds --reload); kept so the image is runnable standalone.
CMD ["uvicorn", "jmfts_core.rest.main:app", "--host", "0.0.0.0", "--port", "8100"]
