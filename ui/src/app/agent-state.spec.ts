import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentApi } from './agent-api';
import { AgentState, AGENT_UI_STORAGE_KEY } from './agent-state';
import { CHAT_STORAGE } from './chat-state';
import {
  FakeAgentApi,
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
  type AgentRun,
  type AgentSession,
  type ResultsPage,
  type RunsPage,
} from './agent.models';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function manyResults(status: 'executing' | 'completed' = 'executing'): AgentRun {
  const run = fixtureRun(status);
  run.plan!.steps = Array.from({ length: 50 }, (_, index) => ({
    ...fixturePlan().steps[0],
    id: `step-${index}`,
  }));
  run.results = Array.from({ length: 20 }, (_, index) => ({
    step_id: `step-${index + 30}`,
    status: 'completed',
    content: `Result ${index + 30}`,
    time: run.updated_at,
  }));
  run.result_count = 50;
  run.completed_steps = 50;
  run.results_before = 30;
  run.answer = status === 'completed' ? 'Complete' : null;
  return run;
}

describe('v2 AgentState paged ownership and recovery', () => {
  let api: FakeAgentApi;
  let storage: ReturnType<typeof memoryAgentStorage>;
  let state: AgentState;
  function setup() {
    TestBed.configureTestingModule({
      providers: [
        { provide: AgentApi, useValue: api },
        { provide: CHAT_STORAGE, useValue: storage },
      ],
    });
    state = TestBed.inject(AgentState);
  }
  beforeEach(() => {
    api = new FakeAgentApi();
    storage = memoryAgentStorage();
    setup();
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });
  async function flush() {
    await Promise.resolve();
    await Promise.resolve();
    TestBed.tick();
  }
  async function load(run?: AgentRun, sequence = 1) {
    const session = fixtureSession(sequence, run ? [run] : []);
    api.list.mockResolvedValueOnce(fixtureList([session]));
    api.get.mockResolvedValue(session);
    await state.load();
    return session;
  }
  it.each(['delete-first', 'create-first'] as const)(
    'does not invalidate an in-flight create when deletion completes first (%s)',
    async (order) => {
      await load();
      const deletion = deferred<void>();
      const creation = deferred<AgentSession>();
      api.delete.mockReturnValueOnce(deletion.promise);
      api.create.mockReturnValueOnce(creation.promise);
      let deleting: Promise<void>;
      let creating: Promise<AgentSession | null>;
      if (order === 'delete-first') {
        deleting = state.delete('agent-1');
        creating = state.create('model');
      } else {
        creating = state.create('model');
        deleting = state.delete('agent-1');
      }
      deletion.resolve();
      await deleting;
      expect(state.selectedId()).toBeNull();
      creation.resolve(fixtureSession(0, [], 'new'));
      await creating;
      expect(state.selectedId()).toBe('new');
    },
  );
  it('removes only the deleted session draft and its feedback drafts from persisted state', async () => {
    await load();
    state.drafts.set({
      'agent-1': 'request',
      'feedback:agent-1:run-1:digest-1': 'feedback one',
      'feedback:agent-1:run-2:digest-2': 'feedback two',
      'feedback:agent-10:run-1:digest-1': 'other feedback',
      'agent-10': 'other request',
      $new: 'new draft',
    });
    await state.delete('agent-1');
    TestBed.tick();
    const expected = {
      'feedback:agent-10:run-1:digest-1': 'other feedback',
      'agent-10': 'other request',
      $new: 'new draft',
    };
    expect(state.drafts()).toEqual(expected);
    expect(JSON.parse(storage.records.get(AGENT_UI_STORAGE_KEY)!).drafts).toEqual(expected);
  });
  it('replaces an interrupted history reload without changing newer navigation', async () => {
    await load();
    const oldPage = deferred<ReturnType<typeof fixtureList>>();
    const replacement = deferred<ReturnType<typeof fixtureList>>();
    api.list.mockReturnValueOnce(oldPage.promise).mockReturnValueOnce(replacement.promise);
    const reloading = state.load();
    const oldSignal = api.list.mock.calls.at(-1)![0];
    state.selectLegacy('legacy');
    await state.delete('agent-1');
    expect(oldSignal.aborted).toBe(true);
    expect(api.list).toHaveBeenCalledTimes(3);
    expect(state.loading()).toBe(true);
    oldPage.resolve(fixtureList([fixtureSession()], 'stale', 10));
    await reloading;
    expect(state.loading()).toBe(true);
    replacement.resolve(fixtureList([fixtureSession(0, [], 'other')], 'next', 7));
    await flush();
    expect(state.loading()).toBe(false);
    expect(state.sessions().map((session) => session.id)).toEqual(['other']);
    expect(state.total()).toBe(7);
    expect(state.nextCursor()).toBe('next');
    expect(state.legacyId()).toBe('legacy');
    expect(state.selectedId()).toBeNull();
  });
  it('does no Agent IO at Chat startup; mode drafts and UI-only persistence remain independent', async () => {
    TestBed.tick();
    expect(api.list).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
    await load();
    state.updateDraft('First draft');
    state.select('agent-2');
    await flush();
    state.updateDraft('Second draft');
    state.setMode('chat');
    TestBed.tick();
    const raw = storage.records.get(AGENT_UI_STORAGE_KEY)!;
    expect(JSON.parse(raw)).toEqual({
      version: 1,
      mode: 'chat',
      selectedId: 'agent-2',
      legacyId: null,
      drafts: { 'agent-1': 'First draft', 'agent-2': 'Second draft' },
    });
    expect(raw).not.toContain('plan');
    expect(raw).not.toContain('results');
  });
  it.each([
    '{bad-json',
    '{"version":99,"private":"keep"}',
    '{"version":1,"mode":"agent","selectedId":null,"drafts":[]}',
  ])('protects unrecognized browser records: %s', (raw) => {
    TestBed.resetTestingModule();
    storage.records.set(AGENT_UI_STORAGE_KEY, raw);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    setup();
    state.updateDraft('private draft');
    TestBed.tick();
    expect(storage.records.get(AGENT_UI_STORAGE_KEY)).toBe(raw);
    expect(warn).toHaveBeenCalledOnce();
    expect(JSON.stringify(warn.mock.calls)).not.toContain('private');
  });
  it('keeps drafts in memory on storage failure without logging exception content', () => {
    storage.setItem.mockImplementation(() => {
      throw new Error('private');
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    state.updateDraft('one');
    TestBed.tick();
    state.updateDraft('two');
    TestBed.tick();
    expect(state.draft()).toBe('two');
    expect(warn).toHaveBeenCalledOnce();
    expect(JSON.stringify(warn.mock.calls)).not.toContain('private');
  });
  it('retains a selected archived session outside page one by direct GET and pins it in the sidebar', async () => {
    TestBed.resetTestingModule();
    storage.records.set(
      AGENT_UI_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        mode: 'agent',
        selectedId: 'old-archived',
        legacyId: null,
        drafts: {},
      }),
    );
    const archived = {
      ...fixtureSession(5, [fixtureRun('rejected')], 'old-archived'),
      archived: true,
    };
    api.list.mockResolvedValue(fixtureList([fixtureSession()], 'next', 100));
    api.get.mockResolvedValue(archived);
    setup();
    await state.load();
    expect(api.get).toHaveBeenCalledWith('old-archived', expect.any(AbortSignal));
    expect(api.list).toHaveBeenCalledOnce();
    expect(state.selectedId()).toBe('old-archived');
    expect(state.sessions().map((session) => session.id)).toEqual(['agent-1', 'old-archived']);
    expect(state.locked()).toBe(true);
    expect(state.total()).toBe(100);
    expect(state.nextCursor()).toBe('next');
  });
  it('falls back only after a restored selection cannot be resolved directly', async () => {
    TestBed.resetTestingModule();
    storage.records.set(
      AGENT_UI_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        mode: 'agent',
        selectedId: 'missing',
        legacyId: null,
        drafts: {},
      }),
    );
    api.get.mockRejectedValueOnce(new Error('HTTP 404'));
    setup();
    await state.load();
    expect(api.get.mock.calls.map(([sid]) => sid)).toEqual(['missing', 'agent-1']);
    expect(state.selectedId()).toBe('agent-1');
  });
  it('loads one list page and only advances on an explicit guarded action', async () => {
    api.list.mockResolvedValueOnce(fixtureList([fixtureSession()], 'page2', 100));
    await state.load();
    expect(api.list).toHaveBeenCalledOnce();
    const pending = deferred<ReturnType<typeof fixtureList>>();
    api.list.mockReturnValueOnce(pending.promise);
    const more = state.loadMoreSessions();
    void state.loadMoreSessions();
    expect(api.list).toHaveBeenCalledTimes(2);
    pending.resolve(fixtureList([fixtureSession(0, [], 'agent-2')], 'page3', 100));
    await more;
    expect(state.sessions()).toHaveLength(2);
    expect(state.total()).toBe(100);
    expect(state.nextCursor()).toBe('page3');
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.detail).not.toHaveBeenCalled();
  });
  it('resumes an interrupted Load more at its cursor without discarding loaded pages or navigation', async () => {
    api.list.mockResolvedValueOnce(fixtureList([fixtureSession()], 'page2', 5));
    await state.load();
    const second = fixtureSession(0, [], 'agent-2');
    api.list.mockResolvedValueOnce(fixtureList([second], 'page3', 5));
    await state.loadMoreSessions();
    state.select('agent-2');
    await flush();
    const stale = deferred<ReturnType<typeof fixtureList>>();
    const resumed = deferred<ReturnType<typeof fixtureList>>();
    api.list.mockReturnValueOnce(stale.promise).mockReturnValueOnce(resumed.promise);
    const more = state.loadMoreSessions();
    const staleSignal = api.list.mock.calls.at(-1)![0];
    await state.delete('agent-1');
    expect(staleSignal.aborted).toBe(true);
    expect(api.list.mock.calls.at(-1)![1]).toBe('page3');
    expect(state.sessions().map((session) => session.id)).toEqual(['agent-2']);
    expect(state.selectedId()).toBe('agent-2');
    expect(state.loadingMore()).toBe(true);
    stale.resolve(
      fixtureList([fixtureSession(), fixtureSession(0, [], 'stale')], 'stale-cursor', 5),
    );
    await more;
    expect(state.loadingMore()).toBe(true);
    expect(state.nextCursor()).toBe('page3');
    resumed.resolve(fixtureList([fixtureSession(0, [], 'agent-3')], 'page4', 4));
    await flush();
    expect(state.loadingMore()).toBe(false);
    expect(state.sessions().map((session) => session.id)).toEqual(['agent-2', 'agent-3']);
    expect(state.selectedId()).toBe('agent-2');
    expect(state.nextCursor()).toBe('page4');
    expect(state.total()).toBe(4);
  });
  it.each([
    'planning',
    'awaiting_clarification',
    'awaiting_approval',
    'approved',
    'executing',
    'cancelling',
  ] as const)(
    'locks preferences and new requests from %s header even when no body is loaded',
    async (status) => {
      const run = fixtureRun(status);
      if (status === 'awaiting_clarification') {
        run.plan = null;
        run.clarification = { id: 'q', questions: ['Which?'] };
      }
      state.sessions.set([fixtureSession(1, [run])]);
      state.selectedId.set('agent-1');
      state.updateDraft('Request');
      expect(state.currentView()).toBeNull();
      expect(state.locked()).toBe(true);
      expect(state.canRequest()).toBe(false);
      await state.preferences({ model: 'other' });
      state.request();
      expect(api.patch).not.toHaveBeenCalled();
      expect(api.calls).toHaveLength(0);
    },
  );
  it('loads ten summaries and one current body, opening replaces the historical body with GET only', async () => {
    const current = normalizeRun(fixtureRun(), 1);
    const historical = Array.from({ length: 10 }, (_, index) => ({
      ...normalizeRun(fixtureRun('rejected'), 1),
      id: `history-${index}`,
    }));
    const page: RunsPage = {
      projection_version: 2,
      session_id: 'agent-1',
      at_sequence: 1,
      total: 50,
      runs: historical.map((run) => ({ ...controlOf(run), request_preview: run.request })),
      next_before: 40,
    };
    api.runs.mockResolvedValue(page);
    api.detail.mockImplementation(async (_sid, rid) =>
      rid === current.id ? current : historical.find((run) => run.id === rid)!,
    );
    const header = { ...fixtureSession(1, [current]), run_count: 50 };
    api.list.mockResolvedValueOnce(fixtureList([header]));
    api.get.mockResolvedValue(header);
    await state.load();
    expect(api.detail).toHaveBeenCalledOnce();
    expect(api.runs).toHaveBeenCalledOnce();
    await state.openRun('history-0');
    await state.openRun('history-1');
    expect(state.visibleRuns().map((run) => run.id)).toEqual(['run-1', 'history-1']);
    expect(api.detail).toHaveBeenCalledTimes(3);
    expect(state.summaries()).toHaveLength(10);
    expect(api.calls).toHaveLength(0);
    state.closeRun();
    expect(state.visibleRuns()).toHaveLength(1);
    await state.loadRuns(true);
    expect(api.runs).toHaveBeenLastCalledWith('agent-1', expect.any(AbortSignal), 40);
  });
  it('does not approve a header identity until the complete matching plan body is loaded', async () => {
    const pending = deferred<AgentRun>();
    api.detail.mockReturnValueOnce(pending.promise);
    const loading = load(fixtureRun());
    await flush();
    expect(state.locked()).toBe(true);
    expect(state.currentView()).toBeNull();
    state.approve(normalizeRun(fixtureRun(), 1));
    expect(api.calls).toHaveLength(0);
    pending.resolve(normalizeRun(fixtureRun(), 1));
    await loading;
    const run = state.activeRun()!;
    expect(state.canApprove(run)).toBe(true);
    state.approve({ ...run });
    state.approve(run);
    state.approve(run);
    expect(api.calls).toHaveLength(1);
    expect(api.calls[0].request.command).toMatchObject({
      action: 'execute',
      run_id: 'run-1',
      plan_id: 'plan-1',
      revision: 1,
      digest: fixturePlan().digest,
    });
  });
  it.each(['revision', 'digest', 'model', 'effort'] as const)(
    'rejects a full body with mismatching %s',
    async (field) => {
      const body = normalizeRun(fixtureRun(), 1);
      if (field === 'revision') body.plan!.revision++;
      if (field === 'digest') body.plan!.digest = 'a'.repeat(64);
      if (field === 'model') body.plan!.model = 'other';
      if (field === 'effort') body.plan!.reasoning_effort = 'high';
      api.detail.mockResolvedValueOnce(body);
      await load(fixtureRun());
      expect(state.currentView()).toBeNull();
      expect(state.pageErrors().get('current')).toContain('Invalid Agent');
      expect(state.locked()).toBe(true);
    },
  );
  it('permits many plan revisions with revision-bound feedback and no automatic execution', async () => {
    const run = fixtureRun();
    run.plan!.revision = 19;
    run.plan!.steps = Array.from({ length: 9 }, (_, index) => ({
      ...fixturePlan().steps[0],
      id: `step-${index}`,
    }));
    await load(run);
    const loaded = state.activeRun()!;
    state.updateFeedback(loaded, 'Change scope');
    state.setMode('chat');
    TestBed.tick();
    expect(state.feedback(loaded)).toBe('Change scope');
    state.revise(loaded, 'Change scope');
    expect(api.calls[0].request.command).toMatchObject({
      action: 'revise',
      revision: 19,
      content: 'Change scope',
    });
    const revised = { ...run, plan: { ...run.plan!, revision: 20, digest: 'a'.repeat(64) } };
    api.event(0, 2, { ...run, status: 'planning', operation_id: 'operation-1' });
    api.event(0, 3, revised);
    api.calls[0].resolve();
    await flush();
    expect(state.activeRun()?.plan?.revision).toBe(20);
    expect(state.feedback(state.activeRun()!)).toBe('');
    state.approve(loaded);
    expect(api.calls).toHaveLength(1);
  });
  it('keeps background operations alive across mode and selection changes without retaining their bodies', async () => {
    await load();
    state.updateDraft('Read the fixture');
    state.request();
    state.setMode('chat');
    state.select('agent-2');
    await flush();
    api.event(0, 2, fixtureRun('planning'));
    api.event(0, 3, fixtureRun());
    api.calls[0].resolve();
    await flush();
    expect(api.calls[0].signal.aborted).toBe(false);
    expect(state.selectedId()).toBe('agent-2');
    expect(state.mode()).toBe('chat');
    expect(state.sessions().find((session) => session.id === 'agent-1')?.current_run?.status).toBe(
      'awaiting_approval',
    );
    expect(state.visibleRuns()).toEqual([]);
  });
  it('preserves an edited draft when planning admission arrives', async () => {
    await load();
    state.updateDraft('Read the fixture');
    state.request();
    state.updateDraft('Next task');
    api.event(0, 2, fixtureRun('planning'));
    expect(state.draft()).toBe('Next task');
  });
  it('recovers interrupted streams with header/detail GET only and never re-POSTs execution', async () => {
    await load(fixtureRun());
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    api.get.mockResolvedValue(fixtureSession(4, [fixtureRun('interrupted')]));
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.error()).toContain('before settlement');
    expect(state.currentView()?.body.status).toBe('interrupted');
    expect(api.calls).toHaveLength(1);
    expect(api.get).toHaveBeenCalledTimes(2);
  });
  it('keeps a newer control locked when an older header/detail response arrives late', async () => {
    await load();
    const pending = deferred<AgentSession>();
    api.get.mockReturnValueOnce(pending.promise);
    const refreshing = state.refresh();
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 2, fixtureRun('planning'));
    pending.resolve(fixtureSession(1));
    await refreshing;
    expect(state.activeControl()?.status).toBe('planning');
    expect(state.locked()).toBe(true);
  });
  it('ignores late review detail after a newer planning control and never unlocks approval', async () => {
    await load(fixtureRun());
    const old = state.activeRun()!;
    const pending = deferred<AgentRun>();
    api.detail.mockReturnValueOnce(pending.promise);
    const opening = state.openRun('run-1', true);
    state.revise(old, 'Change');
    api.event(0, 2, { ...old, status: 'planning', operation_id: 'operation-1' });
    pending.resolve(old);
    await opening;
    expect(state.activeControl()?.status).toBe('planning');
    expect(state.canApprove(old)).toBe(false);
  });
  it.each(['selection', 'sequence'] as const)(
    'ignores a late run-summary page after %s changes',
    async (change) => {
      await load();
      const pending = deferred<RunsPage>();
      api.runs.mockReturnValueOnce(pending.promise);
      const work = state.loadRuns();
      if (change === 'selection') state.select('agent-2');
      else {
        state.updateDraft('Read the fixture');
        state.request();
        api.event(0, 2, fixtureRun('planning'));
      }
      pending.resolve({
        projection_version: 2,
        session_id: 'agent-1',
        at_sequence: 1,
        total: 1,
        runs: [
          { ...controlOf(normalizeRun(fixtureRun('rejected'), 1)), request_preview: 'Obsolete' },
        ],
        next_before: null,
      });
      await work;
      expect(state.summaries().some((entry) => entry.request_preview === 'Obsolete')).toBe(false);
    },
  );
  it('replaces result windows, keeps completed progress accurate, and makes Show recent GET-only', async () => {
    await load(manyResults('completed'));
    const recent = state.currentView()!.body;
    const older: ResultsPage = {
      projection_version: 2,
      session_id: 'agent-1',
      run_id: recent.id,
      at_sequence: 1,
      total: 50,
      completed_steps: 50,
      results: Array.from({ length: 20 }, (_, index) => ({
        ...recent.results[0],
        step_id: `step-${index + 10}`,
      })),
      next_before: 10,
    };
    api.results.mockResolvedValueOnce(older);
    await state.loadResults(recent);
    expect(state.currentView()?.window.kind).toBe('older');
    expect(state.currentView()?.body.results).toHaveLength(20);
    expect(state.currentView()?.body.results[0].step_id).toBe('step-10');
    expect(state.currentView()?.body.completed_steps).toBe(50);
    api.results.mockResolvedValueOnce({ ...older, results: recent.results, next_before: 30 });
    await state.loadResults(state.currentView()!.body, true);
    expect(state.currentView()?.window.kind).toBe('recent');
    expect(state.currentView()?.body.results[0].step_id).toBe('step-30');
    expect(api.calls).toHaveLength(0);
    expect(api.results.mock.calls.map(([, , , before]) => before)).toEqual([30, undefined]);
  });
  it('does not replace a recent live window with an older page that arrives after a current event', async () => {
    const executing = manyResults();
    executing.completed_steps = 49;
    executing.results[19].status = 'running';
    await load(executing);
    const run = state.currentView()!.body;
    const pending = deferred<ResultsPage>();
    api.results.mockReturnValueOnce(pending.promise);
    const loading = state.loadResults(run);
    // This read-only reload observes a newer authoritative execution state.
    const newer = manyResults();
    api.get.mockResolvedValueOnce(fixtureSession(2, [newer]));
    await state.refresh();
    pending.resolve({
      projection_version: 2,
      session_id: 'agent-1',
      run_id: run.id,
      at_sequence: 1,
      total: 50,
      completed_steps: 49,
      results: [],
      next_before: null,
    });
    await loading;
    expect(state.currentView()?.body.version_sequence).toBe(2);
    expect(state.currentView()?.window.kind).toBe('recent');
  });
  it('bounds live recent results at twenty and preserves an explicitly inspected older page', async () => {
    const plan = manyResults().plan!;
    await load({ ...fixtureRun(), plan });
    state.approve(state.activeRun()!);
    api.event(0, 2, { ...fixtureRun('approved'), plan });
    api.event(0, 3, { ...fixtureRun('executing'), plan });
    let sequence = 4;
    for (let index = 0; index < 30; index++) {
      const run = {
        ...fixtureRun('executing'),
        plan,
        result_count: index + 1,
        completed_steps: index + 1,
      };
      const result = {
        step_id: `step-${index}`,
        status: 'completed' as const,
        content: `R${index}`,
        time: run.updated_at,
      };
      for (const event of fixtureStepEvents(api.calls[0].request, sequence, run, result))
        api.calls[0].emit(event);
      sequence += 2;
    }
    expect(state.currentView()?.body.results).toHaveLength(20);
    expect(state.currentView()?.body.results_before).toBe(10);
    const run = state.currentView()!.body;
    api.results.mockResolvedValueOnce({
      projection_version: 2,
      session_id: 'agent-1',
      run_id: run.id,
      at_sequence: sequence - 1,
      total: 30,
      completed_steps: 30,
      results: Array.from({ length: 10 }, (_, index) => ({
        ...run.results[0],
        step_id: `step-${index}`,
      })),
      next_before: null,
    });
    await state.loadResults(run);
    const inspected = state.currentView()!.body.results;
    for (const event of fixtureStepEvents(
      api.calls[0].request,
      sequence,
      { ...fixtureRun('executing'), plan },
      { ...inspected[0], step_id: 'step-30' },
    ))
      api.calls[0].emit(event);
    expect(state.currentView()?.window.kind).toBe('older');
    expect(state.currentView()?.body.results).toBe(inspected);
    expect(state.currentView()?.body.completed_steps).toBe(31);
  });
  it('guards double-click detail/results GETs and scopes failures without clearing controls', async () => {
    await load(manyResults());
    const run = state.currentView()!.body;
    const pending = deferred<ResultsPage>();
    api.results.mockReturnValueOnce(pending.promise);
    const first = state.loadResults(run);
    void state.loadResults(run);
    expect(api.results).toHaveBeenCalledOnce();
    pending.reject(new Error('Older results unavailable'));
    await first;
    expect(state.pageErrors().get('results:current')).toBe('Older results unavailable');
    expect(state.sessionError()).toBeNull();
    expect(state.locked()).toBe(true);
    const detail = deferred<AgentRun>();
    api.detail.mockReturnValueOnce(detail.promise);
    const open = state.openRun(run.id, true);
    void state.openRun(run.id, true);
    expect(api.detail).toHaveBeenCalledTimes(2);
    detail.resolve(run);
    await open;
  });
  it('discards a late older-results page after a live current event without evicting the new recent window', async () => {
    const plan = manyResults().plan!;
    await load({ ...fixtureRun(), plan });
    state.approve(state.activeRun()!);
    api.event(0, 2, { ...fixtureRun('approved'), plan });
    api.event(0, 3, { ...fixtureRun('executing'), plan });
    const emitResult = (index: number) => {
      const run = {
        ...fixtureRun('executing'),
        plan,
        result_count: index + 1,
        completed_steps: index + 1,
      };
      const result = {
        step_id: `step-${index}`,
        status: 'completed' as const,
        content: `R${index}`,
        time: fixtureRun().updated_at,
      };
      for (const event of fixtureStepEvents(api.calls[0].request, index * 2 + 4, run, result))
        api.calls[0].emit(event);
    };
    for (let index = 0; index < 25; index++) emitResult(index);
    const pending = deferred<ResultsPage>();
    api.results.mockReturnValueOnce(pending.promise);
    const older = state.loadResults(state.currentView()!.body);
    emitResult(25);
    pending.resolve({
      projection_version: 2,
      session_id: 'agent-1',
      run_id: 'run-1',
      at_sequence: 53,
      total: 25,
      completed_steps: 25,
      results: [],
      next_before: null,
    });
    await older;
    expect(state.currentView()?.window.kind).toBe('recent');
    expect(state.currentView()?.body.result_count).toBe(26);
    expect(state.currentView()?.body.results).toHaveLength(20);
    expect(state.currentView()?.body.results.at(-1)?.step_id).toBe('step-25');
    expect(state.pageErrors().size).toBe(0);
  });
  it('targets Stop from authoritative control even without loaded detail and has no run timeout', async () => {
    vi.useFakeTimers();
    state.sessions.set([fixtureSession(1, [fixtureRun('executing')])]);
    state.selectedId.set('agent-1');
    const control = state.activeControl()!;
    await vi.advanceTimersByTimeAsync(86400000);
    await state.stop(control);
    expect(api.cancel).toHaveBeenCalledWith(
      'agent-1',
      'run-1',
      expect.any(String),
      'operation-1',
      expect.any(AbortSignal),
    );
    expect(state.activeControl()?.status).toBe('cancelling');
    expect(state.locked()).toBe(true);
    await state.stop(control);
    expect(api.cancel).toHaveBeenCalledOnce();
  });
  it('keeps in-flight archive ownership across older reads and stream completion, then preserves 202 pending', async () => {
    await load(fixtureRun());
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    const pending = deferred<AgentSession>();
    api.archive.mockReturnValueOnce(pending.promise);
    const archiving = state.archive('agent-1');
    api.event(0, 3, fixtureRun('cancelled'));
    api.calls[0].resolve();
    await flush();
    expect(state.archivePending().has('agent-1')).toBe(true);
    expect(state.locked()).toBe(true);
    pending.resolve({ ...fixtureSession(4, [fixtureRun('cancelled')]), archive_pending: true });
    await archiving;
    expect(state.archivePending().has('agent-1')).toBe(true);
    api.get.mockResolvedValueOnce({
      ...fixtureSession(5, [fixtureRun('cancelled')]),
      archived: true,
    });
    await state.refresh();
    expect(state.activeSession()?.archived).toBe(true);
    expect(state.archivePending().size).toBe(0);
  });
  it('keeps failed archive acknowledgement uncertain until an authoritative GET clears it', async () => {
    await load();
    api.archive.mockRejectedValueOnce(new Error('Archive unavailable'));
    await state.archive('agent-1');
    expect(state.archivePending().has('agent-1')).toBe(true);
    expect(state.sessionError()).toBe('Archive unavailable');
    api.get.mockResolvedValueOnce(fixtureSession(2));
    await state.refresh();
    expect(state.archivePending().size).toBe(0);
  });
  it('keeps background failures scoped and does not overwrite newer command errors with old read errors', async () => {
    await load();
    const pending = deferred<AgentSession>();
    api.get.mockReturnValueOnce(pending.promise);
    const refreshing = state.refresh();
    api.patch.mockRejectedValueOnce(new Error('New preference error'));
    await state.preferences({ model: 'other' });
    pending.reject(new Error('Old read error'));
    await refreshing;
    expect(state.sessionError()).toBe('New preference error');
    state.updateDraft('Read the fixture');
    state.request();
    api.get.mockImplementation(async (sid) => fixtureSession(1, [], sid));
    state.select('agent-2');
    await flush();
    api.calls[0].reject(new Error('Background failure'));
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.sessionErrors().get('agent-1')).toBe('Background failure');
    expect(state.sessionError()).toBeNull();
  });
  it('retains command error ownership and recovery failure guidance without repeating commands', async () => {
    await load();
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 2, fixtureRun('planning'));
    api.get.mockRejectedValueOnce(new Error('Recovery unavailable'));
    api.calls[0].reject(new Error('Planning failed'));
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.sessionError()).toContain('Planning failed');
    expect(state.sessionError()).toContain('History recovery failed: Recovery unavailable');
    expect(api.calls).toHaveLength(1);
  });
  it('does not let a late creation change a newer selection and aborts all request owners on destruction', async () => {
    await load();
    const created = deferred<AgentSession>();
    api.create.mockReturnValueOnce(created.promise);
    const creating = state.create('model-a');
    api.get.mockImplementation(async (sid) => fixtureSession(1, [], sid));
    state.select('agent-2');
    created.resolve(fixtureSession(0, [], 'new'));
    await creating;
    expect(state.selectedId()).toBe('agent-2');
    await flush();
    state.updateDraft('Read the fixture');
    state.request();
    TestBed.resetTestingModule();
    expect(api.calls[0].signal.aborted).toBe(true);
    api.event(0, 1, fixtureRun('planning'));
    expect(state.activeControl()).toBeNull();
  });
});
