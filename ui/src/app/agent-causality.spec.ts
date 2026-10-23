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
  parseEventsPage,
  planIdentity,
  type AgentEvent,
  type AgentRun,
  type AgentSession,
  type ResultsPage,
  type RunsPage,
} from './agent.models';
import { CHAT_STORAGE } from './chat-state';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function frame(event: AgentEvent) {
  return { event: 'agent', id: String(event.sequence), data: JSON.stringify(event) };
}
function summaryPage(header: AgentSession, runs: AgentRun[], next: number | null = null): RunsPage {
  return {
    projection_version: 2,
    session_id: header.id,
    at_sequence: header.sequence,
    total: header.run_count,
    runs: runs.map((run) => ({ ...controlOf(run), request_preview: run.request })),
    next_before: next,
  };
}
function completed(sequence = 8, id = 'run-1'): AgentRun {
  const run = fixtureRun('completed');
  return {
    ...run,
    id,
    version_sequence: sequence,
    answer: 'Done',
    result_count: 1,
    completed_steps: 1,
    results: [
      { step_id: 'step-1', status: 'completed', content: 'Recorded result', time: run.updated_at },
    ],
  };
}
const OUTCOME = {
  code: 'outcome_unknown',
  message: 'Interrupted during restart; work was not replayed.',
};
function interrupted(sequence = 4, id = 'run-1'): AgentRun {
  return { ...fixtureRun('interrupted'), id, version_sequence: sequence, error: OUTCOME };
}
function recovery(
  request: AgentStreamRequest,
  header: AgentSession,
  run: AgentRun | null,
): AgentEvent {
  return {
    ...fixtureEvent(request, header.sequence, run ?? fixtureRun('interrupted')),
    session: header,
    invalidates_runs: true,
    request_id: 'recovery',
    type: 'run.interrupted',
    run_id: null,
    operation_id: null,
    run: run ? controlOf(run) : null,
    result: null,
    error: run?.error ?? null,
    plan: null,
    request: null,
    answer: null,
  };
}

describe('transactional Agent admission and archive-only recovery', () => {
  const request: AgentStreamRequest = {
    session_id: 'agent-1',
    request_id: 'execute',
    sequence: 0,
    snapshot: fixtureSession(0, [fixtureRun()]),
    reviewed_plan: fixturePlan(),
    command: { action: 'execute', run_id: 'run-1', ...planIdentity(fixturePlan()) },
  };
  it('rejects zero-prefix completion without committing a terminal decoder state', () => {
    const decoder = new AgentEventDecoder(request);
    decoder.decode(frame(fixtureEvent(request, 1, fixtureRun('approved'))));
    const impossible = { ...fixtureRun('completed'), answer: 'Unperformed work' };
    expect(() => decoder.decode(frame(fixtureEvent(request, 2, impossible)))).toThrow(
      'Invalid Agent',
    );
    expect(decoder.terminal).toBe(false);
    expect(decoder.decode(frame(fixtureEvent(request, 2, fixtureRun('executing'))))?.sequence).toBe(
      2,
    );
  });
  it.each([false, true])(
    'accepts the exact archive-only recovery page with retained history=%s',
    (history) => {
      const header = { ...fixtureSession(1, history ? [completed(1)] : []), archive_pending: true };
      const archived = { ...header, sequence: 2, archived: true, archive_pending: false };
      const event = recovery({ ...request, sequence: 1, snapshot: header }, archived, null);
      expect(parseAgentEvent(event)).toBe(event);
      const page = {
        projection_version: 2,
        events: [event],
        next_after: 2,
        through_sequence: 2,
        has_more: false,
      };
      expect(parseEventsPage(page)).toEqual(page);
      const decoder = new AgentEventDecoder({ ...request, sequence: 1, snapshot: header });
      expect(decoder.decode(frame(event))).toEqual(event);
      expect(decoder.terminal).toBe(true);
      expect(event.run).toBeNull();
    },
  );
  it.each([
    'request',
    'type',
    'flag',
    'run_id',
    'operation_id',
    'result',
    'error',
    'plan',
    'answer',
    'unarchived',
  ])('keeps archive-only recovery correlation and payload strict for %s', (change) => {
    const event = recovery(request, { ...fixtureSession(1), archived: true }, null);
    if (change === 'request') event.request_id = 'foreign';
    if (change === 'type') event.type = 'session.archived';
    if (change === 'flag') event.invalidates_runs = false;
    if (change === 'run_id') event.run_id = 'spoofed';
    if (change === 'operation_id') event.operation_id = 'spoofed';
    if (change === 'result')
      event.result = {
        step_id: 'step-1',
        status: 'failed',
        content: '',
        time: event.time,
        error: OUTCOME,
      };
    if (change === 'error') event.error = OUTCOME;
    if (change === 'plan') event.plan = fixturePlan();
    if (change === 'answer') event.answer = 'Invented';
    if (change === 'unarchived') event.session.archived = false;
    // A normal session.archived record remains valid; it must not be accepted as recovery work.
    if (change === 'type') event.run_id = 'spoofed';
    expect(() => parseAgentEvent(event)).toThrow('Invalid Agent');
  });
});

describe('causal Agent refresh ownership and staged publication', () => {
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
  async function load(runs: AgentRun[] = [fixtureRun()]) {
    const header = fixtureSession(1, runs);
    api.list.mockResolvedValue(fixtureList([header]));
    api.get.mockResolvedValue(header);
    await state.load();
    return header;
  }
  it.each(['impossible completion', 'body-only rejected identity failure'])(
    'preserves every last valid projection after %s and a failed recovery GET',
    async (kind) => {
      await load(kind === 'impossible completion' ? [fixtureRun()] : []);
      if (kind === 'impossible completion') {
        state.approve(state.activeRun()!);
        api.event(0, 2, fixtureRun('approved'));
      } else {
        state.updateDraft('Read the fixture');
        state.request();
        api.event(0, 2, fixtureRun('planning'));
      }
      state.updateDraft('Next request');
      const header = state.activeSession();
      const current = state.currentView();
      const summaries = state.summaries();
      const bad =
        kind === 'impossible completion'
          ? { ...fixtureRun('completed'), answer: 'Unperformed work' }
          : { ...fixtureRun('rejected'), plan: null };
      api.get.mockRejectedValueOnce(new Error('Recovery unavailable'));
      expect(() => api.event(0, 3, bad)).toThrow('Invalid Agent');
      expect(state.activeSession()).toBe(header);
      expect(state.currentView()).toBe(current);
      expect(state.summaries()).toBe(summaries);
      api.calls[0].reject(new Error('Invalid Agent response'));
      await vi.waitFor(() => expect(state.busy().size).toBe(0));
      expect(state.activeSession()).toBe(header);
      expect(state.currentView()).toBe(current);
      expect(state.locked()).toBe(true);
      expect(state.canRequest()).toBe(false);
      expect(state.sessionError()).toContain('Recovery unavailable');
      expect(api.calls).toHaveLength(1);
    },
  );

  async function invalidateWithPendingBody(key: 'current' | 'opened', rejectCurrent = false) {
    const other = { ...fixtureRun('executing'), id: 'other' };
    const initial = fixtureSession(1, [other, fixtureRun()]);
    initial.current_run = controlOf(normalizeRun(fixtureRun(), 1));
    api.list.mockResolvedValue(fixtureList([initial]));
    api.get.mockResolvedValue(initial);
    await state.load();
    await state.openRun(other.id);
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    api.event(0, 3, fixtureRun('executing'));
    const a = interrupted();
    const x = interrupted(4, other.id);
    const header = fixtureSession(4, [x, a]);
    const pending = deferred<AgentRun>();
    const rid = key === 'current' ? a.id : x.id;
    api.get.mockResolvedValue(header);
    api.runs.mockResolvedValue(summaryPage(header, [a, x]));
    api.detail.mockImplementation(async (_sid, id) => {
      if (id === rid) return pending.promise;
      if (id === a.id && rejectCurrent) throw new Error('Required current detail unavailable');
      return id === a.id ? a : x;
    });
    api.calls[0].emit(recovery(api.calls[0].request, header, a));
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.pageLoading().has(key)).toBe(true));
    return { header, a, x, pending, rid };
  }
  it.each(['success', 'failure'] as const)(
    'reconciles the remaining view set after closing invalidated history before its late %s',
    async (outcome) => {
      const { header, x, pending, rid } = await invalidateWithPendingBody('opened');
      await vi.waitFor(() => {
        expect(state.currentView()?.window.at_sequence).toBe(header.sequence);
        expect(state.pageLoading().has('runs')).toBe(false);
        expect(state.pageLoading().has('current')).toBe(false);
      });
      state.updateDraft('Next request');
      const signal = api.detail.mock.calls.filter(([, id]) => id === rid).at(-1)![2];
      state.closeRun();
      state.closeRun(); // An already-closed pane must not schedule another reconciliation.
      expect(signal.aborted).toBe(true);
      await vi.waitFor(() => expect(state.invalidating().size).toBe(0));
      expect(state.openedView()).toBeNull();
      expect(state.activeSession()?.sequence).toBe(header.sequence);
      expect(state.currentView()?.window.at_sequence).toBe(header.sequence);
      expect(state.pageLoading().size).toBe(0);
      expect(state.pageErrors().size).toBe(0);
      expect(state.canRequest()).toBe(true);
      expect(api.get).toHaveBeenCalledTimes(3);
      expect(api.runs).toHaveBeenCalledTimes(3);
      expect(api.detail).toHaveBeenCalledTimes(5);
      expect(api.detail.mock.calls.filter(([, id]) => id === rid)).toHaveLength(2);
      if (outcome === 'failure') pending.reject(new Error('Closed history unavailable'));
      else pending.resolve(x);
      await Promise.resolve();
      await Promise.resolve();
      expect(state.openedView()).toBeNull();
      expect(state.invalidating().size).toBe(0);
      expect(state.pageErrors().size).toBe(0);
      expect(api.get).toHaveBeenCalledTimes(3);
      expect(api.calls).toHaveLength(1);
    },
  );
  it('does not count a failed current detail as admitted when optional history is closed', async () => {
    const { pending, x } = await invalidateWithPendingBody('opened', true);
    await vi.waitFor(() =>
      expect(state.pageErrors().get('current')).toBe('Required current detail unavailable'),
    );
    state.updateDraft('Next request');
    state.closeRun();
    await vi.waitFor(() => {
      expect(api.detail.mock.calls.filter(([, id]) => id === 'run-1')).toHaveLength(3);
      expect(state.pageLoading().size).toBe(0);
      expect(state.pageErrors().get('current')).toBe('Required current detail unavailable');
    });
    pending.resolve(x);
    await Promise.resolve();
    await Promise.resolve();
    expect(state.openedView()).toBeNull();
    expect(state.invalidating().has('agent-1')).toBe(true);
    expect(state.locked()).toBe(true);
    expect(state.canRequest()).toBe(false);
    expect(api.get).toHaveBeenCalledTimes(3);
    expect(api.detail).toHaveBeenCalledTimes(5);
    expect(api.calls).toHaveLength(1);
  });
  it.each(['success', 'failure'] as const)(
    'does not let a closed history callback or superseded close refresh unlock a newer invalidation: %s',
    async (outcome) => {
      const { header, pending, x } = await invalidateWithPendingBody('opened');
      await vi.waitFor(() => expect(state.currentView()?.window.at_sequence).toBe(header.sequence));
      const closeHeader = deferred<AgentSession>();
      api.get.mockReturnValueOnce(closeHeader.promise);
      state.closeRun();
      expect(api.get).toHaveBeenCalledTimes(3);
      const a5 = interrupted(5);
      const x5 = interrupted(5, 'other');
      const newer = fixtureSession(5, [x5, a5]);
      const fresh = deferred<AgentRun>();
      api.get.mockResolvedValue(newer);
      api.runs.mockResolvedValue(summaryPage(newer, [a5, x5]));
      api.detail.mockReturnValueOnce(fresh.promise);
      // A subsequent validated journal invalidation may supersede the close-triggered refresh.
      const event = parseAgentEvent(recovery(api.calls[0].request, newer, a5));
      (state as unknown as { applyEvent(event: AgentEvent): void }).applyEvent(event);
      await vi.waitFor(() => expect(state.pageLoading().has('current')).toBe(true));
      if (outcome === 'failure') pending.reject(new Error('Obsolete closed history failure'));
      else pending.resolve(x);
      closeHeader.resolve(header);
      await Promise.resolve();
      await Promise.resolve();
      expect(state.activeSession()?.sequence).toBe(5);
      expect(state.openedView()).toBeNull();
      expect(state.invalidating().has('agent-1')).toBe(true);
      expect(state.canRequest()).toBe(false);
      expect(state.pageErrors().size).toBe(0);
      fresh.resolve(a5);
      await vi.waitFor(() => expect(state.invalidating().size).toBe(0));
      expect(state.currentView()?.window.at_sequence).toBe(5);
      expect(state.pageLoading().size).toBe(0);
      expect(api.get).toHaveBeenCalledTimes(4);
      expect(api.detail).toHaveBeenCalledTimes(5);
      expect(api.calls).toHaveLength(1);
    },
  );
  it.each([
    ['current', 'success'],
    ['current', 'failure'],
    ['opened', 'success'],
    ['opened', 'failure'],
  ] as const)(
    'awaits a shared pending %s detail across overlapping refreshes: %s',
    async (key, outcome) => {
      const { pending, rid, a, x } = await invalidateWithPendingBody(key);
      const overlap = state.refresh();
      await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(3));
      await Promise.resolve();
      await Promise.resolve();
      expect(state.invalidating().has('agent-1')).toBe(true);
      expect(state.pageLoading().has(key)).toBe(true);
      expect(api.detail.mock.calls.filter(([, id]) => id === rid)).toHaveLength(2);
      if (outcome === 'failure') pending.reject(new Error('Required view unavailable'));
      else pending.resolve(rid === a.id ? a : x);
      expect(await overlap).toBe(outcome === 'success');
      expect(state.invalidating().has('agent-1')).toBe(outcome === 'failure');
      if (outcome === 'failure') {
        expect(state.pageErrors().get(key)).toBe('Required view unavailable');
        expect(state.locked()).toBe(true);
      } else {
        expect(state.currentView()?.window.at_sequence).toBe(4);
        expect(state.openedView()?.window.at_sequence).toBe(4);
      }
      expect(api.calls).toHaveLength(1);
    },
  );
  it.each([
    ['current', 'success'],
    ['current', 'failure'],
    ['opened', 'success'],
    ['opened', 'failure'],
  ] as const)(
    'fences a superseded %s detail when a newer header arrives before late %s',
    async (key, outcome) => {
      const { header, pending, rid, a, x } = await invalidateWithPendingBody(key);
      const oldSignal = api.detail.mock.calls.filter(([, id]) => id === rid).at(-1)![2];
      const fresh = deferred<AgentRun>();
      const newer = { ...header, sequence: 5, title: 'Newer metadata' };
      api.get.mockResolvedValue(newer);
      api.runs.mockResolvedValue(summaryPage(newer, [a, x]));
      api.detail.mockImplementation(async (_sid, id) =>
        id === rid ? fresh.promise : id === a.id ? a : x,
      );
      const refreshing = state.refresh();
      await vi.waitFor(() => expect(oldSignal.aborted).toBe(true));
      if (outcome === 'failure') pending.reject(new Error('Obsolete failure'));
      else pending.resolve(rid === a.id ? a : x);
      await Promise.resolve();
      await Promise.resolve();
      expect(state.invalidating().has('agent-1')).toBe(true);
      expect(state.pageLoading().has(key)).toBe(true);
      expect(state.pageErrors().has(key)).toBe(false);
      fresh.resolve(rid === a.id ? a : x);
      await refreshing;
      expect(state.invalidating().size).toBe(0);
      expect(state.activeSession()?.sequence).toBe(5);
      expect(
        (key === 'current' ? state.currentView() : state.openedView())?.window.at_sequence,
      ).toBe(5);
      expect(api.calls).toHaveLength(1);
    },
  );
  it.each(['success', 'failure'] as const)(
    'does not treat an invalidated pending results read as fresh detail admission: %s',
    async (outcome) => {
      const run = fixtureRun();
      run.plan!.steps = Array.from({ length: 25 }, (_, index) => ({
        ...fixturePlan().steps[0],
        id: `step-${index}`,
      }));
      await load([run]);
      state.approve(state.activeRun()!);
      api.event(0, 2, { ...run, status: 'approved', operation_id: 'operation-1' });
      api.event(0, 3, { ...run, status: 'executing', operation_id: 'operation-1' });
      for (let index = 0; index < 21; index++) {
        fixtureStepEvents(api.calls[0].request, 4 + index * 2, run, {
          step_id: `step-${index}`,
          status: 'completed',
          content: 'Done',
          time: run.updated_at,
        }).forEach(api.calls[0].emit);
      }
      const body = state.currentView()!.body;
      const results = deferred<ResultsPage>();
      api.results.mockReturnValueOnce(results.promise);
      const reading = state.loadResults(body);
      const resultSignal = api.results.mock.calls[0][2];
      const recovered = {
        ...body,
        status: 'interrupted' as const,
        operation_id: null,
        version_sequence: 46,
        error: OUTCOME,
      };
      const header = fixtureSession(46, [recovered]);
      const detail = deferred<AgentRun>();
      api.get.mockResolvedValue(header);
      api.runs.mockResolvedValue(summaryPage(header, [recovered]));
      api.detail.mockReturnValueOnce(detail.promise);
      api.calls[0].emit(recovery(api.calls[0].request, header, recovered));
      api.calls[0].resolve();
      await vi.waitFor(() => expect(state.pageLoading().has('current')).toBe(true));
      const overlap = state.refresh();
      expect(resultSignal.aborted).toBe(true);
      if (outcome === 'failure') results.reject(new Error('Old result page failed'));
      else
        results.resolve({
          projection_version: 2,
          session_id: 'agent-1',
          run_id: run.id,
          at_sequence: 45,
          total: 21,
          completed_steps: 21,
          results: [],
          next_before: null,
        });
      expect(await reading).toBe(false);
      expect(state.invalidating().has('agent-1')).toBe(true);
      expect(state.pageErrors().has('results:current')).toBe(false);
      detail.resolve(recovered);
      await overlap;
      expect(state.invalidating().size).toBe(0);
      expect(state.currentView()?.body.status).toBe('interrupted');
      expect(state.currentView()?.window.at_sequence).toBe(46);
    },
  );

  it('coalesces one fresh recent-summary read after a late admission GET releases ownership', async () => {
    await load([]);
    const admission = deferred<RunsPage>();
    api.runs.mockReturnValueOnce(admission.promise);
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 2, fixtureRun('planning'));
    api.event(0, 3, fixtureRun());
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(api.runs).toHaveBeenCalledTimes(2);
    const review = normalizeRun(fixtureRun(), 3);
    api.runs.mockResolvedValueOnce(summaryPage(fixtureSession(3, [review]), [review]));
    admission.resolve(
      summaryPage(fixtureSession(2, [fixtureRun('planning')]), [
        normalizeRun(fixtureRun('planning'), 2),
      ]),
    );
    await vi.waitFor(() => expect(state.summaries()[0]?.version_sequence).toBe(3));
    expect(api.runs).toHaveBeenCalledTimes(3);
    state.approve(state.activeRun()!);
    api.event(1, 4, fixtureRun('approved'));
    api.event(1, 5, fixtureRun('executing'));
    const done = completed();
    fixtureStepEvents(api.calls[1].request, 6, fixtureRun(), done.results[0]).forEach(
      api.calls[1].emit,
    );
    api.event(1, 8, done);
    api.calls[1].resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    const b = { ...fixtureRun('planning'), id: 'run-B' };
    const header = fixtureSession(9, [done, b]);
    api.runs.mockResolvedValueOnce(summaryPage(header, [normalizeRun(b, 9), done]));
    api.detail.mockResolvedValueOnce(done);
    state.updateDraft('Read the fixture');
    state.request();
    const event = fixtureEvent(api.calls[2].request, 9, b);
    event.session = header;
    api.calls[2].emit(event);
    expect(await state.openRun(done.id)).toBe(true);
    expect(state.openedView()?.body.status).toBe('completed');
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.runs).toHaveBeenCalledTimes(4);
  });
  it('retains an explicit older-page intent instead of replaying a queued automatic recent read', async () => {
    const history = Array.from({ length: 25 }, (_, index) => completed(1, `history-${index}`));
    const initial = fixtureSession(1, history);
    api.list.mockResolvedValueOnce(fixtureList([initial]));
    api.get.mockResolvedValue(initial);
    api.runs.mockResolvedValueOnce(summaryPage(initial, history.slice(-10).reverse(), 15));
    await state.load();
    const admission = deferred<RunsPage>();
    api.runs.mockReturnValueOnce(admission.promise);
    state.updateDraft('Read the fixture');
    state.request();
    const a = fixtureRun('planning');
    const header = fixtureSession(2, [...history, a]);
    const event = fixtureEvent(api.calls[0].request, 2, a);
    event.session = header;
    api.calls[0].emit(event);
    api.runs.mockResolvedValueOnce(summaryPage(header, history.slice(5, 15).reverse(), 5));
    expect(await state.loadRuns(true)).toBe(true);
    const reviewed = fixtureEvent(api.calls[0].request, 3, fixtureRun());
    reviewed.session = fixtureSession(3, [...history, fixtureRun()]);
    api.calls[0].emit(reviewed);
    api.calls[0].resolve();
    admission.resolve(
      summaryPage(header, [normalizeRun(a, 2), ...history.slice(-9).reverse()], 16),
    );
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.summaries()[0].id).toBe('history-14');
    expect(state.summaries()).toHaveLength(10);
    expect(state.nextRunsBefore()).toBe(5);
    expect(api.runs).toHaveBeenCalledTimes(3);
  });
  it('does not auto-refetch repeatedly while an ongoing stream keeps invalidating the summary read', async () => {
    await load([]);
    const admission = deferred<RunsPage>();
    api.runs.mockReturnValueOnce(admission.promise);
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 2, fixtureRun('planning'));
    for (let sequence = 3; sequence <= 50; sequence++) {
      const event = fixtureEvent(api.calls[0].request, sequence, fixtureRun('planning'));
      event.type = 'planner.attempt';
      event.request = null;
      api.calls[0].emit(event);
    }
    admission.resolve(
      summaryPage(fixtureSession(2, [fixtureRun('planning')]), [
        normalizeRun(fixtureRun('planning'), 2),
      ]),
    );
    await vi.waitFor(() => expect(state.pageLoading().has('runs')).toBe(false));
    expect(api.runs).toHaveBeenCalledTimes(2);
    expect(state.busy().has('agent-1')).toBe(true);
    const review = normalizeRun(fixtureRun(), 51);
    api.runs.mockResolvedValueOnce(summaryPage(fixtureSession(51, [review]), [review]));
    api.event(0, 51, review);
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.summaries()[0]?.version_sequence).toBe(51));
    expect(api.runs).toHaveBeenCalledTimes(3);
    expect(state.summaries()).toHaveLength(1);
  });
  it.each([false, true])(
    'reconciles archive-only invalidation with bounded GETs and no invented run: history=%s',
    async (history) => {
      await load(history ? [completed(1)] : []);
      state.updateDraft('Read the fixture');
      state.request();
      const header = { ...fixtureSession(2, history ? [completed(1)] : []), archived: true };
      api.get.mockResolvedValue(header);
      api.runs.mockResolvedValue(summaryPage(header, history ? [completed(1)] : []));
      api.detail.mockResolvedValue(completed(1));
      const count = api.detail.mock.calls.length;
      api.calls[0].emit(recovery(api.calls[0].request, header, null));
      api.calls[0].resolve();
      await vi.waitFor(() => expect(state.invalidating().size).toBe(0));
      expect(state.activeSession()?.archived).toBe(true);
      expect(state.activeSession()?.run_count).toBe(history ? 1 : 0);
      expect(api.detail).toHaveBeenCalledTimes(count + (history ? 1 : 0));
      expect(api.runs).toHaveBeenCalledTimes(2);
      expect(api.get).toHaveBeenCalledTimes(2);
      expect(api.calls).toHaveLength(1);
      expect(state.draft()).toBe('Read the fixture');
    },
  );
});
