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
# docker-compose bind-mounts jmfts_core/ and api/ over these, so edits are live.
COPY pyproject.toml ./
COPY jmfts_core ./jmfts_core
COPY api ./api
RUN pip install --no-cache-dir -e .

EXPOSE 8100
# Overridden by compose (adds --reload); kept so the image is runnable standalone.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8100"]
