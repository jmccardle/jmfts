"""PELT segmentation for conversations was HERE. ``SPRINT_JOBS.md`` 15.4 S7 removed it.

The module tested ``conversation_ingest.segment_conversation`` — an opt-in, default-off
stage that ran PELT over a conversation's turn embeddings and inserted topic containers,
plus the pipeline wiring that exposed it as ``pipeline_config["segment"]``.

**The capability did not go away; it stopped being the conversation pipeline's.**
``jmfts_core.rollup_tasks.run_structure_semantic`` segments any node wider than
``rollup.max_children``, for every format, at the settling boundary — where it can also
segment containers a previous pass created (``INGEST_SPEC.md`` 11.4's two recursion
directions), which the conversation-only version could not. It is tested in
``tests/test_rollup_pelt.py``, and its effect on a real ingested conversation is
``tests/test_conversation_ingest.py``.

Three of this file's claims are worth naming, because they were about the SHAPE of the
answer rather than about conversations, and each has a successor:

* "PELT ran and found one topic" is ``completed``, not ``skipped`` (3.4). Carried by
  ``run_structure_semantic``'s own outcome, asserted in ``tests/test_rollup_pelt.py``.
* A penalty override suppresses splitting. Now ``rollup.penalty``, an ingest option, so it
  is checked where every option is: ``tests/test_ingest_options.py``.
* Segmentation is opt-in. It no longer is, and that is the deliberate change — a flat node
  with two hundred children gives retrieval no mid-scale representation whatever produced
  it, so the trigger is fan-out rather than a caller's flag (``ROLLUP_PARAMS``).

This file is a marker, not a test module. Delete it once nobody is looking for the tests
it used to hold.
"""
