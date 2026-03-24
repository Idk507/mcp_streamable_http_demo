"""
MCP HTTP Transport Layer - Production
======================================
Implements:
- HTTP + SSE transport (production alternative to stdio)
- JWT authentication middleware
- Multi-tenant context isolation
- Request tracing & correlation IDs
- Graceful shutdown
- CORS & security headers
"""


import asyncio
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx
import jwt  # PyJWT
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel


from datetime import datetime



logger = logging.getLogger("mcp_http_transport")

#config

@dataclass 
class ServerConfig :
    host : str = "0.0.0.0"
    port : int = 8080
    jwt_secret : str = field(default_factory=lambda : secrets.token_hex(32))
    jwt_algorithm : str = "HS256"
    allowed_origins : list[str] = field(default_factory=lambda : ["*"])
    max_request_size_bytes : int = 10 * 1024 * 1024 # 10 MB 
    sse_keepalive_seconds : int = 15 
    tenant_isolation : bool = True 

config = ServerConfig()


# Tenant Context 
@dataclass
class TenantContext :
    tenant_id : str 
    user_id : str
    permissions : list[str] = field(default_factory=list)
    tier : str  #" free, pro, enterpise"
    rate_limit_override : Optional[int] = None

    def can(self, permission : str) -> bool :
        return permission in self.permissions or "admin" in self.permissions
    
    def rate_limit(self) -> int :
        if self.rate_limit_override is not None :
            return self.rate_limit_override
        return {
            "free" : 100,
            "pro" : 1000,
            "enterprise" : 10000,
        }.get(self.tier, 100)

# Per - request context store 
_request_contexts : dict[str, TenantContext] = {}

#JWT Auth 

security  = HTTPBearer()

def create_token(tenant_id: str, user_id: str, permissions: list[str], tier: str) -> str:
    payload = {
        "sub": user_id,
        "tenant": tenant_id,
        "permissions": permissions,
        "tier": tier,
        "iat": int(time.time()),
        "exp": int(time.time()) + (config.jwt_expiry_hours * 3600),
        "jti": str(uuid.uuid4()),  # unique token ID for revocation
    }
    return jwt.encode(payload, config.jwt_secret, algorithm=config.jwt_algorithm)


async def require_auth(
    request : Request, credentials : HTTPAuthorizationCredentials = Depends(security)
) -> TenantContext :
    try : 
        payload = jwt.decode(
            credentials.credentials ,
            config.jwt_secret,
            algorithms = [config.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError :
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    ctx = TenantContext(
        tenant_id=payload["tenant"],
        user_id=payload["sub"],
        permissions=payload.get("permissions", []),
        tier=payload.get("tier", "free"),
    )

    # store in request state for downstream access 
    request.state.tenant = ctx

    # trace correlation 
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = correlation_id

    logger.info(
        "Request authenticated",
        extra={
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "permissions": ctx.permissions,
            "tier": ctx.tier,
            "correlation_id": correlation_id,
        }
    )
    return ctx

# MCP Message Models 

class MCPRequest(BaseModel):
    jsonrpc : str = "2.0"
    id : Optional[str | int] = None
    method : str 
    params : Optional


class MCPResponse(BaseModel):
    jsonrpc : str = "2.0"
    id : Optional[str | int] = None
    result : Optional[dict] = None
    error : Optional[dict] = None



# SSE Connection Manager


class SSEConnectionManager:
    """Manages active SSE connections per tenant."""
    
    def __init__(self):
        self._connections: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
    
    async def connect(self, tenant_id: str) -> asyncio.Queue:
        async with self._lock:
            queue: asyncio.Queue = asyncio.Queue(maxsize=100)
            if tenant_id not in self._connections:
                self._connections[tenant_id] = []
            self._connections[tenant_id].append(queue)
        return queue
    
    async def disconnect(self, tenant_id: str, queue: asyncio.Queue):
        async with self._lock:
            if tenant_id in self._connections:
                try:
                    self._connections[tenant_id].remove(queue)
                except ValueError:
                    pass
    
    async def broadcast(self, tenant_id: str, event: dict):
        """Send event to all connections of a tenant."""
        if tenant_id not in self._connections:
            return
        
        dead_queues = []
        for queue in self._connections.get(tenant_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead_queues.append(queue)
        
        # Clean up dead connections
        for q in dead_queues:
            await self.disconnect(tenant_id, q)
    
    async def broadcast_all(self, event: dict):
        """Send event to all connected tenants."""
        for tenant_id in list(self._connections.keys()):
            await self.broadcast(tenant_id, event)
    
    @property
    def connection_count(self) -> int:
        return sum(len(v) for v in self._connections.values())

sse_manager = SSEConnectionManager()


# Tool Dispatcher 

class ToolDispatcher:
    """Routes MCP JSON-RPC requests to registered handlers."""
    
    # In production this imports from your actual MCP server module
    MOCK_TOOLS = {
        "search_documents": {
            "description": "Search documents",
            "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        "execute_query": {
            "description": "Execute SQL query",
            "schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
        "send_notification": {
            "description": "Send notifications",
            "schema": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "destination": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["channel", "destination", "subject", "body"],
            },
        },
    }

    async def dispatch(
        self,
        method: str,
        params: Optional[dict],
        ctx: TenantContext,
    ) -> dict:
        handlers = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_list_tools,
            "tools/call": self._handle_call_tool,
            "resources/list": self._handle_list_resources,
            "resources/read": self._handle_read_resource,
            "prompts/list": self._handle_list_prompts,
            "prompts/get": self._handle_get_prompt,
        }
        
        handler = handlers.get(method)
        if not handler:
            raise ValueError(f"Method not found: {method}")
        
        return await handler(params or {}, ctx)
    
    async def _handle_initialize(self, params: dict, ctx: TenantContext) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "production-mcp-server",
                "version": "1.0.0",
                "tenantId": ctx.tenant_id,
            },
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True, "listChanged": True},
                "prompts": {"listChanged": False},
                "logging": {},
            },
        }
    
    async def _handle_list_tools(self, params: dict, ctx: TenantContext) -> dict:
        # Filter tools by tenant permissions
        tools = []
        for name, tool_def in self.MOCK_TOOLS.items():
            if ctx.can("tools:read") or ctx.can("admin"):
                tools.append({
                    "name": name,
                    "description": tool_def["description"],
                    "inputSchema": tool_def["schema"],
                })
        return {"tools": tools}
    
    async def _handle_call_tool(self, params: dict, ctx: TenantContext) -> dict:
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        if not ctx.can("tools:execute"):
            raise PermissionError(f"Tenant {ctx.tenant_id} lacks tools:execute permission")
        
        if tool_name not in self.MOCK_TOOLS:
            raise ValueError(f"Tool '{tool_name}' not found")
        
        # Simulate tool execution
        await asyncio.sleep(0.05)
        
        # Broadcast execution event to tenant's SSE connections
        await sse_manager.broadcast(ctx.tenant_id, {
            "event": "tool_executed",
            "tool": tool_name,
            "tenant": ctx.tenant_id,
            "timestamp": time.time(),
        })
        
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "status": "success",
                        "tool": tool_name,
                        "result": f"Executed {tool_name} for tenant {ctx.tenant_id}",
                        "arguments": arguments,
                    }, indent=2),
                }
            ],
            "isError": False,
        }
    
    async def _handle_list_resources(self, params: dict, ctx: TenantContext) -> dict:
        return {
            "resources": [
                {
                    "uri": f"config://server/info",
                    "name": "Server Info",
                    "mimeType": "application/json",
                },
                {
                    "uri": f"tenant://{ctx.tenant_id}/profile",
                    "name": "Tenant Profile",
                    "mimeType": "application/json",
                },
            ]
        }
    
    async def _handle_read_resource(self, params: dict, ctx: TenantContext) -> dict:
        uri = params.get("uri", "")
        
        if uri == "config://server/info":
            content = json.dumps({"version": "1.0.0", "status": "healthy"})
        elif uri.startswith(f"tenant://{ctx.tenant_id}/"):
            content = json.dumps({"tenant_id": ctx.tenant_id, "tier": ctx.tier})
        else:
            raise ValueError(f"Resource not found or not permitted: {uri}")
        
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": content}]}
    
    async def _handle_list_prompts(self, params: dict, ctx: TenantContext) -> dict:
        return {
            "prompts": [
                {
                    "name": "analyze_query_results",
                    "description": "Analyze SQL results",
                    "arguments": [
                        {"name": "query", "required": True},
                        {"name": "results", "required": True},
                    ],
                }
            ]
        }
    
    async def _handle_get_prompt(self, params: dict, ctx: TenantContext) -> dict:
        name = params.get("name", "")
        args = params.get("arguments", {})
        return {
            "description": f"Prompt: {name}",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Execute prompt '{name}' with args: {json.dumps(args)}",
                    },
                }
            ],
        }

dispatcher = ToolDispatcher()


# FastAPI App Setup


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle."""
    logger.info("MCP HTTP server starting")
    
    # Start SSE keepalive task
    async def keepalive():
        while True:
            await asyncio.sleep(config.sse_keepalive_seconds)
            await sse_manager.broadcast_all({
                "event": "ping",
                "timestamp": time.time(),
                "connections": sse_manager.connection_count,
            })
    
    keepalive_task = asyncio.create_task(keepalive())
    
    yield
    
    # Graceful shutdown
    keepalive_task.cancel()
    logger.info(
        "MCP HTTP server shutting down",
        extra={"active_connections": sse_manager.connection_count}
    )

app = FastAPI(
    title="MCP Production Server",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID", "X-RateLimit-Remaining"],
)

# ─── Security Headers Middleware ──────────────────────────────────────────

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    
    if hasattr(request.state, "correlation_id"):
        response.headers["X-Correlation-ID"] = request.state.correlation_id
    
    return response


# ─── Routes ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Public health endpoint — no auth required."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "active_sse_connections": sse_manager.connection_count,
    }


@app.post("/token")
async def create_access_token(request: Request):
    """
    Issue a JWT token.
    In production: validate credentials against your identity provider.
    """
    body = await request.json()
    
    # In production: verify against your auth system
    # user = await auth_service.verify(body["username"], body["password"])
    
    token = create_token(
        tenant_id=body.get("tenant_id", "default"),
        user_id=body.get("user_id", "anonymous"),
        permissions=body.get("permissions", ["tools:read", "tools:execute"]),
        tier=body.get("tier", "pro"),
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": config.jwt_expiry_hours * 3600,
    }


@app.post("/mcp")
async def mcp_endpoint(
    request: Request,
    ctx: TenantContext = Depends(require_auth),
):
    """Main MCP JSON-RPC endpoint."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    mcp_req = MCPRequest(**body)
    
    try:
        result = await dispatcher.dispatch(mcp_req.method, mcp_req.params, ctx)
        return MCPResponse(id=mcp_req.id, result=result)
    
    except PermissionError as e:
        return MCPResponse(
            id=mcp_req.id,
            error={"code": -32001, "message": "Permission denied", "data": str(e)},
        )
    except ValueError as e:
        return MCPResponse(
            id=mcp_req.id,
            error={"code": -32601, "message": "Method/resource not found", "data": str(e)},
        )
    except Exception as e:
        logger.exception("Unhandled error in MCP dispatch")
        return MCPResponse(
            id=mcp_req.id,
            error={"code": -32603, "message": "Internal error", "data": str(e)},
        )


@app.get("/mcp/events")
async def sse_events(
    request: Request,
    ctx: TenantContext = Depends(require_auth),
):
    """
    Server-Sent Events endpoint for real-time MCP notifications.
    Clients connect here to receive push events (tool completions, alerts).
    """
    queue = await sse_manager.connect(ctx.tenant_id)
    
    async def event_generator():
        try:
            # Send initial connected event
            yield f"data: {json.dumps({'event': 'connected', 'tenant': ctx.tenant_id})}\n\n"
            
            while True:
                if await request.is_disconnected():
                    break
                
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive comment
                    yield ": keepalive\n\n"
        
        except asyncio.CancelledError:
            pass
        finally:
            await sse_manager.disconnect(ctx.tenant_id, queue)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        },
    )


@app.get("/mcp/stats")
async def server_stats(ctx: TenantContext = Depends(require_auth)):
    """Admin endpoint for server statistics."""
    if not ctx.can("admin"):
        raise HTTPException(status_code=403, detail="Admin permission required")
    
    return {
        "active_connections": sse_manager.connection_count,
        "tenant_id": ctx.tenant_id,
        "uptime_seconds": int(time.monotonic()),
    }


# ─── Startup ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,  # Use our JSON logger
        access_log=False,
    )
