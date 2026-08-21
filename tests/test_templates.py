"""Tests for prompt template library.

Tests the template rendering logic, variable extraction, category validation,
and schema construction. Pure logic tests — no database or embedding service required.
"""

import re


# ============================================================================
# Inline copies of template logic for testability (canonical in routers/templates.py)
# ============================================================================

VALID_CATEGORIES = {"implementation", "grooming", "evaluation", "ideation", "refinement"}


def validate_category(category: str) -> bool:
    return category in VALID_CATEGORIES


def extract_placeholders(template_body: str) -> set[str]:
    """Extract all {{variable}} placeholder names from a template."""
    return set(re.findall(r"\{\{(\w+)\}\}", template_body))


def render_template(
    template_body: str,
    variables: dict[str, str],
    defined_vars: list[dict],
) -> tuple[str, list[str]]:
    """Render a template and return (rendered_text, missing_required_vars).

    Mirrors the logic in routers/templates.py render_template endpoint.
    """
    required_vars = {v["name"] for v in defined_vars if v.get("required", True)}
    defined_names = {v["name"] for v in defined_vars}
    placeholders = extract_placeholders(template_body)

    # Missing = required vars (or undeclared placeholders) not provided
    missing = (required_vars | (placeholders - defined_names)) - set(variables.keys())

    rendered = template_body
    for var_name, var_value in variables.items():
        rendered = rendered.replace("{{" + var_name + "}}", var_value)

    return rendered, sorted(missing)


def build_structured_content(category: str, variables: list[dict]) -> dict:
    """Build structured_content for a template document."""
    return {
        "category": category,
        "variables": variables,
        "usage_count": 0,
        "success_rate": 0.0,
        "last_used": None,
    }


# ============================================================================
# Category Validation
# ============================================================================


class TestCategoryValidation:
    def test_valid_categories(self):
        for cat in ["implementation", "grooming", "evaluation", "ideation", "refinement"]:
            assert validate_category(cat) is True

    def test_invalid_category(self):
        assert validate_category("debugging") is False

    def test_empty_string(self):
        assert validate_category("") is False

    def test_case_sensitive(self):
        assert validate_category("Implementation") is False


# ============================================================================
# Placeholder Extraction
# ============================================================================


class TestPlaceholderExtraction:
    def test_single_placeholder(self):
        assert extract_placeholders("Hello {{name}}!") == {"name"}

    def test_multiple_placeholders(self):
        result = extract_placeholders("{{greeting}} {{name}}, welcome to {{place}}")
        assert result == {"greeting", "name", "place"}

    def test_no_placeholders(self):
        assert extract_placeholders("No variables here.") == set()

    def test_duplicate_placeholders(self):
        result = extract_placeholders("{{x}} and {{x}} again")
        assert result == {"x"}

    def test_nested_braces_ignored(self):
        # {{{x}}} should still find x
        result = extract_placeholders("{{{x}}}")
        assert "x" in result

    def test_word_chars_only(self):
        # {{foo-bar}} should not match (hyphen not \w)
        result = extract_placeholders("{{foo_bar}} and {{foo-bar}}")
        assert result == {"foo_bar"}

    def test_multiline_template(self):
        tmpl = """## {{task_title}}
### Context
{{context}}
### Requirements
{{requirements}}"""
        result = extract_placeholders(tmpl)
        assert result == {"task_title", "context", "requirements"}


# ============================================================================
# Template Rendering
# ============================================================================


class TestTemplateRendering:
    def test_basic_substitution(self):
        body = "Hello {{name}}, your task is {{task}}."
        variables = [
            {"name": "name", "required": True},
            {"name": "task", "required": True},
        ]
        rendered, missing = render_template(body, {"name": "John", "task": "build API"}, variables)
        assert rendered == "Hello John, your task is build API."
        assert missing == []

    def test_missing_required_variable(self):
        body = "Hello {{name}}, your task is {{task}}."
        variables = [
            {"name": "name", "required": True},
            {"name": "task", "required": True},
        ]
        rendered, missing = render_template(body, {"name": "John"}, variables)
        assert "{{task}}" in rendered
        assert missing == ["task"]

    def test_optional_variable_not_reported_missing(self):
        body = "Hello {{name}}. Notes: {{notes}}"
        variables = [
            {"name": "name", "required": True},
            {"name": "notes", "required": False},
        ]
        rendered, missing = render_template(body, {"name": "John"}, variables)
        assert "{{notes}}" in rendered
        assert missing == []

    def test_extra_variables_ignored(self):
        body = "Hello {{name}}."
        variables = [{"name": "name", "required": True}]
        rendered, missing = render_template(body, {"name": "John", "extra": "ignored"}, variables)
        assert rendered == "Hello John."
        assert missing == []

    def test_undeclared_placeholder_treated_as_required(self):
        body = "Hello {{name}} from {{city}}."
        variables = [{"name": "name", "required": True}]
        # city is in the template but not declared — treated as required
        rendered, missing = render_template(body, {"name": "John"}, variables)
        assert "{{city}}" in rendered
        assert "city" in missing

    def test_all_variables_provided(self):
        body = "Task #{{task_id}}: {{title}}"
        variables = [
            {"name": "task_id", "required": True},
            {"name": "title", "required": True},
        ]
        rendered, missing = render_template(body, {"task_id": "42", "title": "Fix bug"}, variables)
        assert rendered == "Task #42: Fix bug"
        assert missing == []

    def test_empty_template(self):
        rendered, missing = render_template("", {}, [])
        assert rendered == ""
        assert missing == []

    def test_multiline_rendering(self):
        body = """## {{task_title}}

### Context
{{context}}

### Requirements
{{requirements}}"""
        variables = [
            {"name": "task_title", "required": True},
            {"name": "context", "required": False},
            {"name": "requirements", "required": True},
        ]
        rendered, missing = render_template(
            body,
            {"task_title": "Build API", "requirements": "REST endpoints"},
            variables,
        )
        assert "## Build API" in rendered
        assert "REST endpoints" in rendered
        assert "{{context}}" in rendered  # optional, not provided
        assert missing == []


# ============================================================================
# Structured Content Construction
# ============================================================================


class TestStructuredContent:
    def test_builds_correct_structure(self):
        sc = build_structured_content(
            "implementation",
            [{"name": "task_id", "description": "Task ID", "required": True}],
        )
        assert sc["category"] == "implementation"
        assert len(sc["variables"]) == 1
        assert sc["variables"][0]["name"] == "task_id"
        assert sc["usage_count"] == 0
        assert sc["success_rate"] == 0.0
        assert sc["last_used"] is None

    def test_empty_variables_list(self):
        sc = build_structured_content("ideation", [])
        assert sc["variables"] == []


# ============================================================================
# Pydantic Schema Validation
# ============================================================================


class TestSchemaValidation:
    def test_template_create_schema(self):
        from jmfts_core.rest.schemas import TemplateCreate

        tc = TemplateCreate(
            title="Test",
            content="Hello {{name}}",
            category="implementation",
            variables=[],
        )
        assert tc.title == "Test"
        assert tc.category == "implementation"

    def test_template_create_with_variables(self):
        from jmfts_core.rest.schemas import TemplateCreate, TemplateVariable

        tc = TemplateCreate(
            title="Test",
            content="Hello {{name}}",
            category="grooming",
            variables=[
                TemplateVariable(name="name", description="User name", required=True),
            ],
        )
        assert len(tc.variables) == 1
        assert tc.variables[0].name == "name"

    def test_template_update_all_optional(self):
        from jmfts_core.rest.schemas import TemplateUpdate

        tu = TemplateUpdate()
        assert tu.title is None
        assert tu.content is None
        assert tu.category is None
        assert tu.variables is None

    def test_template_render_request(self):
        from jmfts_core.rest.schemas import TemplateRenderRequest

        rr = TemplateRenderRequest(variables={"name": "John", "task": "build"})
        assert rr.variables["name"] == "John"

    def test_template_search_request_defaults(self):
        from jmfts_core.rest.schemas import TemplateSearchRequest

        sr = TemplateSearchRequest(query="implementation task")
        assert sr.limit == 10
        assert sr.category is None

    def test_template_response_schema(self):
        from jmfts_core.rest.schemas import TemplateResponse

        tr = TemplateResponse(
            id=1,
            title="Test",
            content="Body",
            category="evaluation",
            variables=[],
            usage_count=5,
            success_rate=0.8,
        )
        assert tr.id == 1
        assert tr.usage_count == 5
        assert tr.success_rate == 0.8

    def test_template_render_response(self):
        from jmfts_core.rest.schemas import TemplateRenderResponse

        rr = TemplateRenderResponse(
            rendered="Hello John",
            template_id=42,
            missing_variables=["city"],
        )
        assert rr.rendered == "Hello John"
        assert rr.missing_variables == ["city"]
