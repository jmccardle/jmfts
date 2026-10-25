"""Where a job lives: a local directory, or an Azure blob container reached by SAS URL.

Standard library only, on purpose. A fresh VM reads its job spec and source tarball from
the store BEFORE anything is pip-installed, so the bootstrap has to be able to import this
with a bare ``python3``.

The calls a job needs and no more:

=====================  ====================================================================
``put``/``put_file``   write, replacing
``get``                read, or None if absent
``get_tagged``         read with a version tag, for compare-and-swap
``create``             write only if absent — the shard claim; True if this call made it
``replace_if``         write only if the version tag still matches — taking over a claim
``list``               keys under a prefix
=====================  ====================================================================

``create`` is the scheduler. On a directory it is ``O_CREAT | O_EXCL``; on a blob it is a
PUT with ``If-None-Match: *``, which Azure answers 409 when the blob exists. Both are atomic,
so two hosts claiming one shard cannot both win.
"""

from __future__ import annotations

import fcntl
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Union

_BLOB_API_VERSION = "2021-08-06"
_RETRIES = 4


class Store:
    """The interface. See the module docstring for what each call promises."""

    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def put_file(self, key: str, path: Union[str, Path]) -> None:
        self.put(key, Path(path).read_bytes())

    def get(self, key: str) -> Optional[bytes]:
        tagged = self.get_tagged(key)
        return None if tagged is None else tagged[0]

    def get_tagged(self, key: str) -> Optional[tuple[bytes, str]]:
        raise NotImplementedError

    def create(self, key: str, data: bytes) -> bool:
        raise NotImplementedError

    def replace_if(self, key: str, data: bytes, tag: str) -> bool:
        raise NotImplementedError

    def list(self, prefix: str = "") -> list[str]:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


def open_store(location: str) -> Store:
    """A store from its location string: an ``https://`` container URL, or a directory."""
    if location.startswith("https://"):
        return BlobStore(location)
    return LocalStore(location)


# ---------------------------------------------------------------------------
# A directory
# ---------------------------------------------------------------------------


class LocalStore(Store):
    def __init__(self, root: Union[str, Path]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError(f"key {key!r} escapes the store root {self.root}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get_tagged(self, key: str) -> Optional[tuple[bytes, str]]:
        path = self._path(key)
        try:
            data = path.read_bytes()
            stat = path.stat()
        except FileNotFoundError:
            return None
        return data, f"{stat.st_mtime_ns}-{stat.st_size}"

    def create(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return True

    def replace_if(self, key: str, data: bytes, tag: str) -> bool:
        path = self._path(key)
        lock = path.with_name(f".{path.name}.lock")
        with open(lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            current = self.get_tagged(key)
            if current is None or current[1] != tag:
                return False
            # A rewrite inside the same mtime tick would keep the tag; wait one out so the
            # next compare-and-swap sees a change.
            time.sleep(0.002)
            self.put(key, data)
            return True

    def list(self, prefix: str = "") -> list[str]:
        out = []
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)

    def describe(self) -> str:
        return str(self.root)


# ---------------------------------------------------------------------------
# An Azure blob container, by SAS URL
# ---------------------------------------------------------------------------


class BlobStore(Store):
    """A container URL carrying a SAS query string, e.g. from ``backends.azure``.

    The SAS is the whole credential: whoever holds the URL can do what its permissions say
    until it expires, which is why the Azure backend mints one per job with an expiry a
    little past the job's own deadline.
    """

    def __init__(self, container_url: str):
        parts = urllib.parse.urlsplit(container_url)
        self._scheme, self._host = parts.scheme, parts.netloc
        self._container = parts.path.rstrip("/")
        self._sas = parts.query

    def _url(self, key: str = "", extra: str = "") -> str:
        path = self._container + ("/" + urllib.parse.quote(key, safe="/") if key else "")
        query = "&".join(q for q in (extra, self._sas) if q)
        return urllib.parse.urlunsplit((self._scheme, self._host, path, query, ""))

    def _request(self, method: str, url: str, data=None, headers=None, length=None):
        """One call, retried on a dropped connection or a 5xx. Returns (status, headers, body).

        4xx is returned, not raised: 404, 409 and 412 are answers this module branches on.
        """
        headers = {"x-ms-version": _BLOB_API_VERSION, **(headers or {})}
        if length is not None:
            headers["Content-Length"] = str(length)
        for attempt in range(_RETRIES):
            if hasattr(data, "seek"):
                data.seek(0)
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == _RETRIES - 1:
                    return exc.code, exc.headers, exc.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == _RETRIES - 1:
                    raise
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def _put(self, key: str, data, length: int, condition: Optional[dict] = None) -> int:
        headers = {"x-ms-blob-type": "BlockBlob", **(condition or {})}
        status, _, body = self._request("PUT", self._url(key), data, headers, length)
        if status not in (201, 409, 412):
            raise OSError(f"PUT {key}: HTTP {status}: {body[:300]!r}")
        return status

    def put(self, key: str, data: bytes) -> None:
        self._put(key, data, len(data))

    def put_file(self, key: str, path: Union[str, Path]) -> None:
        path = Path(path)
        with open(path, "rb") as fh:
            self._put(key, fh, path.stat().st_size)

    def get_tagged(self, key: str) -> Optional[tuple[bytes, str]]:
        status, headers, body = self._request("GET", self._url(key))
        if status == 404:
            return None
        if status != 200:
            raise OSError(f"GET {key}: HTTP {status}: {body[:300]!r}")
        return body, headers["ETag"]

    def create(self, key: str, data: bytes) -> bool:
        return self._put(key, data, len(data), {"If-None-Match": "*"}) == 201

    def replace_if(self, key: str, data: bytes, tag: str) -> bool:
        return self._put(key, data, len(data), {"If-Match": tag}) == 201

    def list(self, prefix: str = "") -> list[str]:
        keys, marker = [], ""
        while True:
            extra = "restype=container&comp=list&prefix=" + urllib.parse.quote(prefix, safe="")
            if marker:
                extra += "&marker=" + urllib.parse.quote(marker, safe="")
            status, _, body = self._request("GET", self._url(extra=extra))
            if status != 200:
                raise OSError(f"LIST {prefix}: HTTP {status}: {body[:300]!r}")
            root = ET.fromstring(body)
            keys.extend(e.text for e in root.iter("Name") if e.text)
            marker = (root.findtext("NextMarker") or "").strip()
            if not marker:
                return sorted(keys)

    def describe(self) -> str:
        return f"{self._scheme}://{self._host}{self._container}"
