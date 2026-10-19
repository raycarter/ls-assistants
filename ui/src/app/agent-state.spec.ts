import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentApi } from './agent-api';
import { AgentState, AGENT_UI_STORAGE_KEY } from './agent-state';
import { CHAT_STORAGE } from './chat-state';
import {
  FakeAgentApi,
  fixtureEvent,
  FIXTURE_DIGEST,
  fixtureRun,
  fixtureSession,
  memoryAgentStorage,
} from './agent-api.testing';

describe('AgentState ownership, concurrency and UI storage', () => {
  let api: FakeAgentApi;
  let storage: ReturnType<typeof memoryAgentStorage>;
  let state: AgentState;
  beforeEach(() => {
    api = new FakeAgentApi();
    storage = memoryAgentStorage();
    TestBed.configureTestingModule({
      providers: [
        { provide: AgentApi, useValue: api },
        { provide: CHAT_STORAGE, useValue: storage },
      ],
    });
    state = TestBed.inject(AgentState);
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });
  async function ready(): Promise<void> {
    await state.load();
  }
  async function flush(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
    TestBed.tick();
  }
  function review(): void {
    state.sessions.set([fixtureSession(1, [fixtureRun()])]);
    state.selectedId.set('agent-1');
  }
  it('does not fetch Agent history on initial Chat startup', () => {
    TestBed.tick();
    expect(api.list).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
    expect(state.mode()).toBe('chat');
  });
  it('keeps drafts and selection independent, persists only UI metadata, and normalizes a missing server selection', async () => {
    await ready();
    state.updateDraft(' unsent 🌱 ');
    state.sessions.update((sessions) => [...sessions, fixtureSession(0, [], 'agent-2')]);
    state.select('agent-2');
    state.updateDraft('second draft');
    await flush();
    state.setMode('agent');
    TestBed.tick();
    const saved = JSON.parse(storage.records.get(AGENT_UI_STORAGE_KEY)!);
    expect(saved).toEqual({
      version: 1,
      mode: 'agent',
      selectedId: 'agent-2',
      legacyId: null,
      drafts: { 'agent-1': ' unsent 🌱 ', 'agent-2': 'second draft' },
    });
    expect(storage.records.get(AGENT_UI_STORAGE_KEY)).not.toContain('runs');
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AgentApi, useValue: api },
        { provide: CHAT_STORAGE, useValue: storage },
      ],
    });
    state = TestBed.inject(AgentState);
    expect(state.mode()).toBe('agent');
    expect(state.draft()).toBe('second draft');
    await state.load();
    expect(state.selectedId()).toBe('agent-1');
    expect(state.draft()).toBe(' unsent 🌱 ');
    expect(api.calls).toHaveLength(0);
  });
  it.each([
    '{not-json',
    '{"version":99,"private":"keep-me"}',
    '{"version":1,"mode":"agent","selectedId":null,"drafts":[]}',
  ])('leaves invalid storage untouched and warns without content', (raw) => {
    TestBed.resetTestingModule();
    storage.records.set(AGENT_UI_STORAGE_KEY, raw);
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    TestBed.configureTestingModule({
      providers: [
        { provide: AgentApi, useValue: api },
        { provide: CHAT_STORAGE, useValue: storage },
      ],
    });
    state = TestBed.inject(AgentState);
    state.updateDraft('private draft');
    TestBed.tick();
    expect(storage.records.get(AGENT_UI_STORAGE_KEY)).toBe(raw);
    expect(warning).toHaveBeenCalledOnce();
    expect(JSON.stringify(warning.mock.calls)).not.toContain('private');
  });
  it('keeps drafts in memory on quota errors without logging exception content', () => {
    storage.setItem.mockImplementation(() => {
      throw new Error('private exception');
    });
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    state.updateDraft('draft');
    TestBed.tick();
    state.updateDraft('changed');
    TestBed.tick();
    expect(state.draft()).toBe('changed');
    expect(warning).toHaveBeenCalledOnce();
    expect(JSON.stringify(warning.mock.calls)).not.toContain('private exception');
  });
  it('guards duplicate approval, stale revision objects, and preference changes while waiting', async () => {
    review();
    await state.preferences({ model: 'model-b' });
    expect(api.patch).not.toHaveBeenCalled();
    state.approve({ ...state.activeRun()!, plan: { ...state.activeRun()!.plan!, revision: 0 } });
    expect(api.calls).toHaveLength(0);
    const run = state.activeRun()!;
    state.approve(run);
    state.approve(run);
    expect(api.calls).toHaveLength(1);
    expect(api.calls[0].request.command).toEqual({
      action: 'execute',
      run_id: run.id,
      plan_id: 'plan-1',
      revision: 1,
      digest: FIXTURE_DIGEST,
    });
  });
  it.each([
    'planning',
    'awaiting_clarification',
    'awaiting_approval',
    'approved',
    'executing',
    'cancelling',
  ] as const)('locks model and effort during %s', async (status) => {
    const run = fixtureRun(status);
    if (status === 'awaiting_clarification')
      run.clarification = { id: 'q1', questions: ['Which file?'] };
    state.sessions.set([fixtureSession(1, [run])]);
    state.selectedId.set('agent-1');
    expect(state.locked()).toBe(true);
    await state.preferences({ reasoning_effort: 'high' });
    expect(api.patch).not.toHaveBeenCalled();
  });
  it('keeps an operation alive across mode/selection changes and never activates its session on completion', async () => {
    await ready();
    state.updateDraft('Read the fixture');
    state.request();
    state.setMode('chat');
    state.select('agent-2');
    await flush();
    api.event(0, 1, fixtureRun('planning'));
    api.event(0, 2, fixtureRun());
    api.calls[0].resolve();
    await flush();
    expect(api.calls[0].signal.aborted).toBe(false);
    expect(state.mode()).toBe('chat');
    expect(state.selectedId()).toBe('agent-2');
    expect(state.sessions().find((session) => session.id === 'agent-1')!.runs[0].status).toBe(
      'awaiting_approval',
    );
  });
  it('preserves a draft edited while its planning request is in flight', async () => {
    await ready();
    state.updateDraft('Read the fixture');
    state.request();
    state.updateDraft('Next task');
    api.event(0, 1, fixtureRun('planning'));
    expect(state.draft()).toBe('Next task');
  });
  it('recovers interrupted streams with GET and never repeats a POST', async () => {
    await ready();
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 1, fixtureRun('planning'));
    api.get.mockResolvedValue(fixtureSession(2, [fixtureRun('interrupted')]));
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.error()).toContain('before settlement');
    expect(state.activeSession()!.runs[0].status).toBe('interrupted');
    expect(api.calls).toHaveLength(1);
    expect(api.get).toHaveBeenCalledTimes(2);
  });
  it('rejects stale callbacks from injected clients, then recovers by GET', async () => {
    await ready();
    state.updateDraft('Read the fixture');
    state.request();
    const call = api.calls[0];
    const pending = fixtureEvent(
      { ...call.request, request_id: 'stale' },
      1,
      fixtureRun('planning'),
    );
    expect(() => call.emit(pending)).toThrow('Invalid Agent');
    expect(state.activeSession()!.runs).toEqual([]);
    call.reject(new Error('invalid callback'));
    await flush();
    expect(api.calls).toHaveLength(1);
  });
  it('does not let delayed GET snapshots overwrite newer stream data', async () => {
    await ready();
    let resolve!: (value: ReturnType<typeof fixtureSession>) => void;
    api.get.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const refresh = state.refresh();
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 1, fixtureRun('planning'));
    resolve(fixtureSession());
    await refresh;
    expect(state.activeRun()!.status).toBe('planning');
  });
  it('aborts superseded reads and all request owners on root destruction, guarding late callbacks', async () => {
    await ready();
    const signals: AbortSignal[] = [];
    api.get.mockImplementation((_sid, signal) => {
      signals.push(signal);
      return new Promise(() => {});
    });
    state.select('agent-1');
    state.select('agent-1');
    expect(signals[0].aborted).toBe(true);
    state.updateDraft('Read the fixture');
    state.request();
    TestBed.resetTestingModule();
    expect(signals[1].aborted).toBe(true);
    expect(api.calls[0].signal.aborted).toBe(true);
    api.event(0, 1, fixtureRun('planning'));
    expect(state.activeSession()!.runs).toEqual([]);
  });
  it('targets Stop at the captured operation and keeps cancelling nonterminal', async () => {
    state.sessions.set([fixtureSession(1, [fixtureRun('executing')])]);
    state.selectedId.set('agent-1');
    const old = state.activeRun()!;
    await state.stop(old);
    expect(api.cancel).toHaveBeenCalledWith(
      'agent-1',
      'run-1',
      expect.any(String),
      'operation-1',
      expect.any(AbortSignal),
    );
    expect(state.activeRun()!.status).toBe('cancelling');
    expect(state.locked()).toBe(true);
    state.sessions.set([
      fixtureSession(3, [{ ...fixtureRun('executing'), operation_id: 'operation-2' }]),
    ]);
    await state.stop(old);
    expect(api.cancel).toHaveBeenCalledOnce();
  });
  it('rejects a waiting plan or clarification without execution', async () => {
    review();
    await state.reject(state.activeRun()!);
    expect(api.reject).toHaveBeenCalledWith(
      'agent-1',
      'run-1',
      expect.any(String),
      { kind: 'plan', plan_id: 'plan-1', revision: 1, digest: FIXTURE_DIGEST },
      expect.any(AbortSignal),
    );
    expect(state.activeRun()).toBeNull();
    const run = {
      ...fixtureRun('awaiting_clarification'),
      plan: null,
      clarification: { id: 'q1', questions: ['Which workspace?'] },
    };
    state.sessions.set([fixtureSession(1, [run])]);
    await state.reject(run);
    expect(api.reject).toHaveBeenLastCalledWith(
      'agent-1',
      'run-1',
      expect.any(String),
      { kind: 'clarification', clarification_id: 'q1' },
      expect.any(AbortSignal),
    );
    expect(api.calls).toHaveLength(0);
  });
  it('binds plan feedback to the current revision and never executes it', () => {
    review();
    state.revise(state.activeRun()!, 'Use fewer lines');
    expect(api.calls[0].request.command).toEqual({
      action: 'revise',
      run_id: 'run-1',
      kind: 'feedback',
      content: 'Use fewer lines',
      plan_id: 'plan-1',
      revision: 1,
      digest: FIXTURE_DIGEST,
    });
  });
  it('retains pending archive history and only marks archived after a server acknowledgement', async () => {
    state.sessions.set([fixtureSession(1, [fixtureRun('executing')])]);
    state.selectedId.set('agent-1');
    api.archive.mockResolvedValueOnce({
      ...fixtureSession(2, [fixtureRun('cancelling')]),
      archive_pending: true,
    });
    await state.archive('agent-1');
    expect(state.archivePending().has('agent-1')).toBe(true);
    expect(state.activeSession()!.archived).toBe(false);
    expect(state.sessions()).toHaveLength(1);
    api.get.mockResolvedValue({ ...fixtureSession(3, [fixtureRun('cancelled')]), archived: true });
    await state.refresh();
    expect(state.activeSession()!.archived).toBe(true);
    expect(state.archivePending().size).toBe(0);
    expect(state.sessions()).toHaveLength(1);
  });
  it('surfaces unavailable BFF errors with no fabricated history', async () => {
    api.list.mockRejectedValue(new Error('Agent is unavailable; configure the BFF.'));
    await state.load();
    expect(state.sessions()).toEqual([]);
    expect(state.error()).toContain('configure the BFF');
  });
  it('aborts superseded lists and does not let a late creation change a newer selection', async () => {
    const signals: AbortSignal[] = [];
    let finishList!: (sessions: ReturnType<typeof fixtureSession>[]) => void;
    api.list.mockImplementationOnce((signal) => {
      signals.push(signal);
      return new Promise((resolve) => {
        finishList = resolve;
      });
    });
    const oldList = state.load();
    await state.load();
    expect(signals[0].aborted).toBe(true);
    finishList([fixtureSession(0, [], 'obsolete')]);
    await oldList;
    expect(state.sessions().some((session) => session.id === 'obsolete')).toBe(false);
    let finishCreate!: (session: ReturnType<typeof fixtureSession>) => void;
    api.create.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishCreate = resolve;
        }),
    );
    const creating = state.create('model-a');
    state.select('agent-1');
    finishCreate(fixtureSession(0, [], 'created-later'));
    await creating;
    expect(state.selectedId()).toBe('agent-1');
    expect(state.sessions().some((session) => session.id === 'created-later')).toBe(true);
  });
  it('persists revision-bound feedback and does not carry it into a different plan', () => {
    review();
    const run = state.activeRun()!;
    state.updateFeedback(run, 'Only one line, please');
    state.setMode('chat');
    TestBed.tick();
    expect(state.feedback(run)).toBe('Only one line, please');
    expect(storage.records.get(AGENT_UI_STORAGE_KEY)).toContain('Only one line, please');
    const revised = { ...run, plan: { ...run.plan!, revision: 2, digest: 'r2' } };
    state.sessions.set([fixtureSession(2, [revised])]);
    expect(state.feedback(revised)).toBe('');
    state.approve(run);
    expect(api.calls).toHaveLength(0);
  });
  it('keeps failed archive acknowledgement uncertain until authoritative state clears it', async () => {
    state.sessions.set([fixtureSession(1, [fixtureRun('cancelling')])]);
    state.selectedId.set('agent-1');
    api.archive.mockRejectedValueOnce(new Error('Archive unavailable'));
    await state.archive('agent-1');
    expect(state.archivePending().has('agent-1')).toBe(true);
    expect(state.activeSession()!.archived).toBe(false);
    expect(state.error()).toBe('Archive unavailable');
    api.get.mockResolvedValueOnce({
      ...fixtureSession(2, [fixtureRun('cancelling')]),
      archive_pending: false,
    });
    await state.refresh();
    expect(state.archivePending().size).toBe(0);
  });
  it.each([undefined, true])(
    'keeps a settled run locked until pending archive is acknowledged (flag=%s)',
    async (archive_pending) => {
      state.sessions.set([fixtureSession(1, [fixtureRun('executing')])]);
      state.selectedId.set('agent-1');
      state.updateDraft('Keep this unsent draft');
      api.archive.mockResolvedValueOnce({
        ...fixtureSession(2, [fixtureRun('cancelling')]),
        archive_pending: true,
      });
      await state.archive('agent-1');
      const session = fixtureSession(3, [fixtureRun('cancelled')]);
      if (archive_pending === undefined) delete session.archive_pending;
      else session.archive_pending = archive_pending;
      api.get.mockResolvedValueOnce(session);
      await state.refresh();
      expect(state.activeRun()).toBeNull();
      expect(state.archivePending().has('agent-1')).toBe(true);
      expect(state.locked()).toBe(true);
      expect(state.canRequest()).toBe(false);
      state.updateDraft('Must not replace the pending-archive draft');
      await state.preferences({ model: 'model-b', reasoning_effort: 'high' });
      state.request();
      expect(state.draft()).toBe('Keep this unsent draft');
      expect(api.patch).not.toHaveBeenCalled();
      expect(api.calls).toHaveLength(0);
      api.get.mockResolvedValueOnce({
        ...fixtureSession(4, [fixtureRun('cancelled')]),
        archived: true,
        archive_pending: false,
      });
      await state.refresh();
      expect(state.archivePending().size).toBe(0);
      expect(state.activeSession()!.archived).toBe(true);
    },
  );
  it('restores the server pending-archive marker and blocks review mutations', async () => {
    const session = { ...fixtureSession(1, [fixtureRun()]), archive_pending: true };
    api.list.mockResolvedValueOnce([session]);
    api.get.mockResolvedValueOnce(session);
    await ready();
    const run = state.activeRun()!;
    state.approve(run);
    state.revise(run, 'Must not revise');
    state.updateFeedback(run, 'Must not edit');
    await state.reject(run);
    expect(state.archivePending().has(session.id)).toBe(true);
    expect(api.calls).toHaveLength(0);
    expect(api.reject).not.toHaveBeenCalled();
    expect(state.feedback(run)).toBe('');
  });
  it.each(['wrong content', 'historical run'])(
    'does not apply a %s planning acknowledgement or clear its draft',
    async (kind) => {
      const initial = fixtureSession(0, kind === 'historical run' ? [fixtureRun('rejected')] : []);
      api.list.mockResolvedValueOnce([initial]);
      api.get.mockResolvedValue(initial);
      await ready();
      state.updateDraft('Read the fixture');
      state.request();
      const acknowledgement = { ...initial, sequence: 1 };
      api.calls[0].emit({
        ...fixtureEvent(api.calls[0].request, 1, fixtureRun('planning')),
        run_id: null,
        operation_id: null,
        type: 'session.updated',
        session: acknowledgement,
      });
      expect(state.draft()).toBe('Read the fixture');
      const badRun = fixtureRun('planning');
      if (kind === 'wrong content') badRun.request = 'Some unrelated request';
      expect(() => api.event(0, 2, badRun)).toThrow('Invalid Agent');
      expect(state.activeSession()).toEqual(acknowledgement);
      expect(state.draft()).toBe('Read the fixture');
      api.calls[0].reject(new Error('Invalid planning acknowledgement'));
      await vi.waitFor(() => expect(state.busy().size).toBe(0));
      expect(state.activeSession()).toEqual(acknowledgement);
      expect(state.draft()).toBe('Read the fixture');
      expect(api.calls).toHaveLength(1);
    },
  );
  it('keeps background operation failures with their session and preserves newer foreground/global errors', async () => {
    await ready();
    state.updateDraft('Read the fixture');
    state.request();
    state.select('agent-2');
    await flush();
    state.updateDraft('Read the fixture');
    state.request();
    api.calls[1].reject(new Error('B foreground failure'));
    await vi.waitFor(() => expect(state.sessionError()).toBe('B foreground failure'));
    api.calls[0].reject(new Error('A background failure'));
    await vi.waitFor(() =>
      expect(state.sessionErrors().get('agent-1')).toBe('A background failure'),
    );
    expect(state.error()).toBe('B foreground failure');
    expect(state.selectedId()).toBe('agent-2');
    api.list.mockRejectedValueOnce(new Error('Global list failure'));
    await state.load();
    expect(state.globalError()).toBe('Global list failure');
    expect(state.sessionError()).toBe('B foreground failure');
    api.create.mockRejectedValueOnce(new Error('Global creation failure'));
    await state.create('model-a');
    expect(state.globalError()).toBe('Global creation failure');
    expect(state.sessionError()).toBe('B foreground failure');
    state.select('agent-1');
    await flush();
    expect(state.sessionError()).toBe('A background failure');
  });
  it('does not let an older read failure overwrite a newer operation error in the same session', async () => {
    await ready();
    let rejectRead!: (error: Error) => void;
    api.get.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectRead = reject;
        }),
    );
    const reading = state.refresh();
    api.patch.mockRejectedValueOnce(new Error('Newer preference failure'));
    await state.preferences({ model: 'model-b' });
    rejectRead(new Error('Obsolete read failure'));
    await reading;
    expect(state.sessionError()).toBe('Newer preference failure');
    expect(state.globalError()).toBeNull();
  });
  it('keeps an in-flight archive locked when an older GET and terminal stream snapshot say not pending', async () => {
    review();
    state.updateDraft('Next task');
    state.approve(state.activeRun()!);
    api.event(0, 2, fixtureRun('approved'));
    api.event(0, 3, fixtureRun('executing'));
    let finishRead!: (session: ReturnType<typeof fixtureSession>) => void;
    api.get.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRead = resolve;
        }),
    );
    const reading = state.refresh();
    let finishArchive!: (session: ReturnType<typeof fixtureSession>) => void;
    api.archive.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishArchive = resolve;
        }),
    );
    const archiving = state.archive('agent-1');
    const completed = {
      ...fixtureRun('completed'),
      answer: 'Read excerpt.',
      results: [
        {
          step_id: 'step-1',
          status: 'completed' as const,
          content: 'Excerpt',
          time: fixtureRun().updated_at,
        },
      ],
    };
    api.event(0, 4, completed);
    api.calls[0].resolve();
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    finishRead(fixtureSession(3, [fixtureRun('executing')]));
    await reading;
    expect(state.activeSession()?.archive_pending).toBe(false);
    expect(state.activeRun()).toBeNull();
    expect(state.archivePending().has('agent-1')).toBe(true);
    expect(state.locked()).toBe(true);
    state.request();
    await state.preferences({ model: 'model-b', reasoning_effort: 'high' });
    state.updateDraft('Must not change while archiving');
    expect(api.calls).toHaveLength(1);
    expect(api.patch).not.toHaveBeenCalled();
    expect(state.draft()).toBe('Next task');
    finishArchive({ ...fixtureSession(5, [completed]), archived: true });
    await archiving;
    expect(state.archivePending().size).toBe(0);
    expect(state.activeSession()?.archived).toBe(true);
  });
  it('retains active command error ownership across a successful refresh and a failed recovery GET', async () => {
    await ready();
    state.updateDraft('Read the fixture');
    state.request();
    api.event(0, 1, fixtureRun('planning'));
    api.get.mockResolvedValueOnce(fixtureSession(1, [fixtureRun('planning')]));
    await state.refresh();
    api.get.mockRejectedValueOnce(new Error('BFF recovery unavailable'));
    api.calls[0].reject(new Error('Planning transport failed'));
    await vi.waitFor(() => expect(state.busy().size).toBe(0));
    expect(state.sessionError()).toContain('Planning transport failed');
    expect(state.sessionError()).toContain('History recovery failed: BFF recovery unavailable');
    expect(state.sessionError()).toContain('Reload Agent history');
    expect(api.calls).toHaveLength(1);
    expect(api.get).toHaveBeenCalledTimes(3);
  });
  it.each(['plan-less execution', 'archived active run'])(
    'preserves state and drafts when an injected API returns %s',
    async (kind) => {
      await ready();
      const initial = state.activeSession();
      state.updateDraft('Preserve this draft');
      const invalid = fixtureSession(1, [fixtureRun('executing')]);
      if (kind === 'plan-less execution') invalid.runs[0].plan = null;
      else invalid.archived = true;
      api.get.mockResolvedValueOnce(invalid);
      await state.refresh();
      expect(state.activeSession()).toBe(initial);
      expect(state.draft()).toBe('Preserve this draft');
      expect(state.sessionError()).toContain('Invalid Agent response');
      expect(api.calls).toHaveLength(0);
    },
  );
});
