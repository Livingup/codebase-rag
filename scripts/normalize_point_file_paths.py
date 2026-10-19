"""Normalize file paths of existing points in Qdrant collections to canonical POSIX format.

Scans points across Qdrant collections (e.g. 'codebase', 'project_source_qa'),
identifies points whose `file_path` contains backslashes '\\', leading slashes,
or redundant '//', and updates their payload with canonical POSIX relative paths.
Optionally also scans and normalizes local KnowledgeSync JSON files on disk.

Usage:
    # Dry-run preview
    python scripts/normalize_point_file_paths.py --dry-run

    # Execute normalization on default 'codebase' collection
    python scripts/normalize_point_file_paths.py

    # Normalize all collections and local sync data
    python scripts/normalize_point_file_paths.py --all-collections
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from qdrant_client import QdrantClient


def canonicalize_path(raw_path: str) -> str:
    """Normalize file path to canonical POSIX relative path:
    1. Replace all backslashes '\\' with forward slashes '/'
    2. Strip leading and trailing whitespace
    3. Strip leading slashes '/' (ensure relative path)
    4. Collapse multiple consecutive slashes '//'
    """
    if not raw_path:
        return ""
    norm = raw_path.replace("\\", "/").strip().lstrip("/")
    while "//" in norm:
        norm = norm.replace("//", "/")
    return norm


class PointNormalizer:
    def __init__(
        self,
        client: QdrantClient,
        collections: List[str],
        batch_size: int = 500,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.collections = collections
        self.batch_size = batch_size
        self.dry_run = dry_run

    def run(self) -> Dict[str, Tuple[int, int]]:
        """Run normalization across collections.

        Returns:
            Dict mapping collection_name -> (scanned_count, fixed_count)
        """
        results = {}
        for coll in self.collections:
            print(f"\n{'=' * 60}")
            print(f"Scanning collection: '{coll}'")
            print(f"{'=' * 60}")
            scanned, fixed = self._process_collection(coll)
            results[coll] = (scanned, fixed)
        return results

    def _process_collection(self, collection: str) -> Tuple[int, int]:
        offset = None
        total_scanned = 0
        to_fix: List[Tuple[str | int, str, str, Optional[str]]] = []  # (point_id, old_fp, new_fp, fixed_entity_name)

        # Step 1: Scan
        while True:
            scroll_result = self.client.scroll(
                collection_name=collection,
                limit=self.batch_size,
                offset=offset,
                with_payload=["file_path", "entity_name"],
                with_vectors=False,
            )
            points, next_offset = scroll_result
            if not points:
                break

            total_scanned += len(points)
            for p in points:
                payload = p.payload or {}
                old_fp = payload.get("file_path", "")
                if not old_fp or not isinstance(old_fp, str):
                    continue

                new_fp = canonicalize_path(old_fp)
                fp_needs_fix = (new_fp != old_fp)

                old_name = payload.get("entity_name", "")
                name_needs_fix = isinstance(old_name, str) and ("\\" in old_name)
                fixed_name = old_name.replace("\\", "/") if name_needs_fix else None

                if fp_needs_fix or name_needs_fix:
                    to_fix.append((p.id, old_fp, new_fp, fixed_name))

            offset = next_offset
            if offset is None:
                break

        print(f"Total points scanned: {total_scanned}")
        print(f"Points requiring normalization: {len(to_fix)}")

        if not to_fix:
            print("[OK] All points in this collection already have canonical POSIX paths.")
            return total_scanned, 0

        # Step 2: Display preview
        print("\nSample fixes:")
        for pid, old_fp, new_fp, fixed_name in to_fix[:10]:
            print(f"  Point ID: {pid}")
            print(f"    file_path:   {old_fp}")
            print(f"              -> {new_fp}")
            if fixed_name is not None:
                print(f"    entity_name: -> {fixed_name}")

        if len(to_fix) > 10:
            print(f"  ... and {len(to_fix) - 10} more points.")

        if self.dry_run:
            print(f"\n[DRY RUN] Would update {len(to_fix)} points in '{collection}'. No changes made.")
            return total_scanned, len(to_fix)

        # Step 3: Apply fixes
        print(f"\nApplying payload updates to {len(to_fix)} points...")
        start = time.monotonic()
        updated_count = 0

        for pid, old_fp, new_fp, fixed_name in to_fix:
            update_payload = {"file_path": new_fp}
            if fixed_name is not None:
                update_payload["entity_name"] = fixed_name

            try:
                self.client.set_payload(
                    collection_name=collection,
                    payload=update_payload,
                    points=[pid],
                    wait=True,
                )
                updated_count += 1
            except Exception as e:
                print(f"  ERROR updating point {pid}: {e}", file=sys.stderr)

        elapsed = time.monotonic() - start
        print(f"[OK] Successfully updated {updated_count}/{len(to_fix)} points in {elapsed:.2f}s.")
        return total_scanned, updated_count


def normalize_sync_data(sync_data_dir: str, dry_run: bool = False) -> Tuple[int, int]:
    """Scan and normalize local KnowledgeSync JSON files on disk."""
    if not os.path.isdir(sync_data_dir):
        print(f"Sync data directory does not exist: {sync_data_dir}")
        return 0, 0

    print(f"\n{'=' * 60}")
    print(f"Scanning local KnowledgeSync directory: {sync_data_dir}")
    print(f"{'=' * 60}")

    total_scanned = 0
    fixed_files = 0

    for root, _, files in os.walk(sync_data_dir):
        for fname in files:
            if not fname.endswith(".json") or fname.endswith(".tmp"):
                continue

            file_path = os.path.join(root, fname)
            total_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue

            changed = False

            # Check job-level file_path
            if "file_path" in data and isinstance(data["file_path"], str):
                orig = data["file_path"]
                canon = canonicalize_path(orig)
                if canon != orig:
                    data["file_path"] = canon
                    changed = True

            # Check points-level file_path
            if "points" in data and isinstance(data["points"], list):
                for pt in data["points"]:
                    if isinstance(pt, dict) and "file_path" in pt:
                        orig = pt["file_path"]
                        canon = canonicalize_path(orig)
                        if canon != orig:
                            pt["file_path"] = canon
                            changed = True

            if changed:
                fixed_files += 1
                if dry_run:
                    print(f"  [DRY RUN] Would update: {file_path}")
                else:
                    tmp_path = file_path + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, file_path)
                    print(f"  [OK] Updated: {file_path}")

    print(f"Total sync files scanned: {total_scanned}")
    print(f"Sync files updated: {fixed_files}")
    return total_scanned, fixed_files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize stored point file paths in Qdrant to canonical POSIX format."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying Qdrant or files.",
    )
    parser.add_argument(
        "--collection",
        default="codebase",
        help="Specific collection to normalize (default: 'codebase').",
    )
    parser.add_argument(
        "--all-collections",
        action="store_true",
        help="Normalize all available collections in Qdrant.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for scrolling points (default: 500).",
    )
    parser.add_argument(
        "--sync-data-dir",
        default=None,
        help="Optional path to local knowledge_sync directory to normalize JSON files.",
    )
    parser.add_argument(
        "--skip-sync-data",
        action="store_true",
        help="Skip scanning local knowledge_sync directory.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to .env file.",
    )

    args = parser.parse_args()

    # Load environment variables
    env_file = args.env_file
    if not env_file:
        candidate = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.isfile(candidate):
            env_file = candidate

    if env_file and os.path.isfile(env_file):
        load_dotenv(env_file)
        print(f"Loaded environment from: {env_file}")

    qdrant_host = os.getenv("QDRANT_HOST", "localhost")
    qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
    qdrant_api_key = os.getenv("QDRANT_API_KEY") or None
    qdrant_https = os.getenv("QDRANT_HTTPS", "false").lower() in ("true", "1")

    print(f"Connecting to Qdrant at {qdrant_host}:{qdrant_port} (https={qdrant_https})...")
    client = QdrantClient(
        host=qdrant_host,
        port=qdrant_port,
        api_key=qdrant_api_key,
        https=qdrant_https,
        timeout=60,
    )

    try:
        available_colls = [c.name for c in client.get_collections().collections]
        print(f"Available collections: {available_colls}")

        if args.all_collections:
            target_colls = available_colls
        else:
            if args.collection not in available_colls:
                print(f"ERROR: Collection '{args.collection}' not found in Qdrant.", file=sys.stderr)
                sys.exit(1)
            target_colls = [args.collection]

        normalizer = PointNormalizer(
            client=client,
            collections=target_colls,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        normalizer.run()

        # Optional sync data dir
        if not args.skip_sync_data:
            sync_dir = args.sync_data_dir
            if not sync_dir:
                default_sync = os.path.join(
                    os.getenv("CODEBASE_RAG_DATA_DIR", "C:/ProgramData/CodebaseRAG"),
                    "data",
                    "knowledge_sync",
                )
                if os.path.isdir(default_sync):
                    sync_dir = default_sync

            if sync_dir:
                normalize_sync_data(sync_dir, dry_run=args.dry_run)

    finally:
        client.close()

    print("\n[OK] Normalization process completed.")


if __name__ == "__main__":
    main()
