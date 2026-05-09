from __future__ import annotations

import hashlib
import json
import os
import time
from typing import List, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import SparseVector
from codebase_rag.services.knowledge_sync.models import KnowledgeSyncJob
from codebase_rag.services.qdrant_storage.models import PointData

logger = get_logger(__name__)


class KnowledgeSyncStore:
    def __init__(self, store_dir: str) -> None:
        self._store_dir = store_dir

    def ensure_dir(self) -> None:
        os.makedirs(self._jobs_dir, exist_ok=True)
        os.makedirs(self._latest_dir, exist_ok=True)
        logger.info("knowledge_sync_store_ready", store_dir=self._store_dir)

    @property
    def _jobs_dir(self) -> str:
        return os.path.join(self._store_dir, "jobs")

    @property
    def _latest_dir(self) -> str:
        return os.path.join(self._store_dir, "latest")

    def _project_dir(self, project_id: str) -> str:
        return os.path.join(self._jobs_dir, project_id)

    def _job_path(self, project_id: str, file_version_id: str) -> str:
        return os.path.join(self._project_dir(project_id), f"{file_version_id}.json")

    def _latest_path(self, project_id: str, file_path: str) -> str:
        digest = hashlib.sha256(file_path.replace("\\", "/").encode("utf-8")).hexdigest()
        return os.path.join(self._latest_dir, project_id, f"{digest}.json")

    def put_job(self, job: KnowledgeSyncJob) -> None:
        now = time.time()
        if job.created_at <= 0:
            job.created_at = now
        job.updated_at = now
        path = self._job_path(job.project_id, job.file_version_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = self._serialize_job(job)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        logger.debug(
            "knowledge_sync_job_put",
            project_id=job.project_id,
            file_path=job.file_path,
            file_version_id=job.file_version_id[:16],
            source=job.source,
            points=len(job.points),
        )

    def get_job(self, project_id: str, file_version_id: str) -> Optional[KnowledgeSyncJob]:
        path = self._job_path(project_id, file_version_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return self._deserialize_job(json.load(f))
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(
                "knowledge_sync_job_get_failed",
                project_id=project_id,
                file_version_id=file_version_id[:16],
                error=str(e),
            )
            return None

    def remove_job(self, project_id: str, file_version_id: str) -> None:
        path = self._job_path(project_id, file_version_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning(
                "knowledge_sync_job_remove_failed",
                project_id=project_id,
                file_version_id=file_version_id[:16],
                error=str(e),
            )

    def set_latest(self, project_id: str, file_path: str, file_version_id: str) -> None:
        path = self._latest_path(project_id, file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "project_id": project_id,
            "file_path": file_path,
            "file_version_id": file_version_id,
            "updated_at": time.time(),
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)

    def get_latest(self, project_id: str, file_path: str) -> Optional[str]:
        path = self._latest_path(project_id, file_path)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("project_id") != project_id:
                return None
            if data.get("file_path") != file_path:
                return None
            fvid = data.get("file_version_id")
            return fvid if isinstance(fvid, str) and fvid else None
        except (json.JSONDecodeError, OSError):
            return None

    def cancel_stale_file_jobs(
        self,
        project_id: str,
        file_path: str,
        latest_file_version_id: str,
    ) -> int:
        cancelled = 0
        for job in self.iter_project_jobs(project_id):
            if job.file_path != file_path:
                continue
            if job.file_version_id == latest_file_version_id:
                continue
            job.status = "cancelled"
            job.last_error = "cancelled_by_newer_file_version"
            self.put_job(job)
            cancelled += 1
        if cancelled:
            logger.info(
                "knowledge_sync_stale_jobs_cancelled",
                project_id=project_id,
                file_path=file_path,
                latest_file_version_id=latest_file_version_id[:16],
                cancelled=cancelled,
            )
        return cancelled

    def iter_project_jobs(self, project_id: str) -> List[KnowledgeSyncJob]:
        project_dir = self._project_dir(project_id)
        if not os.path.isdir(project_dir):
            return []
        jobs: List[KnowledgeSyncJob] = []
        for fname in os.listdir(project_dir):
            if not fname.endswith(".json"):
                continue
            fvid = fname[:-5]
            job = self.get_job(project_id, fvid)
            if job is not None:
                jobs.append(job)
        return jobs

    def iter_all_jobs(self) -> List[KnowledgeSyncJob]:
        if not os.path.isdir(self._jobs_dir):
            return []
        jobs: List[KnowledgeSyncJob] = []
        for project_id in os.listdir(self._jobs_dir):
            project_dir = os.path.join(self._jobs_dir, project_id)
            if os.path.isdir(project_dir):
                jobs.extend(self.iter_project_jobs(project_id))
        return jobs

    def rebrand_project_id(self, old_project_id: str, new_project_id: str) -> int:
        if old_project_id == new_project_id:
            return 0
        affected = 0
        old_project_dir = self._project_dir(old_project_id)
        if os.path.isdir(old_project_dir):
            os.makedirs(self._project_dir(new_project_id), exist_ok=True)
            for job in self.iter_project_jobs(old_project_id):
                self.remove_job(old_project_id, job.file_version_id)
                job.project_id = new_project_id
                self.put_job(job)
                affected += 1
            try:
                if not os.listdir(old_project_dir):
                    os.rmdir(old_project_dir)
            except OSError:
                pass

        old_latest_dir = os.path.join(self._latest_dir, old_project_id)
        if os.path.isdir(old_latest_dir):
            os.makedirs(os.path.join(self._latest_dir, new_project_id), exist_ok=True)
            for fname in os.listdir(old_latest_dir):
                old_path = os.path.join(old_latest_dir, fname)
                if not fname.endswith(".json") or not os.path.isfile(old_path):
                    continue
                try:
                    with open(old_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["project_id"] = new_project_id
                    new_path = os.path.join(self._latest_dir, new_project_id, fname)
                    tmp_path = new_path + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.replace(tmp_path, new_path)
                    os.remove(old_path)
                except (json.JSONDecodeError, OSError):
                    continue
            try:
                if not os.listdir(old_latest_dir):
                    os.rmdir(old_latest_dir)
            except OSError:
                pass
        return affected

    @staticmethod
    def _serialize_job(job: KnowledgeSyncJob) -> dict:
        return {
            "project_id": job.project_id,
            "file_path": job.file_path,
            "blob_hash": job.blob_hash,
            "file_version_id": job.file_version_id,
            "source": job.source,
            "status": job.status,
            "old_file_version_id": job.old_file_version_id,
            "retry_count": job.retry_count,
            "last_error": job.last_error,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "points": [
                {
                    "file_path": p.file_path,
                    "entity_type": p.entity_type,
                    "entity_name": p.entity_name,
                    "business_description": p.business_description,
                    "blob_hash": p.blob_hash,
                    "dense_vector": p.dense_vector,
                    "sparse_vector": {
                        "indices": p.sparse_vector.indices,
                        "values": p.sparse_vector.values,
                    },
                }
                for p in job.points
            ],
        }

    @staticmethod
    def _deserialize_job(data: dict) -> KnowledgeSyncJob:
        points = []
        for p in data["points"]:
            sv = p["sparse_vector"]
            points.append(PointData(
                file_path=p["file_path"],
                entity_type=p["entity_type"],
                entity_name=p["entity_name"],
                business_description=p["business_description"],
                blob_hash=p["blob_hash"],
                dense_vector=p["dense_vector"],
                sparse_vector=SparseVector(
                    indices=sv["indices"],
                    values=sv["values"],
                ),
            ))
        return KnowledgeSyncJob(
            project_id=data["project_id"],
            file_path=data["file_path"],
            blob_hash=data["blob_hash"],
            file_version_id=data["file_version_id"],
            source=data["source"],
            status=data.get("status", "ready"),
            old_file_version_id=data.get("old_file_version_id"),
            retry_count=int(data.get("retry_count", 0)),
            last_error=data.get("last_error", ""),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            points=points,
        )
