import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import fixture from './agent-contract.fixture.json';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import { FakeAgentApi, fixtureList, memoryAgentStorage } from './agent-api.testing';
import { reduceRunView } from './agent-event-state';
import { advanceResults } from './agent-progress';
import { AgentState } from './agent-state';
import {
  isLlmEvent,
  isRecoveryEvent,
  parseAgentEvent,
  parseAgentRun,
  parseEventsPage,
  planIdentity,
  type AgentEvent,
  type AgentRun,
  type AgentSession,
  type RunView,
} from './agent.models';
import { CHAT_STORAGE } from './chat-state';
import { SseDecoder } from './sse-decoder';

interface Exchange {
  name: string;
  baseline: AgentSession;
  terminal_run: AgentRun;
  sse: string;
}
const streams = fixture.streams as unknown as Exchange[];
const recorded = streams.find((entry) => entry.name === 'recorded_planning')!;
const kinds = ['llm.intent', 'llm.dispatched', 'llm.output', 'llm.outcome', 'llm.validation'];
function frames(entry: Exchange): AgentEvent[] {
  return new SseDecoder()
    .push(new TextEncoder().encode(entry.sse))
    .map((frame) => parseAgentEvent(JSON.parse(frame.data)));
}
function frame(event: AgentEvent) {
  return { event: 'agent', id: String(event.sequence), data: JSON.stringify(event) };
}
function request(): AgentStreamRequest {
  return {
    session_id: recorded.baseline.id,
    request_id: 'plan',
    sequence: recorded.baseline.sequence,
    snapshot: recorded.baseline,
    command: { action: 'runs', content: recorded.terminal_run.request },
  };
}
function loadedView(entry: Exchange): RunView {
  const { plan_identity: _, ...control } = entry.baseline.current_run!;
  const body = parseAgentRun({
    ...entry.terminal_run,
    ...control,
    results: [],
    answer: null,
    results_before: null,
  });
  return { body, window: { kind: 'recent', before: null, at_sequence: entry.baseline.sequence } };
}
function progress(previous: AgentEvent, type: string): AgentEvent {
  return {
    ...structuredClone(previous),
    type,
    sequence: previous.sequence + 1,
    session: { ...structuredClone(previous.session), sequence: previous.sequence + 1 },
    request: null,
    plan: null,
    result: null,
    answer: null,
  };
}

describe('private LLM events as public neutral progress', () => {
  it('decodes the actual partial/full response stream and creates a plan only at review', () => {
    const decoder = new AgentEventDecoder(request());
    let view: RunView | null = null;
    const events = frames(recorded);
    expect(events.filter(isLlmEvent).map((event) => event.type)).toEqual([
      'llm.intent',
      'llm.dispatched',
      'llm.output',
      'llm.output',
      'llm.outcome',
      'llm.validation',
    ]);
    for (const event of events) {
      expect(decoder.decode(frame(event))).toEqual(event);
      view = reduceRunView(view, event);
      expect(view?.body.results).toEqual([]);
      expect(view?.body.completed_steps).toBe(0);
      if (event.type === 'run.awaiting_approval') {
        expect(decoder.terminal).toBe(true);
        expect(view?.body).toEqual(recorded.terminal_run);
      } else {
        expect(decoder.terminal).toBe(false);
        expect(view?.body.status).toBe('planning');
        expect(view?.body.plan).toBeNull();
      }
    }
  });

  it.each(kinds)('does not fabricate a missing body from %s', (kind) => {
    const event = frames(recorded).find((event) => event.type === kind)!;
    expect(reduceRunView(null, event)).toBeNull();
  });

  it('accepts recording-only version and clock changes without changing coordinator state', () => {
    const events = frames(recorded);
    const view = reduceRunView(null, events[0]);
    const event = structuredClone(events[1]);
    event.time = event.session.updated_at = event.run!.updated_at = '2026-10-16T09:00:01.000Z';
    event.run!.version_sequence = event.sequence;
    event.session.current_run = event.run;
    const observed = reduceRunView(view, parseAgentEvent(event))!;
    expect(observed.body).toEqual({
      ...view!.body,
      updated_at: event.time,
      version_sequence: event.sequence,
    });
    expect(observed.body.plan).toBeNull();
  });

  it.each(kinds)('rejects injected public bodies and private fields on %s', (kind) => {
    const event = frames(recorded).find((event) => event.type === kind)!;
    const execution = streams.find((entry) => entry.name === 'execution')!;
    const result = execution.terminal_run.results[0];
    for (const injection of [
      { request: 'SYNTHETIC-private-user-message' },
      { plan: recorded.terminal_run.plan },
      { result },
      { answer: 'SYNTHETIC-private-output' },
      { error: { code: 'provider_failure', message: 'Injected provider failure' } },
      { audit: { data: { body: 'SYNTHETIC-private-output' } } },
      { input: 'SYNTHETIC-private-user-message' },
      { output: 'SYNTHETIC-private-output' },
      { usage: { input_tokens: 11 } },
      { previous_response_id: 'SYNTHETIC-private-continuation' },
      { client: { credential: 'SYNTHETIC-client-credential' } },
      { run: { ...event.run, usage: { output_tokens: 23 } } },
      { invalidates_runs: true },
    ]) {
      expect(() => parseAgentEvent({ ...event, ...injection }), JSON.stringify(injection)).toThrow(
        'Invalid Agent',
      );
    }
  });

  it.each(kinds)('cannot unlock approval or settle work with %s', (kind) => {
    const original = frames(recorded).find((event) => event.type === kind)!;
    const approval = frames(recorded).at(-1)!;
    for (const status of [
      'awaiting_approval',
      'awaiting_clarification',
      'completed',
      'cancelled',
    ]) {
      const event = structuredClone(original);
      event.run = { ...approval.run!, status: status as AgentRun['status'] };
      event.session.current_run = event.run;
      expect(() => parseAgentEvent(event)).toThrow('Invalid Agent');
    }
  });

  it('rejects an invented candidate identity in a loaded planning body', () => {
    const events = frames(recorded);
    const view = reduceRunView(null, events[0]);
    const event = structuredClone(events[1]);
    event.run!.plan_identity = events.at(-1)!.run!.plan_identity;
    event.session.current_run = event.run;
    // A planning identity may legitimately name a prior revision; the loaded body
    // supplies the causal check that this new run had no such candidate.
    expect(() => reduceRunView(view, parseAgentEvent(event))).toThrow('Invalid Agent');
    expect(view?.body.plan).toBeNull();
  });

  it.each(['execution', 'cancellation_archive'])(
    'preserves the running step frontier in %s',
    (name) => {
      const entry = streams.find((entry) => entry.name === name)!;
      let view = loadedView(entry);
      const events = frames(entry);
      const through = name === 'execution' ? 'tool.intent' : 'run.cancelling';
      const prefix = events.slice(0, events.findIndex((event) => event.type === through) + 1);
      for (const event of prefix) view = reduceRunView(view, event)!;
      const previous = prefix.at(-1)!;
      const frontier = { step_id: view.body.results[0].step_id, status: 'running' as const };
      const allowed = [...(name === 'execution' ? kinds : kinds.slice(2)), 'tool.chunk'];
      for (const kind of allowed) {
        const event = parseAgentEvent(progress(previous, kind));
        const decoder = new AgentEventDecoder({
          session_id: entry.baseline.id,
          request_id: 'execute',
          sequence: entry.baseline.sequence,
          snapshot: entry.baseline,
          reviewed_plan: view.body.plan!,
          command: { action: 'execute', run_id: view.body.id, ...planIdentity(view.body.plan!) },
        });
        for (const event of prefix) decoder.decode(frame(event));
        expect(decoder.decode(frame(event))).toEqual(event);
        expect(decoder.terminal).toBe(false);
        expect(advanceResults(previous.run, event, view.body.plan!, frontier)).toBe(frontier);
        for (const window of ['recent', 'older'] as const) {
          const paged = {
            ...view,
            body: window === 'older' ? { ...view.body, results: [] } : view.body,
            window: { ...view.window, kind: window },
          };
          const next = reduceRunView(paged, event)!;
          expect(next.body.results).toEqual(paged.body.results);
          expect(next.body.plan).toBe(view.body.plan);
          expect(next.body.status).toBe(view.body.status);
          expect(next.body.result_count).toBe(view.body.result_count);
          expect(next.body.completed_steps).toBe(view.body.completed_steps);
          expect(next.window.kind).toBe(window);
          if (window === 'older') expect(next.window).toBe(paged.window);
        }
      }
      for (const kind of [...kinds, 'tool.chunk']) {
        const injected = progress(previous, kind);
        injected.run!.completed_steps = 1;
        injected.session.current_run = injected.run;
        expect(() => advanceResults(previous.run, injected, view.body.plan!, frontier)).toThrow(
          'Invalid Agent',
        );
        expect(() => reduceRunView(view, injected)).toThrow('Invalid Agent');
      }
      if (name === 'cancellation_archive') {
        for (const kind of kinds.slice(0, 2))
          expect(() => parseAgentEvent(progress(previous, kind))).toThrow('Invalid Agent');
      }
    },
  );

  it('restricts tool.chunk to body-free, private-metadata-free executing/cancelling progress', () => {
    const entry = streams.find((entry) => entry.name === 'execution')!;
    const intent = frames(entry).find((event) => event.type === 'tool.intent')!;
    const chunk = progress(intent, 'tool.chunk');
    expect(parseAgentEvent(chunk)).toEqual(chunk);
    expect(reduceRunView(null, chunk)).toBeNull();
    for (const injection of [
      { request: 'private request' },
      { plan: entry.terminal_run.plan },
      { result: intent.result },
      { answer: 'private findings' },
      { read: { chunks: 1 } },
      { payload: { bytes_read: 100 } },
      { audit: { body: 'private chunk' } },
      { content: 'private chunk' },
      { error: { code: 'injected', message: 'injected' } },
      { invalidates_runs: true },
    ])
      expect(() => parseAgentEvent({ ...chunk, ...injection })).toThrow('Invalid Agent');
    for (const status of ['planning', 'approved', 'awaiting_approval', 'completed'] as const) {
      const event = structuredClone(chunk);
      event.run!.status = status;
      event.run!.result_count = event.run!.completed_steps = 0;
      event.run!.operation_id = ['planning', 'approved'].includes(status)
        ? event.operation_id
        : null;
      event.session.current_run = event.run;
      expect(() => parseAgentEvent(event)).toThrow('Invalid Agent');
    }
    const view = reduceRunView(loadedView(entry), intent)!;
    for (const mutation of [
      { status: 'cancelling' as const },
      { operation_id: 'another-operation' },
      { result_count: 0 },
      { completed_steps: 1 },
      { plan_identity: { ...chunk.run!.plan_identity!, digest: 'f'.repeat(64) } },
    ]) {
      const event = structuredClone(chunk);
      Object.assign(event.run!, mutation);
      event.session.current_run = event.run;
      for (const kind of ['recent', 'older'] as const) {
        expect(() => reduceRunView({ ...view, window: { ...view.window, kind } }, event)).toThrow(
          'Invalid Agent',
        );
      }
    }
    const clock = structuredClone(chunk);
    clock.run!.version_sequence = clock.sequence;
    clock.run!.updated_at = '2026-10-16T09:00:01.000Z';
    clock.session.current_run = clock.run;
    expect(reduceRunView(view, parseAgentEvent(clock))!.body).toEqual({
      ...view.body,
      version_sequence: clock.sequence,
      updated_at: clock.run!.updated_at,
    });
  });

  it('accepts the real unmatched-intent recovery outcome before null-ID invalidation', () => {
    const recovery = fixture.recoveries.find((entry) => entry.name === 'llm_startup_recovery')!;
    const page = parseEventsPage(recovery.body);
    const [outcome, interrupted] = page.events;
    expect(isRecoveryEvent(outcome)).toBe(false);
    expect(outcome).toMatchObject({
      type: 'llm.outcome',
      request_id: 'recovery',
      invalidates_runs: false,
      run_id: recovery.baseline_run.id,
      operation_id: recovery.baseline_run.operation_id,
      request: null,
      plan: null,
      result: null,
      answer: null,
      error: null,
    });
    const view: RunView = {
      body: parseAgentRun(recovery.baseline_run),
      window: { kind: 'recent', before: null, at_sequence: recovery.baseline.sequence },
    };
    const observed = reduceRunView(view, outcome)!;
    expect(observed.body).toEqual(view.body);
    expect(observed.body.plan).toBeNull();
    expect(observed.window.at_sequence).toBe(outcome.sequence);
    expect(isRecoveryEvent(interrupted)).toBe(true);
    expect(interrupted.run_id).toBeNull();
    expect(interrupted.operation_id).toBeNull();
    expect(reduceRunView(observed, interrupted)?.body).toEqual(recovery.terminal_run);
  });
});

describe('LLM progress approval controls', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('keeps approval locked through validation and exposes only the reviewed plan', async () => {
    const api = new FakeAgentApi();
    api.list.mockResolvedValue(fixtureList([recorded.baseline]));
    api.get.mockResolvedValue(recorded.baseline);
    TestBed.configureTestingModule({
      providers: [
        { provide: AgentApi, useValue: api },
        { provide: CHAT_STORAGE, useValue: memoryAgentStorage() },
      ],
    });
    const state = TestBed.inject(AgentState);
    await state.load();
    state.updateDraft(recorded.terminal_run.request);
    state.request();
    const call = api.calls[0];
    for (const event of frames(recorded)) {
      call.emit({ ...event, request_id: call.request.request_id });
      const body = state.currentView()!.body;
      expect(state.canApprove(body)).toBe(event.type === 'run.awaiting_approval');
      expect(body.results).toEqual([]);
      if (event.type !== 'run.awaiting_approval') {
        expect(body.plan).toBeNull();
        state.approve(body);
        expect(api.calls).toHaveLength(1);
      }
    }
    call.resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.currentView()!.body.plan).toEqual(recorded.terminal_run.plan);
    expect(api.calls).toHaveLength(1);
  });
});
