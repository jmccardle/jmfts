"""Error classification for queued ingest tasks. ``INGEST_SPEC.md`` 3.4 and Part 5.

Ported from triskelion's ``vdo_core/task_error_handling.py``. The reason the port keeps
the enum rather than inventing one is spec 3.4's: *retry policy is decided by
classification, not by a string match on the message*. A worker that decides whether to
retry by looking for ``"timeout"`` in ``str(exc)`` re-decides the policy at every call
site, gets it subtly different each time, and cannot be tested without constructing the
exact exception text.

The retry policy itself is NOT here — it is in
:meth:`jmfts_core.repositories.task_queue.TaskQueueRepository.fail`, one place, in
Python. Triskelion put it in a plpgsql ``mark_task_failed`` function; that split the
queue's behaviour across two languages and two files with a thin Python wrapper doing
nothing but ``SELECT mark_task_failed(...)``, and its backoff read columns it then never
used. See migration 010's header for the full note.

Three deliberate departures from the source classifier:

* it imported ``requests`` **inside the function**; JMFTS speaks ``httpx``, so the
  ``requests.exceptions.*`` arms would have matched nothing and the import would have
  failed outright on a machine without it;
* it mapped connection failures to ``TIMEOUT``. Behaviourally identical under the retry
  policy (both retry), but it makes the error_type column lie about what happened, and
  that column exists to be read. Connection failures are ``RETRYABLE`` here;
* it mapped ``KeyError`` / ``AttributeError`` to ``PERMANENT``. Those are overwhelmingly
  bugs in our own extractor rather than facts about the document, and a permanent
  failure needs a human to requeue it. That is the correct outcome — a bug does not get
  better on retry, and burning three retries on it hides it — so the mapping is kept,
  and it is called out here because it means "fix the code, then re-ingest".
"""

from __future__ import annotations

import sqlalchemy.exc
from enum import Enum

import httpx


class ErrorType(str, Enum):
    """Why a task failed, which is what decides whether it runs again.

    ``str`` mixin so the value goes straight into the ``VARCHAR`` column and compares
    equal to the string the database CHECK constraint pins.
    """

    #: Transient. The same call may well work in a minute — service unavailable,
    #: connection refused, a lock we lost. Retried with backoff.
    RETRYABLE = "retryable"

    #: Will never succeed as-is: malformed input, a limit exceeded, a bug. No retry.
    #: A node whose task failed this way goes to ``settled = 'failed'`` (spec 2.1).
    PERMANENT = "permanent"

    #: Exceeded a time limit. Retried with backoff, kept distinct from RETRYABLE so
    #: "this model is too slow for this document" is legible in the stats.
    TIMEOUT = "timeout"

    #: A prerequisite is missing or a parent task failed. No retry: retrying the
    #: dependent before the dependency is fixed just burns the retry budget.
    DEPENDENCY = "dependency"


#: The classifications that schedule another attempt. Everything else is terminal.
RETRYABLE_ERROR_TYPES: frozenset[ErrorType] = frozenset({ErrorType.RETRYABLE, ErrorType.TIMEOUT})


def classify_exception(exception: BaseException) -> ErrorType:
    """Map an exception to the retry policy it should get.

    The default is :attr:`ErrorType.RETRYABLE`, matching the source. That is the
    conservative direction for an *unrecognised* exception: the cost of a wrong guess is
    at most ``max_retries`` extra attempts and a failed task at the end, whereas guessing
    PERMANENT would strand a task that a retry would have completed. It is a bounded
    default, not a swallowed error — the exception text and the classification both land
    on the task row and in the node's attempt log either way.
    """
    # Timeouts first: httpx.TimeoutException is a subclass of httpx.TransportError, so
    # the order of these two arms is what keeps a timeout from reading as a connection
    # failure.
    if isinstance(exception, (httpx.TimeoutException, TimeoutError, sqlalchemy.exc.TimeoutError)):
        return ErrorType.TIMEOUT

    # Transport-level HTTP failures and pool/connection errors from the database.
    if isinstance(
        exception,
        (httpx.TransportError, ConnectionError, sqlalchemy.exc.OperationalError),
    ):
        return ErrorType.RETRYABLE

    # A 5xx may recover; a 4xx will not. httpx only raises this from
    # `raise_for_status()`, so the response is always attached.
    #
    # 429 IS THE EXCEPTION, and it is not a special case so much as the one 4xx that is not
    # a statement about the request. "You asked correctly, just not now" is the normal
    # backpressure signal of every metered LLM API, and a worker whose whole job is passing
    # jobs to one will meet it routinely. Classified PERMANENT it would burn the retry
    # budget instantly and settle the node 'failed' during an ordinary traffic spike — a
    # tree marked permanently broken because somebody else was busy. The existing
    # exponential backoff is exactly the right response.
    #
    # 408 (Request Timeout) joins it for the same reason, and is given TIMEOUT so it reads
    # in the attempt log as what it is.
    if isinstance(exception, httpx.HTTPStatusError):
        status_code = exception.response.status_code
        if status_code == 408:
            return ErrorType.TIMEOUT
        if status_code == 429 or status_code >= 500:
            return ErrorType.RETRYABLE
        return ErrorType.PERMANENT

    # Data and programming errors. See the module docstring: this includes our own bugs
    # on purpose, because retrying a bug three times only delays noticing it.
    #
    # ImportError joins them for the same reason with a sharper edge: a package that is not
    # installed is not installed on the third attempt either. The live case is
    # `embedding.ModelStackNotInstalled` — a worker built without the `embed` extra and
    # given no JMFTS_RUNNER_URL claiming an `embed` task — and its whole value is a message
    # saying which of the two is missing. Left RETRYABLE, that message would arrive three
    # backoffs later, with the node parked in the meantime.
    if isinstance(exception, (ValueError, TypeError, KeyError, AttributeError, ImportError)):
        return ErrorType.PERMANENT

    # The SAME RULE, for the database's way of saying it. `DataError` is "these bytes
    # cannot go in this column" and `IntegrityError` is "this row breaks a constraint" —
    # both are statements about the value, decided by the value, and identical on every
    # attempt. Left to the RETRYABLE default they burn the whole retry budget before the
    # node reaches its terminal state, and everything that depends on that task waits for
    # it. Measured: a PDF whose text layer carried a NUL took three 22-second attempts to
    # fail at a `jsonb` write that could not have gone any other way
    # (`docs/STRESS_CORPUS.md` 4.2).
    #
    # `OperationalError` is deliberately NOT here — it is above, as RETRYABLE, because it
    # covers a lost connection and a lock timeout, which are facts about the moment rather
    # than about the row. `ProgrammingError` is not here either: it is our own malformed
    # SQL, which the `DatabaseError` base would sweep up, and keeping it out means this
    # arm names only the two the DATA decides.
    if isinstance(exception, (sqlalchemy.exc.DataError, sqlalchemy.exc.IntegrityError)):
        return ErrorType.PERMANENT

    # A full disk is the one OSError worth retrying — it is the only one an operator
    # can clear without changing the input. Everything else (missing file, permission
    # denied, bad descriptor) is a fact about the environment that a retry repeats.
    if isinstance(exception, OSError):
        return ErrorType.RETRYABLE if "No space left" in str(exception) else ErrorType.PERMANENT

    return ErrorType.RETRYABLE
