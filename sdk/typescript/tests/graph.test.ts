/**
 * Tests for EntityGraph / PolicyGraph / WorkflowTrace (C6).
 *
 * Acceptance criteria: the AP-invoice, compliance-evidence, and migration
 * use cases can each be expressed against this interface without SDK code
 * changes per use case.
 */
import { createHash } from 'crypto';
import {
  EntityGraph, PolicyGraph, WorkflowTrace, Policy,
  canonicalJson, computeEntryHash,
} from '../src/graph';
import { Entity } from '../src/types';

function makeEntity(id: string, entityType: string, entityKey: string, attributes: Record<string, any> = {}): Entity {
  return {
    id, entityType, entityKey, attributes,
    confidence: 1.0, createdAt: new Date().toISOString(),
  };
}

describe('EntityGraph', () => {
  test('adds and queries entities/edges', () => {
    const g = new EntityGraph();
    g.addEntity(makeEntity('inv-1', 'invoice', 'INV-001', { amount: 1200.0 }));
    g.addEntity(makeEntity('ven-1', 'vendor', 'Acme Corp'));
    g.addEdge('inv-1', 'ven-1', 'billed_by');

    expect(g.size).toBe(2);
    expect(g.getEntity('inv-1')?.attributes.amount).toBe(1200.0);
    expect(g.entitiesByType('invoice')).toHaveLength(1);
    expect(g.related('inv-1', 'billed_by')).toHaveLength(1);
    expect(g.related('inv-1', 'billed_by')[0].id).toBe('ven-1');
  });

  test('edge requires known entities', () => {
    const g = new EntityGraph();
    g.addEntity(makeEntity('a', 'x', 'A'));
    expect(() => g.addEdge('a', 'missing', 'rel')).toThrow();
  });

  test('roundtrips through JSON', () => {
    const g = new EntityGraph();
    g.addEntity(makeEntity('a', 'table', 'shipments'));
    g.addEntity(makeEntity('b', 'column', 'shipments.eta'));
    g.addEdge('b', 'a', 'belongs_to');

    const restored = EntityGraph.fromJSON(JSON.parse(JSON.stringify(g.toJSON())));
    expect(restored.size).toBe(2);
    expect(restored.related('b', 'belongs_to')).toHaveLength(1);
  });
});

describe('PolicyGraph', () => {
  test('defaults to deny with no matching policy', () => {
    const pg = new PolicyGraph();
    const decision = pg.evaluate({ principal: { role: 'agent' }, action: 'invoice.approve' });
    expect(decision.allow).toBe(false);
    expect(decision.reason).toContain('default deny');
  });

  test('permits with obligations when a policy matches', () => {
    const pg = new PolicyGraph();
    pg.addPolicy({
      name: 'auto-approve small invoices', effect: 'permit',
      action: { eq: 'invoice.approve' },
      conditions: [{ path: 'resource.amount', op: 'lt', value: 5000 }],
      obligations: ['log_to_audit'],
    });
    const decision = pg.evaluate({
      principal: { role: 'ap_agent' }, action: 'invoice.approve',
      resource: { amount: 1200.0 },
    });
    expect(decision.allow).toBe(true);
    expect(decision.obligations).toEqual(['log_to_audit']);
  });

  test('forbid wins over permit', () => {
    const pg = new PolicyGraph();
    pg.addPolicy({ name: 'permit all', effect: 'permit', action: { eq: 'invoice.approve' }, priority: 100 });
    pg.addPolicy({
      name: 'forbid over-budget', effect: 'forbid', action: { eq: 'invoice.approve' }, priority: 10,
      conditions: [{ path: 'resource.amount', op: 'gte', value: 5000 }],
    });
    const decision = pg.evaluate({
      principal: { role: 'ap_agent' }, action: 'invoice.approve', resource: { amount: 9000 },
    });
    expect(decision.allow).toBe(false);
    expect(decision.reason).toContain('forbid over-budget');
  });

  test('compliance-evidence use case: pii requires dual signoff', () => {
    const pg = new PolicyGraph();
    pg.addPolicy({
      name: 'pii dual signoff', effect: 'permit', action: { startswith: 'evidence.' },
      resource: { data_classification: 'pii' },
      obligations: ['require_dual_signoff', 'log_to_audit'],
    });
    pg.addPolicy({
      name: 'internal auto', effect: 'permit', action: { startswith: 'evidence.' },
      resource: { data_classification: 'internal' },
      obligations: ['log_to_audit'],
    });

    const piiDecision = pg.evaluate({
      principal: { role: 'compliance_agent' }, action: 'evidence.collect',
      resource: { data_classification: 'pii' },
    });
    expect(piiDecision.allow).toBe(true);
    expect(piiDecision.obligations).toContain('require_dual_signoff');

    const internalDecision = pg.evaluate({
      principal: { role: 'compliance_agent' }, action: 'evidence.collect',
      resource: { data_classification: 'internal' },
    });
    expect(internalDecision.obligations).not.toContain('require_dual_signoff');
  });
});

describe('WorkflowTrace', () => {
  test('chains entries and verifies', () => {
    const trace = new WorkflowTrace();
    trace.append('migration.step_started', { table: 'shipments' }, 'schema_mapper');
    trace.append('migration.step_completed', { table: 'shipments', rows: 1200 }, 'schema_mapper');

    expect(trace.entries).toHaveLength(2);
    expect(trace.entries[0].prevHash).toBe('');
    expect(trace.entries[1].prevHash).toBe(trace.entries[0].entryHash);
    expect(trace.tipHash).toBe(trace.entries[trace.entries.length - 1].entryHash);
    expect(trace.verifyChain()).toBe(true);
  });

  test('detects tampering', () => {
    const trace = new WorkflowTrace();
    trace.append('action.approved', { amount: 100 });
    trace.append('action.approved', { amount: 200 });

    // entries getter returns a shallow copy of the array but shares payload
    // objects by reference, so this mutates the real stored entry.
    (trace.entries[0].payload as any).amount = 999999;
    expect(trace.verifyChain()).toBe(false);
  });

  test('roundtrips through JSON', () => {
    const trace = new WorkflowTrace();
    trace.append('kind.a', { x: 1 });
    trace.append('kind.b', { y: 2 });

    const restored = WorkflowTrace.fromJSON(JSON.parse(JSON.stringify(trace.toJSON())));
    expect(restored.tipHash).toBe(trace.tipHash);
    expect(restored.verifyChain()).toBe(true);
  });

  test('hash algorithm matches the control plane exactly', () => {
    const payload = { b: 2, a: 1 };
    expect(canonicalJson(payload)).toBe('{"a":1,"b":2}');
    const expected = createHash('sha256').update('').update('{"a":1,"b":2}').digest('hex');
    expect(computeEntryHash('', payload)).toBe(expected);
  });
});

describe('migration use case end-to-end', () => {
  test('entities + policy + trace work together', () => {
    const graph = new EntityGraph();
    graph.addEntity(makeEntity('shipments', 'table', 'shipments', { row_count: 50000 }));
    graph.addEntity(makeEntity('shipments.eta', 'column', 'eta', { type: 'timestamp' }));
    graph.addEdge('shipments.eta', 'shipments', 'belongs_to');

    const policy = new PolicyGraph();
    policy.addPolicy({
      name: 'no drop outside dev', effect: 'forbid', action: { startswith: 'schema.drop_' },
      conditions: [{ path: 'context.environment', op: 'in', value: ['uat', 'prod'] }],
    });
    policy.addPolicy({
      name: 'permit schema changes in dev', effect: 'permit', action: { startswith: 'schema.' },
      obligations: ['log_to_audit'],
    });

    const trace = new WorkflowTrace();

    const decision = policy.evaluate({
      principal: { agentId: 'schema-mapper-1' }, action: 'schema.alter_table',
      context: { environment: 'dev' },
    });
    expect(decision.allow).toBe(true);
    trace.append('schema.alter_table', { table: graph.getEntity('shipments')?.entityKey, decision: decision.allow }, 'schema-mapper-1');

    const dropDecision = policy.evaluate({
      principal: { agentId: 'schema-mapper-1' }, action: 'schema.drop_table',
      context: { environment: 'prod' },
    });
    expect(dropDecision.allow).toBe(false);
    trace.append('schema.drop_table_denied', { table: graph.getEntity('shipments')?.entityKey, reason: dropDecision.reason }, 'schema-mapper-1');

    expect(trace.verifyChain()).toBe(true);
    expect(trace.entries).toHaveLength(2);
  });
});
