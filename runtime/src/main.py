"""Vouchstone Agent Runtime - Main Entry Point"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Dict, Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
from fastapi.responses import Response

from .runtime import AgentRuntime
from .config import Settings

logger = structlog.get_logger()

# Metrics - handle duplicate registration on reload
def get_or_create_metric(metric_class, name, description, labels):
    """Get existing metric or create new one, handling duplicates"""
    try:
        return metric_class(name, description, labels)
    except ValueError:
        # Metric already exists, find and return it
        for collector in list(REGISTRY._collector_to_names.keys()):
            if hasattr(collector, '_name') and collector._name == name:
                return collector
        # If not found, raise the original error
        raise

try:
    REQUESTS = Counter("agent_requests_total", "Total agent requests", ["agent_id", "status"])
    LATENCY = Histogram("agent_request_latency_seconds", "Request latency", ["agent_id"])
except ValueError:
    # Metrics already registered (hot reload), find existing ones
    import sys
    REQUESTS = None
    LATENCY = None
    for collector in list(REGISTRY._collector_to_names.keys()):
        if hasattr(collector, '_name'):
            if collector._name == "agent_requests_total":
                REQUESTS = collector
            elif collector._name == "agent_request_latency_seconds":
                LATENCY = collector
    
    # If still not found, create with different names
    if REQUESTS is None:
        REQUESTS = Counter("agent_requests_total_v2", "Total agent requests", ["agent_id", "status"])
    if LATENCY is None:
        LATENCY = Histogram("agent_request_latency_seconds_v2", "Request latency", ["agent_id"])


settings = Settings()
runtime: AgentRuntime = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runtime
    logger.info("Starting Vouchstone Agent Runtime", version="1.0.0")
    
    runtime = AgentRuntime(settings)
    await runtime.initialize()
    
    yield
    
    logger.info("Shutting down runtime")
    await runtime.shutdown()


app = FastAPI(
    title="Vouchstone Agent Runtime",
    version="1.0.0",
    lifespan=lifespan
)


class ExecuteRequest(BaseModel):
    agent_id: str
    input: Dict[str, Any]
    session_id: str = None
    metadata: Dict[str, Any] = {}


class ExecuteResponse(BaseModel):
    output: Any
    session_id: str
    execution_id: str = ""
    metadata: Dict[str, Any] = {}


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/ready")
async def ready():
    """Readiness check. Distinguishes 'alive but couldn't reach the
    control plane' from truly ready -- see AgentRuntime.is_ready()."""
    if runtime and runtime.is_ready():
        return {"status": "ready", **runtime.health_detail()}
    detail = runtime.health_detail() if runtime else {"process_alive": False}
    raise HTTPException(status_code=503, detail={"status": "not_ready", **detail})


@app.post("/execute", response_model=ExecuteResponse)
async def execute_agent(request: ExecuteRequest):
    """Execute an agent"""
    with LATENCY.labels(agent_id=request.agent_id).time():
        try:
            result = await runtime.execute(
                agent_id=request.agent_id,
                input_data=request.input,
                session_id=request.session_id,
                metadata=request.metadata
            )
            REQUESTS.labels(agent_id=request.agent_id, status="success").inc()
            return ExecuteResponse(
                output=result["output"],
                session_id=result["session_id"],
                execution_id=result.get("execution_id", ""),
                metadata=result.get("metadata", {})
            )
        except Exception as e:
            REQUESTS.labels(agent_id=request.agent_id, status="error").inc()
            logger.error("Agent execution failed", agent_id=request.agent_id, error=str(e))
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/executions/{execution_id}")
async def get_execution(execution_id: str):
    """Status (+ latest checkpoint, if any) of one execution -- durable
    across restarts, see execution_store.py. Poll this for a long-running
    agent task instead of holding the /execute request open."""
    result = await runtime.get_execution_status(execution_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@app.get("/executions")
async def list_executions_endpoint(agent_id: str = None, status: str = None, limit: int = 100):
    """List tracked executions, optionally filtered by agent_id/status
    (running|completed|failed|interrupted). `interrupted` surfaces
    executions that were still running when the process last stopped --
    the crash-recovery visibility this exists for."""
    return {"executions": await runtime.list_executions(agent_id=agent_id, status=status, limit=limit)}


@app.get("/agents")
async def list_agents():
    """List loaded agents"""
    return {"agents": runtime.list_agents()}


@app.post("/agents/{agent_id}/load")
async def load_agent(agent_id: str):
    """Load an agent into the runtime"""
    try:
        await runtime.load_agent(agent_id)
        return {"status": "loaded", "agent_id": agent_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/agents/{agent_id}")
async def unload_agent(agent_id: str):
    """Unload an agent from the runtime"""
    await runtime.unload_agent(agent_id)
    return {"status": "unloaded", "agent_id": agent_id}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8080,
        reload=settings.DEBUG
    )
