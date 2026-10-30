import { describe, expect, it } from 'vitest';
import {
  FIXTURE_TIME,
  fixtureEvent,
  fixturePlan,
  fixtureRun,
  fixtureSession,
  fixtureStepEvents,
  normalizeRun,
} from './agent-api.testing';
import { AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import { validPlan as validV1Plan } from './agent-legacy.models';
import {
  parseAgentEvent,
  parseAgentRun,
  parseResultsPage,
  planIdentity,
  validPlan,
  validResult,
  type AgentEvent,
  type AgentPlan,
  type StepResult,
} from './agent.models';

function plan(): AgentPlan {
  return {
    ...fixturePlan(),
    steps: [
      {
        id: 'step-1',
        title: 'Search',
        description: 'Find sources',
        tool: 'web_search',
        arguments: { query: '  exact query  ', max_results: 2 },
      },
    ],
  };
}
function result(): StepResult {
  return {
    step_id: 'step-1',
    status: 'completed',
    content: '',
    time: FIXTURE_TIME,
    path: null,
    offset: null,
    returned_lines: null,
    truncated: false,
    search: {
      query: '  exact query  ',
      max_results: 2,
      results: [
        { title: 'Title', url: 'javascript:alert(1)', snippet: 'Snippet', published_date: null },
      ],
    },
  };
}
function run(value = result(), approved = plan()) {
  return parseAgentRun(
    normalizeRun(
      { ...fixtureRun('completed'), plan: approved, results: [value], answer: 'Done' },
      4,
    ),
  );
}

describe('v2 web search contract', () => {
  it('preserves exact arguments and results across run and result windows, without changing v1', () => {
    expect(validPlan(plan())).toBe(true);
    expect(validV1Plan(plan())).toBe(false);
    expect(run().results).toEqual([result()]);
    expect(
      parseResultsPage(
        {
          projection_version: 2,
          session_id: 's',
          run_id: 'r',
          at_sequence: 4,
          total: 1,
          completed_steps: 1,
          results: [result()],
          next_before: null,
        },
        plan(),
      ).results,
    ).toEqual([result()]);
    const empty = result();
    empty.search!.results = [];
    expect(run(empty).results).toEqual([empty]);
    expect(
      validResult(
        { step_id: 'step-1', status: 'completed', content: '', time: FIXTURE_TIME },
        fixturePlan(),
      ),
    ).toBe(true);
  });
  it.each([
    { query: '' },
    { query: ' \t\n ' },
    { query: null },
    { max_results: 0 },
    { max_results: 21 },
    { max_results: 1.5 },
    { max_results: '2' },
    { extra: 1 },
    { query: undefined },
  ])('rejects bad arguments %j', (change) => {
    const p = plan();
    Object.assign(p.steps[0].arguments, change);
    expect(validPlan(p)).toBe(false);
  });
  it.each(['', ' \t\n ', '\u2003'])('rejects blank result queries without a plan: %j', (query) => {
    const r = result();
    r.search!.query = query;
    expect(validResult(r)).toBe(false);
  });
  it.each([
    { query: 'different' },
    { max_results: 1 },
    { max_results: 21 },
    { results: null },
    { extra: 1 },
    { query: undefined },
    { max_results: undefined },
  ])('rejects invalid or uncorrelated metadata %j', (change) => {
    const r = result();
    Object.assign(r.search!, change);
    expect(() => run(r)).toThrow('Invalid Agent');
  });
  it.each([
    { title: null },
    { url: 2 },
    { snippet: undefined },
    { published_date: undefined },
    { extra: 1 },
  ])('rejects invalid result entries %j', (change) => {
    const r = result();
    Object.assign(r.search!.results[0], change);
    expect(validResult(r)).toBe(false);
  });
  it('enforces bounds, exclusivity, status, and approved tool identity', () => {
    const r = result();
    r.search!.results.push(...r.search!.results, ...r.search!.results);
    expect(validResult(r)).toBe(false);
    for (const change of [
      { search: null },
      { read: {} },
      { fetch: {} },
      { grep: {} },
      { path: 'x' },
      { offset: 1 },
      { returned_lines: 1 },
      { truncated: true },
      { content: 'text' },
      { status: 'running' },
    ])
      expect(() => run({ ...result(), ...change } as StepResult)).toThrow('Invalid Agent');
    expect(validResult(result(), fixturePlan())).toBe(false);
    const missing = result();
    delete missing.search;
    expect(validResult(missing, plan())).toBe(false);
  });
  it('correlates streamed search results with the reviewed plan and run', () => {
    const pending = { ...fixtureRun(), plan: plan() };
    const request: AgentStreamRequest = {
      session_id: 'agent-1',
      request_id: 'execute',
      sequence: 0,
      snapshot: fixtureSession(0, [pending]),
      reviewed_plan: plan(),
      command: { action: 'execute', run_id: pending.id, ...planIdentity(plan()) },
    };
    const frame = (event: AgentEvent) => ({
      event: 'agent',
      id: String(event.sequence),
      data: JSON.stringify(event),
    });
    const decoder = new AgentEventDecoder(request);
    decoder.decode(
      frame(
        fixtureEvent(request, 1, { ...pending, status: 'approved', operation_id: 'operation-1' }),
      ),
    );
    decoder.decode(
      frame(
        fixtureEvent(request, 2, { ...pending, status: 'executing', operation_id: 'operation-1' }),
      ),
    );
    const [intent, finished] = fixtureStepEvents(request, 3, pending, result());
    decoder.decode(frame(intent));
    expect(parseAgentEvent(finished).result).toEqual(result());
    const forged = structuredClone(finished);
    forged.result!.search!.query = 'other';
    expect(() => decoder.decode(frame(forged))).toThrow('Invalid Agent');
    expect(decoder.decode(frame(finished))!.result).toEqual(result());
  });
});
