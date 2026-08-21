"""Which worker should run which task type. The ``service_badge`` policy.

``claim_next`` has had the routing mechanism since migration 010: a badged worker claims
its own badge plus un-badged work, an un-badged worker claims anything. What it has never
had is a POLICY — something that says which task types are worth routing. This module is
that policy, and it is deliberately small: a map from task type to badge, and a lookup
``enqueue`` consults when the caller does not name a badge itself.

WHAT THE ROUTING IS FOR. Ingest work is not uniform. Measured end-to-end over a real
directory (``scripts/e2e_ingest_corpus.py``, 5 files, 559 nodes, CPU embedding):

    task_type            runs   total_s   mean_s   share of wall clock
    structure:declared      5     153.6    30.72                  54%
    summarize               5     128.5    25.69                  46%
    extract:text            5       0.1     0.01                0.04%
    probe                   5       0.02    0.02                0.03%

Two task types were essentially all of it, and they were exactly the two that embedded.
``structure:declared`` was not a text-splitting task that happened to be slow: it created
every chunk with ``auto_embed=True``, so its 30 seconds were N transformer forward passes
with ``output_attentions=True``, run in a loop inside one claimed task.

**That embedding is now its own task type** (``embed``,
:data:`~jmfts_core.ingest_tasks.TASK_EMBED`), which is what makes this table's conclusion
finally match its own reasoning. The split is not a guess about "heavy" and "light" work
and never was — it is: does the handler run the embedding model. Now exactly two do:

* ``embed`` — one node's vectors, one forward pass, once per chunk. This is the bulk
  embedding in JMFTS and it is what a GPU is for. The 54% above is this, redistributed
  over as many tasks as the document has chunks, which is also why it now parallelises.
* ``summarize`` — one vector per interior node, plus the fit check that decides whether
  ``summarize:llm`` is needed.

Everything else is off the model. ``probe`` inspects the file; ``extract:text`` writes
text and says outright that the file node is not embedded there; the structure rungs now
split text and enqueue, which is string work and INSERTs; ``structure:semantic`` is PELT
over embeddings that already exist.

WHY THE DEFAULT IS NO BADGES AT ALL. An empty policy reproduces today's behaviour exactly,
and that is the safe default rather than the timid one. Badge semantics are asymmetric: a
``cpu``-badged worker will NOT claim ``gpu``-badged work. A cluster that applies the GPU
policy but runs no GPU worker therefore does not run slowly — it STALLS, permanently and
silently, with the tasks sitting ``pending`` and every affected node's ``settled`` stuck
in flight. Nothing in the queue can tell the difference between "the GPU worker is busy"
and "there is no GPU worker", because a worker that does not exist does not report in.

Turning the policy on is therefore a statement that the deployment HAS the workers it
names, and it belongs where that is known — the deployment manifest — not in a library
default. ``deploy/k8s/`` sets it and defines both worker pools in the same directory.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from jmfts_core.config import get_settings

#: "The caller did not say" — apply the policy. Distinct from ``None``, which means "leave
#: this task un-badged whatever the policy says" and is what Part 7's review tasks need,
#: since a task claimed by a person must never be routed at a machine pool.
#:
#: A plain ``None`` default cannot express both, and the difference is not cosmetic: with
#: one default, every task enqueued through a planner that merely failed to mention a badge
#: would silently opt out of routing. That is exactly what happened the first time this
#: shipped — ``settling.TaskSpec`` defaulted its field to ``None``, so every structure and
#: summarize task the settle walk created came out un-badged and the GPU pool never saw
#: any of the work it exists for.
BADGE_FROM_POLICY: Any = object()

#: What an enqueue caller may pass: a badge, ``None`` for deliberately un-badged, or the
#: sentinel for "apply the policy".
BadgeRequest = Union[str, None, Any]

#: Badge for the task types that run the embedding model. See the module docstring for the
#: measurement behind the membership of this map.
#:
#: Offered as a named constant rather than as a default so a deployment copies it
#: deliberately: applying it commits the cluster to running a worker that answers to
#: ``gpu``, and a cluster that does not stalls.
BADGE_EMBED = "embed"

#: The LLM pool. Named for the RESOURCE, not for the hardware, because the two deployments
#: that serve it look nothing alike: a host running a model locally on a GPU, and a light
#: process that forwards to a metered web API and needs no accelerator at all. A worker
#: answers this badge if it can get a completion, however it gets one.
BADGE_LLM = "llm"

#: Embedding work: the handlers that run the embedding model. See the measurement above.
#:
#: The structure rungs used to be in here and are deliberately NOT any more. They earned
#: their badge by embedding inline, and once ``embed`` became its own task they stopped —
#: leaving them badged would pin text splitting and tree INSERTs to the scarce pool, which
#: is the opposite of what routing them was for. This is the map getting SMALLER as the
#: policy gets sharper.
EMBEDDING_POLICY: dict[str, str] = {
    "embed": BADGE_EMBED,
    "summarize": BADGE_EMBED,
}

#: Adds the LLM half. ``summarize`` stays on the embedding pool — it always embeds, and it
#: only DECIDES whether an LLM is needed — while ``summarize:llm``, the task it defers to
#: when the concatenation overflows the window, goes to the LLM pool.
#:
#: Splitting these is what makes routing by cost possible at all. Before the split there
#: was one badge for a handler that needed two different scarce resources, and no way to
#: name the expensive one without also claiming the cheap one.
EMBEDDING_AND_LLM_POLICY: dict[str, str] = {
    **EMBEDDING_POLICY,
    "summarize:llm": BADGE_LLM,
}


def badge_for(task_type: str) -> Optional[str]:
    """The badge this task type should carry, or None to leave it un-badged.

    Reads ``Settings.task_badges``, which is empty unless a deployment sets
    ``JMFTS_TASK_BADGES``. An unknown task type is un-badged, which is the claimable-by-
    anyone case: a task type nobody has routed should still run.
    """
    return get_settings().task_badges.get(task_type)


def resolve_badge(task_type: str, requested: BadgeRequest) -> Optional[str]:
    """The badge to store, given what the caller asked for.

    One function rather than the same conditional at each enqueue site, because the two
    sites are reached by different paths — a direct enqueue and the settle walk's planner —
    and the failure mode when they disagree is silent: the tasks simply come out un-badged
    and the pool they were meant for idles.
    """
    return badge_for(task_type) if requested is BADGE_FROM_POLICY else requested
