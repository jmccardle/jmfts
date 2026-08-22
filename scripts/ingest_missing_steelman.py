"""Ingest 5 missing projects into steelman root 37387.

Usage: python -m scripts.ingest_missing_steelman
"""

import sys
import time
import httpx
from pathlib import Path

API = "http://192.168.1.100:8007"
STEELMAN_ROOT = 37387
INVENTORY_DIR = Path("/home/john/Development/fullstack-ai-202602/inventory-2026-04-05")

MISSING_PROJECTS = {
    "triskelion": "triskelion-INVENTORY-2026-04-05.md",
    "autokicad": "autokicad-INVENTORY-2026-04-05.md",
    "autofreecad": "autofreecad-INVENTORY-2026-04-05.md",
    "AdventureClemGame": "AdventureClemGame-INVENTORY-2026-04-05.md",
    "100daysofml.github.io": "100daysofml.github.io-INVENTORY-2026-04-05.md",
}


def ingest_document(project_name: str, filename: str) -> dict:
    filepath = INVENTORY_DIR / filename
    content = filepath.read_text()
    title = f"{project_name} Inventory"

    print(f"\n--- Ingesting: {title} ({len(content)} chars) ---")

    payload = {
        "content": content,
        "title": title,
        "usetype": "markdown",
        "parent_id": STEELMAN_ROOT,
        "pipeline_config": {
            "summarize": True,
            "extract_facts": False,
        },
    }

    resp = httpx.post(f"{API}/ingest", json=payload, timeout=300)
    resp.raise_for_status()
    result = resp.json()

    print(f"  source_document_id: {result.get('source_document_id')}")
    print(f"  segment_count: {result.get('segment_count')}")
    print(f"  summary_count: {result.get('summary_count')}")
    print(f"  tree_depth: {result.get('tree_depth')}")
    for stage in result.get("stages", []):
        status = stage.get("status", "?")
        name = stage.get("stage", "?")
        detail = stage.get("detail", "")
        error = stage.get("error", "")
        flag = "OK" if status == "success" else f"FAIL: {error or detail}"
        print(f"  [{name}] {flag}")

    return result


def check_children_count() -> int:
    resp = httpx.get(f"{API}/documents/{STEELMAN_ROOT}/children?limit=200", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    return len(items)


def run_raptor(doc_id: int, project_name: str) -> dict:
    print(f"  Running RAPTOR on doc {doc_id} ({project_name})...")
    resp = httpx.post(f"{API}/documents/{doc_id}/raptor", json={}, timeout=300)
    if resp.status_code == 200:
        result = resp.json()
        print(f"  RAPTOR: {result}")
        return result
    else:
        print(f"  RAPTOR failed: {resp.status_code} {resp.text[:200]}")
        return {}


def verify_search(query: str, label: str, min_score: float = 0.75) -> bool:
    payload = {
        "query": query,
        "limit": 20,
        "parent_id": STEELMAN_ROOT,
    }
    resp = httpx.post(f"{API}/search/", json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    items = results.get("results", results) if isinstance(results, dict) else results
    found = [r for r in items if r.get("score", 0) >= min_score]
    print(f"\nSearch '{label}': {len(items)} results, {len(found)} above {min_score}")
    for r in items[:5]:
        print(f"  score={r.get('score', 0):.4f}  title={r.get('title', '?')[:80]}")
    return len(found) > 0


def main():
    print(f"=== Steelman Re-ingest: 5 Missing Projects ===")
    print(f"API: {API}")
    print(f"Root: {STEELMAN_ROOT}")

    # Check initial count
    initial_count = check_children_count()
    print(f"\nInitial children count: {initial_count}")

    ingested_ids = {}

    for project_name, filename in MISSING_PROJECTS.items():
        try:
            result = ingest_document(project_name, filename)
            doc_id = result.get("source_document_id")
            ingested_ids[project_name] = doc_id

            # Check if RAPTOR summary was generated
            summary_count = result.get("summary_count", 0)
            if summary_count == 0:
                print(f"  WARNING: No summaries generated, running RAPTOR separately...")
                run_raptor(doc_id, project_name)

            time.sleep(2)  # brief pause between ingestions
        except Exception as e:
            print(f"  ERROR ingesting {project_name}: {e}", file=sys.stderr)
            sys.exit(1)

    # Verify count
    final_count = check_children_count()
    print(f"\n=== Final children count: {final_count} (was {initial_count}) ===")
    expected = initial_count + len(MISSING_PROJECTS)
    if final_count == expected:
        print(f"COUNT OK: {final_count}")
    else:
        print(f"COUNT MISMATCH: expected {expected}, got {final_count}")

    # Test vector search
    print("\n=== Search Verification ===")
    verify_search("triskelion agents-as-documents VDO architecture", "triskelion")
    verify_search("autokicad MCP PCB simulated annealing", "autokicad")

    print("\n=== Done ===")
    print(f"Ingested doc IDs: {ingested_ids}")


if __name__ == "__main__":
    main()
