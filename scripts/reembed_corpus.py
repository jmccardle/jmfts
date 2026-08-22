#!/usr/bin/env python3
"""
Re-embed all documents with nomic task prefixes and updated token tiers.

Uses batch processing for GPU efficiency. Processes all documents that have
content, re-generating both document-level and token-level embeddings.

Usage:
    python scripts/reembed_corpus.py                        # all documents
    python scripts/reembed_corpus.py multihop:chunk          # specific usetype only
    python scripts/reembed_corpus.py --continue              # resume after crash
    python scripts/reembed_corpus.py --continue multihop:chunk  # resume specific usetype
"""

import sys
import time

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/john/Development/jmfts")

from sqlalchemy import func, text
from jmfts_core.database import get_session
from jmfts_core.embedding import get_embedding_service
from jmfts_core.models.document import Document
from jmfts_core.models.token_embedding import TokenEmbedding
from jmfts_core.config import get_settings


def main():
    settings = get_settings()
    batch_size = settings.token_batch_size
    top_percent = settings.token_top_percent

    # Parse CLI args
    args = sys.argv[1:]
    continue_mode = "--continue" in args
    args = [a for a in args if a != "--continue"]
    usetype_filter = args[0] if args else None

    print(f"Re-embedding corpus with nomic 'search_document:' prefix", flush=True)
    print(f"Device: {settings.embedding_device}, Batch size: {batch_size}, "
          f"Token percent: {top_percent*100:.0f}%", flush=True)
    if continue_mode:
        print("Mode: CONTINUE (skipping already-embedded docs)", flush=True)
    if usetype_filter:
        print(f"Filtering to usetype: {usetype_filter}", flush=True)
    print("=" * 60, flush=True)

    with get_session() as session:
        # Get documents with content
        query = session.query(Document).filter(Document.content.isnot(None), Document.content != "")
        if usetype_filter:
            query = query.filter(Document.usetype == usetype_filter)

        if continue_mode:
            # Skip docs that already have token embeddings
            already_done = session.query(TokenEmbedding.document_id).distinct().subquery()
            query = query.filter(~Document.id.in_(session.query(already_done)))

        docs = query.all()

        print(f"Found {len(docs)} documents to re-embed", flush=True)
        if not docs:
            print("Nothing to do.", flush=True)
            return

        # Preload content to avoid detached session issues
        doc_data = [(d.id, d.content) for d in docs]
        doc_ids_set = {d[0] for d in doc_data}

        if not continue_mode:
            # Clear existing token embeddings for these documents
            print("Clearing existing token embeddings...", flush=True)
            if usetype_filter:
                deleted = session.query(TokenEmbedding).filter(
                    TokenEmbedding.document_id.in_(doc_ids_set)
                ).delete(synchronize_session=False)
            else:
                deleted = session.query(TokenEmbedding).delete()
            session.commit()
            print(f"  Deleted {deleted} token embeddings", flush=True)

        # Get embedding service (triggers model load)
        print("Loading embedding model...", flush=True)
        service = get_embedding_service()
        # Warm up — first call loads weights
        _ = service.embed_text("warmup", prefix="search_document: ")
        print(f"  Model loaded on {service.device}", flush=True)

        start_time = time.time()
        processed = 0

        # Process in batches
        for batch_start in range(0, len(doc_data), batch_size):
            batch = doc_data[batch_start:batch_start + batch_size]
            doc_ids = [d[0] for d in batch]
            contents = [d[1] for d in batch]

            # Batch embed with nomic task prefix
            results = service.embed_batch_with_tokens(
                contents, top_percent=top_percent, prefix="search_document: "
            )

            # Store results
            for doc_id, result in zip(doc_ids, results):
                # Update document embedding
                doc = session.get(Document, doc_id)
                doc.embed = result.document_embedding.tolist()

                # Sort tokens by importance and assign tiers (5-50)
                sorted_tokens = sorted(
                    result.token_embeddings,
                    key=lambda t: t.importance_score,
                    reverse=True
                )
                n_tokens = len(sorted_tokens)
                tier_size = max(1, n_tokens // 10)  # 10 tiers

                for i, tok in enumerate(sorted_tokens):
                    tier_idx = min(i // tier_size, 9)
                    tier = (tier_idx + 1) * 5  # 5, 10, 15, 20, 25, 30, 35, 40, 45, 50

                    token_emb = TokenEmbedding(
                        document_id=doc_id,
                        token_idx=tok.token_idx,
                        token_text=tok.token_text,
                        importance_score=tok.importance_score,
                        tier=tier,
                    )

                    # Store 256-dim embedding only
                    truncated = service.truncate_embedding(tok.embedding, 256)
                    token_emb.embed_256 = truncated.tolist()

                    session.add(token_emb)

            processed += len(batch)

            # Commit and report progress
            session.commit()
            elapsed = time.time() - start_time
            rate = processed / elapsed
            eta = (len(doc_data) - processed) / rate if rate > 0 else 0
            print(f"  {processed}/{len(doc_data)} docs ({rate:.1f}/s, ETA: {eta:.0f}s)", flush=True)

        # Final stats
        total_tokens = session.query(TokenEmbedding).count()
        elapsed = time.time() - start_time

        print(f"\nComplete!", flush=True)
        print(f"  {len(doc_data)} documents re-embedded in {elapsed:.0f}s", flush=True)
        print(f"  {total_tokens} token embeddings stored", flush=True)
        if len(doc_data) > 0:
            print(f"  Avg {total_tokens/len(doc_data):.0f} tokens/doc", flush=True)

        # Show tier distribution
        tier_counts = session.query(
            TokenEmbedding.tier, func.count(TokenEmbedding.id)
        ).group_by(TokenEmbedding.tier).order_by(TokenEmbedding.tier).all()

        print(f"\nTier distribution:", flush=True)
        for tier, count in tier_counts:
            print(f"  tier={tier}: {count:,} tokens", flush=True)


if __name__ == "__main__":
    main()
