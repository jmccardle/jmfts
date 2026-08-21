"""In-process thread-safety of the lazy-init singletons and the embed lock.

These exercise ROADMAP "Concurrency & thread safety" Axis-A fixes #2 and #3:
- the four ``get_*`` singletons are double-checked so a concurrent first-touch builds
  exactly one instance, and
- ``EmbeddingService.model`` loads exactly once even under a hammering thread burst
  (the double-load hazard) — proven with a counting fake model, so no real weights load.

No database required; these are pure in-process concurrency tests.
"""

import threading

import pytest


def _hammer(fn, n_threads=32):
    """Call ``fn()`` from ``n_threads`` threads released simultaneously; collect results."""
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads
    errors = []

    def worker(i):
        try:
            barrier.wait()  # maximise contention on the first-touch window
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"worker(s) raised: {errors}"
    return results


def test_get_embedding_service_singleton_under_threads():
    import jmfts_core.embedding as emb

    emb._embedding_service = None  # force the first-touch race
    try:
        results = _hammer(emb.get_embedding_service)
        first = results[0]
        assert first is not None
        assert all(r is first for r in results), "double-checked lock built >1 service"
    finally:
        emb._embedding_service = None


def test_get_token_selector_singleton_under_threads():
    import jmfts_core.token_selection as ts

    ts._default_selector = None
    try:
        results = _hammer(ts.get_token_selector)
        first = results[0]
        assert first is not None
        assert all(r is first for r in results), "double-checked lock built >1 selector"
    finally:
        ts._default_selector = None


def test_get_engine_singleton_under_threads():
    # create_engine() does not connect, so this needs no live database.
    import jmfts_core.database as db

    saved = db._engine
    db._engine = None
    try:
        results = _hammer(db.get_engine)
        first = results[0]
        assert first is not None
        assert all(r is first for r in results), "double-checked lock built >1 engine"
    finally:
        db._engine = saved


def test_model_loads_exactly_once_under_thread_burst(monkeypatch):
    """The ``model`` property must load the weights once even when many threads race
    to first-touch it — the double-load hazard. A counting, slightly-slow fake model
    stands in for SentenceTransformer so no real weights are loaded.

    The stand-in goes onto ``sentence_transformers`` rather than onto
    ``jmfts_core.embedding``, because the import is now inside the property — the module
    holds no ``SentenceTransformer`` attribute to replace. That laziness is deliberate: a
    process embedding through another JMFTS's ``/runner`` surface must not import torch
    just to reach the tokenizer (see ``jmfts_core/embedder.py``).
    """
    import sentence_transformers

    import jmfts_core.embedding as emb

    load_count = {"n": 0}
    count_lock = threading.Lock()

    class _FakeModel:
        def __init__(self, *args, **kwargs):
            with count_lock:
                load_count["n"] += 1
            # Widen the race window so an unlocked check-then-set would double-load.
            _busy_wait(0.02)

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeModel)

    service = emb.EmbeddingService()  # nothing loaded yet (lazy)
    models = _hammer(lambda: service.model, n_threads=16)

    assert load_count["n"] == 1, f"model loaded {load_count['n']} times (double-load)"
    first = models[0]
    assert all(m is first for m in models)


def _busy_wait(seconds: float):
    """Sleep without importing time at module top (keep the fake constructor tiny)."""
    import time

    time.sleep(seconds)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
