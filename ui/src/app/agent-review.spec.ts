import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import { AgentState } from './agent-state';
import {
  FakeAgentApi,
  fixtureEvent,
  fixtureList,
  fixturePlan,
  fixtureRun,
  fixtureSession,
  fixtureStepEvents,
  memoryAgentStorage,
  normalizeRun,
} from './agent-api.testing';
import {
  controlOf,
  parseAgentEvent,
  parseAgentRun,
  parseAgentSession,
  parseResultsPage,
  planIdentity,
  type AgentEvent,
  type AgentRun,
  type AgentSession,
  type ResultsPage,
  type RunsPage,
} from './agent.models';
import { FETCH } from './chat-api';
import { CHAT_STORAGE } from './chat-state';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function planned(count = 3): AgentRun {
  const run = fixtureRun();
  run.plan!.steps = Array.from({ length: count }, (_, index) => ({
    ...fixturePlan().steps[0],
    id: `step-${index + 1}`,
  }));
  return run;
}
function completed(id = 'run-1', count = 1, sequence = 6): AgentRun {
  const run = planned(count);
  return {
    ...run,
    id,
    status: 'completed',
    operation_id: null,
    answer: 'Complete',
    version_sequence: sequence,
    result_count: count,
    completed_steps: count,
    results_before: count > 20 ? count - 20 : null,
    results: run.plan!.steps.slice(-20).map((step) => ({
      step_id: step.id,
      status: 'completed',
      content: step.id,
      time: run.updated_at,
    })),
  };
}
function frame(event: AgentEvent) {
  return { event: 'agent', id: String(event.sequence), data: JSON.stringify(event) };
}
function runsPage(header: AgentSession, runs: AgentRun[], before: number | null = null): RunsPage {
  return {
    projection_version: 2,
    session_id: header.id,
    at_sequence: header.sequence,
    total: header.run_count,
    runs: runs.map((run) => ({ ...controlOf(run), request_preview: run.request })),
    next_before: before,
  };
}
function recovered(
  start: AgentStreamRequest,
  sequence: number,
  run = fixtureRun('interrupted'),
): AgentEvent {
  const error = {
    code: 'outcome_unknown',
    message: 'Interrupted during restart; work was not replayed.',
  };
  const body = { ...run, status: 'interrupted' as const, operation_id: null, error };
  return {
    ...fixtureEvent(start, sequence, body),
    invalidates_runs: true,
    request_id: 'recovery',
    run_id: null,
    operation_id: null,
    error,
  };
}

describe('review regressions: causal public views and constant-memory progress', () => {
  const run = planned();
  const start: AgentStreamRequest = {
    session_id: 'agent-1',
    request_id: 'execute',
    sequence: 0,
    snapshot: fixtureSession(0, [run]),
    reviewed_plan: run.plan!,
    command: { action: 'execute', run_id: run.id, ...planIdentity(run.plan!) },
  };
  function afterFirstStep() {
    const decoder = new AgentEventDecoder(start);
    decoder.decode(
      frame(fixtureEvent(start, 1, { ...run, status: 'approved', operation_id: 'operation-1' })),
    );
    decoder.decode(
      frame(fixtureEvent(start, 2, { ...run, status: 'executing', operation_id: 'operation-1' })),
    );
    const events = fixtureStepEvents(start, 3, run, {
      step_id: 'step-1',
      status: 'completed',
      content: 'Done',
      time: run.updated_at,
    });
    events.forEach((event) => decoder.decode(frame(event)));
    return { decoder, events };
  }
  it.each([
    'completed step intent',
    'out-of-order step',
    'changed replay',
    'completed intent',
    'running result',
    'skipped count',
    'counter without result',
  ])('rejects %s at a new contiguous journal sequence', (kind) => {
    const { decoder, events } = afterFirstStep();
    let event = fixtureStepEvents(start, 5, run, {
      step_id: 'step-2',
      status: 'completed',
      content: 'Second',
      time: run.updated_at,
    })[0];
    if (kind === 'completed step intent') event.result!.step_id = 'step-1';
    if (kind === 'out-of-order step') event.result!.step_id = 'step-3';
    if (kind === 'changed replay') {
      event = structuredClone(events[1]);
      event.sequence = 5;
      event.session.sequence = 5;
      event.run!.version_sequence = 5;
      event.session.current_run!.version_sequence = 5;
      event.result!.content = 'Changed';
    }
    if (kind === 'completed intent') event.result!.status = 'completed';
    if (kind === 'running result') event.type = 'tool.result';
    if (kind === 'skipped count') {
      event.run!.result_count = 3;
      event.session.current_run!.result_count = 3;
    }
    if (kind === 'counter without result') {
      event.type = 'run.executing';
      event.result = null;
    }
    expect(() => decoder.decode(frame(event))).toThrow('Invalid Agent');
  });
  it('accepts ordered intent/result and terminal failure with only one frontier record', () => {
    const { decoder } = afterFirstStep();
    const [intent, failure] = fixtureStepEvents(start, 5, run, {
      step_id: 'step-2',
      status: 'failed',
      content: '',
      time: run.updated_at,
      error: { code: 'read_failed', message: 'Unavailable' },
    });
    decoder.decode(frame(intent));
    failure.type = 'run.failed';
    failure.run!.status = 'failed';
    failure.run!.operation_id = null;
    failure.session.current_run = null;
    expect(decoder.decode(frame(failure))?.run?.completed_steps).toBe(1);
    expect(decoder.terminal).toBe(true);
    expect((decoder as unknown as { frontier: unknown }).frontier).toEqual({
      step_id: 'step-2',
      status: 'failed',
    });
  });
  it.each(['completed prefix running', 'completion beyond prefix', 'frontier beyond count'])(
    'rejects detail and result windows with %s',
    (kind) => {
      const body = {
        ...planned(),
        status: 'executing' as const,
        operation_id: 'operation-1',
        completed_steps: 1,
        result_count: 2,
        results: [
          {
            step_id:
              kind === 'completed prefix running'
                ? 'step-1'
                : kind === 'completion beyond prefix'
                  ? 'step-2'
                  : 'step-3',
            status:
              kind === 'completion beyond prefix' ? ('completed' as const) : ('running' as const),
            content: '',
            time: run.updated_at,
          },
        ],
      };
      expect(() => parseAgentRun(body)).toThrow('Invalid Agent');
      const page = {
        projection_version: 2,
        session_id: 'agent-1',
        run_id: run.id,
        at_sequence: 5,
        total: 2,
        completed_steps: 1,
        results: body.results,
        next_before: null,
      };
      expect(() => parseResultsPage(page, body.plan)).toThrow('Invalid Agent');
    },
  );
  it('binds foreign session records to the active run independently of the latest run', () => {
    const older = { ...run, id: 'older' };
    const snapshot = fixtureSession(0, [older, { ...fixtureRun('rejected'), id: 'newer' }]);
    const request = {
      ...start,
      snapshot,
      command: { ...start.command, run_id: older.id },
    } as AgentStreamRequest;
    const decoder = new AgentEventDecoder(request);
    const admission = fixtureEvent(request, 1, {
      ...older,
      status: 'approved',
      operation_id: 'operation-1',
    });
    admission.session = fixtureSession(1, [
      { ...older, status: 'approved', operation_id: 'operation-1' },
      { ...fixtureRun('rejected'), id: 'newer' },
    ]);
    decoder.decode(frame(admission));
    const metadata = {
      ...admission,
      sequence: 2,
      request_id: 'foreign-stop',
      run_id: null,
      operation_id: null,
      run: null,
      type: 'session.updated',
      session: { ...admission.session, sequence: 2 },
    };
    expect(decoder.decode(frame(metadata))?.session.latest_run_id).toBe('newer');
    expect(decoder.terminal).toBe(false);
  });
  it('requires the invalidation field and accepts only the exact recovery envelope mismatch', () => {
    const normal = fixtureEvent(start, 1, fixtureRun('approved'));
    const missing = { ...normal } as Record<string, unknown>;
    delete missing['invalidates_runs'];
    expect(() => parseAgentEvent(missing)).toThrow('Invalid Agent');
    const recovery = recovered(start, 1);
    expect(parseAgentEvent(recovery)).toBe(recovery);
    const decoder = new AgentEventDecoder(start);
    expect(decoder.decode(frame(recovery))?.run_id).toBeNull();
    expect(decoder.terminal).toBe(true);
  });
  it.each(['request', 'type', 'flag', 'run ID', 'operation', 'error', 'control', 'result'])(
    'rejects recovery spoofing through %s',
    (kind) => {
      const event = recovered(start, 1);
      if (kind === 'request') event.request_id = 'foreign';
      if (kind === 'type') event.type = 'run.cancelled';
      if (kind === 'flag') event.invalidates_runs = false;
      if (kind === 'run ID') event.run_id = 'spoofed';
      if (kind === 'operation') event.operation_id = 'spoofed';
      if (kind === 'error') event.error = { code: 'different', message: 'Different' };
      if (kind === 'control') event.run!.status = 'cancelled';
      if (kind === 'result')
        event.result = {
          step_id: 'step-1',
          status: 'completed',
          content: '',
          time: run.updated_at,
        };
      expect(() => new AgentEventDecoder(start).decode(frame(event))).toThrow('Invalid Agent');
    },
  );
});

describe('review regressions: bounded Agent workspace reconciliation', () => {
  let api: FakeAgentApi;
  let state: AgentState;
  beforeEach(() => {
    api = new FakeAgentApi();
    TestBed.configureTestingModule({
      providers: [
        { provide: AgentApi, useValue: api },
        { provide: CHAT_STORAGE, useValue: memoryAgentStorage() },
      ],
    });
    state = TestBed.inject(AgentState);
  });
  afterEach(() => TestBed.resetTestingModule());
  async function load(runs: AgentRun[]) {
    const header = fixtureSession(1, runs);
    api.get.mockResolvedValue(header);
    api.list.mockResolvedValue(fixtureList([header]));
    await state.load();
    return header;
  }
  it.each(['detail', 'results'] as const)(
    'clears owned loading keys when closing a pending %s and permits reopening',
    async (kind) => {
      const history = completed('history', 25, 1);
      const other = completed('other', 25, 1);
      await load([history, other, fixtureRun()]);
      await state.openRun(history.id);
      const pendingDetail = deferred<AgentRun>();
      const pendingResults = deferred<ResultsPage>();
      let old: Promise<unknown>;
      if (kind === 'detail') {
        api.detail.mockReturnValueOnce(pendingDetail.promise);
        old = state.openRun(other.id);
      } else {
        api.results.mockReturnValueOnce(pendingResults.promise);
        old = state.loadResults(state.openedView()!.body);
      }
      const oldSignal =
        kind === 'detail' ? api.detail.mock.calls.at(-1)![2] : api.results.mock.calls.at(-1)![2];
      state.closeRun();
      expect(oldSignal.aborted).toBe(true);
      expect(state.pageLoading().has('opened')).toBe(false);
      expect(state.pageLoading().has('results:opened')).toBe(false);
      const reopening = deferred<AgentRun>();
      api.detail.mockReturnValueOnce(reopening.promise);
      const fresh = state.openRun(other.id);
      expect(state.pageLoading().has('opened')).toBe(true);
      pendingDetail.resolve(other);
      pendingResults.resolve({
        projection_version: 2,
        session_id: 'agent-1',
        run_id: history.id,
        at_sequence: 1,
        total: 25,
        completed_steps: 25,
        results: [],
        next_before: null,
      });
      await old;
      expect(state.openedView()).toBeNull();
      expect(state.pageLoading().has('opened')).toBe(true);
      reopening.resolve(other);
      await fresh;
      expect(state.openedView()?.body.id).toBe(other.id);
      expect(state.pageLoading().size).toBe(0);
      if (kind === 'results') {
        api.results.mockResolvedValueOnce({
          projection_version: 2,
          session_id: 'agent-1',
          run_id: other.id,
          at_sequence: 1,
          total: 25,
          completed_steps: 25,
          results: other.plan!.steps.slice(0, 5).map((step) => ({
            step_id: step.id,
            status: 'completed',
            content: step.id,
            time: other.updated_at,
          })),
          next_before: null,
        });
        await state.loadResults(state.openedView()!.body);
        expect(api.results).toHaveBeenCalledTimes(2);
        expect(state.openedView()?.window.kind).toBe('older');
        expect(state.pageLoading().size).toBe(0);
      }
    },
  );
  it('reconciles A completion, starts B, and opens A with GET only and no manual reload', async () => {
    await load([fixtureRun()]);
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    api.event(0, 3, fixtureRun('executing'));
    const done = completed();
    fixtureStepEvents(api.calls[0].request, 4, fixtureRun(), done.results[0]).forEach(
      api.calls[0].emit,
    );
    api.event(0, 6, done);
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.summaries()[0].status).toBe('completed');
    const b = { ...fixtureRun('planning'), id: 'run-B' };
    const header = fixtureSession(7, [done, b]);
    api.runs.mockResolvedValueOnce(runsPage(header, [normalizeRun(b, 7), done]));
    api.detail.mockResolvedValueOnce(done);
    state.updateDraft('Read the fixture');
    state.request();
    const event = fixtureEvent(api.calls[1].request, 7, b);
    event.session = header;
    api.calls[1].emit(event);
    await state.openRun(done.id);
    expect(state.openedView()?.body.status).toBe('completed');
    expect(state.pageErrors().size).toBe(0);
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.calls).toHaveLength(2);
    expect(api.detail).toHaveBeenLastCalledWith('agent-1', done.id, expect.any(AbortSignal));
  });
  it('accepts a causally newer historical body when its loaded summary hint is stale', async () => {
    const a = fixtureRun();
    const b = { ...fixtureRun('planning'), id: 'run-B' };
    await load([a, b]);
    const header = fixtureSession(7, [completed(), b]);
    state.sessions.set([header]);
    api.detail.mockResolvedValueOnce(completed());
    await state.openRun(a.id);
    expect(state.openedView()?.body.status).toBe('completed');
    expect(state.summaries().find((entry) => entry.id === a.id)?.version_sequence).toBe(6);
    api.detail.mockResolvedValueOnce(normalizeRun(fixtureRun(), 1));
    await state.openRun(a.id);
    expect(state.openedView()?.body.status).toBe('completed');
  });
  it('uses a fixed ten-summary window when explicit older pages and new runs replace it', async () => {
    const all = Array.from({ length: 25 }, (_, index) => completed(`history-${index}`, 1, 1));
    const header = fixtureSession(1, [...all, fixtureRun()]);
    api.list.mockResolvedValueOnce(fixtureList([header]));
    api.get.mockResolvedValue(header);
    api.runs.mockResolvedValueOnce(runsPage(header, all.slice(15).reverse(), 15));
    await state.load();
    api.runs.mockResolvedValueOnce(runsPage(header, all.slice(5, 15).reverse(), 5));
    await state.loadRuns(true);
    expect(state.summaries()).toHaveLength(10);
    expect(state.summaries()[0].id).toBe('history-14');
    expect(api.runs).toHaveBeenCalledTimes(2);
    expect(api.detail).toHaveBeenCalledOnce();
  });
  it('rejects a higher-sequence historical body that rolls back its known plan revision', async () => {
    const reviewed = fixtureRun();
    reviewed.plan!.revision = 2;
    await load([reviewed]);
    state.sessions.set([
      fixtureSession(7, [completed(), { ...fixtureRun('planning'), id: 'run-B' }]),
    ]);
    api.detail.mockResolvedValueOnce(completed());
    await state.openRun(reviewed.id);
    expect(state.openedView()).toBeNull();
    expect(state.pageErrors().get('opened')).toContain('Invalid Agent');
    expect(api.calls).toHaveLength(0);
  });
  it('locks, loads, and stops an older active run while a newer terminal run remains latest', async () => {
    const a = { ...fixtureRun('executing'), id: 'older' };
    const b = { ...fixtureRun('rejected'), id: 'newer' };
    const header = await load([a, b]);
    expect(parseAgentSession(header).latest_run_id).toBe('newer');
    expect(state.currentView()?.body.id).toBe('older');
    expect(state.locked()).toBe(true);
    api.cancel.mockResolvedValueOnce(fixtureSession(2, [{ ...a, status: 'cancelling' }, b]));
    await state.stop(state.activeControl()!);
    expect(api.cancel).toHaveBeenCalledWith(
      'agent-1',
      'older',
      expect.any(String),
      'operation-1',
      expect.any(AbortSignal),
    );
    expect(state.activeControl()?.status).toBe('cancelling');
    expect(state.locked()).toBe(true);
  });
  it('refreshes a bounded recent summary page when a new run supersedes an explicitly inspected older page', async () => {
    const history = Array.from({ length: 25 }, (_, index) => completed(`history-${index}`, 1, 1));
    const header = fixtureSession(1, history);
    api.list.mockResolvedValueOnce(fixtureList([header]));
    api.get.mockResolvedValue(header);
    api.runs.mockResolvedValueOnce(runsPage(header, history.slice(15).reverse(), 15));
    await state.load();
    api.runs.mockResolvedValueOnce(runsPage(header, history.slice(5, 15).reverse(), 5));
    await state.loadRuns(true);
    const b = { ...fixtureRun('planning'), id: 'new-run' };
    const newer = fixtureSession(2, [...history, b]);
    api.runs.mockResolvedValueOnce(
      runsPage(newer, [normalizeRun(b, 2), ...history.slice(-9).reverse()], 16),
    );
    state.updateDraft('Read the fixture');
    state.request();
    const event = fixtureEvent(api.calls[0].request, 2, b);
    event.session = newer;
    api.calls[0].emit(event);
    await vi.waitFor(() => expect(state.summaries()[0].id).toBe(b.id));
    expect(state.summaries()).toHaveLength(10);
    expect(state.nextRunsBefore()).toBe(16);
    expect(api.runs).toHaveBeenCalledTimes(3);
    expect(api.runs.mock.calls.at(-1)?.[2]).toBeUndefined();
    expect(api.detail).toHaveBeenCalledOnce();
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.list).toHaveBeenCalledOnce();
    expect(api.calls).toHaveLength(1);
  });
  it('refreshes only the current/opened bodies and loaded summary window on multi-run invalidation', async () => {
    const other = {
      ...fixtureRun('executing'),
      id: 'other',
      result_count: 1,
      results: [
        {
          step_id: 'step-1',
          status: 'running' as const,
          content: '',
          time: fixtureRun().updated_at,
        },
      ],
    };
    const initial = fixtureSession(1, [other, fixtureRun()]);
    initial.current_run = controlOf(normalizeRun(fixtureRun(), 1));
    api.get.mockResolvedValue(initial);
    api.list.mockResolvedValue(fixtureList([initial]));
    await state.load();
    await state.openRun(other.id);
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    api.event(0, 3, fixtureRun('executing'));
    const error = {
      code: 'outcome_unknown',
      message: 'Interrupted during restart; work was not replayed.',
    };
    const a = { ...fixtureRun('interrupted'), error };
    const x = {
      ...other,
      status: 'interrupted' as const,
      operation_id: null,
      error,
      results: other.results.map((result) => ({ ...result, status: 'failed' as const, error })),
    };
    const header = fixtureSession(4, [x, a]);
    const pending = deferred<AgentSession>();
    api.get.mockReturnValueOnce(pending.promise);
    const event = recovered(api.calls[0].request, 4, x);
    event.session = header;
    event.result = x.results[0];
    api.calls[0].emit(event);
    api.calls[0].resolve();
    expect(state.invalidating().has(header.id)).toBe(true);
    expect(state.locked()).toBe(true);
    expect(state.canRequest()).toBe(false);
    state.approve(state.currentView()?.body ?? a);
    expect(api.calls).toHaveLength(1);
    api.detail.mockImplementation(async (_sid, rid) => normalizeRun(rid === other.id ? x : a, 4));
    api.runs.mockResolvedValueOnce(runsPage(header, [normalizeRun(a, 4), normalizeRun(x, 4)]));
    pending.resolve(header);
    await vi.waitFor(() => expect(state.invalidating().size).toBe(0));
    expect(state.currentView()?.body.status).toBe('interrupted');
    expect(state.openedView()?.body.status).toBe('interrupted');
    expect(state.currentView()?.body.id).toBe(a.id);
    expect(state.openedView()?.body.results[0].status).toBe('failed');
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(api.detail).toHaveBeenCalledTimes(4);
    expect(api.runs).toHaveBeenCalledTimes(2);
    expect(api.calls).toHaveLength(1);
  });
  it('does not unlock invalidated views when the recovery header GET is older than the invalidation sequence', async () => {
    await load([fixtureRun()]);
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    const pending = deferred<AgentSession>();
    api.get.mockReturnValueOnce(pending.promise);
    const event = recovered(api.calls[0].request, 3);
    api.calls[0].emit(event);
    api.calls[0].resolve();
    api.detail.mockResolvedValueOnce(normalizeRun(fixtureRun('interrupted'), 3));
    pending.resolve(fixtureSession(2));
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.invalidating().has('agent-1')).toBe(true);
    expect(state.locked()).toBe(true);
    expect(api.calls).toHaveLength(1);
  });
});

describe('nonlatest current control over real public GET transport', () => {
  afterEach(() => TestBed.resetTestingModule());
  it('accepts list/header reads with an older authoritative active run', async () => {
    const header = fixtureSession(3, [
      { ...fixtureRun('executing'), id: 'older' },
      { ...fixtureRun('rejected'), id: 'newer' },
    ]);
    const fetchDouble = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(fixtureList([header])))
      .mockResolvedValueOnce(Response.json(header));
    TestBed.configureTestingModule({ providers: [{ provide: FETCH, useValue: fetchDouble }] });
    const api = TestBed.inject(AgentApi);
    const signal = new AbortController().signal;
    expect((await api.list(signal)).sessions[0].current_run?.id).toBe('older');
    expect((await api.get(header.id, signal)).latest_run_id).toBe('newer');
    expect(fetchDouble.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
  });
});
