import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import { FETCH } from './chat-api';
import {
  fixtureEvent,
  FIXTURE_DIGEST,
  fixturePlan,
  fixtureRun,
  fixtureSession,
  sseResponse,
} from './agent-api.testing';
import { parseAgentSession, type AgentEvent, type AgentRun } from './agent.models';

const request: AgentStreamRequest = {
  session_id: 'agent-1',
  request_id: 'request-1',
  sequence: 0,
  command: { action: 'runs', content: 'Read the fixture' },
};
function frame(event = fixtureEvent(request, 1, fixtureRun('planning'))) {
  return { event: 'agent', id: String(event.sequence), data: JSON.stringify(event) };
}
function executionRequest(sequence = 0): AgentStreamRequest {
  return {
    ...request,
    sequence,
    snapshot: fixtureSession(sequence, [fixtureRun()]),
    command: {
      action: 'execute',
      run_id: 'run-1',
      plan_id: 'plan-1',
      revision: 1,
      digest: FIXTURE_DIGEST,
    },
  };
}
function finishedRun(): AgentRun {
  return {
    ...fixtureRun('completed'),
    answer: 'Recorded excerpt only.',
    results: [
      {
        step_id: 'step-1',
        status: 'completed',
        content: '1: Public excerpt',
        time: fixtureRun().updated_at,
      },
    ],
  };
}
describe('Agent protocol validation', () => {
  it('accepts planning to review and identical duplicate frames', () => {
    const decoder = new AgentEventDecoder(request);
    const first = frame();
    expect(decoder.decode(first)?.session.sequence).toBe(1);
    expect(decoder.decode(first)).toBeNull();
    expect(
      decoder.decode(frame(fixtureEvent(request, 2, fixtureRun())))?.session.runs[0].plan,
    ).toEqual(fixturePlan());
    expect(decoder.terminal).toBe(true);
  });
  it.each([
    ['schema_version', 2],
    ['session_id', 'other'],
    ['request_id', 'stale'],
    ['run_id', 'missing'],
    ['operation_id', 'stale'],
    ['sequence', 0],
    ['time', '09:00'],
    ['time', 'not-a-time'],
    ['time', '2026-02-31T00:00:00Z'],
    ['type', ''],
  ])('rejects invalid envelope %s=%s', (field, value) => {
    const event = {
      ...fixtureEvent(request, 1, fixtureRun('planning')),
      [field]: value,
    } as AgentEvent;
    expect(() => new AgentEventDecoder(request).decode(frame(event))).toThrow('Invalid Agent');
  });
  it.each([
    'id',
    'sessionSequence',
    'planArguments',
    'resultTime',
    'multipleActive',
    'effort',
    'runStatus',
    'error',
    'createdTime',
  ])('rejects malformed snapshot: %s', (field) => {
    const session = fixtureSession(1, [fixtureRun()]);
    if (field === 'id') session.id = '';
    if (field === 'sessionSequence') session.sequence = -1;
    if (field === 'planArguments') session.runs[0].plan!.steps[0].arguments!.offset = 0;
    if (field === 'resultTime')
      session.runs[0].results = [
        { step_id: 'step-1', status: 'completed', content: '', time: 'bad' },
      ];
    if (field === 'multipleActive') session.runs.push({ ...fixtureRun(), id: 'run-2' });
    if (field === 'effort')
      (session as unknown as Record<string, unknown>)['reasoning_effort'] = 'bogus';
    if (field === 'runStatus')
      (session.runs[0] as unknown as Record<string, unknown>)['status'] = 'bogus';
    if (field === 'error') (session.runs[0] as unknown as Record<string, unknown>)['error'] = {};
    if (field === 'createdTime') session.created_at = 'not-a-server-time';
    expect(() => parseAgentSession(session)).toThrow('Invalid Agent');
  });
  it('rejects changed duplicates, gaps, stale operations and events after terminal', () => {
    for (const change of ['duplicate', 'gap', 'operation', 'run', 'time', 'snapshot']) {
      const decoder = new AgentEventDecoder(request);
      decoder.decode(frame());
      const event = fixtureEvent(
        request,
        change === 'duplicate' ? 1 : change === 'gap' ? 3 : 2,
        fixtureRun('planning'),
      );
      if (change === 'duplicate') event.type = 'changed';
      if (change === 'operation') event.operation_id = 'other';
      if (change === 'run') event.run_id = 'other';
      if (change === 'time') event.time = 'invalid-time';
      if (change === 'snapshot') event.session.sequence = 0;
      expect(() => decoder.decode(frame(event)), change).toThrow('Invalid Agent');
    }
    const terminal = new AgentEventDecoder(request);
    terminal.decode(frame(fixtureEvent(request, 1, fixtureRun())));
    expect(() => terminal.decode(frame(fixtureEvent(request, 2, fixtureRun())))).toThrow(
      'Invalid Agent',
    );
  });
  it('binds execution to exactly the displayed plan identity', () => {
    const execute: AgentStreamRequest = {
      ...request,
      command: {
        action: 'execute',
        run_id: 'run-1',
        plan_id: 'plan-1',
        revision: 1,
        digest: FIXTURE_DIGEST,
      },
    };
    const run = fixtureRun('executing');
    expect(
      new AgentEventDecoder(execute).decode(frame(fixtureEvent(execute, 1, run))),
    ).not.toBeNull();
    run.plan!.digest = 'changed';
    expect(() =>
      new AgentEventDecoder(execute).decode(frame(fixtureEvent(execute, 1, run))),
    ).toThrow('Invalid Agent');
  });
  it('ends planning at clarification without requiring a plan', () => {
    const run = {
      ...fixtureRun('awaiting_clarification'),
      plan: null,
      clarification: { id: 'question-1', questions: ['Which file?'] },
    };
    const decoder = new AgentEventDecoder(request);
    expect(
      decoder.decode(frame(fixtureEvent(request, 1, run)))?.session.runs[0].clarification?.id,
    ).toBe('question-1');
    expect(decoder.terminal).toBe(true);
  });
  it.each(['wrong content', 'historical run'])(
    'rejects a first planning event for %s before dispatch',
    (kind) => {
      const start = {
        ...request,
        snapshot: fixtureSession(0, kind === 'historical run' ? [fixtureRun('rejected')] : []),
      };
      const run = fixtureRun('planning');
      if (kind === 'wrong content') run.request = 'An unrelated request';
      expect(() => new AgentEventDecoder(start).decode(frame(fixtureEvent(start, 1, run)))).toThrow(
        'Invalid Agent',
      );
    },
  );
  it.each(['old Stop', 'current Stop', 'archive'])(
    'accepts interleaved %s session records without replacing the owned operation',
    (kind) => {
      const start = executionRequest(4);
      const decoder = new AgentEventDecoder(start);
      decoder.decode(frame(fixtureEvent(start, 5, fixtureRun('approved'))));
      decoder.decode(frame(fixtureEvent(start, 6, fixtureRun('executing'))));
      let sequence = 7;
      const waiting = kind === 'old Stop' ? fixtureRun('executing') : fixtureRun('cancelling');
      if (kind !== 'old Stop') decoder.decode(frame(fixtureEvent(start, sequence++, waiting)));
      const sessionRecord: AgentEvent = {
        ...fixtureEvent(start, sequence++, waiting),
        type: kind === 'archive' ? 'session.archive_pending' : 'session.updated',
        request_id: kind === 'old Stop' ? 'stop-old-planning-operation' : 'another-control-command',
        run_id: null,
        operation_id: null,
      };
      if (kind === 'archive') sessionRecord.session.archive_pending = true;
      expect(decoder.decode(frame(sessionRecord))?.sequence).toBe(sequence - 1);
      expect(decoder.terminal).toBe(false);
      expect(decoder.decode(frame(sessionRecord))).toBeNull();
      const terminal = fixtureEvent(
        start,
        sequence,
        kind === 'old Stop' ? finishedRun() : fixtureRun('cancelled'),
      );
      if (kind === 'archive') terminal.session.archived = true;
      expect(decoder.decode(frame(terminal))?.run_id).toBe('run-1');
      expect(decoder.terminal).toBe(true);
    },
  );
  it('rejects foreign work and changed operation IDs even after accepting foreign session acknowledgements', () => {
    const start = executionRequest();
    for (const change of ['request', 'run', 'operation', 'session event identity']) {
      const decoder = new AgentEventDecoder(start);
      decoder.decode(frame(fixtureEvent(start, 1, fixtureRun('approved'))));
      const metadata: AgentEvent = {
        ...fixtureEvent(start, 2, fixtureRun('executing')),
        type: 'session.updated',
        request_id: 'old-stop',
        run_id: null,
        operation_id: null,
      };
      decoder.decode(frame(metadata));
      const event = fixtureEvent(start, 3, fixtureRun('executing'));
      if (change === 'request') event.request_id = 'malicious-foreign-work';
      if (change === 'run') event.run_id = 'another-run';
      if (change === 'operation') event.operation_id = 'another-operation';
      if (change === 'session event identity') event.type = 'session.updated';
      expect(() => decoder.decode(frame(event)), change).toThrow('Invalid Agent');
    }
  });
  it.each(['new run', 'revise', 'execute'] as const)(
    'establishes the first owned %s admission baseline and then requires contiguous events',
    (kind) => {
      const start: AgentStreamRequest =
        kind === 'execute'
          ? executionRequest(2)
          : kind === 'revise'
            ? {
                ...executionRequest(2),
                command: {
                  action: 'revise',
                  run_id: 'run-1',
                  kind: 'feedback',
                  content: 'Use fewer lines',
                  plan_id: 'plan-1',
                  revision: 1,
                  digest: FIXTURE_DIGEST,
                },
              }
            : { ...request, sequence: 2, snapshot: fixtureSession(2) };
      const run =
        kind === 'execute'
          ? fixtureRun('approved')
          : { ...fixtureRun('planning'), plan: kind === 'revise' ? fixturePlan() : null };
      const admission = fixtureEvent(start, 10, run);
      admission.session.title = 'Title patched by another tab before admission';
      const decoder = new AgentEventDecoder(start);
      expect(decoder.decode(frame(admission))?.sequence).toBe(10);
      expect(decoder.decode(frame(admission))).toBeNull();
      const next = fixtureEvent(start, 11, kind === 'execute' ? fixtureRun('executing') : run);
      expect(decoder.decode(frame(next))?.sequence).toBe(11);
      expect(() => decoder.decode(frame(fixtureEvent(start, 13, run)))).toThrow('Invalid Agent');
    },
  );
  it('does not use a baseline jump to accept stale approval, foreign work or a non-admission event', () => {
    for (const change of ['digest', 'request', 'not admission']) {
      const start = executionRequest(2);
      const event = fixtureEvent(start, 10, fixtureRun('approved'));
      if (change === 'digest') event.session.runs[0].plan!.digest = 'b'.repeat(64);
      if (change === 'request') event.request_id = 'foreign';
      if (change === 'not admission') event.type = 'planner.attempt';
      expect(() => new AgentEventDecoder(start).decode(frame(event)), change).toThrow(
        'Invalid Agent',
      );
    }
  });
});

function invalidLifecycle(kind: string) {
  const run = fixtureRun('executing');
  const session = fixtureSession(1, [run]);
  if (kind === 'approved without plan') {
    run.status = 'approved';
    run.plan = null;
  }
  if (kind === 'executing without plan') run.plan = null;
  if (kind === 'archived active run') session.archived = true;
  if (kind === 'archived and pending') {
    session.archived = true;
    session.archive_pending = true;
    session.runs = [];
  }
  if (kind === 'invalid archive flag')
    (session as unknown as Record<string, unknown>)['archive_pending'] = 'pending';
  if (kind === 'preapproval tool result') run.status = 'awaiting_approval';
  if (kind.startsWith('completed')) {
    run.status = 'completed';
    run.operation_id = null;
    run.answer = 'A verified answer';
  }
  run.results = [
    { step_id: 'step-1', status: 'completed', content: 'Recorded excerpt', time: run.updated_at },
  ];
  if (kind === 'completed without plan') {
    run.plan = null;
    run.results = [];
  }
  if (kind === 'completed without answer') run.answer = null;
  if (kind === 'completed without result') run.results = [];
  if (kind === 'completed with failed result') run.results[0].status = 'failed';
  if (kind === 'completed with error') run.error = { code: 'failure', message: 'Not complete' };
  if (kind === 'completed result with error')
    run.results[0].error = { code: 'failure', message: 'Not a successful result' };
  if (kind === 'rejected without identity') {
    run.status = 'rejected';
    run.operation_id = null;
    run.plan = null;
    run.results = [];
  }
  if (kind === 'settled running result') {
    run.status = 'interrupted';
    run.operation_id = null;
    run.results[0].status = 'running';
  }
  if (kind === 'settled operation owner') {
    run.status = 'cancelled';
  }
  return session;
}

describe('AgentApi transport', () => {
  let fetchDouble: ReturnType<typeof vi.fn<typeof fetch>>;
  let api: AgentApi;
  beforeEach(() => {
    fetchDouble = vi.fn<typeof fetch>();
    TestBed.configureTestingModule({ providers: [{ provide: FETCH, useValue: fetchDouble }] });
    api = TestBed.inject(AgentApi);
  });
  afterEach(() => TestBed.resetTestingModule());
  it('uses separate session routes, encoded IDs, fresh command identities and no Chat fields', async () => {
    fetchDouble.mockImplementation(async () => Response.json(fixtureSession(0, [], 'id /1')));
    const signal = new AbortController().signal;
    await api.get('id /1', signal);
    await api.patch('id /1', 'patch-id', { reasoning_effort: 'high' }, signal);
    await api.reject(
      'id /1',
      'run /1',
      'reject-id',
      { kind: 'clarification', clarification_id: 'question-1' },
      signal,
    );
    await api.cancel('id /1', 'run /1', 'stop-id', 'op-1', signal);
    await api.archive('id /1', 'archive-id', signal);
    expect(fetchDouble.mock.calls.map(([url]) => url)).toEqual([
      '/api/agent/sessions/id%20%2F1',
      '/api/agent/sessions/id%20%2F1',
      '/api/agent/sessions/id%20%2F1/runs/run%20%2F1/reject',
      '/api/agent/sessions/id%20%2F1/runs/run%20%2F1/cancel',
      '/api/agent/sessions/id%20%2F1/archive',
    ]);
    expect(JSON.parse(fetchDouble.mock.calls[2][1]!.body as string)).toEqual({
      request_id: 'reject-id',
      kind: 'clarification',
      clarification_id: 'question-1',
    });
    expect(JSON.parse(fetchDouble.mock.calls[3][1]!.body as string)).toEqual({
      request_id: 'stop-id',
      operation_id: 'op-1',
    });
    expect(fetchDouble.mock.calls[1][1]?.method).toBe('PATCH');
  });
  it('lists archived records and creates only with preferences and request ID', async () => {
    fetchDouble
      .mockResolvedValueOnce(Response.json({ sessions: [fixtureSession()] }))
      .mockResolvedValueOnce(Response.json(fixtureSession()));
    const signal = new AbortController().signal;
    expect(await api.list(signal)).toHaveLength(1);
    await api.create('create-id', 'model-a', 'medium', signal);
    expect(fetchDouble.mock.calls[0][0]).toBe('/api/agent/sessions?include_archived=true');
    expect(JSON.parse(fetchDouble.mock.calls[1][1]!.body as string)).toEqual({
      request_id: 'create-id',
      model: 'model-a',
      reasoning_effort: 'medium',
    });
  });
  it('decodes chunked UTF-8 SSE and cancels/releases the reader at review', async () => {
    const payload = await sseResponse([
      fixtureEvent(request, 1, fixtureRun('planning')),
      fixtureEvent(request, 2, fixtureRun()),
    ]).text();
    const cancel = vi.fn();
    const bytes = new TextEncoder().encode(payload);
    fetchDouble.mockResolvedValue(
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
    await api.stream(request, new AbortController().signal, emit);
    expect(emit).toHaveBeenCalledTimes(2);
    expect(cancel).toHaveBeenCalledOnce();
    expect(JSON.parse(fetchDouble.mock.calls[0][1]!.body as string)).toEqual({
      request_id: 'request-1',
      content: 'Read the fixture',
    });
  });
  it.each([
    [
      'HTTP',
      () =>
        Response.json(
          { detail: { code: 'planner_unavailable', message: 'Configure an Agent planner.' } },
          { status: 503 },
        ),
      'Configure an Agent planner.',
    ],
    ['content type', () => new Response('<html>'), 'Invalid Agent'],
    [
      'EOF',
      () => sseResponse([fixtureEvent(request, 1, fixtureRun('planning'))]),
      'before settlement',
    ],
    [
      'bad JSON',
      () =>
        new Response('event: agent\nid: 1\ndata: nope\n\n', {
          headers: { 'content-type': 'text/event-stream' },
        }),
      'Invalid Agent',
    ],
  ] as const)('fails safely on %s without another POST', async (_name, response, message) => {
    fetchDouble.mockResolvedValue(response());
    const emit = vi.fn();
    await expect(api.stream(request, new AbortController().signal, emit)).rejects.toThrow(message);
    expect(fetchDouble).toHaveBeenCalledOnce();
  });
  it('aborts a stalled reader without accepting later bytes', async () => {
    const cancel = vi.fn();
    fetchDouble.mockResolvedValue(
      new Response(new ReadableStream({ cancel }), {
        headers: { 'content-type': 'text/event-stream' },
      }),
    );
    const controller = new AbortController();
    const emit = vi.fn();
    const pending = api.stream(request, controller.signal, emit);
    await vi.waitFor(() => expect(fetchDouble).toHaveBeenCalledOnce());
    controller.abort();
    await expect(pending).rejects.toThrow();
    expect(emit).not.toHaveBeenCalled();
    expect(cancel).toHaveBeenCalledOnce();
  });
  it.each([
    'approved without plan',
    'executing without plan',
    'archived active run',
    'archived and pending',
    'invalid archive flag',
    'preapproval tool result',
    'completed without plan',
    'completed without answer',
    'completed without result',
    'completed with failed result',
    'completed with error',
    'settled running result',
    'settled operation owner',
    'completed result with error',
    'rejected without identity',
  ])('rejects %s from raw JSON and SSE before exposing a snapshot', async (kind) => {
    const session = invalidLifecycle(kind);
    fetchDouble.mockResolvedValueOnce(Response.json(session));
    await expect(api.get('agent-1', new AbortController().signal)).rejects.toThrow('Invalid Agent');
    const event = fixtureEvent(request, 1, session.runs[0] ?? fixtureRun('planning'));
    event.session = session;
    fetchDouble.mockResolvedValueOnce(sseResponse([event]));
    const emit = vi.fn();
    await expect(api.stream(request, new AbortController().signal, emit)).rejects.toThrow(
      'Invalid Agent',
    );
    expect(emit).not.toHaveBeenCalled();
  });
  it('accepts plan-less planning cancellation and recovered partial execution failures', async () => {
    const stoppedPlanning = { ...fixtureRun('cancelled'), plan: null };
    const interruptedPlanning = { ...fixtureRun('interrupted'), id: 'planning-2', plan: null };
    const recovered = {
      ...fixtureRun('interrupted'),
      id: 'execution-1',
      error: { code: 'outcome_unknown', message: 'Interrupted during restart' },
      results: [
        {
          step_id: 'step-1',
          status: 'failed' as const,
          content: '',
          time: stoppedPlanning.updated_at,
          error: { code: 'outcome_unknown', message: 'The running result was settled as failed' },
        },
      ],
    };
    const session = {
      ...fixtureSession(1, [stoppedPlanning, interruptedPlanning, recovered]),
      archive_pending: true,
    };
    fetchDouble.mockResolvedValueOnce(Response.json(session));
    expect(await api.get('agent-1', new AbortController().signal)).toEqual(session);
    const completed = {
      ...fixtureRun('completed'),
      answer: 'Verified answer',
      results: [
        {
          step_id: 'step-1',
          status: 'completed' as const,
          content: 'Excerpt',
          time: stoppedPlanning.updated_at,
        },
      ],
    };
    expect(
      parseAgentSession({
        ...fixtureSession(2, [completed]),
        archived: true,
        archive_pending: false,
      }).archived,
    ).toBe(true);
  });
  it('streams an old Stop acknowledgement while execution continues through its own terminal result', async () => {
    const start = executionRequest();
    const metadata: AgentEvent = {
      ...fixtureEvent(start, 3, fixtureRun('executing')),
      type: 'session.updated',
      request_id: 'stop-an-already-settled-planning-operation',
      run_id: null,
      operation_id: null,
    };
    fetchDouble.mockResolvedValueOnce(
      sseResponse([
        fixtureEvent(start, 1, fixtureRun('approved')),
        fixtureEvent(start, 2, fixtureRun('executing')),
        metadata,
        fixtureEvent(start, 4, finishedRun()),
      ]),
    );
    const emit = vi.fn();
    await api.stream(start, new AbortController().signal, emit);
    expect(emit).toHaveBeenCalledTimes(4);
    expect(emit.mock.calls[2][0].request_id).toBe(metadata.request_id);
    expect(emit.mock.calls[3][0].session.runs[0].status).toBe('completed');
    expect(fetchDouble).toHaveBeenCalledOnce();
  });
  it('uses sequence causality when valid UTC wall-clock readings step backwards in GET, list and streams', async () => {
    const planning = fixtureEvent(request, 1, fixtureRun('planning'));
    const attempt = fixtureEvent(request, 2, fixtureRun('planning'));
    attempt.type = 'planner.attempt';
    const review = fixtureEvent(request, 3, fixtureRun());
    for (const [index, event] of [planning, attempt, review].entries()) {
      event.time = `2026-10-16T08:00:0${3 - index}.000Z`;
      event.session.updated_at = `2026-10-16T08:01:0${3 - index}.000Z`;
      event.session.runs[0].updated_at = `2026-10-16T07:00:0${3 - index}.000Z`;
    }
    fetchDouble.mockResolvedValueOnce(Response.json(review.session));
    expect(await api.get('agent-1', new AbortController().signal)).toEqual(review.session);
    fetchDouble.mockResolvedValueOnce(Response.json({ sessions: [review.session] }));
    expect(await api.list(new AbortController().signal)).toEqual([review.session]);
    fetchDouble.mockResolvedValueOnce(sseResponse([planning, attempt, review]));
    const emit = vi.fn();
    await api.stream(request, new AbortController().signal, emit);
    expect(emit).toHaveBeenCalledTimes(3);
    expect(emit.mock.calls[2][0].sequence).toBe(3);
  });
  it.each([
    ['path', 123],
    ['offset', 0],
    ['offset', 1.5],
    ['returned_lines', -1],
    ['truncated', 'yes'],
  ])('rejects malformed optional excerpt metadata %s=%s', async (field, value) => {
    const run = finishedRun();
    (run.results[0] as unknown as Record<string, unknown>)[field] = value;
    fetchDouble.mockResolvedValueOnce(Response.json(fixtureSession(1, [run])));
    await expect(api.get('agent-1', new AbortController().signal)).rejects.toThrow('Invalid Agent');
  });
  it('preserves nullable running metadata and bounded clipped-line result metadata in JSON and SSE', async () => {
    const executing = fixtureRun('executing');
    executing.results = [
      {
        step_id: 'step-1',
        status: 'running',
        content: '',
        time: executing.updated_at,
        path: null,
        offset: null,
        returned_lines: null,
        truncated: false,
      },
    ];
    expect(parseAgentSession(fixtureSession(1, [executing])).runs[0].results[0].path).toBeNull();
    const run = finishedRun();
    run.results[0] = {
      ...run.results[0],
      content: `1: ${'x'.repeat(1000)}`,
      path: 'example.md',
      offset: 1,
      returned_lines: 1,
      truncated: true,
    };
    fetchDouble.mockResolvedValueOnce(Response.json(fixtureSession(2, [run])));
    expect((await api.get('agent-1', new AbortController().signal)).runs[0].results[0]).toEqual(
      run.results[0],
    );
    const start = executionRequest();
    fetchDouble.mockResolvedValueOnce(
      sseResponse([fixtureEvent(start, 1, fixtureRun('approved')), fixtureEvent(start, 2, run)]),
    );
    const emit = vi.fn();
    await api.stream(start, new AbortController().signal, emit);
    expect(emit.mock.calls[1][0].session.runs[0].results[0]).toEqual(run.results[0]);
  });
});
