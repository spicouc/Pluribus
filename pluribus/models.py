"""Models Pydantic per a requests i responses de l'API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from pluribus.validation import (
    VALID_CATEGORIES,
    validate_agent_name,
    validate_category,
    validate_content,
    validate_identifier,
    validate_key,
    validate_metadata,
    validate_permissions,
    validate_query,
    validate_scope,
    validate_scopes,
    validate_ttl,
)


class WriteRequest(BaseModel):
    content: str
    scope: str = "shared"
    category: str = "events"
    key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_days: int | None = None

    _content = field_validator("content")(validate_content)
    _scope = field_validator("scope")(validate_scope)
    _category = field_validator("category")(validate_category)
    _key = field_validator("key")(validate_key)
    _metadata = field_validator("metadata")(validate_metadata)
    _ttl = field_validator("ttl_days")(validate_ttl)


class WriteResponse(BaseModel):
    fact_id: str
    message: str = "Fet creat correctament"
    chunks_generated: int = 0


class FactResponse(BaseModel):
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
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    _scope = field_validator("scope")(validate_scope)
    _category = field_validator("category")(validate_category)


class LsResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    scope: str
    filters: dict[str, Any] = Field(default_factory=dict)


class QueryParams(BaseModel):
    q: str
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=50)

    _query = field_validator("q")(validate_query)
    _scope = field_validator("scope")(validate_scope)
    _category = field_validator("category")(validate_category)


class SearchRequest(BaseModel):
    q: str
    semantic: bool = False
    limit: int = Field(default=5, ge=1, le=50)
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None

    _query = field_validator("q")(validate_query)
    _scope = field_validator("scope")(validate_scope)
    _category = field_validator("category")(validate_category)


class SearchResult(BaseModel):
    fact_id: str
    content: str
    scope: str
    category: str = "events"
    agent_id: Optional[str] = None
    key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    match_type: str = "fts5"
    snippet: Optional[str] = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    query: str
    total: int
    semantic_used: bool = False


class SemanticSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=50)
    scope: str = "shared"
    category: str = "events"
    agent_id: Optional[str] = None

    _query = field_validator("query")(validate_query)
    _scope = field_validator("scope")(validate_scope)
    _category = field_validator("category")(validate_category)


class SemanticSearchResult(BaseModel):
    fact_id: str
    content: str
    scope: str
    category: str = "events"
    agent_id: Optional[str] = None
    key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class SemanticSearchResponse(BaseModel):
    results: list[SemanticSearchResult]
    query: str
    top_k: int
    semantic_fallback: bool = False


class UpdateRequest(BaseModel):
    content: str
    metadata: Optional[dict[str, Any]] = None
    category: Optional[str] = None

    _content = field_validator("content")(validate_content)
    _metadata = field_validator("metadata")(validate_metadata)

    @field_validator("category")
    @classmethod
    def validate_optional_category(cls, value: str | None) -> str | None:
        return None if value is None else validate_category(value)


class AuditEntry(BaseModel):
    id: int
    agent_id: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    payload: Optional[str] = None
    timestamp: str


class StatsResponse(BaseModel):
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


class MemoryListRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    agent_id: Optional[str] = None
    scope: Optional[str] = None
    category: Optional[str] = None
    from_date: Optional[str] = Field(default=None, max_length=64)
    to_date: Optional[str] = Field(default=None, max_length=64)
    sort: str = Field(default="created_at:desc", pattern=r"^(created_at|updated_at):(asc|desc)$")

    @field_validator("scope")
    @classmethod
    def validate_optional_scope(cls, value: str | None) -> str | None:
        return None if value is None else validate_scope(value)

    @field_validator("category")
    @classmethod
    def validate_optional_category(cls, value: str | None) -> str | None:
        return None if value is None else validate_category(value)


class MemoryResponse(BaseModel):
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
    ttl_days: Optional[int] = None
    expires_at: Optional[str] = None


class MemoryListResponse(BaseModel):
    facts: list[MemoryResponse]
    total: int
    limit: int
    offset: int


class AgentResponse(BaseModel):
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
    capabilities: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None

    _metadata = field_validator("metadata")(validate_metadata)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_metadata(value)


class AgentRegisterRequest(BaseModel):
    name: str
    permissions: dict[str, Any] = Field(
        default_factory=lambda: {"read": True, "write": True, "delete": False, "admin": False}
    )
    allowed_scopes: list[str] = Field(default_factory=lambda: ["shared", "local"])

    _name = field_validator("name")(validate_agent_name)
    _permissions = field_validator("permissions")(validate_permissions)
    _scopes = field_validator("allowed_scopes")(validate_scopes)


class AgentRegisterResponse(BaseModel):
    agent_id: str
    name: str
    api_key: str
    message: str = "Agent creat correctament. Guarda la clau API, no es podrà recuperar després."


class CreateRelationRequest(BaseModel):
    source_fact_id: str
    target_fact_id: str
    relation_type: str = Field(default="related_to", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    relation_strength: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("source_fact_id")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return validate_identifier(value, "source_fact_id")

    @field_validator("target_fact_id")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return validate_identifier(value, "target_fact_id")


class GraphNode(BaseModel):
    id: str
    type: str = "fact"
    label: str = ""
    content_preview: str = ""
    created_at: str = ""


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str = "related_to"
    strength: float = 0.5


class GraphResponse(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    total_facts: int = 0
    total_relations: int = 0


class RelationResponse(BaseModel):
    id: str
    source_fact_id: str
    target_fact_id: str
    relation_type: str = "related_to"
    relation_strength: float = 0.5
    discovered_by: str = "worker"
    created_at: str = ""


class TraverseNode(BaseModel):
    id: str
    name: str
    type: str = ""
    hop: int = 0


class TraverseEdge(BaseModel):
    subject_id: str
    subject_name: str
    predicate: str
    object_id: str
    object_name: str
    confidence: float = 1.0
    hop: int = 0


class TraverseResponse(BaseModel):
    entity: str
    nodes: list[TraverseNode] = Field(default_factory=list)
    edges: list[TraverseEdge] = Field(default_factory=list)
    hops: int = 0
    total_nodes: int = 0
    total_edges: int = 0
