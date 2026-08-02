"""Runtime Configuration"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    # Runtime
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    
    # Control Plane -- no default: api.vouchstone.ai does not resolve (it
    # was a placeholder, not a real endpoint). Required UNLESS BUNDLE_PATH
    # is set (offline harness mode, C4) -- AgentRuntime.initialize()
    # enforces this explicitly and fails loudly if neither is configured,
    # rather than silently pointing at a dead host.
    CONTROL_PLANE_URL: Optional[str] = None
    API_KEY: str = ""
    TENANT_ID: str = ""

    # Offline harness mode (C4) -- when set, AgentRuntime.initialize_from_bundle()
    # loads agent definitions and the KG snapshot entirely from this local
    # bundle directory. Zero network calls; CONTROL_PLANE_URL is not required.
    BUNDLE_PATH: Optional[str] = None
    
    # AI Models
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    DEFAULT_MODEL: str = "gpt-4-turbo-preview"

    # Sovereign deployment mode (C7b) -- see src/sovereign.py. When true,
    # AgentRuntime.initialize() hard-refuses to start if any external LLM
    # API key is configured (without a LOCAL_LLM_BASE_URL override) or
    # actually reachable over the network. LOCAL_LLM_BASE_URL points the
    # runtime at a self-hosted inference server (vLLM/TGI) instead of a
    # cloud provider; turnkey automated deployment of that server is
    # explicitly out of scope here (needs real GPU/infra).
    SOVEREIGN_MODE: bool = False
    LOCAL_LLM_BASE_URL: Optional[str] = None
    
    # Vector DB
    VECTOR_DB_TYPE: str = "chromadb"  # chromadb, qdrant
    VECTOR_DB_URL: Optional[str] = None
    VECTOR_DB_COLLECTION: str = "vouchstone_memory"
    
    # Graph DB
    GRAPH_DB_URL: Optional[str] = None
    GRAPH_DB_USER: Optional[str] = None
    GRAPH_DB_PASSWORD: Optional[str] = None
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    
    # Storage
    AGENT_CODE_PATH: str = "/app/agents"

    # Control-plane heartbeat (Slice 1)
    HEARTBEAT_URL: Optional[str] = None  # defaults to CONTROL_PLANE_URL when not set
    HEARTBEAT_INTERVAL_SECONDS: int = 30
    RUNTIME_TOKEN: str = ""
    RUNTIME_VERSION: str = "1.0.0"
    LEDGER_REPLAY_INTERVAL_SECONDS: int = 60
    LOCAL_QUEUE_PATH: str = "/var/vouchstone/queue.db"

    # Durable execution tracking -- survives a process restart on the same
    # disk. Does not by itself provide horizontal scaling across replicas
    # (each replica has its own store); see execution_store.py's docstring.
    EXECUTION_STORE_PATH: str = "/var/vouchstone/executions.db"

    model_config = SettingsConfigDict(env_prefix="VOUCHSTONE_", case_sensitive=True)
