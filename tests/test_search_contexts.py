"""Tests for search contexts (usetype presets) feature.

The wildcard matching logic and ``resolve_params`` behaviour.

**These used to be inline copies of the functions under test**, pasted in "to avoid
triggering the full model import chain which has a pre-existing pgvector compat issue in
.venv". Both halves of that stopped being true, and the second one cost something: the copy
of ``_usetype_to_like`` agreed with the original exactly, so when a preset spelled
``"transcript:*,obsidian:*"`` was found to match nothing, eight passing tests said the
translator was fine. A copy can only confirm the behaviour it was copied from. Every test
here now imports the shipped function, and the set-of-globs behaviour those tests could not
have caught is covered in ``tests/test_usetype_filter.py``.
"""

import pytest

from jmfts_client.contracts.search import usetype_globs
from jmfts_core.repositories.search import (
    _usetype_has_wildcard,
    _usetype_matches,
    _usetype_to_like,
)


def _resolve_params(config: dict, overrides: dict) -> dict:
    """Mirrors SearchContextRepository.resolve_params logic."""
    params = dict(config)
    for key, value in overrides.items():
        if value is not None:
            params[key] = value
    return params


# ============================================================================
# Wildcard usetype matching
# ============================================================================


class TestUsetypeHasWildcard:
    def test_star_wildcard(self):
        assert _usetype_has_wildcard("conversation/*") is True

    def test_question_wildcard(self):
        assert _usetype_has_wildcard("doc?") is True

    def test_no_wildcard(self):
        assert _usetype_has_wildcard("exact_type") is False

    def test_empty_string(self):
        assert _usetype_has_wildcard("") is False


class TestUsetypeToLike:
    """ONE glob in, one LIKE pattern out. Splitting a filter is ``usetype_globs``."""

    def test_star_to_percent(self):
        assert _usetype_to_like("conversation/*") == "conversation/%"

    def test_question_to_underscore(self):
        assert _usetype_to_like("doc?") == "doc_"

    def test_no_wildcard_passthrough(self):
        assert _usetype_to_like("exact") == "exact"

    def test_escapes_sql_percent(self):
        assert _usetype_to_like("100%done") == r"100\%done"

    def test_escapes_sql_underscore(self):
        assert _usetype_to_like("my_type") == r"my\_type"

    def test_combined_escaping_and_wildcard(self):
        assert _usetype_to_like("my_type/*") == r"my\_type/%"

    def test_double_star(self):
        assert _usetype_to_like("**") == "%%"

    def test_multiple_wildcards(self):
        assert _usetype_to_like("a/*/b/*") == "a/%/b/%"

    def test_a_comma_is_not_this_function_s_business(self):
        """The defect, stated where it happened.

        A filter naming two globs never reaches here whole any more — ``usetype_globs``
        splits it first. This asserts what the translator does if one ever did, so that the
        pattern below is read as a symptom rather than as intended behaviour.
        """
        assert _usetype_to_like("transcript:*,obsidian:*") == "transcript:%,obsidian:%"
        assert usetype_globs("transcript:*,obsidian:*") == ("transcript:*", "obsidian:*")


class TestUsetypeMatches:
    """The BM25 post-filter. Takes the normalised globs, and matches ANY of them."""

    @staticmethod
    def _match(usetype_filter, value) -> bool:
        return _usetype_matches(usetype_globs(usetype_filter), value)

    def test_exact_match(self):
        assert self._match("conversation", "conversation") is True

    def test_exact_mismatch(self):
        assert self._match("conversation", "notes") is False

    def test_star_matches_suffix(self):
        assert self._match("conversation/*", "conversation/session") is True
        assert self._match("conversation/*", "conversation/summary") is True

    def test_star_no_match_different_prefix(self):
        assert self._match("conversation/*", "notes/daily") is False

    def test_star_matches_multi_level(self):
        # fnmatch * matches any characters including /
        assert self._match("conversation/*", "conversation/a/b") is True

    def test_question_single_char(self):
        assert self._match("doc?", "docs") is True
        assert self._match("doc?", "doc1") is True

    def test_question_too_many_chars(self):
        assert self._match("doc?", "document") is False

    def test_none_value(self):
        assert self._match("conversation/*", None) is False

    def test_star_at_beginning(self):
        assert self._match("*/summary", "conversation/summary") is True
        assert self._match("*/summary", "notes/summary") is True
        assert self._match("*/summary", "notes/detail") is False

    def test_a_set_of_globs_matches_any_of_them(self):
        assert self._match("transcript:*,obsidian:*", "transcript:daily") is True
        assert self._match("transcript:*,obsidian:*", "obsidian:note") is True
        assert self._match("transcript:*,obsidian:*", "wiki:url") is False


# ============================================================================
# SearchContext resolve_params logic
# ============================================================================


class TestResolveParams:
    def test_context_provides_defaults(self):
        params = _resolve_params(
            {"usetype": "conversation/*", "method": "vector", "limit": 20},
            {},
        )
        assert params["usetype"] == "conversation/*"
        assert params["method"] == "vector"
        assert params["limit"] == 20

    def test_overrides_take_precedence(self):
        params = _resolve_params(
            {"usetype": "conversation/*", "limit": 20},
            {"usetype": "notes", "limit": 5},
        )
        assert params["usetype"] == "notes"
        assert params["limit"] == 5

    def test_none_overrides_are_ignored(self):
        params = _resolve_params(
            {"usetype": "conversation/*", "limit": 20},
            {"usetype": None, "parent_id": None},
        )
        assert params["usetype"] == "conversation/*"
        assert params["limit"] == 20

    def test_extra_overrides_added(self):
        params = _resolve_params(
            {"usetype": "notes"},
            {"parent_id": 42},
        )
        assert params["usetype"] == "notes"
        assert params["parent_id"] == 42

    def test_empty_config_returns_overrides(self):
        params = _resolve_params({}, {"usetype": "notes", "limit": 5})
        assert params == {"usetype": "notes", "limit": 5}

    def test_empty_both(self):
        params = _resolve_params({}, {})
        assert params == {}

    def test_a_stored_config_may_hold_either_spelling(self):
        """A ``search_contexts.config`` blob passes through no contract, so both forms
        reach the repository exactly as stored and normalise there."""
        comma = _resolve_params({"usetype": "transcript:*,obsidian:*"}, {})
        listed = _resolve_params({"usetype": ["transcript:*", "obsidian:*"]}, {})
        assert usetype_globs(comma["usetype"]) == usetype_globs(listed["usetype"])

    def test_a_stored_config_that_names_nothing_is_refused(self):
        params = _resolve_params({"usetype": ""}, {})
        with pytest.raises(ValueError):
            usetype_globs(params["usetype"])
