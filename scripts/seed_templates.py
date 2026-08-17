"""Seed the prompt_templates container with initial templates.

Usage:
    python -m scripts.seed_templates          # create all seeds
    python -m scripts.seed_templates --list   # list existing templates
"""

import argparse
import sys

from jmfts_core.database import get_session
from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository

ADJUTANT_ROOT_ID = 7231
TEMPLATE_USETYPE = "adjutant:template"

SEED_TEMPLATES = [
    {
        "title": "Implementation with Kanboard protocol",
        "category": "implementation",
        "variables": [
            {"name": "task_title", "description": "Kanboard task title", "required": True},
            {"name": "task_id", "description": "Kanboard task ID", "required": True},
            {"name": "project_id", "description": "Kanboard project ID", "required": True},
            {"name": "column_id_done", "description": "Column ID for Done state", "required": True},
            {
                "name": "requirements",
                "description": "Detailed implementation requirements",
                "required": True,
            },
            {
                "name": "context",
                "description": "Background context for the task",
                "required": False,
            },
            {"name": "repository", "description": "Target git repository path", "required": True},
        ],
        "content": """## {{task_title}} (#{{task_id}})

### Kanboard Protocol
Kanboard API at http://localhost:8790/jsonrpc.php, auth: HTTP Basic user `john`, token in ~/Development/john-does-kanboard/token.
Task #{{task_id}} (project_id {{project_id}}) is already In Progress. Post a comment when stopping. Move to Done (column_id {{column_id_done}}) if complete.

### Context
{{context}}

### Requirements
{{requirements}}

### Instructions
1. Read CLAUDE.md and understand the existing codebase
2. Implement the requirements
3. Write tests
4. Git commit and push to {{repository}}

### Category: Implementation""",
    },
    {
        "title": "Grooming task brief",
        "category": "grooming",
        "variables": [
            {"name": "task_title", "description": "Task title for grooming", "required": True},
            {"name": "task_id", "description": "Kanboard task ID", "required": True},
            {"name": "project_id", "description": "Kanboard project ID", "required": True},
            {
                "name": "objective",
                "description": "High-level objective to analyze",
                "required": True,
            },
            {"name": "repository", "description": "Target git repository path", "required": True},
            {
                "name": "constraints",
                "description": "Known constraints or boundaries",
                "required": False,
            },
        ],
        "content": """## Grooming: {{task_title}} (#{{task_id}})

### Kanboard Protocol
Kanboard API at http://localhost:8790/jsonrpc.php, auth: HTTP Basic user `john`, token in ~/Development/john-does-kanboard/token.
Task #{{task_id}} (project_id {{project_id}}) is already In Progress. Post a comment when stopping.

### Objective
{{objective}}

### Constraints
{{constraints}}

### Instructions
1. Read the codebase at {{repository}} and understand the architecture
2. Analyze the objective and break it into implementable subtasks
3. For each subtask, estimate complexity (S/M/L) and identify dependencies
4. Create Kanboard tasks for each subtask under project {{project_id}}
5. Post a summary comment on task #{{task_id}} with the breakdown
6. Do NOT implement anything — this is a grooming/planning session only

### Category: Grooming""",
    },
    {
        "title": "Blog review with voice guide",
        "category": "evaluation",
        "variables": [
            {"name": "draft_path", "description": "Path to the blog draft file", "required": True},
            {"name": "voice_guide", "description": "Voice and tone guidelines", "required": False},
            {
                "name": "target_audience",
                "description": "Intended audience for the blog post",
                "required": False,
            },
            {
                "name": "focus_areas",
                "description": "Specific areas to focus review on",
                "required": False,
            },
        ],
        "content": """## Blog Review

### Draft
Review the blog draft at: {{draft_path}}

### Voice Guide
{{voice_guide}}

### Target Audience
{{target_audience}}

### Review Instructions
1. Read the entire draft carefully
2. Evaluate against the voice guide for tone consistency
3. Check technical accuracy of all claims and code samples
4. Assess structure: intro hook, logical flow, clear conclusion
5. Note any jargon that needs explanation for the target audience
6. Provide specific, actionable feedback organized by:
   - **Must fix**: Factual errors, broken examples, unclear sections
   - **Should fix**: Tone drift, structural improvements, missing context
   - **Nice to have**: Style polish, better examples, additional references

### Focus Areas
{{focus_areas}}

### Category: Evaluation""",
    },
    {
        "title": "Research spike",
        "category": "ideation",
        "variables": [
            {"name": "topic", "description": "Research topic or question", "required": True},
            {"name": "scope", "description": "Boundaries of the investigation", "required": True},
            {
                "name": "time_box",
                "description": "Time limit for the spike (e.g., 2 hours)",
                "required": False,
            },
            {"name": "deliverable", "description": "Expected output format", "required": False},
            {
                "name": "prior_art",
                "description": "Known existing work or references",
                "required": False,
            },
        ],
        "content": """## Research Spike: {{topic}}

### Scope
{{scope}}

### Time Box
{{time_box}}

### Prior Art
{{prior_art}}

### Instructions
1. Survey the landscape: what exists, what approaches are common
2. Identify 2-3 candidate approaches with trade-offs
3. For each approach, note:
   - Implementation complexity (hours/days estimate)
   - Dependencies or prerequisites
   - Risks and unknowns
   - How well it fits our existing architecture
4. Produce a recommendation with rationale
5. Document findings in the expected format

### Expected Deliverable
{{deliverable}}

### Category: Ideation""",
    },
]


def find_container(session) -> int:
    """Find the prompt_templates container ID."""
    container = (
        session.query(Document)
        .filter(
            Document.parent_id == ADJUTANT_ROOT_ID,
            Document.title == "prompt_templates",
        )
        .first()
    )
    if not container:
        print("ERROR: prompt_templates container not found under root 7231.")
        print("Create it first or start the API server (auto-creates on first request).")
        sys.exit(1)
    return container.id


def list_templates():
    """List existing templates."""
    with get_session() as session:
        container_id = find_container(session)
        templates = (
            session.query(Document)
            .filter(
                Document.parent_id == container_id,
                Document.usetype == TEMPLATE_USETYPE,
            )
            .order_by(Document.title)
            .all()
        )
        if not templates:
            print("No templates found.")
            return
        for t in templates:
            sc = t.structured_content or {}
            print(f"  [{t.id}] {t.title} (category={sc.get('category', '?')})")


def seed_templates():
    """Create seed templates (skip if title already exists)."""
    with get_session() as session:
        container_id = find_container(session)
        repo = DocumentRepository(session)

        existing = (
            session.query(Document.title)
            .filter(
                Document.parent_id == container_id,
                Document.usetype == TEMPLATE_USETYPE,
            )
            .all()
        )
        existing_titles = {row[0] for row in existing}

        created = 0
        for tmpl in SEED_TEMPLATES:
            if tmpl["title"] in existing_titles:
                print(f"  SKIP (exists): {tmpl['title']}")
                continue

            structured_content = {
                "category": tmpl["category"],
                "variables": tmpl["variables"],
                "usage_count": 0,
                "success_rate": 0.0,
                "last_used": None,
            }

            doc = repo.create(
                title=tmpl["title"],
                content=tmpl["content"],
                parent_id=container_id,
                usetype=TEMPLATE_USETYPE,
                structured_content=structured_content,
                auto_embed=True,
            )
            print(f"  CREATED [{doc.id}]: {tmpl['title']}")
            created += 1

        print(f"\nDone. Created {created} templates, skipped {len(SEED_TEMPLATES) - created}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed prompt templates")
    parser.add_argument("--list", action="store_true", help="List existing templates")
    args = parser.parse_args()

    if args.list:
        list_templates()
    else:
        seed_templates()
