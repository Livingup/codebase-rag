"""FastAPI routes for Management API Service.

All endpoints are async. Error handling uses HTTPException with standard error format.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from codebase_rag.services.management_api.models import (
    BatchRegisterProjectRequest,
    BatchRegisterProjectResponse,
    ClientStatusResponse,
    DeleteProjectResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    ProjectDetailResponse,
    ProjectResponse,
    ProjectStatusResponse,
    RebuildRequest,
    RegisterProjectRequest,
    ReloadConfigResponse,
    SearchRequest,
    SearchResponse,
    TaskResponse,
)
from codebase_rag.services.management_api.admin_ui import require_local_request
from codebase_rag.services.management_api.service import ManagementApiService
from codebase_rag.services.management_api.models import (
    InvalidRequestError,
    ManagementApiError,
    ProjectNotFoundError,
    ServiceUnavailableError,
)

from typing import List


def _error(status_code: int, code: str, message: str):
    """Raise HTTPException with standard error body."""
    raise HTTPException(
        status_code=status_code,
        detail=ErrorResponse(
            error=ErrorDetail(code=code, message=message),
        ).model_dump(),
    )


def create_router(service: ManagementApiService) -> APIRouter:
    """Create FastAPI APIRouter bound to a ManagementApiService instance.

    Args:
        service: Initialized ManagementApiService.

    Returns:
        APIRouter with all management endpoints registered.
    """
    router = APIRouter()

    # =====================================================================
    # 2.1 Project management
    # =====================================================================

    @router.post("/projects", response_model=ProjectResponse, status_code=201)
    async def register_project(req: RegisterProjectRequest):
        try:
            return await service.register_project(
                git_repo_path=req.git_repo_path,
                display_name=req.display_name,
            )
        except ManagementApiError as e:
            _error(400, "INVALID_REQUEST", str(e))

    @router.post(
        "/projects/batch",
        response_model=BatchRegisterProjectResponse,
        status_code=201,
    )
    async def batch_register_projects(req: BatchRegisterProjectRequest):
        return await service.batch_register_projects(
            [p.model_dump() for p in req.projects],
        )

    @router.get("/projects", response_model=List[ProjectResponse])
    async def list_projects():
        return await service.list_projects()

    @router.get("/projects/{project_id}", response_model=ProjectDetailResponse)
    async def get_project(project_id: str):
        try:
            return await service.get_project(project_id)
        except ProjectNotFoundError as e:
            _error(404, "PROJECT_NOT_FOUND", str(e))

    @router.delete("/projects/{project_id}", response_model=DeleteProjectResponse)
    async def delete_project(project_id: str):
        try:
            return await service.delete_project(project_id)
        except ProjectNotFoundError as e:
            _error(404, "PROJECT_NOT_FOUND", str(e))

    # =====================================================================
    # 2.2 Semantic search
    # =====================================================================

    @router.post(
        "/projects/{project_id}/search", response_model=SearchResponse,
    )
    async def search(project_id: str, req: SearchRequest):
        try:
            return await service.search(
                project_id=project_id,
                query=req.query,
                top_k=req.top_k,
                score_threshold=req.score_threshold,
                entity_type=req.filters.entity_type if req.filters else None,
                file_path_prefix=req.filters.file_path_prefix if req.filters else None,
            )
        except ProjectNotFoundError as e:
            _error(404, "PROJECT_NOT_FOUND", str(e))
        except ServiceUnavailableError as e:
            _error(503, "SERVICE_UNAVAILABLE", str(e))

    # =====================================================================
    # 2.3 Manual triggers
    # =====================================================================

    @router.post(
        "/projects/{project_id}/rebuild", response_model=TaskResponse,
    )
    async def rebuild(project_id: str, req: RebuildRequest = RebuildRequest()):
        try:
            return await service.trigger_rebuild(project_id, force=req.force)
        except ProjectNotFoundError as e:
            _error(404, "PROJECT_NOT_FOUND", str(e))

    @router.post(
        "/projects/{project_id}/sync", response_model=TaskResponse,
    )
    async def sync(project_id: str):
        try:
            return await service.trigger_sync(project_id)
        except ProjectNotFoundError as e:
            _error(404, "PROJECT_NOT_FOUND", str(e))

    # =====================================================================
    # 2.4 Status queries
    # =====================================================================

    @router.get(
        "/projects/{project_id}/status", response_model=ProjectStatusResponse,
    )
    async def project_status(project_id: str):
        try:
            return await service.get_project_status(project_id)
        except ProjectNotFoundError as e:
            _error(404, "PROJECT_NOT_FOUND", str(e))

    # =====================================================================
    # 2.5 System endpoints
    # =====================================================================

    @router.get("/health", response_model=HealthResponse)
    async def health():
        return service.get_health()

    @router.get("/status/clients", response_model=ClientStatusResponse)
    async def client_status(request: Request, live: bool = False):
        require_local_request(request)
        return await service.get_client_status(live=live)

    @router.post("/status/clients/{client_id}/probe", response_model=ClientStatusResponse)
    async def probe_client_status(request: Request, client_id: str):
        require_local_request(request)
        try:
            return await service.probe_client_status(client_id)
        except InvalidRequestError as e:
            _error(404, "CLIENT_STATUS_ITEM_NOT_FOUND", str(e))

    @router.post("/config/reload", response_model=ReloadConfigResponse)
    async def reload_config():
        try:
            result = await service.reload_config()
            return ReloadConfigResponse(
                reloaded=True,
                message="Configuration reloaded successfully",
                requires_restart=result.get("requires_restart", False),
                restart_reasons=result.get("restart_reasons", []),
                reconciled_services=result.get("reconciled_services", []),
            )
        except Exception as e:
            _error(500, "RELOAD_ERROR", f"Config reload failed: {e}")

    return router
