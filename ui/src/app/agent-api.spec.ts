import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import { FETCH } from './chat-api';
import {
  fixtureEvent,
  fixtureList,
  fixturePlan,
  fixtureRun,
  fixtureSession,
  normalizeRun,
  sseResponse,
} from './agent-api.testing';
import {
  parseAgentRun,
  parseAgentSession,
  parseAgentEvent,
  planIdentity,
  type AgentEvent,
  type AgentRun,
} from './agent.models';

const request: AgentStreamRequest = {
  session_id: 'agent-1',
  request_id: 'request-1',
  sequence: 0,
  snapshot: fixtureSession(),
  command: { action: 'runs', content: 'Read the fixture' },
};
function frame(event = fixtureEvent(request, 1, fixtureRun('planning'))) {
  return { event: 'agent', id: String(event.sequence), data: JSON.stringify(event) };
}
function execution(): AgentStreamRequest {
  return {
    ...request,
    snapshot: fixtureSession(0, [fixtureRun()]),
    command: { action: 'execute', run_id: 'run-1', ...planIdentity(fixturePlan()) },
    reviewed_plan: fixturePlan(),
  };
}
function resultRun(status: 'executing' | 'completed' = 'completed'): AgentRun {
  return normalizeRun(
    {
      ...fixtureRun(status),
      answer: status === 'completed' ? 'Recorded excerpt only.' : null,
      results: [
        {
          step_id: 'step-1',
          status: 'completed',
          content: 'Public excerpt 🌱',
          time: fixtureRun().updated_at,
        },
      ],
    },
    1,
  );
}
function sessionEvent(
  start: AgentStreamRequest,
  sequence: number,
  run = fixtureRun('executing'),
): AgentEvent {
  return {
    ...fixtureEvent(start, sequence, run),
    type: 'session.updated',
    request_id: 'foreign-stop',
    run_id: null,
    operation_id: null,
    run: null,
    request: null,
    plan: null,
    result: null,
    answer: null,
    error: null,
  };
}

describe('strict separate v2 public projections', () => {
  it('accepts quota-free plans above three steps and eight revisions', () => {
    const run = fixtureRun();
    run.plan!.revision = 100;
    run.plan!.steps = Array.from({ length: 45 }, (_, index) => ({
      ...fixturePlan().steps[0],
      id: `step-${index}`,
    }));
    expect(parseAgentRun(run).plan?.steps).toHaveLength(45);
    expect(parseAgentRun(run).plan?.limits).toBeUndefined();
  });
  it.each([
    'projection',
    'embedded history',
    'missing count',
    'missing current',
    'archived active',
    'archived pending',
    'identity model',
    'identity effort',
    'identity digest',
    'current settled',
    'current future',
  ])('rejects malformed header: %s', (kind) => {
    const header = fixtureSession(1, [fixtureRun()]);
    const raw = header as unknown as Record<string, unknown>;
    if (kind === 'projection') raw['projection_version'] = 1;
    if (kind === 'embedded history') raw['runs'] = [];
    if (kind === 'missing count') delete raw['run_count'];
    if (kind === 'missing current') delete raw['current_run'];
    if (kind === 'archived active') header.archived = true;
    if (kind === 'archived pending') {
      header.current_run = null;
      header.archived = true;
      header.archive_pending = true;
    }
    if (kind === 'identity model') header.current_run!.plan_identity!.model = 'other';
    if (kind === 'identity effort') header.current_run!.plan_identity!.reasoning_effort = 'high';
    if (kind === 'identity digest') header.current_run!.plan_identity!.digest = 'not-a-digest';
    if (kind === 'current settled') header.current_run!.status = 'cancelled';
    if (kind === 'current future') header.current_run!.version_sequence = 2;
    expect(() => parseAgentSession(header)).toThrow('Invalid Agent');
  });
  it('distinguishes completed steps and results from the loaded recent window', () => {
    const run = resultRun();
    run.plan!.steps = Array.from({ length: 60 }, (_, index) => ({
      ...fixturePlan().steps[0],
      id: `step-${index}`,
    }));
    run.completed_steps = 60;
    run.result_count = 60;
    run.results_before = 40;
    run.results = Array.from({ length: 20 }, (_, index) => ({
      ...resultRun().results[0],
      step_id: `step-${index + 40}`,
    }));
    expect(parseAgentRun(run)).toBe(run);
    expect(() => parseAgentRun({ ...run, completed_steps: 59 })).toThrow('Invalid Agent');
    expect(() => parseAgentRun({ ...run, result_count: 59 })).toThrow('Invalid Agent');
  });
  it.each([
    'plan',
    'answer',
    'result-count',
    'completed-count',
    'result-time',
    'result-order',
    'result-step',
    'running-settled',
    'error',
    'operation',
  ])('rejects invalid detail lifecycle: %s', (kind) => {
    const run = resultRun();
    if (kind === 'plan') run.plan = null;
    if (kind === 'answer') run.answer = null;
    if (kind === 'result-count') run.result_count = 0;
    if (kind === 'completed-count') run.completed_steps = 0;
    if (kind === 'result-time') run.results[0].time = '2026-02-31T00:00:00Z';
    if (kind === 'result-order') run.results.push(run.results[0]);
    if (kind === 'result-step') run.results[0].step_id = 'unknown';
    if (kind === 'running-settled') run.results[0].status = 'running';
    if (kind === 'error') run.error = { code: 'failure', message: 'Not complete' };
    if (kind === 'operation') run.operation_id = 'still-owned';
    expect(() => parseAgentRun(run)).toThrow('Invalid Agent');
  });
  it.each([
    'request on attempt',
    'plan on executing',
    'missing review plan',
    'answer early',
    'result early',
    'missing tool result',
    'foreign run',
    'wrong model',
  ])('rejects misplaced v2 event payload: %s', (kind) => {
    const event = fixtureEvent(request, 1, fixtureRun('planning'));
    if (kind === 'request on attempt') event.type = 'planner.attempt';
    if (kind === 'plan on executing') {
      event.request = null;
      event.plan = fixturePlan();
    }
    if (kind === 'missing review plan')
      Object.assign(event, fixtureEvent(request, 1, fixtureRun()), { plan: null });
    if (kind === 'answer early') event.answer = 'unearned';
    if (kind === 'result early') event.result = resultRun().results[0];
    if (kind === 'missing tool result')
      Object.assign(event, fixtureEvent(request, 1, fixtureRun('executing')), {
        type: 'tool.result',
      });
    if (kind === 'foreign run') event.run_id = 'other';
    if (kind === 'wrong model')
      Object.assign(event, fixtureEvent(request, 1, fixtureRun()), {
        session: { ...fixtureSession(1, [fixtureRun()]), model: 'other' },
      });
    expect(() => parseAgentEvent(event)).toThrow('Invalid Agent');
  });
});

describe('v2 causal stream ownership', () => {
  it('accepts admission jumps then requires contiguous sequences and exact duplicates', () => {
    const decoder = new AgentEventDecoder(request);
    const first = frame(fixtureEvent(request, 10, fixtureRun('planning')));
    expect(decoder.decode(first)?.sequence).toBe(10);
    expect(decoder.decode(first)).toBeNull();
    expect(decoder.decode(frame(fixtureEvent(request, 11, fixtureRun())))?.plan).toEqual(
      fixturePlan(),
    );
    expect(decoder.terminal).toBe(true);
  });
  it.each([
    'schema',
    'session',
    'request',
    'run',
    'operation',
    'sequence',
    'time',
    'changed duplicate',
    'gap',
  ])('fails closed for %s', (kind) => {
    const decoder = new AgentEventDecoder(request);
    decoder.decode(frame());
    const event = fixtureEvent(request, 2, fixtureRun('planning'));
    if (kind === 'schema') (event as unknown as Record<string, unknown>)['schema_version'] = 1;
    if (kind === 'session') event.session_id = 'other';
    if (kind === 'request') event.request_id = 'other';
    if (kind === 'run') event.run_id = 'other';
    if (kind === 'operation') event.operation_id = 'other';
    if (kind === 'sequence') event.sequence = 0;
    if (kind === 'time') event.time = '09:00';
    if (kind === 'changed duplicate') {
      event.sequence = 1;
      event.session.sequence = 1;
      event.run!.version_sequence = 1;
      event.type = 'changed';
    }
    if (kind === 'gap') {
      event.sequence = 3;
      event.session.sequence = 3;
      event.run!.version_sequence = 3;
    }
    expect(() => decoder.decode(frame(event))).toThrow('Invalid Agent');
  });
  it('does not accept a request echo belonging to another request or a historical run', () => {
    const event = fixtureEvent(request, 1, fixtureRun('planning'));
    event.request = 'unrelated';
    expect(() => new AgentEventDecoder(request).decode(frame(event))).toThrow('Invalid Agent');
    const historical = { ...request, snapshot: fixtureSession(0, [fixtureRun('rejected')]) };
    expect(() => new AgentEventDecoder(historical).decode(frame())).toThrow('Invalid Agent');
  });
  it.each(['planning', 'execution'] as const)(
    'requires the first owned %s event to be an admission',
    (kind) => {
      const start = kind === 'execution' ? execution() : request;
      const event = fixtureEvent(
        start,
        1,
        fixtureRun(kind === 'execution' ? 'executing' : 'planning'),
      );
      if (kind === 'planning') {
        event.type = 'planner.attempt';
        event.request = null;
      }
      expect(() => new AgentEventDecoder(start).decode(frame(event))).toThrow('Invalid Agent');
    },
  );
  it('binds changed results to the loaded approved plan before exposing them', () => {
    const start = execution();
    const decoder = new AgentEventDecoder(start);
    decoder.decode(frame(fixtureEvent(start, 1, fixtureRun('approved'))));
    const event = fixtureEvent(start, 2, resultRun('executing'));
    event.type = 'tool.result';
    event.result = { ...resultRun().results[0], step_id: 'foreign-step' };
    expect(() => decoder.decode(frame(event))).toThrow('Invalid Agent');
  });
  it.each(['digest', 'revision', 'id', 'model', 'reasoning_effort'] as const)(
    'freezes execution plan %s',
    (field) => {
      const start = execution();
      const decoder = new AgentEventDecoder(start);
      decoder.decode(frame(fixtureEvent(start, 1, fixtureRun('approved'))));
      const event = fixtureEvent(start, 2, fixtureRun('executing'));
      Object.assign(event.run!.plan_identity!, {
        [field]: field === 'revision' ? 2 : field === 'digest' ? 'a'.repeat(64) : 'changed',
      });
      event.session.current_run = structuredClone(event.run);
      expect(() => decoder.decode(frame(event))).toThrow('Invalid Agent');
    },
  );
  it('accepts foreign Stop/archive acknowledgements with null run and operation', () => {
    const start = execution();
    const decoder = new AgentEventDecoder(start);
    decoder.decode(frame(fixtureEvent(start, 1, fixtureRun('approved'))));
    const metadata = sessionEvent(start, 2);
    expect(decoder.decode(frame(metadata))?.request_id).toBe('foreign-stop');
    const archive = {
      ...sessionEvent(start, 3, fixtureRun('cancelling')),
      type: 'session.archive_pending',
    };
    archive.session.archive_pending = true;
    expect(decoder.decode(frame(archive))?.run).toBeNull();
    expect(decoder.terminal).toBe(false);
    expect(
      decoder.decode(frame(fixtureEvent(start, 4, fixtureRun('cancelled'))))?.run?.status,
    ).toBe('cancelled');
    expect(decoder.terminal).toBe(true);
  });
  it('uses causal sequence when valid wall-clock timestamps move backwards', () => {
    const decoder = new AgentEventDecoder(request);
    decoder.decode(frame());
    const event = fixtureEvent(request, 2, fixtureRun());
    event.time = '2025-01-01T00:00:00Z';
    event.run!.updated_at = event.time;
    event.session.updated_at = event.time;
    event.session.current_run!.updated_at = event.time;
    expect(decoder.decode(frame(event))?.sequence).toBe(2);
  });
  it('bounds duplicate signatures at 64 without limiting stream length; ancient repeats reconcile', () => {
    const decoder = new AgentEventDecoder(request);
    const first = frame();
    decoder.decode(first);
    for (let sequence = 2; sequence <= 500; sequence++) {
      const event = fixtureEvent(request, sequence, fixtureRun('planning'));
      event.type = 'planner.attempt';
      event.request = null;
      expect(decoder.decode(frame(event))?.sequence).toBe(sequence);
    }
    expect((decoder as unknown as { signatures: Map<number, string> }).signatures.size).toBe(64);
    expect(() => decoder.decode(first)).toThrow('Invalid Agent');
  });
});

describe('v2 HTTP transport (synthetic FETCH only)', () => {
  let fetchDouble: ReturnType<typeof vi.fn<typeof fetch>>;
  let api: AgentApi;
  beforeEach(() => {
    fetchDouble = vi.fn<typeof fetch>();
    TestBed.configureTestingModule({ providers: [{ provide: FETCH, useValue: fetchDouble }] });
    api = TestBed.inject(AgentApi);
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });
  const signal = () => new AbortController().signal;
  it('uses fixed-size encoded GET pages, no bulk prefetch or mutation', async () => {
    fetchDouble.mockResolvedValueOnce(
      Response.json(fixtureList([fixtureSession()], 'cursor /1', 80)),
    );
    await api.list(signal());
    fetchDouble.mockResolvedValueOnce(Response.json(fixtureList()));
    await api.list(signal(), 'cursor /1');
    fetchDouble.mockResolvedValueOnce(Response.json(fixtureSession(1, [], 'sid /1')));
    await api.get('sid /1', signal());
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        projection_version: 2,
        session_id: 'sid /1',
        at_sequence: 1,
        total: 100,
        runs: [],
        next_before: null,
      }),
    );
    await api.runs('sid /1', signal(), 90);
    fetchDouble.mockResolvedValueOnce(Response.json({ ...fixtureRun(), id: 'rid /1' }));
    await api.detail('sid /1', 'rid /1', signal());
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        projection_version: 2,
        session_id: 'sid /1',
        run_id: 'rid /1',
        at_sequence: 1,
        total: 50,
        completed_steps: 50,
        results: [],
        next_before: null,
      }),
    );
    await api.results('sid /1', 'rid /1', signal(), 20);
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        projection_version: 2,
        events: [],
        next_after: 5,
        through_sequence: 5,
        has_more: false,
      }),
    );
    await api.events('sid /1', 5, signal(), 5);
    expect(fetchDouble.mock.calls.map(([url]) => url)).toEqual([
      '/api/agent/sessions?include_archived=true&limit=25',
      '/api/agent/sessions?include_archived=true&limit=25&cursor=cursor%20%2F1',
      '/api/agent/sessions/sid%20%2F1',
      '/api/agent/sessions/sid%20%2F1/runs?limit=10&before=90',
      '/api/agent/sessions/sid%20%2F1/runs/rid%20%2F1?limit=20',
      '/api/agent/sessions/sid%20%2F1/runs/rid%20%2F1/results?limit=20&before=20',
      '/api/agent/sessions/sid%20%2F1/events?after=5&limit=50&through=5',
    ]);
    expect(fetchDouble.mock.calls.every(([, init]) => init?.method === 'GET' && !init.body)).toBe(
      true,
    );
  });
  it('preserves 202 pending archive header acknowledgements', async () => {
    const header = { ...fixtureSession(2, [fixtureRun('cancelling')]), archive_pending: true };
    fetchDouble.mockResolvedValueOnce(Response.json(header, { status: 202 }));
    expect(await api.archive(header.id, 'archive', signal())).toEqual(header);
    expect(JSON.parse(String(fetchDouble.mock.calls[0][1]?.body))).toEqual({
      request_id: 'archive',
    });
  });
  it('uses exact review identity and operation identity in mutation bodies', async () => {
    fetchDouble.mockImplementation(async () => Response.json(fixtureSession()));
    await api.create('create', 'model-a', 'medium', signal());
    await api.patch('agent-1', 'patch', { reasoning_effort: 'high' }, signal());
    await api.reject(
      'agent-1',
      'run /1',
      'reject',
      { kind: 'plan', ...planIdentity(fixturePlan()) },
      signal(),
    );
    await api.cancel('agent-1', 'run /1', 'stop', 'operation-1', signal());
    expect(fetchDouble.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { request_id: 'create', model: 'model-a', reasoning_effort: 'medium' },
      { request_id: 'patch', reasoning_effort: 'high' },
      { request_id: 'reject', kind: 'plan', ...planIdentity(fixturePlan()) },
      { request_id: 'stop', operation_id: 'operation-1' },
    ]);
  });
  it('streams split UTF-8 and releases the reader at review', async () => {
    const review = fixtureRun();
    review.plan!.objective = 'Public 🌱';
    const bytes = new TextEncoder().encode(
      await sseResponse([
        fixtureEvent(request, 1, fixtureRun('planning')),
        fixtureEvent(request, 2, review),
      ]).text(),
    );
    const cancel = vi.fn();
    fetchDouble.mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
          },
          cancel,
        }),
        { headers: { 'content-type': 'text/event-stream' } },
      ),
    );
    const emit = vi.fn();
    await api.stream(request, signal(), emit);
    expect(emit).toHaveBeenCalledTimes(2);
    expect(cancel).toHaveBeenCalledOnce();
    expect(emit.mock.calls[1][0].plan.objective).toBe('Public 🌱');
    expect(fetchDouble).toHaveBeenCalledOnce();
  });
  it.each(['EOF', 'bad JSON', 'content type', 'HTTP'])(
    'fails %s without reposting',
    async (kind) => {
      fetchDouble.mockResolvedValueOnce(
        kind === 'EOF'
          ? sseResponse([fixtureEvent(request, 1, fixtureRun('planning'))])
          : kind === 'HTTP'
            ? Response.json({ error: { message: 'Planner unavailable' } }, { status: 503 })
            : kind === 'bad JSON'
              ? new Response('event: agent\nid: 1\ndata: nope\n\n', {
                  headers: { 'content-type': 'text/event-stream' },
                })
              : new Response('<html>'),
      );
      await expect(api.stream(request, signal(), vi.fn())).rejects.toThrow();
      expect(fetchDouble).toHaveBeenCalledOnce();
    },
  );
  it('keeps long streams alive without a fixed timer and permits explicit Stop after hours', async () => {
    vi.useFakeTimers();
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const cancel = vi.fn();
    fetchDouble.mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            source = controller;
          },
          cancel,
        }),
        { headers: { 'content-type': 'text/event-stream' } },
      ),
    );
    const owner = new AbortController();
    const emit = vi.fn();
    const pending = api.stream(request, owner.signal, emit);
    const outcome = expect(pending).rejects.toThrow();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000);
    source.enqueue(
      new TextEncoder().encode(
        await sseResponse([fixtureEvent(request, 1, fixtureRun('planning'))]).text(),
      ),
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(owner.signal.aborted).toBe(false);
    owner.abort();
    await outcome;
    expect(cancel).toHaveBeenCalledOnce();
    expect(fetchDouble).toHaveBeenCalledOnce();
  });
});
