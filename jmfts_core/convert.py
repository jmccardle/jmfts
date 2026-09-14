"""Driving LibreOffice. The ``convert`` extra, and the only door to tier 3.

``docs/OFFICE_SPEC.md`` Part 1 splits office support into three tiers by what the dependency
actually *is*, and this module is the boundary of the third one. :mod:`jmfts_core.office` is
the same seam one tier down, and the shape here is copied from it deliberately rather than
reinvented: a ``require_*`` at the point of use, an exception that names the deployment you
are in rather than reading as a broken environment, and one install hint spelled once.

**Tier 3 is not an extra, because ``pip install libreoffice`` is not a thing.** LibreOffice
is a ~500 MB system package; the ``convert`` extra installs the unoserver CLIENT and nothing
else, and that client is useless on its own. So there are TWO ways this appliance can be
unable to convert, they are different deployments, and they get different messages:

* :class:`ConvertStackNotInstalled` — the client is not installed. ``pip install
  'jmfts[convert]'``. This is the base install and most of a fleet.
* :class:`ConverterNotConfigured` — the client is here and nothing has said WHERE the
  LibreOffice is. ``JMFTS_CONVERT_URL``, and ``Dockerfile.libreoffice`` is an image that
  serves one.

Both classify PERMANENT, and each reaches that through a different arm of
:func:`jmfts_core.task_errors.classify_exception` — ``ImportError`` for the first, as
``OfficeStackNotInstalled`` and ``ModelStackNotInstalled`` do, and ``ValueError`` for the
second. The bases are chosen for that and not for taxonomy: a package that is not installed
is not installed on the third attempt, and a setting that is blank is blank on the third
attempt, so spending a retry budget on either only delays the moment somebody reads the
message.

**What ``JMFTS_CONVERT_URL`` points at, and how that differs from what Part 6 proposed.**
Part 6 says "the ``JMFTS_CONVERT_URL`` form is a JMFTS worker in front of it, not raw
unoserver, so the task contract stays a JMFTS task contract". Here it is the unoserver
XML-RPC endpoint directly, and the reason is that the task contract is not at risk: the JMFTS
worker claims ``render:pdf``, calls this module, writes the artefact and completes the task,
all inside one process that already holds the queue. Nothing about the task leaves JMFTS. A
JMFTS-in-front-of-unoserver deployment needs a new gated route — the shape ``/runner`` has —
and that is a route this lane may not add; the day it exists, this setting can name it
instead and nothing above this module changes.

**Where the timeout is, because it is not here.** ``unoserver.client.UnoClient`` builds its
own ``ServerProxy`` and offers no timeout hook, and the two ways to add one on this side are
both worse than not having it: ``socket.setdefaulttimeout`` is process-global and would apply
to the database, and re-implementing the client would duplicate the API-version handshake the
extra exists to provide. The bound lives on the SERVER instead — ``unoserver
--conversion-timeout`` terminates LibreOffice and exits when a conversion stops finishing,
which is the better place for it anyway, because the converter is what can tell. What reaches
this module is then a connection error, which ``task_errors.classify_exception`` grades
RETRYABLE, and the task is retried against a restarted container rather than failed.
``Dockerfile.libreoffice`` sets it; a unoserver run without it can hold a worker's task
indefinitely, and that is worth knowing before pointing this at one.
"""

from __future__ import annotations

import logging
import xmlrpc.client
from dataclasses import dataclass
from urllib.parse import urlparse

from jmfts_core.config import get_settings

logger = logging.getLogger(__name__)

#: unoserver's own default port. Used when ``JMFTS_CONVERT_URL`` names a host and no port,
#: which is what a Kubernetes service name looks like.
DEFAULT_CONVERT_PORT = 2003

#: The target extension for a rendition. ``convert_to`` in unoserver's vocabulary is a file
#: extension and not a filter name: the filter is auto-detected from the input document's
#: type, which is what lets one call serve Writer, Calc and Impress.
TARGET_PDF = "pdf"

#: The target extensions ``convert:ooxml`` uses, by the application probe named
#: (:func:`jmfts_core.probe._probe_ole2` emits ``legacy_application``). One entry per legacy
#: binary format, and nothing else: a container that is not one of these is not a document
#: this appliance converts, and guessing an extension for it would hand LibreOffice bytes it
#: was never going to read.
OOXML_TARGET: dict[str, str] = {
    "word": "docx",
    "excel": "xlsx",
    "powerpoint": "pptx",
}

#: What to do about a missing tier 3. One string, because the message is the whole value of
#: the exception and two copies of it would be free to drift — ``office/__init__.py``'s
#: ``_INSTALL_HINT`` says the same thing about the same kind of exception.
#:
#: It names TWO things, where tier 2's names one. Installing the extra is not sufficient
#: here: the wheel is a client, and a client with nothing to talk to converts nothing.
CONVERT_INSTALL_HINT = (
    "This JMFTS cannot convert documents. Converting needs both halves of tier 3:\n"
    "    pip install 'jmfts[convert]'        # the unoserver client\n"
    "    JMFTS_CONVERT_URL=http://host:2003  # a LibreOffice serving unoserver\n"
    "`Dockerfile.libreoffice` builds an image that serves one, and "
    "deploy/k8s/23-worker-convert.yaml runs a pool of JMFTS workers beside it. A worker "
    "that does not render office documents needs neither; see jmfts_core/convert.py."
)


class ConvertStackNotInstalled(ImportError):
    """This install has no unoserver client, and something asked it to convert.

    ``ImportError`` for :class:`jmfts_core.office.OfficeStackNotInstalled`'s reason and with
    its classification: PERMANENT, because a package that is not installed does not appear on
    the third attempt. The deployment this describes is the ordinary one — base JMFTS detects
    and probes office files without any of tier 3, and a storage-side worker that never
    renders is correctly installed and correctly has no unoserver.
    """


class ConverterNotConfigured(ValueError):
    """Nothing has said where the LibreOffice is.

    ``ValueError`` so that ``classify_exception`` grades it PERMANENT through the arm it
    already has for "this is a statement about the input, not about the moment". A blank
    setting is exactly that, and the value of the exception is a message an operator reads
    once rather than one that arrives three backoffs later.
    """


class ConversionFailed(ValueError):
    """The converter answered, and what it answered is not a document.

    ``ValueError``, so PERMANENT, and that is a decision about WHO answered: this is raised
    when LibreOffice ran and refused, or when it returned bytes that are not the format it
    was asked for. Both are facts about these bytes and identical on every attempt.

    Transport failures are deliberately NOT wrapped in this. A refused connection to a
    unoserver that is restarting raises ``ConnectionError``, which ``classify_exception``
    already grades RETRYABLE — and that is the correct answer for it. Wrapping every failure
    in one class would have made a converter's restart look like a document's fault.
    """


@dataclass(frozen=True)
class ConverterTarget:
    """Where the unoserver is, parsed once from the setting."""

    host: str
    port: int
    protocol: str


def require_unoserver():
    """The ``unoserver.client`` module, or say what is missing and what to do."""
    try:
        from unoserver import client
    except ImportError as exc:
        raise ConvertStackNotInstalled(f"{exc}\n\n{CONVERT_INSTALL_HINT}") from exc
    return client


def converter_target() -> ConverterTarget:
    """Where to send bytes, from ``JMFTS_CONVERT_URL``.

    A URL and not a host/port pair, because one setting is one decision and because the
    scheme is the part a deployment gets wrong silently: unoserver speaks XML-RPC over HTTP,
    and a value with no scheme parses into a host of ``""`` with ``urlparse``, which would
    otherwise become a connection attempt to nowhere. It refuses instead, naming the form.
    """
    raw = get_settings().convert_url.strip()
    if not raw:
        raise ConverterNotConfigured(
            "JMFTS_CONVERT_URL is not set, so this appliance has no LibreOffice to convert "
            f"with.\n\n{CONVERT_INSTALL_HINT}"
        )
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConverterNotConfigured(
            f"JMFTS_CONVERT_URL is {raw!r}, which is not an http(s) URL. unoserver speaks "
            "XML-RPC over HTTP, so the value is a scheme, a host and a port: "
            f"`http://libreoffice:{DEFAULT_CONVERT_PORT}`"
        )
    return ConverterTarget(parsed.hostname, parsed.port or DEFAULT_CONVERT_PORT, parsed.scheme)


def renderer_version() -> str:
    """Which LibreOffice is behind the URL, as the deployment declared it.

    ``SPRINT_0_6_0.md`` 4.9 pins 26.8.0 and requires the version on the rendition, so that an
    upgrade re-renders on read instead of invalidating in bulk. **unoserver cannot answer
    this**: its ``info()`` reports the unoserver version and the filter lists, and nothing in
    the XML-RPC surface names the office suite underneath. So the number is declared by the
    image that baked it — ``JMFTS_CONVERT_VERSION``, set in ``Dockerfile.libreoffice`` beside
    the ``apt`` pin it is a statement about.

    Blank is allowed and returns ``""``, which the caller records as an ABSENT key rather
    than as a null: absent means "not measured" everywhere else in this appliance
    (``probe.py``'s ``legacy_application`` says so at its own definition), and a deployment
    that points at an unlabelled LibreOffice has genuinely not measured it. The consequence
    is stated rather than repaired: renditions from such a deployment cannot be compared
    against an upgrade, because there is nothing to compare.
    """
    return get_settings().convert_version.strip()


def convert_bytes(data: bytes, *, to: str) -> bytes:
    """``data``, converted to the ``to`` extension, through the configured unoserver.

    Bytes in, bytes out, and no path on either side. unoserver's client will happily pass a
    filesystem path when it decides the server is local, which is a shared-filesystem
    assumption this appliance cannot make: the bytes live in a Postgres large object and the
    converter is a different container. Passing ``indata`` with no ``outpath`` is the one
    call shape that holds for both.

    ``filtername`` is left unset on purpose. The export filter is auto-detected from the
    input document's own type, which is what makes one function serve Writer, Calc and
    Impress; naming a filter here would mean a table of them per input type, maintained
    against a LibreOffice build this process cannot interrogate.
    """
    client = require_unoserver()
    target = converter_target()
    logger.info("converting %d bytes to %s via %s:%s", len(data), to, target.host, target.port)
    try:
        result = client.UnoClient(
            server=target.host, port=str(target.port), protocol=target.protocol
        ).convert(indata=data, convert_to=to)
    except xmlrpc.client.Fault as exc:
        # LibreOffice answered and refused. A Fault carries the server-side traceback, which
        # is the "converter's stderr in the attempt record" Part 6 asks for.
        raise ConversionFailed(
            f"the converter refused these bytes when asked for {to!r}: {exc.faultString}"
        ) from exc
    if not result:
        # The client returns None when it wrote to a path, and b"" is a converter that
        # produced nothing. Neither is a document, and storing either would make an empty
        # rendition indistinguishable from a page with nothing on it.
        raise ConversionFailed(
            f"the converter returned no bytes when asked for {to!r}; there is no document to "
            "store, and an empty artefact would be indistinguishable from a blank page"
        )
    return bytes(result)
