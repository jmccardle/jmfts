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
# The other two distributions, installed FIRST, for the reason `jmfts` pins both with `==`:
# one number, three wheels, one tag. An editable install of `jmfts` resolves those pins
# against PyPI and shadows the trees beside this file with whatever the last release was —
# which is the opposite of what a development image is for. Installing the local copies
# satisfies both pins before pip reads them.
#
# (An earlier version of this comment said the reason was that `jmfts-client` "is not on
# PyPI yet". That stopped being true at 0.2.0. The pin is the reason, and it always was.)
COPY jmfts-client ./jmfts-client
RUN pip install --no-cache-dir -e ./jmfts-client
# The front end. It carries built static files and one module that says where they are, with
# no dependencies of its own, so this line costs the image nothing but the bundle. Without it
# `jmfts_core.rest.main` starts normally and serves no /app — which is correct for a worker
# and wrong for the stack the quickstart tells a first-time reader to run.
COPY jmfts-web ./jmfts-web
RUN pip install --no-cache-dir -e ./jmfts-web
# `[embed]` because a dev appliance both embeds and searches, and search embeds its query
# locally whatever JMFTS_RUNNER_URL says. Base JMFTS does NOT install torch any more — it
# assumes external embedding — so an image that serves /search or /runner has to ask for
# the model stack by name. `[web]` is named here rather than left implicit: the editable
# install above already satisfies its pin, so this declares the dependency without
# resolving anything. See pyproject.toml.
RUN pip install --no-cache-dir -e ".[embed,web]"

EXPOSE 8100
# Overridden by compose (adds --reload); kept so the image is runnable standalone.
CMD ["uvicorn", "jmfts_core.rest.main:app", "--host", "0.0.0.0", "--port", "8100"]
