#!/usr/bin/env python3
"""
JMFTS Operations Check Script

Imports test_docs/ corpus, creates indexes, and validates all search methods.
"""

import os
import re
import time
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, "/home/john/Development/jmfts")

from jmfts_core.database import get_session, get_engine
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.models.document import Document
from jmfts_core.models.token_embedding import TokenEmbedding
from jmfts_core.models.search_index import SearchIndex, SearchIndexEntry, SearchTermPosting, SearchTermStats

TEST_DOCS_DIR = Path("/home/john/Development/jmfts/test_docs")

# Patterns to filter out non-content paragraphs
SKIP_PATTERNS = [
    re.compile(r'^https?://', re.IGNORECASE),
    re.compile(r'^Category:', re.IGNORECASE),
    re.compile(r'^---+$'),
    re.compile(r'^thumb\|', re.IGNORECASE),
    re.compile(r'^References\s*$', re.IGNORECASE),
    re.compile(r'^External links\s*$', re.IGNORECASE),
    re.compile(r'^Videos:\s*$', re.IGNORECASE),
    re.compile(r'^Websites\s*$', re.IGNORECASE),
]


def should_skip_paragraph(para: str) -> bool:
    """Check if paragraph should be skipped."""
    if len(para) < 50:
        return True
    if any(p.search(para) for p in SKIP_PATTERNS):
        return True
    # Skip if mostly non-alphanumeric
    alpha_ratio = sum(c.isalpha() for c in para) / len(para) if para else 0
    if alpha_ratio < 0.5:
        return True
    return False


def load_corpus_from_directory(corpus_dir: Path) -> list[tuple[str, str, list[str]]]:
    """
    Load documents from a directory.

    Returns list of (filename, title, paragraphs) tuples.
    """
    documents = []

    for fpath in sorted(corpus_dir.glob("*")):
        if not fpath.is_file():
            continue
        if not (fpath.suffix in ['.txt', '.md']):
            continue

        try:
            content = fpath.read_text(encoding='utf-8')
        except Exception as e:
            print(f"  Warning: Could not read {fpath}: {e}")
            continue

        # Extract title (first line or filename)
        lines = content.split('\n')
        title = lines[0].strip().lstrip('#').strip() if lines else fpath.stem
        if not title or len(title) < 3:
            title = fpath.stem

        # Split into paragraphs
        paragraphs = re.split(r'\n\n+', content)
        valid_paragraphs = [
            p.strip() for p in paragraphs
            if not should_skip_paragraph(p.strip())
        ]

        if valid_paragraphs:
            documents.append((fpath.name, title, valid_paragraphs))

    return documents


def wipe_database(session):
    """Clear all documents, embeddings, and indexes."""
    print("  Clearing token embeddings...")
    session.query(TokenEmbedding).delete()

    print("  Clearing search index entries...")
    session.query(SearchTermPosting).delete()
    session.query(SearchTermStats).delete()
    session.query(SearchIndexEntry).delete()
    session.query(SearchIndex).delete()

    print("  Clearing documents...")
    session.query(Document).delete()

    session.commit()
    print("  Database cleared.")


def import_corpus(session, corpus_name: str, corpus_dir: Path) -> int:
    """
    Import a corpus from a directory.

    Creates:
    - Root document for corpus
    - Child document for each file
    - Grandchild documents for each paragraph (chunk)

    Returns root document ID.
    """
    doc_repo = DocumentRepository(session)

    documents = load_corpus_from_directory(corpus_dir)
    if not documents:
        print(f"  No documents found in {corpus_dir}")
        return None

    print(f"  Found {len(documents)} files with content")

    # Create corpus root
    root = doc_repo.create(
        title=f"{corpus_name.title()} Corpus",
        content=f"Collection of {corpus_name} documents.",
        usetype=f"{corpus_name}:corpus",
        auto_embed=False,  # Root doesn't need embedding
    )
    print(f"  Created corpus root: {root.id} - {root.title}")

    total_chunks = 0

    for fname, title, paragraphs in documents:
        # Create document node
        doc = doc_repo.create(
            title=title[:100],  # Truncate long titles
            content=f"Source: {fname}. Contains {len(paragraphs)} sections.",
            parent_id=root.id,
            usetype=f"{corpus_name}:document",
            auto_embed=False,
        )
        print(f"    📄 {doc.id}: {title[:50]}... ({len(paragraphs)} chunks)")

        # Create paragraph chunks
        for i, para in enumerate(paragraphs):
            chunk = doc_repo.create(
                title=f"{title[:40]} - chunk {i+1}",
                content=para,
                parent_id=doc.id,
                usetype=f"{corpus_name}:chunk",
                auto_embed=True,  # This triggers embedding with new token selection!
            )
            total_chunks += 1

        session.commit()

    print(f"  Total chunks created: {total_chunks}")
    return root.id


def test_search(search_repo, query, method, index_name=None, embed_dim=256, parent_id=None):
    """Run a search and return timing + results."""
    start = time.time()

    if method == "vector":
        results = search_repo.vector_search_text(query, limit=5, parent_id=parent_id)
    elif method == "fulltext":
        results = search_repo.fulltext_search(query, limit=5, parent_id=parent_id)
    elif method == "bm25":
        results = search_repo.bm25_search(query, index_name=index_name or "default", limit=5)
    elif method == "maxsim":
        results = search_repo.maxsim_search(query, limit=5, embed_dim=embed_dim, parent_id=parent_id)
    elif method == "hybrid":
        results = search_repo.hybrid_search(query, limit=5, index_name=index_name or "default", parent_id=parent_id)
    else:
        return None, 0

    elapsed = (time.time() - start) * 1000
    return results, elapsed


def print_results(results, method, query, elapsed):
    """Pretty print search results."""
    print(f"\n  {method.upper()} search for '{query[:40]}...' ({elapsed:.1f}ms):")
    if not results:
        print("    (no results)")
        return

    for i, r in enumerate(results[:3], 1):
        title = (r.document.title or "(no title)")[:40]
        content_preview = (r.document.content or "")[:50].replace("\n", " ")
        print(f"    {i}. [{r.score:.4f}] {title}")
        print(f"       {content_preview}...")


def inspect_tokens(session, doc_id):
    """Inspect token embeddings for a document."""
    doc_repo = DocumentRepository(session)
    doc = doc_repo.get_with_embeddings(doc_id)

    if not doc or not doc.token_embeddings:
        print(f"  No tokens for document {doc_id}")
        return

    print(f"\n  Token embeddings for doc {doc_id}:")
    print(f"  Content: {doc.content[:60]}...")
    print(f"  Total tokens stored: {len(doc.token_embeddings)}")
    print("  Tokens (by importance):")

    sorted_tokens = sorted(doc.token_embeddings, key=lambda t: -t.importance_score)
    for tok in sorted_tokens[:8]:
        clean_text = tok.token_text.replace("Ġ", " ").replace("Ċ", "\\n")
        print(f"    '{clean_text}' (score={tok.importance_score:.4f})")


def main():
    print("=" * 70)
    print("JMFTS Operations Check")
    print("=" * 70)

    with get_session() as session:
        doc_repo = DocumentRepository(session)
        search_repo = SearchRepository(session)

        # =====================================================================
        # STEP 1: Wipe and import fresh corpus
        # =====================================================================
        print("\n[1] Wiping database and importing test_docs corpus...")
        wipe_database(session)

        # Import each corpus type
        corpus_roots = {}

        for corpus_name, subdir in [("articles", "articles"), ("notes", "notes"), ("prose", "prose")]:
            corpus_path = TEST_DOCS_DIR / subdir
            if corpus_path.exists():
                print(f"\n  Importing {corpus_name} from {corpus_path}...")
                root_id = import_corpus(session, corpus_name, corpus_path)
                if root_id:
                    corpus_roots[corpus_name] = root_id

        session.commit()

        # =====================================================================
        # STEP 2: Create BM25 indexes
        # =====================================================================
        print("\n[2] Creating BM25 indexes...")

        for corpus_name, root_id in corpus_roots.items():
            idx = search_repo.create_index(corpus_name, f"{corpus_name} corpus index")
            search_repo.add_root_to_index(corpus_name, root_id)
            session.flush()  # Make the member visible for refresh_index query
            result = search_repo.refresh_index(corpus_name)
            print(f"  {corpus_name}: {result}")
            session.commit()

        # =====================================================================
        # STEP 3: Inspect token selection quality
        # =====================================================================
        print("\n[3] Inspecting token selection quality...")

        # Find some chunks to inspect
        for corpus_name in corpus_roots:
            chunks = doc_repo.find(usetype=f"{corpus_name}:chunk", limit=2)
            for chunk in chunks:
                inspect_tokens(session, chunk.id)

        # =====================================================================
        # STEP 4: Test search methods
        # =====================================================================
        print("\n[4] Testing search methods...")

        # Test queries for each corpus
        test_queries = {
            "articles": [
                "3D printing additive manufacturing",
                "nylon synthetic polymer",
                "laser sintering gold jewelry",
            ],
            "notes": [
                "libtcod pull request",
                "engineering resume experience",
            ],
            "prose": [
                "library books reading itself",
                "science fiction physics",
            ],
        }

        for corpus_name, queries in test_queries.items():
            if corpus_name not in corpus_roots:
                continue

            root_id = corpus_roots[corpus_name]
            print(f"\n  --- {corpus_name.upper()} corpus ---")

            for query in queries[:2]:  # First 2 queries per corpus
                print(f"\n  Query: '{query}'")

                for method in ["vector", "fulltext", "maxsim"]:
                    try:
                        results, elapsed = test_search(
                            search_repo, query, method,
                            index_name=corpus_name, parent_id=root_id
                        )
                        if results:
                            r = results[0]
                            title = (r.document.title or "")[:30]
                            print(f"    {method:8s}: [{r.score:.3f}] {title}... ({elapsed:.0f}ms)")
                        else:
                            print(f"    {method:8s}: (no results) ({elapsed:.0f}ms)")
                    except Exception as e:
                        print(f"    {method:8s}: ERROR - {e}")

        # =====================================================================
        # STEP 5: Test cross-corpus isolation
        # =====================================================================
        print("\n[5] Testing cross-corpus isolation (MaxSim subtree filtering)...")

        # Query that could match multiple corpora
        test_query = "battery management system"

        print(f"\n  Query: '{test_query}'")
        for corpus_name, root_id in corpus_roots.items():
            results, elapsed = test_search(
                search_repo, test_query, "maxsim",
                parent_id=root_id
            )
            if results:
                titles = [r.document.title[:25] for r in results[:3]]
                print(f"    {corpus_name:10s}: {titles}")
            else:
                print(f"    {corpus_name:10s}: (no results)")

        # =====================================================================
        # STEP 6: Performance summary
        # =====================================================================
        print("\n[6] Performance summary...")

        if corpus_roots:
            first_corpus = list(corpus_roots.keys())[0]
            first_root = corpus_roots[first_corpus]

            timings = {}
            for method in ["vector", "fulltext", "bm25", "maxsim"]:
                try:
                    _, elapsed = test_search(
                        search_repo, "test query performance",
                        method, index_name=first_corpus, parent_id=first_root
                    )
                    timings[method] = elapsed
                except Exception as e:
                    timings[method] = f"ERROR: {e}"

            print("\n  Search latencies (single query):")
            for method, elapsed in timings.items():
                if isinstance(elapsed, float):
                    status = "OK" if elapsed < 1000 else "SLOW"
                    print(f"    {method:10s}: {elapsed:7.1f}ms [{status}]")
                else:
                    print(f"    {method:10s}: {elapsed}")

        # Final stats
        doc_count = session.query(Document).count()
        token_count = session.query(TokenEmbedding).count()
        print(f"\n  Final stats: {doc_count} documents, {token_count} token embeddings")

        print("\n" + "=" * 70)
        print("Operations check complete!")
        print("=" * 70)


if __name__ == "__main__":
    main()
