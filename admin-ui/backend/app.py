"""
Phase 1 admin backend for server-config.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading
import time

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx
import uvicorn

from config import get_settings
from auth import (
    authenticate_credentials,
    clear_session_cookie,
    create_session_token,
    get_current_admin,
    set_session_cookie,
)
from schemas import (
    ApiMessage,
    AuthLoginPayload,
    AuthStatusResponse,
    AgentDetailResponse,
    AgentListResponse,
    AgentPayload,
    BotDetailResponse,
    BotListResponse,
    BotPayload,
    MCPServerDetailResponse,
    MCPServerListResponse,
    MCPServerPayload,
    OptionsResponse,
    OverviewResponse,
    RuntimeGatewayResponse,
    RuntimeGatewaySessionDetailResponse,
    RobotDetailResponse,
    RobotListResponse,
    RobotPayload,
    RobotSecretResponse,
    TTSProfileDetailResponse,
    TTSProfileListResponse,
    TTSProfilePayload,
)
from server_config.repository import ConfigRepository


settings = get_settings()
repository = ConfigRepository(settings.database_url)
FRONTEND_DIST_DIR = ROOT_DIR / "admin-ui" / "dist"
FRONTEND_INDEX_FILE = FRONTEND_DIST_DIR / "index.html"
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()

app = FastAPI(title="Admin UI Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


PUBLIC_AUTH_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/health",
}


@app.middleware("http")
async def require_admin_auth(request: Request, call_next):
    path = request.url.path
    protected_docs = path in {"/docs", "/redoc", "/openapi.json"}
    protected_api = path.startswith("/api/") and path not in PUBLIC_AUTH_PATHS

    if (
        not settings.admin_auth_disabled
        and request.method != "OPTIONS"
        and (protected_api or protected_docs)
        and not get_current_admin(request, settings)
    ):
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "请先登录 Admin UI"},
        )

    return await call_next(request)


def _runtime_error_payload(service: str, exc: Exception) -> dict:
    return {
        "success": False,
        "source": "db",
        "message": f"获取 {service} 运行状态失败: {exc}",
        "last_reload_error": str(exc),
    }


def _merge_runtime_status(llm_status: dict, gateway_status: dict, tts_status: dict) -> dict:
    llm_version = llm_status.get("config_version")
    gateway_version = gateway_status.get("config_version")
    tts_version = tts_status.get("config_version")
    in_sync = (
        llm_status.get("success", False)
        and gateway_status.get("success", False)
        and tts_status.get("success", False)
        and llm_version is not None
        and llm_version == gateway_version == tts_version
    )

    errors = [
        message
        for message in (
            llm_status.get("last_reload_error") or llm_status.get("message"),
            gateway_status.get("last_reload_error") or gateway_status.get("message"),
            tts_status.get("last_reload_error") or tts_status.get("message"),
        )
        if message
    ]

    return {
        "success": bool(
            llm_status.get("success", False)
            and gateway_status.get("success", False)
            and tts_status.get("success", False)
        ),
        "source": "db",
        "config_version": llm_version if in_sync else None,
        "loaded_at": llm_status.get("loaded_at") if in_sync else None,
        "bot_count": llm_status.get("bot_count"),
        "robot_count": gateway_status.get("robot_count"),
        "mcp_count": llm_status.get("mcp_count"),
        "default_bot_id": llm_status.get("default_bot_id") if in_sync else None,
        "last_reload_error": " | ".join(errors) if errors else None,
        "mcp_status": llm_status.get("mcp_status", []),
        "in_sync": in_sync,
        "llm": llm_status,
        "gateway": gateway_status,
        "tts": tts_status,
    }


async def fetch_runtime_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            llm_response, gateway_response, tts_response = await asyncio.gather(
                client.get(f"{settings.llm_internal_base_url}/internal/config/status"),
                client.get(f"{settings.gateway_internal_base_url}/internal/config/status"),
                client.get(f"{settings.tts_internal_base_url}/internal/config/status"),
                return_exceptions=True,
            )
    except Exception as exc:
        llm_status = _runtime_error_payload("LLM", exc)
        gateway_status = _runtime_error_payload("Gateway", exc)
        tts_status = _runtime_error_payload("TTS", exc)
        return _merge_runtime_status(llm_status, gateway_status, tts_status)

    if isinstance(llm_response, Exception):
        llm_status = _runtime_error_payload("LLM", llm_response)
    else:
        try:
            llm_response.raise_for_status()
            llm_status = llm_response.json()
        except Exception as exc:
            llm_status = _runtime_error_payload("LLM", exc)

    if isinstance(gateway_response, Exception):
        gateway_status = _runtime_error_payload("Gateway", gateway_response)
    else:
        try:
            gateway_response.raise_for_status()
            gateway_status = gateway_response.json()
        except Exception as exc:
            gateway_status = _runtime_error_payload("Gateway", exc)

    if isinstance(tts_response, Exception):
        tts_status = _runtime_error_payload("TTS", tts_response)
    else:
        try:
            tts_response.raise_for_status()
            tts_status = tts_response.json()
        except Exception as exc:
            tts_status = _runtime_error_payload("TTS", exc)

    return _merge_runtime_status(llm_status, gateway_status, tts_status)


async def _call_config_endpoint(
    client: httpx.AsyncClient,
    *,
    service_name: str,
    base_url: str,
    action: str,
    version: int,
) -> dict:
    method = client.get if action == "validate" else client.post
    try:
        response = await method(
            f"{base_url}/internal/config/{action}",
            params={"version": version},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", True):
            raise RuntimeError(payload.get("message") or f"{service_name} {action} failed")
        return payload
    except Exception as exc:
        raise RuntimeError(f"{service_name} {action} v{version} 失败: {exc}") from exc


async def trigger_runtime_reload() -> dict:
    target_version = repository.get_latest_config_version()
    async with httpx.AsyncClient(timeout=10.0) as client:
        validate_results = await asyncio.gather(
            _call_config_endpoint(
                client,
                service_name="LLM",
                base_url=settings.llm_internal_base_url,
                action="validate",
                version=target_version,
            ),
            _call_config_endpoint(
                client,
                service_name="Gateway",
                base_url=settings.gateway_internal_base_url,
                action="validate",
                version=target_version,
            ),
            _call_config_endpoint(
                client,
                service_name="TTS",
                base_url=settings.tts_internal_base_url,
                action="validate",
                version=target_version,
            ),
            return_exceptions=True,
        )
        validate_errors = [str(result) for result in validate_results if isinstance(result, Exception)]
        if validate_errors:
            raise RuntimeError(
                f"config v{target_version} validate failed: {' | '.join(validate_errors)}"
            )

        llm_status, gateway_status, tts_status = await asyncio.gather(
            _call_config_endpoint(
                client,
                service_name="LLM",
                base_url=settings.llm_internal_base_url,
                action="reload",
                version=target_version,
            ),
            _call_config_endpoint(
                client,
                service_name="Gateway",
                base_url=settings.gateway_internal_base_url,
                action="reload",
                version=target_version,
            ),
            _call_config_endpoint(
                client,
                service_name="TTS",
                base_url=settings.tts_internal_base_url,
                action="reload",
                version=target_version,
            ),
        )

    runtime = _merge_runtime_status(llm_status, gateway_status, tts_status)
    runtime["target_config_version"] = target_version
    return runtime


async def fetch_gateway_runtime() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            gateway_response, llm_response = await asyncio.gather(
                client.get(f"{settings.gateway_internal_base_url}/internal/runtime/robots"),
                client.get(f"{settings.llm_internal_base_url}/internal/config/status"),
                return_exceptions=True,
            )
            if isinstance(gateway_response, Exception):
                raise gateway_response
            gateway_response.raise_for_status()
            payload = gateway_response.json()

            agents = []
            if isinstance(llm_response, Exception):
                payload["agents_error"] = str(llm_response)
            else:
                try:
                    llm_response.raise_for_status()
                    agents = llm_response.json().get("agents", [])
                except Exception as exc:
                    payload["agents_error"] = str(exc)
            payload["agents"] = agents
            return payload
    except Exception as exc:
        return {
            "success": False,
            "stats": {
                "active_connections": 0,
                "total_sessions": 0,
                "registered_sessions": 0,
            },
            "sessions": [],
            "robots": [],
            "agents": [],
            "message": str(exc),
        }


async def fetch_gateway_session_detail(session_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.gateway_internal_base_url}/internal/runtime/sessions/{session_id}"
            )
            if response.status_code == 404:
                payload = response.json()
                return {
                    "success": False,
                    "session": None,
                    "message": payload.get("message") or "会话不存在或已清理",
                }
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {
            "success": False,
            "session": None,
            "message": str(exc),
        }


async def fetch_gateway_traces(limit: int = 100, offset: int = 0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.gateway_internal_base_url}/internal/runtime/traces",
                params={"limit": limit, "offset": offset},
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {
            "success": False,
            "enabled": False,
            "items": [],
            "stats": {},
            "message": str(exc),
        }


async def fetch_gateway_trace_detail(trace_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.gateway_internal_base_url}/internal/runtime/traces/{trace_id}"
            )
            if response.status_code == 404:
                return response.json()
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {
            "success": False,
            "enabled": False,
            "trace": None,
            "message": str(exc),
        }


async def trigger_mcp_reconnect(server_key: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"{settings.llm_internal_base_url}/internal/mcp/{server_key}/reconnect")
        response.raise_for_status()
        return response.json()


def _handle_repo_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _login_rate_limited(key: str) -> bool:
    now = time.time()
    with _login_failures_lock:
        failures = [
            item
            for item in _login_failures.get(key, [])
            if now - item < LOGIN_FAILURE_WINDOW_SECONDS
        ]
        _login_failures[key] = failures
        return len(failures) >= LOGIN_FAILURE_LIMIT


def _record_login_failure(key: str) -> None:
    now = time.time()
    with _login_failures_lock:
        failures = [
            item
            for item in _login_failures.get(key, [])
            if now - item < LOGIN_FAILURE_WINDOW_SECONDS
        ]
        failures.append(now)
        _login_failures[key] = failures


def _clear_login_failures(key: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(key, None)


@app.get("/health", response_model=ApiMessage)
def health() -> ApiMessage:
    return ApiMessage(success=True, message="ok")


@app.post("/api/auth/login", response_model=AuthStatusResponse)
def login(payload: AuthLoginPayload, request: Request, response: Response) -> AuthStatusResponse | JSONResponse:
    client_key = _client_key(request)
    if _login_rate_limited(client_key):
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "authenticated": False,
                "message": "登录失败次数过多，请稍后再试",
            },
        )

    if not authenticate_credentials(payload.username, payload.password, settings):
        _record_login_failure(client_key)
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "authenticated": False,
                "message": "用户名或密码错误",
            },
        )

    _clear_login_failures(client_key)
    token = create_session_token(settings.admin_username, settings)
    set_session_cookie(response, token, settings)
    return AuthStatusResponse(
        success=True,
        authenticated=True,
        username=settings.admin_username,
        message="login success",
    )


@app.post("/api/auth/logout", response_model=ApiMessage)
def logout(response: Response) -> ApiMessage:
    clear_session_cookie(response, settings)
    return ApiMessage(success=True, message="logout success")


@app.get("/api/auth/me", response_model=AuthStatusResponse)
def me(request: Request) -> AuthStatusResponse:
    admin = get_current_admin(request, settings)
    if not admin:
        return AuthStatusResponse(success=True, authenticated=False, message="not authenticated")
    return AuthStatusResponse(
        success=True,
        authenticated=True,
        username=str(admin.get("sub") or settings.admin_username),
    )


@app.get("/api/config/overview", response_model=OverviewResponse)
async def get_overview() -> OverviewResponse:
    return OverviewResponse(
        success=True,
        db=repository.get_overview_db_status(),
        runtime=await fetch_runtime_status(),
        snapshots=repository.list_config_snapshots(limit=10),
    )


@app.post("/api/config/reload", response_model=ApiMessage)
async def reload_runtime() -> ApiMessage:
    try:
        runtime = await trigger_runtime_reload()
        return ApiMessage(success=True, message="reload success", detail=runtime)
    except Exception as exc:
        return ApiMessage(success=False, message="reload failed", detail=str(exc))


@app.post("/api/runtime/mcp/{server_key}/reconnect", response_model=ApiMessage)
async def reconnect_mcp(server_key: str) -> ApiMessage:
    try:
        payload = await trigger_mcp_reconnect(server_key)
        if not payload.get("success", False):
            return ApiMessage(
                success=False,
                message=payload.get("message") or f"{server_key} reconnect failed",
                detail=payload,
            )
        return ApiMessage(success=True, message=f"{server_key} reconnect success", detail=payload)
    except Exception as exc:
        return ApiMessage(success=False, message=f"{server_key} reconnect failed", detail=str(exc))


@app.get("/api/runtime/gateway", response_model=RuntimeGatewayResponse)
async def get_gateway_runtime() -> RuntimeGatewayResponse:
    payload = await fetch_gateway_runtime()
    return RuntimeGatewayResponse(**payload)


@app.get("/api/runtime/gateway/sessions/{session_id}", response_model=RuntimeGatewaySessionDetailResponse)
async def get_gateway_session_detail(session_id: str) -> RuntimeGatewaySessionDetailResponse:
    payload = await fetch_gateway_session_detail(session_id)
    return RuntimeGatewaySessionDetailResponse(**payload)


@app.get("/api/runtime/traces")
async def get_gateway_traces(limit: int = 100, offset: int = 0) -> dict:
    return await fetch_gateway_traces(limit=limit, offset=offset)


@app.get("/api/runtime/traces/{trace_id:path}")
async def get_gateway_trace_detail(trace_id: str) -> dict:
    return await fetch_gateway_trace_detail(trace_id)


@app.get("/api/options", response_model=OptionsResponse)
def get_options() -> OptionsResponse:
    return OptionsResponse(success=True, **repository.get_options())


@app.get("/api/tts-profiles", response_model=TTSProfileListResponse)
def list_tts_profiles() -> TTSProfileListResponse:
    try:
        return TTSProfileListResponse(success=True, items=repository.list_tts_profiles())
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/tts-profiles/{tts_id}", response_model=TTSProfileDetailResponse)
def get_tts_profile(tts_id: str) -> TTSProfileDetailResponse:
    try:
        item = repository.get_tts_profile(tts_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"TTS Profile 不存在: {tts_id}")
        return TTSProfileDetailResponse(success=True, item=item)
    except HTTPException:
        raise
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.post("/api/tts-profiles", response_model=TTSProfileDetailResponse)
def create_tts_profile(payload: TTSProfilePayload) -> TTSProfileDetailResponse:
    try:
        item = repository.upsert_tts_profile(payload.model_dump())
        return TTSProfileDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.put("/api/tts-profiles/{tts_id}", response_model=TTSProfileDetailResponse)
def update_tts_profile(tts_id: str, payload: TTSProfilePayload) -> TTSProfileDetailResponse:
    if tts_id != payload.tts_id:
        raise HTTPException(status_code=400, detail="路径中的 tts_id 与请求体不一致")
    try:
        item = repository.upsert_tts_profile(payload.model_dump())
        return TTSProfileDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.delete("/api/tts-profiles/{tts_id}", response_model=ApiMessage)
def delete_tts_profile(tts_id: str) -> ApiMessage:
    try:
        repository.delete_tts_profile(tts_id)
        return ApiMessage(success=True, message=f"已删除 TTS Profile: {tts_id}")
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/bots", response_model=BotListResponse)
def list_bots() -> BotListResponse:
    return BotListResponse(success=True, items=repository.list_bots())


@app.get("/api/bots/{bot_id}", response_model=BotDetailResponse)
def get_bot(bot_id: str) -> BotDetailResponse:
    item = repository.get_bot(bot_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Bot 不存在: {bot_id}")
    return BotDetailResponse(success=True, item=item)


@app.post("/api/bots", response_model=BotDetailResponse)
def create_bot(payload: BotPayload) -> BotDetailResponse:
    try:
        item = repository.upsert_bot(payload.model_dump())
        return BotDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.put("/api/bots/{bot_id}", response_model=BotDetailResponse)
def update_bot(bot_id: str, payload: BotPayload) -> BotDetailResponse:
    if bot_id != payload.bot_id:
        raise HTTPException(status_code=400, detail="路径中的 bot_id 与请求体不一致")
    try:
        item = repository.upsert_bot(payload.model_dump())
        return BotDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.delete("/api/bots/{bot_id}", response_model=ApiMessage)
def delete_bot(bot_id: str) -> ApiMessage:
    try:
        repository.delete_bot(bot_id)
        return ApiMessage(success=True, message=f"已删除 Bot: {bot_id}")
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/agents", response_model=AgentListResponse)
def list_agents() -> AgentListResponse:
    try:
        return AgentListResponse(success=True, items=repository.list_agents())
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/agents/{agent_id}", response_model=AgentDetailResponse)
def get_agent(agent_id: str) -> AgentDetailResponse:
    try:
        item = repository.get_agent(agent_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
        return AgentDetailResponse(success=True, item=item)
    except HTTPException:
        raise
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.put("/api/agents/{agent_id}", response_model=AgentDetailResponse)
def update_agent(agent_id: str, payload: AgentPayload) -> AgentDetailResponse:
    if agent_id != payload.agent_id:
        raise HTTPException(status_code=400, detail="路径中的 agent_id 与请求体不一致")
    try:
        payload_data = payload.model_dump()
        if "enabled" not in payload.model_fields_set:
            payload_data.pop("enabled", None)
        item = repository.upsert_agent(payload_data)
        return AgentDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/robots", response_model=RobotListResponse)
def list_robots() -> RobotListResponse:
    return RobotListResponse(success=True, items=repository.list_robots())


@app.get("/api/robots/{robot_id}", response_model=RobotDetailResponse)
def get_robot(robot_id: str) -> RobotDetailResponse:
    item = repository.get_robot(robot_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Robot 不存在: {robot_id}")
    return RobotDetailResponse(success=True, item=item)


@app.post("/api/robots", response_model=RobotDetailResponse)
def create_robot(payload: RobotPayload) -> RobotDetailResponse:
    try:
        item = repository.upsert_robot(payload.model_dump())
        return RobotDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.put("/api/robots/{robot_id}", response_model=RobotDetailResponse)
def update_robot(robot_id: str, payload: RobotPayload) -> RobotDetailResponse:
    if robot_id != payload.robot_id:
        raise HTTPException(status_code=400, detail="路径中的 robot_id 与请求体不一致")
    try:
        item = repository.upsert_robot(payload.model_dump())
        return RobotDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.delete("/api/robots/{robot_id}", response_model=ApiMessage)
def delete_robot(robot_id: str) -> ApiMessage:
    try:
        repository.delete_robot(robot_id)
        return ApiMessage(success=True, message=f"已删除 Robot: {robot_id}")
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.post("/api/robots/{robot_id}/secret/reset", response_model=RobotSecretResponse)
def reset_robot_secret(robot_id: str) -> RobotSecretResponse:
    try:
        payload = repository.reset_robot_secret(robot_id)
        return RobotSecretResponse(
            success=True,
            robot_id=payload["robot_id"],
            robot_secret=payload["robot_secret"],
            robot_secret_updated_at=payload["robot_secret_updated_at"],
            message="robot secret 已重置，请立即保存到对应客户端环境变量 ROBOT_SECRET",
        )
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/mcp-servers", response_model=MCPServerListResponse)
def list_mcp_servers() -> MCPServerListResponse:
    try:
        return MCPServerListResponse(success=True, items=repository.list_mcp_servers())
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.get("/api/mcp-servers/{server_key}", response_model=MCPServerDetailResponse)
def get_mcp_server(server_key: str) -> MCPServerDetailResponse:
    try:
        item = repository.get_mcp_server(server_key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"MCP Server 不存在: {server_key}")
        return MCPServerDetailResponse(success=True, item=item)
    except HTTPException:
        raise
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.post("/api/mcp-servers", response_model=MCPServerDetailResponse)
def create_mcp_server(payload: MCPServerPayload) -> MCPServerDetailResponse:
    try:
        item = repository.upsert_mcp_server(payload.model_dump())
        return MCPServerDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.put("/api/mcp-servers/{server_key}", response_model=MCPServerDetailResponse)
def update_mcp_server(server_key: str, payload: MCPServerPayload) -> MCPServerDetailResponse:
    if server_key != payload.server_key:
        raise HTTPException(status_code=400, detail="路径中的 server_key 与请求体不一致")
    try:
        payload_data = payload.model_dump()
        if "enabled" not in payload.model_fields_set:
            payload_data.pop("enabled", None)
        item = repository.upsert_mcp_server(payload_data)
        return MCPServerDetailResponse(success=True, item=item)
    except Exception as exc:
        raise _handle_repo_error(exc)


@app.delete("/api/mcp-servers/{server_key}", response_model=ApiMessage)
def delete_mcp_server(server_key: str) -> ApiMessage:
    try:
        repository.delete_mcp_server(server_key)
        return ApiMessage(success=True, message=f"已删除 MCP Server: {server_key}")
    except Exception as exc:
        raise _handle_repo_error(exc)

@app.get("/{full_path:path}", include_in_schema=False, response_model=None)
def serve_spa(full_path: str):
    if full_path.startswith(("api/", "docs", "openapi.json", "health")):
        raise HTTPException(status_code=404, detail="Not Found")

    if not FRONTEND_INDEX_FILE.exists():
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "message": "前端构建产物不存在，请先在 admin-ui/frontend 下执行 npm run build",
            },
        )

    requested = (FRONTEND_DIST_DIR / full_path).resolve()
    if full_path and requested.exists() and requested.is_file() and requested.is_relative_to(FRONTEND_DIST_DIR):
        return FileResponse(requested)

    return FileResponse(FRONTEND_INDEX_FILE)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.admin_api_host, port=settings.admin_api_port)
