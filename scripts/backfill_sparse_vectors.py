"""Backfill missing sparse vectors for points in the codebase collection.

Scans all points, identifies those with dense_vectors but no sparse_vectors,
calls DashScope API to generate sparse embeddings from business_description,
and updates only the sparse_vectors using Qdrant's update_vectors API.

Usage:
    python scripts/backfill_sparse_vectors.py [--dry-run] [--batch-size 10]

Requires:
    - .env file with DASHSCOPE_API_KEY
    - Qdrant server running at localhost:6333
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointVectors,
    SparseVector as QdrantSparseVector,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COLLECTION_DEFAULT = "codebase"
QDRANT_HOST = "127.0.0.1"
QDRANT_PORT = 6333
SCROLL_BATCH = 100

DASHSCOPE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "text-embedding/text-embedding"
)
DASHSCOPE_MODEL = "text-embedding-v4"
DASHSCOPE_DIMENSION = 2048
EMBED_BATCH_SIZE = 10          # DashScope max 25, keep conservative
EMBED_TIMEOUT = 30
SPARSE_VECTOR_NAME = "sparse_vectors"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class PointToFix:
    point_id: str | int
    business_description: str


# ---------------------------------------------------------------------------
# Step 1: Scan and find points missing sparse vectors
# ---------------------------------------------------------------------------

def scan_missing_sparse(client: QdrantClient, collection: str) -> List[PointToFix]:
    """Scroll through all points and find those without sparse_vectors."""
    missing: List[PointToFix] = []
    has_both = 0
    no_desc = 0
    offset = None

    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=SCROLL_BATCH,
            offset=offset,
            with_vectors=True,
            with_payload=True,
        )

        for p in points:
            vectors = p.vector or {}
            has_dense = "dense_vectors" in vectors and len(vectors["dense_vectors"]) > 0
            has_sparse = (
                "sparse_vectors" in vectors
                and hasattr(vectors["sparse_vectors"], "indices")
                and len(vectors["sparse_vectors"].indices) > 0
            )

            if has_dense and has_sparse:
                has_both += 1
                continue

            if has_sparse:
                # sparse-only — skip
                continue

            # Missing sparse — need business_description to generate
            payload = p.payload or {}
            desc = payload.get("business_description", "")
            if not desc:
                no_desc += 1
                continue

            missing.append(PointToFix(point_id=p.id, business_description=desc))

        if next_offset is None:
            break
        offset = next_offset

    print(f"Scan complete:")
    print(f"  Already have both vectors: {has_both}")
    print(f"  Missing sparse (fixable):  {len(missing)}")
    print(f"  Missing sparse (no desc):  {no_desc}")
    return missing


# ---------------------------------------------------------------------------
# Step 2: Generate sparse vectors via DashScope
# ---------------------------------------------------------------------------

async def generate_sparse_batch(
    http_client: httpx.AsyncClient,
    api_key: str,
    texts: List[str],
) -> List[Optional[QdrantSparseVector]]:
    """Call DashScope to generate sparse embeddings for a batch of texts."""
    body = {
        "model": DASHSCOPE_MODEL,
        "input": {"texts": texts},
        "parameters": {
            "text_type": "document",
            "dimension": DASHSCOPE_DIMENSION,
            "output_type": "sparse",
        },
    }

    resp = await http_client.post(
        DASHSCOPE_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code"):
        raise RuntimeError(
            f"DashScope error: code={data['code']}, message={data.get('message')}"
        )

    embeddings = data.get("output", {}).get("embeddings", [])
    embeddings.sort(key=lambda x: x.get("text_index", 0))

    results: List[Optional[QdrantSparseVector]] = []
    for emb in embeddings:
        sparse_data = emb.get("sparse_embedding", [])
        if not sparse_data:
            results.append(None)
            continue
        indices = [item["index"] for item in sparse_data]
        values = [item["value"] for item in sparse_data]
        results.append(QdrantSparseVector(indices=indices, values=values))

    return results


# ---------------------------------------------------------------------------
# Step 3: Update vectors in Qdrant
# ---------------------------------------------------------------------------

def update_sparse_vectors(
    client: QdrantClient,
    collection: str,
    point_ids: list,
    sparse_vectors: List[QdrantSparseVector],
) -> None:
    """Use update_vectors to set sparse_vectors on existing points."""
    point_vectors = []
    for pid, sv in zip(point_ids, sparse_vectors):
        if sv is None:
            continue
        point_vectors.append(
            PointVectors(
                id=pid,
                vector={SPARSE_VECTOR_NAME: sv},
            )
        )

    if point_vectors:
        client.update_vectors(
            collection_name=collection,
            points=point_vectors,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dry_run: bool, batch_size: int, collection: str) -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY not found in .env")
        sys.exit(1)

    print(f"DashScope key: ...{api_key[-6:]}")
    print(f"Collection: {collection}")
    print(f"Batch size: {batch_size}")
    print(f"Dry run: {dry_run}")
    print()

    # Step 1: Scan
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=30)
    missing = scan_missing_sparse(client, collection)

    if not missing:
        print("\nNo points need fixing. Done!")
        client.close()
        return

    if dry_run:
        print(f"\n[DRY RUN] Would fix {len(missing)} points. Exiting.")
        client.close()
        return

    # Step 2 + 3: Generate and update in batches
    print(f"\nBackfilling {len(missing)} points...")
    start = time.monotonic()
    fixed = 0
    failed = 0

    async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as http_client:
        for i in range(0, len(missing), batch_size):
            batch = missing[i : i + batch_size]
            texts = [p.business_description for p in batch]
            point_ids = [p.point_id for p in batch]

            try:
                sparse_vecs = await generate_sparse_batch(http_client, api_key, texts)

                # Filter out None results
                valid_ids = []
                valid_vecs = []
                for pid, sv in zip(point_ids, sparse_vecs):
                    if sv is not None:
                        valid_ids.append(pid)
                        valid_vecs.append(sv)

                if valid_vecs:
                    update_sparse_vectors(client, collection, valid_ids, valid_vecs)
                    fixed += len(valid_vecs)

                skipped = len(batch) - len(valid_vecs)
                if skipped:
                    failed += skipped

                progress = i + len(batch)
                elapsed = time.monotonic() - start
                print(
                    f"  [{progress}/{len(missing)}] "
                    f"fixed={fixed}, failed={failed}, "
                    f"elapsed={elapsed:.1f}s"
                )

            except Exception as e:
                failed += len(batch)
                print(f"  [{i}/{len(missing)}] BATCH FAILED: {e}")

    elapsed = time.monotonic() - start
    client.close()

    print()
    print("=" * 50)
    print("Backfill complete!")
    print(f"  Fixed:   {fixed}")
    print(f"  Failed:  {failed}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Backfill missing sparse vectors")
    parser.add_argument("--dry-run", action="store_true", help="Only scan, don't fix")
    parser.add_argument(
        "--batch-size", type=int, default=EMBED_BATCH_SIZE,
        help=f"Texts per DashScope API call (default: {EMBED_BATCH_SIZE})",
    )
    parser.add_argument(
        "--collection", type=str, default=COLLECTION_DEFAULT,
        help=f"Qdrant collection name (default: {COLLECTION_DEFAULT})",
    )
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, batch_size=args.batch_size, collection=args.collection))


if __name__ == "__main__":
    main()
