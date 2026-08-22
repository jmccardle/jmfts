"""Unit tests for jmfts_core.view_renderer (no DB)."""

from jmfts_core.view_renderer import build_children_stubs, resolve_references

# ---------------------------------------------------------------------------
# resolve_references
# ---------------------------------------------------------------------------


class TestResolveReferences:
    def test_inline_doc_ref_replaced_with_link(self):
        body = "See [[doc:5]] for details."
        out = resolve_references(
            body,
            link_handling="hidden",
            title_lookup={5: "Five"},
            outbound_links=[],
            triples=[],
        )
        assert "[Five](/view/5)" in out

    def test_short_form_ref_replaced(self):
        body = "Compare [[42]]."
        out = resolve_references(
            body,
            link_handling="hidden",
            title_lookup={42: "Forty-Two"},
            outbound_links=[],
            triples=[],
        )
        assert "[Forty-Two](/view/42)" in out

    def test_unknown_id_falls_back_to_doc_n(self):
        body = "[[doc:99]]"
        out = resolve_references(
            body,
            link_handling="hidden",
            title_lookup={},
            outbound_links=[],
            triples=[],
        )
        assert "[doc 99](/view/99)" in out

    def test_footnotes_section_appended(self):
        out = resolve_references(
            "body",
            link_handling="footnotes",
            title_lookup={5: "Target"},
            outbound_links=[
                {"target_id": 5, "title": "Target", "link_type": "cites"},
            ],
            triples=[
                {"object_id": 7, "object_title": "Other", "predicate_name": "mentions"},
            ],
        )
        assert "## Footnotes" in out
        assert "[1] **cites** [Target](/view/5)" in out
        assert "[2] **mentions** → [Other](/view/7)" in out

    def test_inline_citations_uses_references_header(self):
        out = resolve_references(
            "body",
            link_handling="inline-citations",
            title_lookup={},
            outbound_links=[{"target_id": 5, "title": "T", "link_type": "cites"}],
            triples=[],
        )
        assert "## References" in out

    def test_sidebar_marker_inserted(self):
        out = resolve_references(
            "body",
            link_handling="sidebar",
            title_lookup={},
            outbound_links=[{"target_id": 5, "title": "T", "link_type": "cites"}],
            triples=[],
        )
        assert "<!-- sidebar -->" in out

    def test_hidden_omits_section(self):
        out = resolve_references(
            "body",
            link_handling="hidden",
            title_lookup={},
            outbound_links=[{"target_id": 5, "title": "T", "link_type": "cites"}],
            triples=[],
        )
        assert "## Footnotes" not in out
        assert "## References" not in out

    def test_no_refs_no_footer(self):
        out = resolve_references(
            "body",
            link_handling="footnotes",
            title_lookup={},
            outbound_links=[],
            triples=[],
        )
        assert "## Footnotes" not in out
        assert out == "body"

    def test_empty_content(self):
        out = resolve_references(
            None,
            link_handling="footnotes",
            title_lookup={},
            outbound_links=[{"target_id": 5, "title": "T", "link_type": "cites"}],
            triples=[],
        )
        assert "## Footnotes" in out


# ---------------------------------------------------------------------------
# build_children_stubs
# ---------------------------------------------------------------------------


class TestBuildChildrenStubs:
    children = [
        {
            "id": 1,
            "title": "First",
            "usetype": "chunk",
            "content": "para one\n\npara two\n\npara three",
            "child_count": 0,
        },
        {
            "id": 2,
            "title": "Second",
            "usetype": "chunk",
            "content": "# Heading\n\nbody text follows here",
            "child_count": 3,
        },
    ]

    def test_hidden_returns_empty(self):
        assert build_children_stubs(self.children, child_handling="hidden") == []

    def test_collapsed_truncates_to_preview(self):
        stubs = build_children_stubs(self.children, child_handling="collapsed", preview_chars=15)
        # The first child's preview should be truncated and end with an ellipsis.
        assert stubs[0]["preview"].startswith("para one")
        assert stubs[0]["preview"].endswith("…")

    def test_first_paragraph_gets_just_first(self):
        stubs = build_children_stubs(self.children, child_handling="first-paragraph")
        assert stubs[0]["preview"] == "para one"

    def test_inline_headings_includes_first_heading(self):
        stubs = build_children_stubs(self.children, child_handling="inline-headings")
        assert "# Heading" in stubs[1]["preview"]

    def test_expand_url_uses_id(self):
        stubs = build_children_stubs(self.children, child_handling="collapsed")
        assert stubs[0]["expand_url"] == "/view/1"
        assert stubs[1]["expand_url"] == "/view/2"

    def test_child_count_passed_through(self):
        stubs = build_children_stubs(self.children, child_handling="collapsed")
        assert stubs[1]["child_count"] == 3
