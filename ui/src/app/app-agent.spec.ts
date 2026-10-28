import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './app';
import { FETCH, ChatApi } from './chat-api';
import { FakeChatApi } from './chat-api.testing';
import { CHAT_STORAGE, CHAT_STORAGE_KEY, ChatState } from './chat-state';
import { AgentState, AGENT_UI_STORAGE_KEY } from './agent-state';
import { AgentApi } from './agent-api';
import {
  FakeAgentApi,
  fixtureEvent,
  FIXTURE_DIGEST,
  fixtureRun,
  fixtureSession,
  fixtureStepEvents,
  fixtureList,
  normalizeRun,
  memoryAgentStorage,
  READ_FILE_FIXTURE,
  sseResponse,
} from './agent-api.testing';
import { controlOf, type AgentSession, type AgentRun } from './agent.models';
import type { AgentStreamRequest } from './agent-api';
import { SettingsDialog } from './settings-dialog';
import { By } from '@angular/platform-browser';

describe('Agent shell and mocked vertical slice', () => {
  let fixture: ComponentFixture<App>;
  let storage: ReturnType<typeof memoryAgentStorage>;
  let chat: FakeChatApi;
  let api: FakeAgentApi;
  let dialogDescriptors: PropertyDescriptorMap;
  beforeEach(() => {
    storage = memoryAgentStorage();
    chat = new FakeChatApi();
    api = new FakeAgentApi();
    dialogDescriptors = Object.getOwnPropertyDescriptors(HTMLDialogElement.prototype);
    Object.defineProperties(HTMLDialogElement.prototype, {
      showModal: {
        configurable: true,
        value: function (this: HTMLDialogElement) {
          this.open = true;
        },
      },
      close: {
        configurable: true,
        value: function (this: HTMLDialogElement) {
          this.open = false;
        },
      },
    });
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    for (const name of ['showModal', 'close']) {
      if (dialogDescriptors[name])
        Object.defineProperty(HTMLDialogElement.prototype, name, dialogDescriptors[name]);
      else Reflect.deleteProperty(HTMLDialogElement.prototype, name);
    }
    vi.restoreAllMocks();
  });
  async function render(fetchDouble?: typeof fetch): Promise<void> {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [
        { provide: CHAT_STORAGE, useValue: storage },
        { provide: ChatApi, useValue: chat },
        ...(fetchDouble
          ? [{ provide: FETCH, useValue: fetchDouble }]
          : [{ provide: AgentApi, useValue: api }]),
      ],
    });
    fixture = TestBed.createComponent(App);
    await settle();
    await vi.waitFor(() => expect(TestBed.inject(AgentState).loading()).toBe(false));
    await settle();
  }
  function root(): HTMLElement {
    return fixture.nativeElement;
  }
  function element<T extends HTMLElement>(selector: string): T {
    const element = root().querySelector<T>(selector);
    if (!element) throw new Error(`Missing ${selector}`);
    return element;
  }
  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }
  async function mode(value: 'chat' | 'agent'): Promise<void> {
    element<HTMLInputElement>(`input[name="interactive-mode"][value="${value}"]`).click();
    await settle();
    if (value === 'agent') {
      await vi.waitFor(() => expect(TestBed.inject(AgentState).loading()).toBe(false));
      await settle();
    }
  }
  async function button(label: string): Promise<void> {
    const button = [...root().querySelectorAll('button')].find(
      (button) => button.textContent?.trim() === label,
    );
    if (!button) throw new Error(`Missing button ${label}`);
    button.click();
    await settle();
  }
  async function fill(selector: string, value: string): Promise<void> {
    const field = element<HTMLTextAreaElement>(selector);
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    await settle();
  }
  it('runs request → review → exact approval → fixture read result → reload through the real AgentApi and HTTP doubles', async () => {
    let stored: AgentSession | null = null;
    let storedRun: AgentRun | null = null;
    let readCalls = 0;
    const fetchDouble = vi.fn<typeof fetch>(async (input, options) => {
      const url = String(input);
      const body = options?.body ? JSON.parse(String(options.body)) : {};
      if (url === '/api/agent/sessions?include_archived=true&limit=25')
        return Response.json(fixtureList(stored ? [stored] : []));
      if (url === '/api/agent/sessions' && options?.method === 'POST') {
        stored = fixtureSession();
        return Response.json(stored);
      }
      if (url === '/api/agent/sessions/agent-1' && options?.method === 'GET')
        return Response.json(stored);
      if (url === '/api/agent/sessions/agent-1/runs?limit=10')
        return Response.json({
          projection_version: 2,
          session_id: 'agent-1',
          at_sequence: stored!.sequence,
          total: storedRun ? 1 : 0,
          runs: storedRun ? [{ ...controlOf(storedRun), request_preview: storedRun.request }] : [],
          next_before: null,
        });
      if (url === '/api/agent/sessions/agent-1/runs/run-1?limit=20')
        return Response.json(storedRun);
      if (url === '/api/agent/sessions/agent-1/runs') {
        expect(readCalls).toBe(0);
        const request: AgentStreamRequest = {
          session_id: 'agent-1',
          request_id: body.request_id,
          sequence: 0,
          command: { action: 'runs', content: body.content },
        };
        const review = fixtureRun();
        review.plan!.search_policy = { kind: 'exact_queries', queries: [], enabled: false };
        stored = fixtureSession(2, [review]);
        storedRun = normalizeRun(review, 2);
        return sseResponse([
          fixtureEvent(request, 1, fixtureRun('planning')),
          fixtureEvent(request, 2, review),
        ]);
      }
      if (url === '/api/agent/sessions/agent-1/runs/run-1/execute') {
        expect(body).toEqual({
          request_id: expect.any(String),
          plan_id: 'plan-1',
          revision: 1,
          digest: FIXTURE_DIGEST,
        });
        readCalls++;
        const request: AgentStreamRequest = {
          session_id: 'agent-1',
          request_id: body.request_id,
          sequence: 2,
          command: {
            action: 'execute',
            run_id: 'run-1',
            plan_id: body.plan_id,
            revision: body.revision,
            digest: body.digest,
          },
        };
        const executing = {
          ...storedRun!,
          status: 'executing' as const,
          operation_id: 'operation-1',
        };
        const completed = {
          ...executing,
          status: 'completed' as const,
          operation_id: null,
          results: [
            {
              step_id: 'step-1',
              status: 'completed' as const,
              content: READ_FILE_FIXTURE,
              time: executing.updated_at,
            },
          ],
          answer: '**Fixture verified.** <img src=x onerror="alert(1)">',
        };
        stored = fixtureSession(7, [completed]);
        storedRun = normalizeRun(completed, 7);
        return sseResponse([
          fixtureEvent(request, 3, { ...executing, status: 'approved' }),
          fixtureEvent(request, 4, executing),
          ...fixtureStepEvents(request, 5, executing, completed.results[0]),
          fixtureEvent(request, 7, completed),
        ]);
      }
      throw new Error(`Unexpected mocked URL ${url}`);
    });
    await render(fetchDouble);
    expect(fetchDouble).not.toHaveBeenCalled();
    await mode('agent');
    element<HTMLButtonElement>('[aria-label="New session"]').click();
    await vi.waitFor(() => expect(TestBed.inject(AgentState).activeSession()).not.toBeNull());
    await settle();
    await fill('#agent-request', 'Read the fixture');
    await button('Request plan');
    await vi.waitFor(() =>
      expect(TestBed.inject(AgentState).activeRun()?.status).toBe('awaiting_approval'),
    );
    await vi.waitFor(() => expect(TestBed.inject(AgentState).busy().size).toBe(0));
    await settle();
    expect(readCalls).toBe(0);
    const plan = element('[aria-label="Reviewed plan"]');
    for (const scope of [
      'public-fixtures',
      'example.md',
      'read_file',
      'model-a',
      'medium',
      'exact_queries',
      FIXTURE_DIGEST,
      'Revision 1',
    ])
      expect(plan.textContent).toContain(scope);
    expect(plan.querySelector('h2')!.textContent).toBe('Plan for review');
    expect(plan.textContent).not.toContain('unverified');
    expect(plan.textContent).not.toContain('Lines 1–2');
    expect(plan.querySelector('[aria-label="Exact tool arguments"]')).toBeNull();
    expect(plan.querySelector('[aria-label="Exact recorded plan"]')).toBeNull();
    expect(element<HTMLSelectElement>('#model-input').disabled).toBe(true);
    expect(root().querySelector('[aria-labelledby="prompts-heading"]')).toBeNull();
    const history = element('[aria-label="Agent run history"]');
    history.scrollTop = 42;
    await button('Approve and run');
    await vi.waitFor(() =>
      expect(TestBed.inject(AgentState).currentView()?.body.status).toBe('completed'),
    );
    await settle();
    expect(history.scrollTop).toBe(42);
    expect(readCalls).toBe(1);
    expect(root().textContent).toContain('A bounded, approved example.');
    expect(element('[aria-label="Agent answer"] strong').textContent).toBe('Fixture verified.');
    expect(root().querySelector('[onerror], script')).toBeNull();
    expect(root().textContent).not.toContain('Approve and run');
    expect(JSON.parse(storage.records.get(AGENT_UI_STORAGE_KEY)!)).toMatchObject({
      mode: 'agent',
      selectedId: 'agent-1',
    });
    const postCount = fetchDouble.mock.calls.filter(([, init]) => init?.method === 'POST').length;
    TestBed.resetTestingModule();
    await render(fetchDouble);
    await vi.waitFor(() =>
      expect(TestBed.inject(AgentState).currentView()?.body.status).toBe('completed'),
    );
    await settle();
    expect(root().textContent).toContain('A bounded, approved example.');
    expect(element('[aria-label="Agent answer"] strong').textContent).toBe('Fixture verified.');
    expect(fetchDouble.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(
      postCount,
    );
    expect(readCalls).toBe(1);
  });
  it('uses one native radio group, keeps Chat selection/draft and background streaming on mode navigation', async () => {
    await render();
    const state = TestBed.inject(ChatState);
    state.updateDraft('Chat request');
    fixture.componentInstance.sendMessage();
    chat.event(0, 'start');
    state.updateDraft('Chat next draft');
    const id = state.activeSessionId();
    await mode('agent');
    expect(root().querySelectorAll('input[name="interactive-mode"]:checked')).toHaveLength(1);
    expect(element<HTMLInputElement>('input[value="agent"]').type).toBe('radio');
    expect(root().querySelector('#message-input')).toBeNull();
    expect(root().querySelector('[aria-labelledby="prompts-heading"]')).toBeNull();
    expect(root().textContent).not.toContain('Choose a system prompt');
    await fill('#agent-request', 'Agent draft');
    chat.event(0, 'delta', 'background answer');
    chat.event(0, 'done');
    await settle();
    expect(chat.calls[0].signal.aborted).toBe(false);
    expect(state.activeSessionId()).toBe(id);
    await mode('chat');
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Chat next draft');
    expect(root().textContent).toContain('background answer');
    await mode('agent');
    expect(element<HTMLTextAreaElement>('#agent-request').value).toBe('Agent draft');
  });
  it('pins a restored archived selection outside page one and exposes explicit sidebar pagination with accurate counts', async () => {
    storage.records.set(
      AGENT_UI_STORAGE_KEY,
      JSON.stringify({ version: 1, mode: 'agent', selectedId: 'old', legacyId: null, drafts: {} }),
    );
    const archived = fixtureSession(1, [fixtureRun('rejected')], 'old');
    archived.archived = true;
    archived.title = 'Pinned archive';
    api.list.mockResolvedValueOnce(fixtureList([fixtureSession()], 'page2', 80));
    api.get.mockResolvedValue(archived);
    await render();
    expect(TestBed.inject(AgentState).selectedId()).toBe('old');
    const sidebar = element('[aria-label="Agent session history"]');
    expect(sidebar.textContent).toContain('2 loaded');
    expect(sidebar.textContent).toContain('80 total');
    expect(sidebar.querySelector('[aria-current="true"]')?.textContent).toContain('Pinned archive');
    expect(api.list).toHaveBeenCalledOnce();
    expect(api.get.mock.calls[0][0]).toBe('old');
    expect(root().querySelector('#agent-request')).toBeNull();
    api.list.mockResolvedValueOnce(
      fixtureList([fixtureSession(0, [], 'page2-session')], 'page3', 80),
    );
    await button('Load more sessions');
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(TestBed.inject(AgentState).selectedId()).toBe('old');
    expect(sidebar.textContent).toContain('3 loaded');
  });
  it('persists a background Chat title without leaking its conversation or reply controls into Agent mode', async () => {
    await render();
    const chatState = TestBed.inject(ChatState);
    const agentState = TestBed.inject(AgentState);
    await fill('#message-input', 'Chat title request');
    element<HTMLButtonElement>('button[type="submit"]').click();
    await settle();
    const call = chat.calls[0];
    expect(call.request).toMatchObject({
      session_id: chatState.activeSessionId(),
      generate_title: true,
    });
    await fill('#message-input', 'Chat next draft');
    await mode('agent');
    const agentId = agentState.selectedId();
    expect(agentId).toBe('agent-1');
    await fill('#agent-request', 'Agent draft during Chat reply');

    chat.event(0, 'start');
    chat.event(0, 'session_title', 'Generated background Chat title');
    await settle();
    expect(chatState.activeSession()).toMatchObject({
      id: call.request.session_id,
      title: 'Generated background Chat title',
      draft: 'Chat next draft',
    });
    expect(JSON.parse(storage.records.get(CHAT_STORAGE_KEY)!).sessions).toContainEqual(
      expect.objectContaining({
        id: call.request.session_id,
        title: 'Generated background Chat title',
      }),
    );
    expect(fixture.componentInstance.turns.activePending()).toBe(true);
    expect(agentState.mode()).toBe('agent');
    expect(root().querySelector('[role="log"][aria-label="Conversation messages"]')).toBeNull();
    expect(root().querySelector('#message-input')).toBeNull();
    expect(root().textContent).not.toContain('Receiving reply');
    expect(root().textContent).not.toContain('Stop reply');
    expect(root().textContent).not.toContain('Retry reply');

    chat.event(0, 'delta', 'Background Chat answer');
    chat.event(0, 'done');
    await settle();
    expect(call.signal.aborted).toBe(false);
    expect(fixture.componentInstance.turns.activePending()).toBe(false);
    expect(agentState.selectedId()).toBe(agentId);
    expect(agentState.draft()).toBe('Agent draft during Chat reply');
    expect(element<HTMLTextAreaElement>('#agent-request').value).toBe(
      'Agent draft during Chat reply',
    );
    expect(root().textContent).not.toContain('Background Chat answer');

    await mode('chat');
    expect(chatState.activeSessionId()).toBe(call.request.session_id);
    expect(
      root().querySelectorAll('[role="log"][aria-label="Conversation messages"]'),
    ).toHaveLength(1);
    const conversation = element('[role="log"][aria-label="Conversation messages"]');
    expect(conversation.querySelectorAll('article')).toHaveLength(2);
    expect(conversation.textContent).toContain('Chat title request');
    expect(conversation.textContent).toContain('Background Chat answer');
    expect(element('#assistant-sidebar button[aria-current="true"]').textContent).toContain(
      'Generated background Chat title',
    );
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Chat next draft');
    await mode('agent');
    expect(agentState.selectedId()).toBe(agentId);
    expect(element<HTMLTextAreaElement>('#agent-request').value).toBe(
      'Agent draft during Chat reply',
    );
  });
  it('keeps the root Agent request alive across mode navigation without review autoscroll', async () => {
    await render();
    const state = TestBed.inject(AgentState);
    await mode('agent');
    await fill('#agent-request', 'Read the fixture');
    await button('Request plan');
    await mode('chat');
    api.event(0, 1, fixtureRun('planning'));
    api.event(0, 2, fixtureRun());
    api.calls[0].resolve();
    await mode('agent');
    expect(api.calls[0].signal.aborted).toBe(false);
    expect(root().textContent).toContain('Approve and run');
    const history = element('[aria-label="Agent run history"]');
    history.scrollTop = 70;
    fixture.componentInstance.sidebarOpen.set(true);
    await settle();
    fixture.componentInstance.onNavigationKeydown(new KeyboardEvent('keydown', { key: 'Escape' }));
    await settle();
    expect(fixture.componentInstance.sidebarOpen()).toBe(false);
    expect(history.scrollTop).toBe(70);
    expect(state.activeRun()!.status).toBe('awaiting_approval');
  });
  it('edits the shared Chat catalogue in Agent Settings without an active Chat prompt-selection context', async () => {
    await render();
    const chatState = TestBed.inject(ChatState);
    const prompt = chatState.prompts()[0];
    chatState.selectPrompt(prompt.id);
    chatState.updateDraft('Lock it');
    chatState.sendMessage();
    const locked = chatState.activeSession().prompt;
    await mode('agent');
    fixture.componentInstance.openSettings();
    await settle();
    const dialog = fixture.debugElement.query(By.directive(SettingsDialog))
      .componentInstance as SettingsDialog;
    expect(dialog.chatSelectionContext()).toBe(false);
    expect(root().querySelector('input[name="settings-system-prompt"]')).toBeNull();
    expect(root().textContent).toContain('No active Chat selection context');
    expect(element('dialog fieldset legend').textContent?.trim()).toBe(
      'Manage Chat system prompts',
    );
    expect(root().textContent).not.toContain('Choose a system prompt for this chat');
    expect(root().textContent).not.toContain(
      'Select a prompt before sending the first message in a chat.',
    );
    dialog.promptSelected.emit(null);
    dialog.promptSaved.emit({
      ...prompt,
      title: 'Edited catalogue',
      content: 'Edited instructions',
    });
    await settle();
    expect(chatState.prompts()[0].title).toBe('Edited catalogue');
    expect(chatState.activeSession().prompt).toEqual(locked);
    expect(TestBed.inject(AgentState).activeSession()).not.toHaveProperty('prompt');
  });
  it('shows truthful Stopping and pending archive states, retaining the history', async () => {
    api.list.mockResolvedValue(fixtureList([fixtureSession(1, [fixtureRun('executing')])]));
    api.get.mockResolvedValue(fixtureSession(1, [fixtureRun('executing')]));
    await render();
    await mode('agent');
    await button('Stop operation');
    expect(root().textContent).toContain('Stopping…');
    expect(element<HTMLSelectElement>('#thinking-input').disabled).toBe(true);
    api.get.mockResolvedValueOnce({
      ...fixtureSession(3, [fixtureRun('cancelling')]),
      archive_pending: true,
    });
    await TestBed.inject(AgentState).refresh();
    await settle();
    expect(root().textContent).toContain('Archive pending');
    expect(root().textContent).toContain('Fixture Agent');
    expect(TestBed.inject(AgentState).sessions()).toHaveLength(1);
  });
  it('confirms before deleting archived sessions and preserves another selection', async () => {
    const archived = { ...fixtureSession(0, [], 'old'), title: 'Old', archived: true };
    api.list.mockResolvedValue(fixtureList([fixtureSession(), archived]));
    await render();
    await mode('agent');
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    element<HTMLButtonElement>('[aria-label="Delete session: Old"]').click();
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Permanently delete'));
    expect(api.delete).not.toHaveBeenCalled();
    element<HTMLButtonElement>('[aria-label="Delete session: Old"]').click();
    await settle();
    expect(api.delete).toHaveBeenCalledOnce();
    expect(api.delete).toHaveBeenCalledWith('old', expect.any(AbortSignal));
    expect(TestBed.inject(AgentState).selectedId()).toBe('agent-1');
    expect(
      TestBed.inject(AgentState)
        .sessions()
        .map((session) => session.id),
    ).toEqual(['agent-1']);
    expect(TestBed.inject(AgentState).total()).toBe(1);
  });
  it('retains selected history on deletion error and clears it only on success', async () => {
    await render();
    await mode('agent');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.delete.mockRejectedValueOnce(new Error('Stop the run first.'));
    element<HTMLButtonElement>('[aria-label="Delete session: Fixture Agent"]').click();
    await settle();
    expect(root().textContent).toContain('Stop the run first.');
    expect(TestBed.inject(AgentState).selectedId()).toBe('agent-1');
    element<HTMLButtonElement>('[aria-label="Delete session: Fixture Agent"]').click();
    await settle();
    expect(TestBed.inject(AgentState).sessions()).toHaveLength(0);
    expect(TestBed.inject(AgentState).selectedId()).toBeNull();
    expect(TestBed.inject(AgentState).currentView()).toBeNull();
  });
  it('keeps a newly selected session when an earlier deletion completes', async () => {
    const other = { ...fixtureSession(0, [], 'other'), title: 'Other' };
    api.list.mockResolvedValue(fixtureList([fixtureSession(), other]));
    await render();
    await mode('agent');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let finish!: () => void;
    api.delete.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    element<HTMLButtonElement>('[aria-label="Delete session: Fixture Agent"]').click();
    fixture.detectChanges();
    expect(
      element<HTMLButtonElement>('[aria-label="Delete session: Fixture Agent"]').disabled,
    ).toBe(true);
    TestBed.inject(AgentState).select('other');
    finish();
    await settle();
    expect(TestBed.inject(AgentState).selectedId()).toBe('other');
    expect(
      TestBed.inject(AgentState)
        .sessions()
        .map((session) => session.id),
    ).toEqual(['other']);
  });
  it('shows actionable API errors rather than a demo plan', async () => {
    await render();
    await mode('agent');
    await fill('#agent-request', 'Read the fixture');
    await button('Request plan');
    api.calls[0].reject(new Error('Planner unavailable. Configure an injected Agent planner.'));
    await settle();
    expect(root().textContent).toContain('Configure an injected Agent planner');
    expect(root().querySelector('[aria-label="Reviewed plan"]')).toBeNull();
    expect(root().textContent).toContain('approved source excerpts');
    expect(api.calls).toHaveLength(1);
  });
  it('rejects review without execution and preserves feedback across mode switches', async () => {
    api.list.mockResolvedValue(fixtureList([fixtureSession(1, [fixtureRun()])]));
    api.get.mockResolvedValue(fixtureSession(1, [fixtureRun()]));
    await render();
    await mode('agent');
    await fill('#feedback-run-1', 'Read just one line');
    await mode('chat');
    await mode('agent');
    expect(element<HTMLTextAreaElement>('#feedback-run-1').value).toBe('Read just one line');
    await button('Reject');
    await vi.waitFor(() =>
      expect(TestBed.inject(AgentState).currentView()?.body.status).toBe('rejected'),
    );
    await settle();
    expect(root().textContent).not.toContain('Approve and run');
    await fill('#agent-request', 'A new request');
    expect(TestBed.inject(AgentState).canRequest()).toBe(true);
    expect(api.reject).toHaveBeenCalledOnce();
    expect(api.calls).toHaveLength(0);
  });
  it('does not steal Chat navigation focus when an Agent creation finishes in the background', async () => {
    await render();
    await mode('agent');
    let finish!: (session: AgentSession) => void;
    api.create.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    fixture.componentInstance.newSession();
    await settle();
    await mode('chat');
    const chatRadio = element<HTMLInputElement>('input[name="interactive-mode"][value="chat"]');
    chatRadio.focus();
    finish(fixtureSession(0, [], 'created-in-background'));
    await settle();
    expect(document.activeElement).toBe(chatRadio);
    expect(TestBed.inject(AgentState).mode()).toBe('chat');
  });
  it.each([
    { selector: '#model-input', attempted: 'model-b', canonical: 'model-a' },
    { selector: '#thinking-input', attempted: 'high', canonical: 'medium' },
  ])(
    'rolls the actual $selector control back to canonical preferences after a failed PATCH',
    async ({ selector, attempted, canonical }) => {
      await render();
      await mode('agent');
      api.patch.mockRejectedValueOnce(new Error('Preference update rejected'));
      const control = element<HTMLSelectElement>(selector);
      control.value = attempted;
      control.dispatchEvent(new Event('change', { bubbles: true }));
      await settle();
      expect(api.patch).toHaveBeenCalledOnce();
      await vi.waitFor(() => expect(control.value).toBe(canonical));
      const state = TestBed.inject(AgentState);
      expect(element<HTMLSelectElement>('#model-input').value).toBe(state.model());
      expect(element<HTMLSelectElement>('#thinking-input').value).toBe(state.effort());
      expect(root().textContent).toContain('Preference update rejected');
      TestBed.inject(ChatState).selectModel('model-b');
      TestBed.inject(ChatState).selectReasoningEffort('high');
      await mode('chat');
      expect(element<HTMLSelectElement>('#model-input').value).toBe('model-b');
      expect(element<HTMLSelectElement>('#thinking-input').value).toBe('high');
      await mode('agent');
      expect(element<HTMLSelectElement>('#model-input').value).toBe(state.model());
      expect(element<HTMLSelectElement>('#thinking-input').value).toBe(state.effort());
      expect(api.patch).toHaveBeenCalledOnce();
      await fill('#agent-request', 'Read the fixture');
      await button('Request plan');
      expect(api.calls[0].request.snapshot).toMatchObject({
        model: 'model-a',
        reasoning_effort: 'medium',
      });
    },
  );
  it('reconciles successful preference acknowledgements and uses those settings for the next request', async () => {
    await render();
    await mode('agent');
    api.patch.mockResolvedValueOnce(fixtureSession(1));
    const model = element<HTMLSelectElement>('#model-input');
    model.value = 'model-b';
    model.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    await vi.waitFor(() => expect(model.value).toBe('model-a'));
    api.patch.mockResolvedValueOnce({ ...fixtureSession(2), model: 'model-b' });
    model.value = 'model-b';
    model.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    api.patch.mockResolvedValueOnce({
      ...fixtureSession(3),
      model: 'model-b',
      reasoning_effort: 'high',
    });
    const effort = element<HTMLSelectElement>('#thinking-input');
    effort.value = 'high';
    effort.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    expect(model.value).toBe(TestBed.inject(AgentState).model());
    expect(effort.value).toBe(TestBed.inject(AgentState).effort());
    await fill('#agent-request', 'Read the fixture');
    await button('Request plan');
    expect(api.calls[0].request.snapshot).toMatchObject({
      model: 'model-b',
      reasoning_effort: 'high',
    });
  });
  it("shows the current mode's acknowledged model in the selector tooltip", async () => {
    await render();
    const model = element<HTMLSelectElement>('#model-input');
    expect(model.value).toBe('model-a');
    expect(model.title).toBe('model-a');
    await mode('agent');
    const agentState = TestBed.inject(AgentState);
    let acknowledge!: (session: AgentSession) => void;
    api.patch.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          acknowledge = resolve;
        }),
    );
    const selection = vi.spyOn(fixture.componentInstance, 'selectModel');
    model.value = 'model-b';
    model.dispatchEvent(new Event('change', { bubbles: true }));
    const pending = selection.mock.results[0].value;
    await settle();
    expect(api.patch).toHaveBeenCalledOnce();
    expect(agentState.model()).toBe('model-a');
    expect(model.title).toBe('model-a');

    acknowledge({ ...fixtureSession(1), model: 'model-b' });
    await pending;
    await settle();
    expect(agentState.model()).toBe('model-b');
    expect(model.value).toBe('model-b');
    expect(model.title).toBe('model-b');
    expect(fixture.componentInstance.turns.selectedModel()).toBe('model-a');

    await mode('chat');
    expect(model.value).toBe('model-a');
    expect(model.title).toBe('model-a');
    await mode('agent');
    expect(model.value).toBe('model-b');
    expect(model.title).toBe('model-b');
  });
  it.each([
    { field: 'model', outcome: 'ack' },
    { field: 'model', outcome: 'failure' },
    { field: 'effort', outcome: 'ack' },
    { field: 'effort', outcome: 'failure' },
  ])(
    'does not mutate an inherited Chat preference when late Agent $field PATCH returns $outcome',
    async ({ field, outcome }) => {
      await render();
      const chatState = TestBed.inject(ChatState);
      const before = structuredClone(chatState.activeSession());
      expect(Object.hasOwn(before, 'model')).toBe(false);
      expect(Object.hasOwn(before, 'reasoningEffort')).toBe(false);
      await mode('agent');
      let resolve!: (session: AgentSession) => void;
      let reject!: (error: Error) => void;
      api.patch.mockImplementationOnce(
        () =>
          new Promise((done, fail) => {
            resolve = done;
            reject = fail;
          }),
      );
      const selection = vi.spyOn(
        fixture.componentInstance,
        field === 'model' ? 'selectModel' : 'selectEffort',
      );
      const control = element<HTMLSelectElement>(
        field === 'model' ? '#model-input' : '#thinking-input',
      );
      control.value = field === 'model' ? 'model-b' : 'high';
      control.dispatchEvent(new Event('change', { bubbles: true }));
      const pending = selection.mock.results[0].value;
      await mode('chat');
      if (outcome === 'ack')
        resolve({ ...fixtureSession(1), model: 'model-b', reasoning_effort: 'high' });
      else reject(new Error('Agent preference rejected after navigation'));
      await pending;
      await settle();
      expect(chatState.activeSession()).toStrictEqual(before);
      expect(selection).toHaveBeenCalledOnce();
      expect(element<HTMLSelectElement>('#model-input').value).toBe('model-a');
      expect(element<HTMLSelectElement>('#thinking-input').value).toBe('medium');
    },
  );
  it('shows clipped file excerpt metadata during execution and after reloading history', async () => {
    await render();
    await mode('agent');
    await fill('#agent-request', 'Read the fixture');
    await button('Request plan');
    api.event(0, 1, fixtureRun('planning'));
    const reviewed = fixtureRun();
    reviewed.plan!.steps[0].arguments = {
      workspace_id: 'public-fixtures',
      path: 'example.md',
      offset: 7,
      limit: 1,
    };
    api.event(0, 2, reviewed);
    api.calls[0].resolve();
    await settle();
    await button('Approve and run');
    const executing = { ...reviewed, status: 'executing' as const, operation_id: 'operation-1' };
    api.event(1, 3, { ...executing, status: 'approved' });
    api.event(1, 4, executing);
    const clipped = {
      step_id: 'step-1',
      status: 'completed' as const,
      content: `7: ${'x'.repeat(1000)}`,
      time: reviewed.updated_at,
      path: 'example.md',
      offset: 7,
      returned_lines: 1,
      truncated: true,
    };
    const result = { ...executing, results: [clipped] };
    for (const event of fixtureStepEvents(api.calls[1].request, 5, result, clipped))
      api.calls[1].emit(event);
    await settle();
    const assertExcerpt = () => {
      const card = element('[aria-label="Step result"]');
      expect(card.textContent).toContain('Recorded file excerpt');
      expect(card.textContent).toContain('example.md');
      expect(card.textContent).toContain('lines 7–7');
      expect(card.textContent).toContain('Returned lines: 1');
      expect(card.textContent).toContain('Partial/truncated excerpt');
      expect(card.textContent).toContain('long lines were omitted');
      expect(card.textContent).toContain('Do not treat this as the complete file');
      expect(card.textContent).toContain(clipped.content);
    };
    assertExcerpt();
    const completed = {
      ...result,
      status: 'completed' as const,
      operation_id: null,
      answer: 'Only the clipped excerpt was read.',
    };
    api.event(1, 7, completed);
    api.calls[1].resolve();
    await settle();
    const stored = fixtureSession(7, [completed]);
    api.list.mockResolvedValue(fixtureList([stored]));
    api.get.mockResolvedValue(stored);
    TestBed.resetTestingModule();
    await render();
    assertExcerpt();
    expect(api.calls).toHaveLength(2);
  });
  it('reloads the shared model catalogue from restored Agent mode and then creates a session', async () => {
    storage.records.set(
      AGENT_UI_STORAGE_KEY,
      JSON.stringify({ version: 1, mode: 'agent', selectedId: null, legacyId: null, drafts: {} }),
    );
    api.list.mockResolvedValueOnce(fixtureList([]));
    const models = vi
      .spyOn(chat, 'models')
      .mockRejectedValueOnce(new Error('Model catalogue unavailable'));
    await render();
    expect(root().textContent).toContain('Model catalogue unavailable');
    expect(element<HTMLInputElement>('input[name="interactive-mode"][value="agent"]').checked).toBe(
      true,
    );
    await button('Reload models');
    expect(models).toHaveBeenCalledTimes(2);
    expect(root().textContent).not.toContain('Model catalogue unavailable');
    element<HTMLButtonElement>('[aria-label="New session"]').click();
    await settle();
    expect(api.create).toHaveBeenCalledWith(
      expect.any(String),
      'model-a',
      'medium',
      expect.any(AbortSignal),
    );
    expect(root().querySelector('#agent-request')).not.toBeNull();
    expect(element<HTMLInputElement>('input[name="interactive-mode"][value="agent"]').checked).toBe(
      true,
    );
  });
  it.each(['legacy', 'archived'] as const)(
    'moves mobile drawer selection focus to the read-only %s destination',
    async (kind) => {
      const originalWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth');
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 375 });
      try {
        const archived = {
          ...fixtureSession(1, [fixtureRun('rejected')], 'archived-1'),
          archived: true,
          title: 'Archived readable',
        };
        api.list.mockResolvedValueOnce(fixtureList([fixtureSession(), archived]));
        api.get.mockImplementation(async (sid) =>
          sid === archived.id ? archived : fixtureSession(),
        );
        await render();
        if (kind === 'legacy')
          TestBed.inject(ChatState).sessions.update((sessions) => [
            ...sessions,
            {
              id: 'legacy-focus',
              title: 'Legacy readable',
              mode: 'agent',
              messages: [],
              draft: '',
              prompt: null,
              promptLocked: false,
            },
          ]);
        await mode('agent');
        element<HTMLButtonElement>('[aria-label="Open sidebar"]').click();
        await settle();
        const title = kind === 'legacy' ? 'Legacy readable' : archived.title;
        const selection = [
          ...root().querySelectorAll<HTMLButtonElement>(
            '[aria-label="Agent session history"] button',
          ),
        ].find((candidate) => candidate.textContent?.includes(title))!;
        selection.focus();
        selection.click();
        await settle();
        expect(fixture.componentInstance.sidebarOpen()).toBe(false);
        expect(document.activeElement).toBe(element('app-agent-workspace h1'));
        expect(document.activeElement?.textContent).toBe(title);
        expect(root().querySelector('#agent-request')).toBeNull();
      } finally {
        if (originalWidth) Object.defineProperty(window, 'innerWidth', originalWidth);
      }
    },
  );
  it('does not move newer Settings navigation focus when a slow creation selects its new Agent session', async () => {
    await render();
    await mode('agent');
    let finish!: (session: AgentSession) => void;
    api.create.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    fixture.componentInstance.newSession();
    await settle();
    fixture.componentInstance.openSettings();
    await settle();
    const closeSettings = element<HTMLButtonElement>('[aria-label="Close settings"]');
    closeSettings.focus();
    finish(fixtureSession(0, [], 'created-after-settings'));
    await settle();
    expect(TestBed.inject(AgentState).selectedId()).toBe('created-after-settings');
    expect(document.activeElement).toBe(closeSettings);
  });
  it('renders deferred clarification as read-only compatibility history without offering unsupported commands', async () => {
    const session = fixtureSession(1, [
      {
        ...fixtureRun('awaiting_clarification'),
        plan: null,
        clarification: { id: 'q1', questions: ['Which workspace?'] },
      },
    ]);
    api.list.mockResolvedValueOnce(fixtureList([session]));
    api.get.mockResolvedValueOnce(session);
    await render();
    await mode('agent');
    expect(root().textContent).toContain('Which workspace?');
    expect(root().textContent).toContain('including replies and rejection');
    expect(root().textContent).not.toContain('Reject clarification');
    expect(root().querySelector('[aria-label="Reviewed plan"]')).toBeNull();
    expect(api.reject).not.toHaveBeenCalled();
  });
  it('preserves legacy status, metadata, drafts and prompt references through every Chat mutation route', async () => {
    const legacy = {
      id: 'legacy-1',
      title: 'Legacy',
      type: 'Agent',
      selected: true,
      selectedSystemPromptId: 'p1',
      systemPromptSnapshot: null,
      systemPromptLocked: false,
      model: 'old-model',
      reasoningEffort: 'high',
      items: [
        {
          id: 'old-answer',
          time: '2026-01-01T01:02:03+08:00',
          role: 'ai',
          type: 'Message',
          msg: '**old**',
          status: 'streaming',
          requestId: 'old-request',
          replyTo: 'old-user',
        },
      ],
    };
    storage.records.set(
      CHAT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sessions: [legacy],
        systemPrompts: [{ id: 'p1', title: 'Original', content: 'Original' }],
        drafts: { 'legacy-1': 'legacy draft' },
      }),
    );
    await render();
    const state = TestBed.inject(ChatState);
    const fallback = state.activeSessionId();
    state.savePrompt({ id: 'p1', title: 'Changed', content: 'Changed' });
    state.deletePrompt('p1');
    state.updateSession('legacy-1', (entry) => ({ ...entry, draft: 'overwritten' }));
    state.deleteSession('legacy-1');
    state.selectSession('legacy-1');
    expect(state.activeSessionId()).toBe(fallback);
    state.activeSessionId.set('legacy-1');
    state.updateDraft('overwrite');
    state.selectModel('model-a');
    state.selectReasoningEffort('low');
    state.selectPrompt(null);
    expect(state.sendMessage()).toBe(false);
    expect(fixture.componentInstance.turns.send()).toBe(false);
    expect(fixture.componentInstance.turns.retry()).toBe(false);
    fixture.componentInstance.turns.deleteSession('legacy-1');
    state.activeSessionId.set(fallback);
    await settle();
    const saved = JSON.parse(storage.records.get(CHAT_STORAGE_KEY)!);
    expect(saved.sessions.find((entry: { id: string }) => entry.id === 'legacy-1')).toEqual({
      ...legacy,
      selected: false,
    });
    expect(saved.drafts['legacy-1']).toBe('legacy draft');
    await mode('agent');
    TestBed.inject(AgentState).selectLegacy('legacy-1');
    await settle();
    expect(root().textContent).toContain('Read-only legacy browser record');
    expect(root().textContent).toContain('legacy draft');
    expect(root().querySelector('#agent-request')).toBeNull();
    expect(chat.calls).toHaveLength(0);
    expect(api.calls).toHaveLength(0);
  });
});
