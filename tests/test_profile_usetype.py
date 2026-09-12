"""The sheet profile is retrievable, which ``INGEST_SPEC.md`` 8.5 always said it was.

8.5: *"It is embedded and retrievable like any other node"*, and *"a retrieval hit on the
profile tells the agent which columns exist, which ones are closed sets, and which one
identifies a row"*. The node carried ``usetype='summary'``, and ``summary`` is in both
``search_exclude_usetypes`` and ``bm25_exclude_usetypes`` — so it was retrievable by no
method at all. These are the entry conditions for splitting it out as ``profile``.

The exclusion of ``summary`` is NOT the bug and is not touched. It is right for what else
carries the string: a ``summarize:tree`` node's vector is byte-identical to its source
node's on the reference corpus, 13,755 pairs out of 13,755, so admitting them would return
every answer twice.
"""

from __future__ import annotations

import pytest

from jmfts_core.config import get_settings
from jmfts_core.models.document import USETYPE_PROFILE, USETYPE_SUMMARY


class TestTheTwoUsetypesAreSeparate:
    def test_a_profile_is_not_a_summary(self):
        assert USETYPE_PROFILE != USETYPE_SUMMARY

    def test_a_profile_is_held_out_of_no_result_set(self):
        """The whole point of the split. If this ever fails, 8.5's sentence is false again."""
        settings = get_settings()
        assert USETYPE_PROFILE not in settings.search_exclude_usetypes
        assert USETYPE_PROFILE not in settings.bm25_exclude_usetypes

    def test_a_summary_is_still_held_out_of_both(self):
        """Not collateral damage from the split — the reason the split was the fix rather
        than a shorter exclusion list."""
        settings = get_settings()
        assert USETYPE_SUMMARY in settings.search_exclude_usetypes
        assert USETYPE_SUMMARY in settings.bm25_exclude_usetypes


class TestTheScheduleFollowsTheUsetype:
    def test_profile_sheet_declares_it_writes_a_profile(self):
        """``fanout`` names the usetype the handler writes, and EXPLAIN reads that
        declaration rather than the handler. A declaration naming the old string would make
        every prediction about a workbook wrong by one node."""
        from jmfts_core.atoms import ATOMS
        from jmfts_core.ingest_tasks import TASK_PROFILE_SHEET

        assert ATOMS[TASK_PROFILE_SHEET].fanout.counts == USETYPE_PROFILE

    def test_the_embed_row_still_reaches_the_profile_node(self):
        """`embed`'s scope names usetypes explicitly, so a renamed node silently stops being
        embedded — and an unembedded profile is invisible for a second reason, having just
        been made visible for the first."""
        from jmfts_core.ingest_tasks import TASK_EMBED, TASK_ROWS

        row = next(r for r in TASK_ROWS if r.task == TASK_EMBED)
        assert USETYPE_PROFILE in row.scope.usetypes

    def test_no_task_row_still_names_the_summary_usetype_for_a_sheet(self):
        """A leftover would schedule against a node that no longer exists under that name."""
        from jmfts_core.ingest_tasks import TASK_ROWS

        for row in TASK_ROWS:
            usetypes = getattr(row.scope, "usetypes", None) or ()
            if USETYPE_SUMMARY in usetypes:
                pytest.fail(f"{row.task} still scopes to usetype 'summary'")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
