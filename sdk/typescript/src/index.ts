// Client
export { VouchstoneClient, VouchstoneClientConfig } from './client';

// Agent
export { Agent, AgentConfig } from './agent';

// Memory — all 5 layers + pipeline
export {
  WorkingMemory, WorkingMemoryConfig,
  SemanticMemory, SemanticMemoryConfig,
  EpisodicMemory, EpisodicMemoryConfig,
  ProceduralMemory, ProceduralMemoryConfig,
  MetaMemory, MetaMemoryConfig,
  MemoryPipeline, MemoryPipelineConfig,
} from './memory';

// EntityGraph / PolicyGraph / WorkflowTrace — the three-compartment pattern
export {
  EntityGraph, GraphEdge,
  PolicyGraph, Policy, PolicyCondition, PolicyDecision,
  WorkflowTrace, WorkflowTraceEntry,
  canonicalJson, computeEntryHash,
} from './graph';

// Types
export {
  Message, Decision, AgentResponse,
  MemoryEntry, Episode, EpisodicTrace,
  Entity, Skill,
  MemoryContext, TurnResult, HealthReport,
  AgentDefinition, MemoryConfig,
  LedgerEntry, AgentSpec, ToolDefinition, SessionInfo,
  AgentStatus, MemoryLayer,
} from './types';
