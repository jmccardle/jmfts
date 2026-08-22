#!/usr/bin/env python3
"""
JMFTS Embedding Test Script

Tests the embedding service and creates a test document.
Run with: python -m scripts.test_embedding
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from jmfts_core.config import get_settings
from jmfts_core.database import get_session
from jmfts_core.embedding import get_embedding_service
from jmfts_core.repositories.document import DocumentRepository


def test_embedding_service():
    """Test the embedding service directly"""
    print("=" * 60)
    print("Testing Embedding Service")
    print("=" * 60)

    settings = get_settings()
    print(f"\nModel: {settings.embedding_model}")
    print(f"Device: {settings.embedding_device}")
    print(f"Token top percent: {settings.token_top_percent}")

    print("\nLoading embedding model (this may take a moment)...")
    service = get_embedding_service()

    # Test simple embedding
    test_text = "The quick brown fox jumps over the lazy dog."
    print(f"\nTest text: '{test_text}'")

    embedding = service.embed_text(test_text)
    print(f"Embedding shape: {embedding.shape}")
    print(f"Embedding norm: {(embedding ** 2).sum() ** 0.5:.4f}")  # Should be ~1.0

    # Test truncation
    for dim in [128, 256, 384, 512]:
        truncated = service.truncate_embedding(embedding, dim)
        norm = (truncated ** 2).sum() ** 0.5
        print(f"  Truncated to {dim}: shape={truncated.shape}, norm={norm:.4f}")

    # Test with tokens
    print("\nTesting token-level embedding...")
    result = service.embed_with_tokens(test_text)
    print(f"Document embedding shape: {result.document_embedding.shape}")
    print(f"Number of token embeddings: {len(result.token_embeddings)}")

    print("\nTop tokens by importance:")
    for tok in result.token_embeddings[:5]:
        print(f"  [{tok.token_idx}] '{tok.token_text}' - importance: {tok.importance_score:.4f}")

    return True


def test_document_creation():
    """Test creating a document with embeddings"""
    print("\n" + "=" * 60)
    print("Testing Document Creation")
    print("=" * 60)

    test_content = """
    JMFTS (John McCardle's Fusion Tree Search) is a focused retrieval appliance
    that combines multiple search techniques for optimal document retrieval.

    Key features include:
    - Matryoshka embeddings for flexible dimension reduction
    - Late interaction retrieval using token-level embeddings
    - BM25 lexical search with inverted indexes
    - Hybrid search using Reciprocal Rank Fusion
    - Hierarchical document tree navigation

    The system is designed to be an always-on, appliance-like search service
    with an owned embedding model (ModernBERT).
    """

    print(f"\nCreating test document with {len(test_content)} characters...")

    with get_session() as session:
        repo = DocumentRepository(session)

        # Create document with auto-embedding
        doc = repo.create(
            title="JMFTS Overview",
            content=test_content,
            usetype="test",
            auto_embed=True,
        )

        print(f"\nDocument created:")
        print(f"  ID: {doc.id}")
        print(f"  Title: {doc.title}")
        print(f"  Has embedding: {doc.embed is not None}")

        if doc.embed:
            print(f"  Embedding dimensions: {len(doc.embed)}")

        # Check token embeddings
        token_count = len(doc.token_embeddings)
        print(f"  Token embeddings: {token_count}")

        if token_count > 0:
            tok = doc.token_embeddings[0]
            print(f"\n  First token embedding:")
            print(f"    Token: '{tok.token_text}'")
            print(f"    Importance: {tok.importance_score:.4f}")
            print(f"    Has embed_128: {tok.embed_128 is not None}")
            print(f"    Has embed_256: {tok.embed_256 is not None}")
            print(f"    Has embed_384: {tok.embed_384 is not None}")
            print(f"    Has embed_512: {tok.embed_512 is not None}")

        return doc.id


def test_search(doc_id: int):
    """Test searching for the created document"""
    print("\n" + "=" * 60)
    print("Testing Search")
    print("=" * 60)

    from jmfts_core.repositories.search import SearchRepository

    with get_session() as session:
        search_repo = SearchRepository(session)

        # Vector search
        print("\n1. Vector search for 'matryoshka embeddings'...")
        results = search_repo.vector_search_text("matryoshka embeddings", limit=5)
        print(f"   Found {len(results)} results")
        for r in results:
            print(f"   - [{r.document.id}] {r.document.title}: {r.score:.4f}")

        # Fulltext search
        print("\n2. Fulltext search for 'retrieval appliance'...")
        results = search_repo.fulltext_search("retrieval appliance", limit=5)
        print(f"   Found {len(results)} results")
        for r in results:
            print(f"   - [{r.document.id}] {r.document.title}: {r.score:.4f}")

        # MaxSim search
        print("\n3. MaxSim search for 'BM25 lexical' (256-dim)...")
        results = search_repo.maxsim_search("BM25 lexical", limit=5, embed_dim=256)
        print(f"   Found {len(results)} results")
        for r in results:
            print(f"   - [{r.document.id}] {r.document.title}: {r.score:.4f}")


def main():
    print("\n" + "=" * 60)
    print("JMFTS Embedding & Document Test")
    print("=" * 60)

    # Test embedding service
    if not test_embedding_service():
        print("\nEmbedding service test failed!")
        return 1

    # Test document creation
    try:
        doc_id = test_document_creation()
    except Exception as e:
        print(f"\nDocument creation failed: {e}")
        print("Make sure you've run 'jmfts-init-db' first.")
        return 1

    # Test search
    try:
        test_search(doc_id)
    except Exception as e:
        print(f"\nSearch test failed: {e}")
        return 1

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
