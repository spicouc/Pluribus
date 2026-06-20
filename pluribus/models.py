"""Models Pydantic per a requests i responses de l'API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

# Categories disponibles (estil OpenViking)
VALID_CATEGORIES = {
    "",            # legacy / sense categoria
    "profile",     # informació bàsica de l'usuari
    "preferences", # preferències de l'usuari per tema
    "entities",    # entitats (persones, projectes)
    "events",      # registres d'esdeveniments (decisions, fites)
    "cases",       # casos apresos per l'agent
    "patterns",    # patrons apresos per l'agent
}


class WriteRequest(BaseModel):
    """Cos de la petició per escriure un fet a la memòria."""
    content: str
    scope: str = "shared"
    category: str = "events"
    key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WriteResponse(BaseModel):
    """Resposta després de crear un fet."""
    fact_id: str
    message: str = "Fet creat correctament"
    chunks_generated: int = 0


class FactResponse(BaseModel):
    """Representació d'un fet retornat al client."""
    id: str
    scope: str
    category: str = "events"
    agent_id: Optional[str] = None
    key: Optional[str] = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    created_at: str
    updated_at: str


class LsParams(BaseModel):
    """Paràmetres per llistar fets (com un ls de directori)."""
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class LsResponse(BaseModel):
    """Resposta de l'endpoint ls."""
    items: list[dict[str, Any]]
    total: int
    scope: str
    filters: dict[str, Any] = Field(default_factory=dict)


class QueryParams(BaseModel):
    """Paràmetres per a la cerca per text (FTS5)."""
    q: str
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchRequest(BaseModel):
    """Paràmetres per a la cerca GET combinada (FTS5 + semàntica)."""
    q: str
    semantic: bool = False
    limit: int = Field(default=5, ge=1, le=50)
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None


class SearchResult(BaseModel):
    """Un resultat individual de la cerca combinada."""
    fact_id: str
    content: str
    scope: str
    category: str = "events"
    agent_id: Optional[str] = None
    key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    match_type: str = "fts5"  # "fts5" o "semantic"
    snippet: Optional[str] = None


class SearchResponse(BaseModel):
    """Resposta completa de la cerca combinada."""
    results: list[SearchResult]
    query: str
    total: int
    semantic_used: bool = False


class SemanticSearchRequest(BaseModel):
    """Cos de la petició per a la cerca semàntica (POST)."""
    query: str
    top_k: int = Field(default=5, ge=1, le=50)
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None


class SemanticSearchResult(BaseModel):
    """Un resultat individual de la cerca semàntica."""
    fact_id: str
    content: str
    scope: str
    category: str = "events"
    agent_id: Optional[str] = None
    key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class SemanticSearchResponse(BaseModel):
    """Resposta completa de la cerca semàntica."""
    results: list[SemanticSearchResult]
    query: str
    top_k: int
    semantic_fallback: bool = False


class UpdateRequest(BaseModel):
    """Cos de la petició per actualitzar un fet."""
    content: str
    metadata: Optional[dict[str, Any]] = None
    category: Optional[str] = None


class AuditEntry(BaseModel):
    """Una entrada del registre d'auditoria."""
    id: int
    agent_id: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    payload: Optional[str] = None
    timestamp: str


class StatsResponse(BaseModel):
    """Mètriques per al dashboard."""
    facts_last_7_days: list[dict[str, Any]] = Field(default_factory=list)
    facts_by_agent: list[dict[str, Any]] = Field(default_factory=list)
    facts_by_category: list[dict[str, Any]] = Field(default_factory=list)
    db_size_history: list[dict[str, Any]] = Field(default_factory=list)
    total_active: int = 0
    total_deleted: int = 0
    last_10_audit: list[AuditEntry] = Field(default_factory=list)
    total_chunks: int = 0
    total_agents: int = 0
    total_consolidated: int = 0
    total_notion_cached: int = 0
    ollama_connected: bool = False

# ─── Agent models ──────────────────────────────────────

class AgentResponse(BaseModel):
    """Resposta amb dades d'un agent."""
    id: str
    name: str
    permissions: dict[str, Any] = Field(default_factory=lambda: {"read": True, "write": True})
    allowed_scopes: list[str] = Field(default_factory=lambda: ["shared"])
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_active_at: Optional[str] = None
    last_ip: Optional[str] = None
    is_active: bool = True
    created_at: str = ""
    fact_count: int = 0


class AgentUpdateRequest(BaseModel):
    """Cos per actualitzar un agent."""
    capabilities: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


class AgentRegisterRequest(BaseModel):
    """Solicitud de registre d'un agent."""
    name: str
    permissions: dict[str, Any] = Field(default_factory=lambda: {"read": True, "write": True, "delete": False, "admin": False})
    allowed_scopes: list[str] = Field(default_factory=lambda: ["shared", "local"])


class AgentRegisterResponse(BaseModel):
    """Resposta amb la nova clau API."""
    agent_id: str
    name: str
    api_key: str
    message: str = "Agent creat correctament. Guarda la clau API, no es podra recuperar despues."

# ─── Knowledge Graph models ──────────────────────────

class CreateRelationRequest(BaseModel):
    """Cos per crear una relacio entre dos fets."""
    source_fact_id: str
    target_fact_id: str
    relation_type: str = "related_to"
    relation_strength: float = 0.5


class GraphNode(BaseModel):
    """Node del graf de coneixement."""
    id: str
    type: str = "fact"
    label: str = ""
    content_preview: str = ""
    created_at: str = ""


class GraphEdge(BaseModel):
    """Aresta del graf de coneixement."""
    source: str
    target: str
    relation: str = "related_to"
    strength: float = 0.5


class GraphResponse(BaseModel):
    """Resposta del graf de coneixement."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    total_facts: int = 0
    total_relations: int = 0


class RelationResponse(BaseModel):
    """Resposta d'una relacio individual."""
    id: str
    source_fact_id: str
    target_fact_id: str
    relation_type: str = "related_to"
    relation_strength: float = 0.5
    discovered_by: str = "worker"
    created_at: str = ""
