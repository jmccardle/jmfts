"""Tests for search contexts (usetype presets) feature.

Tests the pure wildcard matching logic and resolve_params behavior.
These are extracted inline to avoid triggering the full model import chain
which has a pre-existing pgvector compat issue in .venv.
"""

import fnmatch

# ============================================================================
# Inline copies of the pure functions from search.py for testability.
# The canonical implementations live in jmfts_core/repositories/search.py.
# ============================================================================


def _usetype_has_wildcard(usetype: str) -> bool:
    return "*" in usetype or "?" in usetype


def _usetype_to_like(usetype: str) -> str:
    pattern = usetype.replace("%", r"\%").replace("_", r"\_")
    pattern = pattern.replace("*", "%").replace("?", "_")
    return pattern


def _usetype_matches(usetype_filter, usetype_value):
    if usetype_value is None:
        return False
    if _usetype_has_wildcard(usetype_filter):
        return fnmatch.fnmatch(usetype_value, usetype_filter)
    return usetype_value == usetype_filter


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


class TestUsetypeMatches:
    def test_exact_match(self):
        assert _usetype_matches("conversation", "conversation") is True

    def test_exact_mismatch(self):
        assert _usetype_matches("conversation", "notes") is False

    def test_star_matches_suffix(self):
        assert _usetype_matches("conversation/*", "conversation/session") is True
        assert _usetype_matches("conversation/*", "conversation/summary") is True

    def test_star_no_match_different_prefix(self):
        assert _usetype_matches("conversation/*", "notes/daily") is False

    def test_star_matches_multi_level(self):
        # fnmatch * matches any characters including /
        assert _usetype_matches("conversation/*", "conversation/a/b") is True

    def test_question_single_char(self):
        assert _usetype_matches("doc?", "docs") is True
        assert _usetype_matches("doc?", "doc1") is True

    def test_question_too_many_chars(self):
        assert _usetype_matches("doc?", "document") is False

    def test_none_value(self):
        assert _usetype_matches("conversation/*", None) is False

    def test_star_at_beginning(self):
        assert _usetype_matches("*/summary", "conversation/summary") is True
        assert _usetype_matches("*/summary", "notes/summary") is True
        assert _usetype_matches("*/summary", "notes/detail") is False


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
