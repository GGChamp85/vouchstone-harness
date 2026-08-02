/**
 * EntityGraph / PolicyGraph / WorkflowTrace — the three-compartment pattern.
 *
 * Mirrors data-plane/sdk/python/vouchstone_sdk/graph.py exactly (same
 * hashing algorithm, same policy semantics) so a bundle produced by one
 * SDK is verifiable by the other. See that file's module docstring for
 * the full rationale.
 */

import { Entity } from './types';

// ============================================================
// Canonical JSON + hash chaining
//
// Identical algorithm to control-plane/backend/app/services/ledger_signing.py
// and the Python SDK's graph.py: sha256(prevHash || canonicalJson(payload)).
// Node's crypto module (not an external dependency) provides SHA-256.
// ============================================================

import { createHash } from 'crypto';

export function canonicalJson(payload: Record<string, any>): string {
  const sortKeys = (value: any): any => {
    if (Array.isArray(value)) return value.map(sortKeys);
    if (value !== null && typeof value === 'object') {
      const sorted: Record<string, any> = {};
      for (const key of Object.keys(value).sort()) {
        sorted[key] = sortKeys(value[key]);
      }
      return sorted;
    }
    return value;
  };
  return JSON.stringify(sortKeys(payload));
}

export function computeEntryHash(prevHash: string, payload: Record<string, any>): string {
  const h = createHash('sha256');
  h.update(prevHash || '', 'utf-8');
  h.update(canonicalJson(payload), 'utf-8');
  return h.digest('hex');
}

// ============================================================
// EntityGraph — domain entities + edges
// ============================================================

export interface GraphEdge {
  sourceId: string;
  targetId: string;
  edgeType: string;
  attributes: Record<string, any>;
}

export class EntityGraph {
  private entitiesById: Map<string, Entity> = new Map();
  private edges: GraphEdge[] = [];

  addEntity(entity: Entity): Entity {
    this.entitiesById.set(entity.id, entity);
    return entity;
  }

  getEntity(entityId: string): Entity | undefined {
    return this.entitiesById.get(entityId);
  }

  entitiesByType(entityType: string): Entity[] {
    return Array.from(this.entitiesById.values()).filter((e) => e.entityType === entityType);
  }

  addEdge(sourceId: string, targetId: string, edgeType: string, attributes: Record<string, any> = {}): GraphEdge {
    if (!this.entitiesById.has(sourceId)) throw new Error(`unknown source entity: ${sourceId}`);
    if (!this.entitiesById.has(targetId)) throw new Error(`unknown target entity: ${targetId}`);
    const edge: GraphEdge = { sourceId, targetId, edgeType, attributes };
    this.edges.push(edge);
    return edge;
  }

  related(entityId: string, edgeType?: string): Entity[] {
    return this.edges
      .filter((e) => e.sourceId === entityId && (edgeType === undefined || e.edgeType === edgeType))
      .map((e) => this.entitiesById.get(e.targetId))
      .filter((e): e is Entity => e !== undefined);
  }

  get size(): number {
    return this.entitiesById.size;
  }

  allEntities(): Entity[] {
    return Array.from(this.entitiesById.values());
  }

  toJSON(): Record<string, any> {
    return {
      entities: Array.from(this.entitiesById.values()),
      edges: this.edges,
    };
  }

  static fromJSON(data: Record<string, any>): EntityGraph {
    const graph = new EntityGraph();
    for (const e of data.entities || []) graph.addEntity(e);
    for (const ed of data.edges || []) graph.addEdge(ed.sourceId, ed.targetId, ed.edgeType, ed.attributes);
    return graph;
  }
}

// ============================================================
// PolicyGraph — permit/forbid ruleset with obligations
// ============================================================

type ConditionOp = 'eq' | 'ne' | 'in' | 'not_in' | 'gt' | 'gte' | 'lt' | 'lte' | 'startswith' | 'regex';

export interface PolicyCondition {
  path: string;
  op: ConditionOp;
  value: any;
}

export interface Policy {
  name: string;
  effect: 'permit' | 'forbid';
  action?: { eq?: string; in?: string[]; startswith?: string };
  resource?: Record<string, any>;
  conditions?: PolicyCondition[];
  obligations?: string[];
  priority?: number;
}

export interface PolicyDecision {
  allow: boolean;
  obligations: string[];
  matchedPolicyNames: string[];
  reason: string;
}

const CONDITION_OPS: Record<ConditionOp, (v: any, target: any) => boolean> = {
  eq: (v, t) => v === t,
  ne: (v, t) => v !== t,
  in: (v, t) => Array.isArray(t) && t.includes(v),
  not_in: (v, t) => !(Array.isArray(t) && t.includes(v)),
  gt: (v, t) => v !== undefined && v !== null && v > t,
  gte: (v, t) => v !== undefined && v !== null && v >= t,
  lt: (v, t) => v !== undefined && v !== null && v < t,
  lte: (v, t) => v !== undefined && v !== null && v <= t,
  startswith: (v, t) => typeof v === 'string' && v.startsWith(t),
  regex: (v, t) => typeof v === 'string' && new RegExp(t).test(v),
};

function getPath(root: Record<string, any>, path: string): any {
  let cur: any = root;
  for (const part of path.split('.')) {
    if (cur !== null && typeof cur === 'object') cur = cur[part];
    else return undefined;
  }
  return cur;
}

function matchesAction(spec: Policy['action'], action: string): boolean {
  if (!spec) return true;
  if (spec.eq !== undefined && action !== spec.eq) return false;
  if (spec.in !== undefined && !spec.in.includes(action)) return false;
  if (spec.startswith !== undefined && !action.startsWith(spec.startswith)) return false;
  return true;
}

function matchesResource(spec: Record<string, any> | undefined, resource: Record<string, any>): boolean {
  if (!spec) return true;
  for (const [key, target] of Object.entries(spec)) {
    const value = resource[key];
    if (Array.isArray(target)) {
      if (!target.includes(value)) return false;
    } else if (value !== target) {
      return false;
    }
  }
  return true;
}

function matchesConditions(conditions: PolicyCondition[] | undefined, root: Record<string, any>): boolean {
  for (const cond of conditions || []) {
    if (!cond.path || !CONDITION_OPS[cond.op]) return false;
    if (!CONDITION_OPS[cond.op](getPath(root, cond.path), cond.value)) return false;
  }
  return true;
}

/**
 * A stable, evaluable ruleset. Deny-by-default: an action with no matching
 * permit policy is denied, matching the control plane's ABAC posture.
 */
export class PolicyGraph {
  private policies: Policy[] = [];

  addPolicy(policy: Policy): Policy {
    this.policies.push({ priority: 100, obligations: [], ...policy });
    return policy;
  }

  evaluate(params: {
    principal: Record<string, any>;
    action: string;
    resource?: Record<string, any>;
    context?: Record<string, any>;
  }): PolicyDecision {
    const resource = params.resource || {};
    const context = params.context || {};
    const root = { principal: params.principal, action: params.action, resource, context };

    const obligations: string[] = [];
    const matched: string[] = [];
    let anyPermit = false;

    const sorted = [...this.policies].sort((a, b) => {
      const pa = a.priority ?? 100;
      const pb = b.priority ?? 100;
      return pa !== pb ? pa - pb : a.name.localeCompare(b.name);
    });

    for (const policy of sorted) {
      if (!matchesAction(policy.action, params.action)) continue;
      if (!matchesResource(policy.resource, resource)) continue;
      if (!matchesConditions(policy.conditions, root)) continue;

      matched.push(policy.name);
      if (policy.effect === 'forbid') {
        return { allow: false, obligations: [], matchedPolicyNames: matched, reason: `forbidden by policy: ${policy.name}` };
      }
      anyPermit = true;
      for (const o of policy.obligations || []) {
        if (!obligations.includes(o)) obligations.push(o);
      }
    }

    if (!anyPermit) {
      return { allow: false, obligations: [], matchedPolicyNames: matched, reason: 'no matching permit policy (default deny)' };
    }
    return { allow: true, obligations, matchedPolicyNames: matched, reason: 'allow' };
  }
}

// ============================================================
// WorkflowTrace — append-only, hash-chained record
// ============================================================

export interface WorkflowTraceEntry {
  sequence: number;
  kind: string;
  actor: string;
  payload: Record<string, any>;
  prevHash: string;
  entryHash: string;
  timestamp: string;
}

export class WorkflowTrace {
  private entryList: WorkflowTraceEntry[] = [];

  append(kind: string, payload: Record<string, any>, actor: string = 'agent'): WorkflowTraceEntry {
    const prevHash = this.entryList.length > 0 ? this.entryList[this.entryList.length - 1].entryHash : '';
    const sequence = this.entryList.length + 1;
    const timestamp = new Date().toISOString();
    const body = { sequence, kind, actor, payload, timestamp };
    const entryHash = computeEntryHash(prevHash, body);
    const entry: WorkflowTraceEntry = { sequence, kind, actor, payload, prevHash, entryHash, timestamp };
    this.entryList.push(entry);
    return entry;
  }

  get tipHash(): string {
    return this.entryList.length > 0 ? this.entryList[this.entryList.length - 1].entryHash : '';
  }

  get entries(): WorkflowTraceEntry[] {
    return [...this.entryList];
  }

  verifyChain(): boolean {
    let prevHash = '';
    for (const entry of this.entryList) {
      const body = { sequence: entry.sequence, kind: entry.kind, actor: entry.actor, payload: entry.payload, timestamp: entry.timestamp };
      if (entry.prevHash !== prevHash) return false;
      if (computeEntryHash(prevHash, body) !== entry.entryHash) return false;
      prevHash = entry.entryHash;
    }
    return true;
  }

  toJSON(): Record<string, any> {
    return { tipHash: this.tipHash, entries: this.entryList };
  }

  static fromJSON(data: Record<string, any>): WorkflowTrace {
    const trace = new WorkflowTrace();
    trace.entryList = data.entries || [];
    return trace;
  }
}
