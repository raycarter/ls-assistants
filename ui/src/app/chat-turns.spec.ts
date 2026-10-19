import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatApi, ChatApiError } from './chat-api';
import { FakeChatApi } from './chat-api.testing';
import { CHAT_STORAGE, CHAT_STORAGE_KEY, ChatState } from './chat-state';
import { ChatTurns } from './chat-turns';
import type { StoredChatState } from './chat.models';

describe('ChatTurns', () => {
  let api: FakeChatApi;
  let state: ChatState;
  let turns: ChatTurns;
  let records: Map<string, string>;
  let setItem: ReturnType<typeof vi.fn>;

  async function create(): Promise<void> {
    TestBed.configureTestingModule({
      providers: [
        { provide: ChatApi, useValue: api },
        {
          provide: CHAT_STORAGE,
          useValue: { getItem: (key: string) => records.get(key) ?? null, setItem },
        },
      ],
    });
    state = TestBed.inject(ChatState);
    turns = TestBed.inject(ChatTurns);
    await Promise.resolve();
    TestBed.tick();
  }

  beforeEach(async () => {
    vi.useFakeTimers();
    api = new FakeChatApi();
    records = new Map();
    setItem = vi.fn((key: string, value: string) => records.set(key, value));
    await create();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function send(text = 'Question'): string {
    state.updateDraft(text);
    expect(turns.send()).toBe(true);
    return state.activeSessionId();
  }

  function stored(): StoredChatState {
    return JSON.parse(records.get(CHAT_STORAGE_KEY)!);
  }

  it('defaults to API model and medium without requiring new fields in old records', () => {
    expect(turns.selectedModel()).toBe('model-a');
    expect(turns.selectedEffort()).toBe('medium');
    expect(state.activeSession().model).toBeUndefined();
    expect(state.activeSession().reasoningEffort).toBeUndefined();
  });

  it('captures model, effort, locked prompt and ordered visible history once', () => {
    const prompt = { ...state.prompts()[0] };
    state.selectPrompt(prompt.id);
    state.selectModel('model-b');
    state.selectReasoningEffort('xhigh');
    send(' First question ');
    const request = structuredClone(api.calls[0].request);
    state.updateDraft('Typed while receiving');
    state.selectModel('model-a');
    state.selectReasoningEffort('low');
    state.savePrompt({ ...prompt, content: 'Edited catalogue' });
    state.newSession();
    expect(api.calls[0].request).toEqual(request);
    expect(request).toEqual({
      session_id: expect.any(String),
      request_id: expect.any(String),
      model: 'model-b',
      reasoning_effort: 'xhigh',
      system_prompt: prompt.content,
      messages: [{ role: 'user', content: 'First question' }],
      generate_title: true,
    });
  });

  it('updates and persists the generated title while still waiting for the answer', () => {
    send('Please help me investigate a Kubernetes networking problem');
    api.event(0, 'start');
    api.event(0, 'session_title', 'Kubernetes network troubleshooting');
    expect(state.activeSession().title).toBe('Kubernetes network troubleshooting');
    expect(stored().sessions[0].title).toBe('Kubernetes network troubleshooting');
    expect(turns.activePending()).toBe(true);
    expect(state.activeSession().messages).toHaveLength(2);
    expect(state.activeSession().messages[1]).toMatchObject({
      content: '',
      status: 'streaming',
    });
    api.event(0, 'delta', 'Check your network policies.');
    api.event(0, 'done');
    expect(turns.activePending()).toBe(false);
    expect(state.activeSession().title).toBe('Kubernetes network troubleshooting');
    expect(state.activeSession().messages[1].content).toBe('Check your network policies.');

    send('What next?');
    expect(api.calls[1].request.generate_title).toBe(false);
    expect(state.activeSession().title).toBe('Kubernetes network troubleshooting');
  });

  it('applies title metadata only to its originating session without changing selection', () => {
    const first = send('First question');
    state.newSession();
    const second = state.activeSessionId();
    api.event(0, 'start');
    api.event(0, 'session_title', 'First conversation title');
    expect(state.activeSessionId()).toBe(second);
    expect(state.activeSession().title).toBe('New session');
    expect(state.sessions().find((session) => session.id === first)?.title).toBe(
      'First conversation title',
    );
    expect(turns.pending().has(first)).toBe(true);
  });

  it('creates one bubble with the server ID/time and batches deltas until terminal flush', () => {
    send();
    api.event(0, 'start');
    const first = state.activeSession().messages[1];
    expect(first).toMatchObject({
      id: 'reply-0',
      time: '2026-10-13T12:00:00.000Z',
      status: 'streaming',
      content: '',
    });
    for (const text of ['## Hello', ' 世界', '\n\n**partial', '**']) api.event(0, 'delta', text);
    expect(state.activeSession().messages[1]).toBe(first);
    vi.advanceTimersByTime(32);
    expect(state.activeSession().messages[1].content).toBe('## Hello 世界\n\n**partial**');
    api.event(0, 'delta', ' final');
    api.event(0, 'done');
    expect(state.activeSession().messages).toHaveLength(2);
    expect(state.activeSession().messages[1]).toMatchObject({
      id: first.id,
      time: first.time,
      status: 'complete',
      content: '## Hello 世界\n\n**partial** final',
    });
    expect(turns.activePending()).toBe(false);
    expect(stored().sessions[0].items[1].msg).toBe('## Hello 世界\n\n**partial** final');
  });

  it('preserves send and reply-creation times through deltas, later completion and hydration', async () => {
    const sentAt = '2026-10-13T11:59:58.123Z';
    const replyCreatedAt = '2026-10-13T20:00:00.456+08:00';
    const times = [sentAt, replyCreatedAt];
    vi.setSystemTime(new Date(sentAt));
    send();
    TestBed.tick();
    expect(state.activeSession().messages[0].time).toBe(sentAt);
    expect(stored().sessions[0].items[0].time).toBe(sentAt);

    vi.advanceTimersByTime(3_000);
    api.event(0, 'start', '', { time: replyCreatedAt });
    api.event(0, 'delta', 'Partial');
    vi.advanceTimersByTime(32);
    TestBed.tick();
    vi.advanceTimersByTime(200);
    expect(state.activeSession().messages.map((message) => message.time)).toEqual(times);
    expect(state.activeSession().messages[1]).toMatchObject({
      content: 'Partial',
      status: 'streaming',
    });
    expect(stored().sessions[0].items.map((message) => message.time)).toEqual(times);

    vi.advanceTimersByTime(60_000);
    api.event(0, 'delta', ' answer');
    api.event(0, 'done', '', { time: replyCreatedAt });
    expect(state.activeSession().messages.map((message) => message.time)).toEqual(times);
    expect(state.activeSession().messages[1]).toMatchObject({
      content: 'Partial answer',
      status: 'complete',
    });
    expect(stored().sessions[0].items.map((message) => message.time)).toEqual(times);

    const completed = structuredClone(state.activeSession().messages);
    TestBed.resetTestingModule();
    api = new FakeChatApi();
    await create();
    expect(state.activeSession().messages).toEqual(completed);
    expect(turns.activePending()).toBe(false);
    expect(api.calls).toHaveLength(0);
  });

  it('blocks repeated sends even when a new draft is typed while receiving', () => {
    send();
    state.updateDraft('Next question');
    expect(turns.canSend()).toBe(false);
    expect(turns.send()).toBe(false);
    expect(api.calls).toHaveLength(1);
    api.event(0, 'start');
    api.event(0, 'done');
    expect(state.activeSession().draft).toBe('Next question');
    expect(turns.send()).toBe(true);
    expect(api.calls[1].request.messages).toEqual([
      { role: 'user', content: 'Question' },
      { role: 'user', content: 'Next question' },
    ]);
  });

  it('allows independent in-flight sessions and never activates the origin on callbacks', () => {
    const firstId = send('First');
    state.updateDraft('First draft');
    state.newSession();
    const secondId = send('Second');
    state.updateDraft('Second draft');
    api.event(0, 'start');
    api.event(0, 'delta', 'First reply');
    api.event(0, 'done');
    expect(state.activeSessionId()).toBe(secondId);
    expect(turns.activePending()).toBe(true);
    expect(state.sessions().find((session) => session.id === firstId)).toMatchObject({
      draft: 'First draft',
      messages: [{ content: 'First' }, { content: 'First reply' }],
    });
    api.event(1, 'start');
    api.event(1, 'delta', 'Second reply');
    state.selectSession(firstId);
    api.event(1, 'done');
    expect(state.activeSessionId()).toBe(firstId);
    expect(state.sessions().find((session) => session.id === secondId)).toMatchObject({
      draft: 'Second draft',
      messages: [{ content: 'Second' }, { content: 'Second reply' }],
    });
    expect(turns.pending().size).toBe(0);
  });

  it.each(['before start', 'during reply'])(
    'deletion aborts %s and ignores buffered/late callbacks',
    async (phase) => {
      const id = send();
      if (phase === 'during reply') {
        api.event(0, 'start');
        api.event(0, 'delta', 'Buffered');
      }
      turns.deleteSession(id);
      const remaining = state.sessions();
      expect(api.calls[0].signal.aborted).toBe(true);
      api.event(0, 'start');
      api.event(0, 'session_title', 'Late title');
      api.event(0, 'delta', 'Late');
      api.calls[0].reject(new Error('Late rejection'));
      await Promise.resolve();
      vi.advanceTimersByTime(300);
      expect(state.sessions()).toBe(remaining);
      expect(state.sessions().some((session) => session.id === id)).toBe(false);
      expect(turns.pending().size).toBe(0);
      expect(turns.errors().size).toBe(0);
    },
  );

  it('also aborts requests deleted directly through durable state', () => {
    const id = send();
    state.deleteSession(id);
    api.event(0, 'start');
    expect(state.activeSession().messages).toEqual([]);
    TestBed.tick();
    expect(api.calls[0].signal.aborted).toBe(true);
    expect(turns.pending().size).toBe(0);
  });

  it('preserves partial text and exposes unsupported reasoning errors', () => {
    send();
    api.event(0, 'start');
    api.event(0, 'delta', '**Partial answer');
    api.event(0, 'error');
    const reply = state.activeSession().messages[1];
    expect(reply).toMatchObject({
      content: '**Partial answer',
      status: 'error',
      error: { code: 'unsupported_reasoning', message: 'This model does not support xhigh.' },
    });
    expect(turns.errors().get(state.activeSessionId())).toBe(reply.error?.message);
    expect(turns.retryMessage()?.id).toBe(reply.id);
    expect(stored().sessions[0].items[1]).toMatchObject({
      role: 'ai',
      msg: reply.content,
      status: 'error',
      error: reply.error,
    });
  });

  it('uses a local creation time for an error before start with no timestamp', () => {
    vi.setSystemTime(new Date('2026-10-13T12:34:56.000Z'));
    send();
    api.calls[0].emit({
      type: 'error',
      session_id: api.calls[0].request.session_id,
      request_id: api.calls[0].request.request_id,
      message_id: 'early-error',
      code: 'unsupported_reasoning',
      message: 'This deployment does not support xhigh.',
    });
    api.calls[0].resolve();
    expect(state.activeSession().messages[1]).toMatchObject({
      id: 'early-error',
      time: '2026-10-13T12:34:56.000Z',
      status: 'error',
      error: { code: 'unsupported_reasoning', message: 'This deployment does not support xhigh.' },
    });
    expect(turns.activePending()).toBe(false);
    expect(stored().sessions[0].items[1].time).toBe('2026-10-13T12:34:56.000Z');
  });

  it.each([
    new ChatApiError('http_422', 'Request failed (HTTP 422). Invalid model'),
    new Error('Connection lost'),
  ])('records pre-start failures without losing the submitted user %#', async (error) => {
    send();
    api.calls[0].reject(error);
    await Promise.resolve();
    expect(state.activeSession().messages).toHaveLength(2);
    expect(state.activeSession().messages[1]).toMatchObject({
      status: 'error',
      content: '',
      error: { message: error.message },
    });
    expect(turns.activePending()).toBe(false);
    expect(turns.retryMessage()).not.toBeNull();
  });

  it('retries with fresh request ID, no failed bubble, and the user exactly once; preserves draft', () => {
    send();
    const firstRequest = api.calls[0].request;
    const user = state.activeSession().messages[0];
    api.event(0, 'start');
    api.event(0, 'session_title', 'Stable title');
    api.event(0, 'delta', 'Failed partial');
    api.event(0, 'error');
    state.updateDraft('Unsent next message');
    state.selectReasoningEffort('low');
    expect(turns.retry()).toBe(true);
    expect(turns.retry()).toBe(false);
    const second = api.calls[1].request;
    expect(second.request_id).not.toBe(firstRequest.request_id);
    expect(second.messages).toEqual([{ role: 'user', content: 'Question' }]);
    expect(second.reasoning_effort).toBe('low');
    expect(second.generate_title).toBe(false);
    expect(state.activeSession().title).toBe('Stable title');
    expect(state.activeSession().messages).toEqual([user]);
    api.event(0, 'delta', 'stale');
    api.event(0, 'session_title', 'Stale title');
    expect(state.activeSession().title).toBe('Stable title');
    api.event(1, 'start');
    api.event(1, 'delta', 'Successful answer');
    api.event(1, 'done');
    expect(state.activeSession().messages).toMatchObject([
      { id: user.id },
      { content: 'Successful answer', status: 'complete', replyTo: user.id },
    ]);
    expect(state.activeSession().draft).toBe('Unsent next message');
    expect(turns.errors().size).toBe(0);
  });

  it('excludes failed assistant text from later stateless history', () => {
    send('First');
    api.event(0, 'start');
    api.event(0, 'delta', 'Failed text');
    api.event(0, 'error');
    send('Second');
    expect(api.calls[1].request.messages).toEqual([
      { role: 'user', content: 'First' },
      { role: 'user', content: 'Second' },
    ]);
    expect(turns.retryMessage()).toBeNull();
  });

  it('filters empty completed and legacy messages only from outbound send/retry history', () => {
    state.updateSession(state.activeSessionId(), (session) => ({
      ...session,
      messages: [
        { id: 'empty-user', role: 'user', content: '' },
        { id: 'previous-user', role: 'user', content: 'Previous question' },
        { id: 'empty-complete', role: 'assistant', content: '', status: 'complete' },
        { id: 'empty-legacy', role: 'assistant', content: '' },
        { id: 'previous-answer', role: 'assistant', content: 'Previous answer' },
      ],
    }));
    const previous = [...state.activeSession().messages];
    send('Next question');
    const expected = [
      { role: 'user', content: 'Previous question' },
      { role: 'assistant', content: 'Previous answer' },
      { role: 'user', content: 'Next question' },
    ];
    expect(api.calls[0].request.messages).toEqual(expected);
    api.event(0, 'start');
    api.event(0, 'error');
    expect(state.activeSession().messages.slice(0, previous.length)).toEqual(previous);
    expect(stored().sessions[0].items.filter((message) => message.msg === '')).toHaveLength(4);
    expect(turns.retry()).toBe(true);
    expect(api.calls[1].request.messages).toEqual(expected);
    expect(state.activeSession().messages.slice(0, previous.length)).toEqual(previous);
  });

  it.each(['before start', 'after partial'])(
    'Stop %s preserves text, aborts and enables retry',
    (phase) => {
      send();
      if (phase === 'after partial') {
        api.event(0, 'start');
        api.event(0, 'delta', 'Keep this partial');
      }
      turns.stop();
      const messages = state.activeSession().messages;
      expect(api.calls[0].signal.aborted).toBe(true);
      expect(messages[1]).toMatchObject({
        status: 'stopped',
        content: phase === 'after partial' ? 'Keep this partial' : '',
      });
      api.event(0, 'done');
      api.event(0, 'session_title', 'Ignored title after Stop');
      expect(state.activeSession().messages).toBe(messages);
      expect(state.activeSession().title).toBe('Question');
      expect(turns.retry()).toBe(true);
    },
  );

  it('ignores duplicate and stale terminal callbacks without changing completed state', () => {
    send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Answer');
    api.event(0, 'done');
    const messages = state.activeSession().messages;
    api.event(0, 'error');
    api.event(0, 'done');
    api.event(0, 'delta', 'late');
    vi.advanceTimersByTime(300);
    expect(state.activeSession().messages).toBe(messages);
    expect(turns.errors().size).toBe(0);
  });

  it('guards correlation at the state boundary even for an injected API', () => {
    send();
    expect(() => api.event(0, 'start', '', { session_id: 'wrong' })).toThrow(/invalid/);
    expect(state.activeSession().messages).toHaveLength(1);
  });

  it('rejects a server message ID colliding with a saved user ID', () => {
    send();
    const id = state.activeSession().messages[0].id;
    expect(() => api.event(0, 'start', '', { message_id: id })).toThrow(/already used/);
    expect(state.activeSession().messages).toHaveLength(1);
  });

  it('batches encoding/stringification, with terminal persistence immediate and no duplicate effect write', () => {
    send();
    api.event(0, 'start');
    TestBed.tick();
    setItem.mockClear();
    const stringify = vi.spyOn(JSON, 'stringify');
    for (let i = 0; i < 5; i++) {
      api.event(0, 'delta', 'token');
      vi.advanceTimersByTime(32);
      TestBed.tick();
    }
    expect(stringify).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
    vi.advanceTimersByTime(40);
    expect(stringify).toHaveBeenCalledTimes(1);
    expect(setItem).toHaveBeenCalledTimes(1);
    api.event(0, 'delta', 'terminal');
    api.event(0, 'done');
    expect(stringify).toHaveBeenCalledTimes(2);
    expect(stored().sessions[0].items[1].status).toBe('complete');
    TestBed.tick();
    vi.advanceTimersByTime(500);
    expect(setItem).toHaveBeenCalledTimes(2);
  });

  it('restores preferences, server metadata and errors without restarting a request', async () => {
    state.selectModel('model-b');
    state.selectReasoningEffort('high');
    send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Partial 世界');
    api.event(0, 'error');
    const reply = structuredClone(state.activeSession().messages[1]);
    TestBed.resetTestingModule();
    api = new FakeChatApi();
    await create();
    expect(turns.selectedModel()).toBe('model-b');
    expect(turns.selectedEffort()).toBe('high');
    expect(state.activeSession().messages[1]).toEqual(reply);
    expect(turns.pending().size).toBe(0);
    expect(turns.errors().size).toBe(0);
    expect(turns.retryMessage()).not.toBeNull();
    expect(api.calls).toHaveLength(0);
    expect(turns.retry()).toBe(true);
    expect(api.calls[0].request.messages).toEqual([{ role: 'user', content: 'Question' }]);
  });

  it('normalizes a saved streaming reply to interrupted with partial text and retry linkage', async () => {
    send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Interrupted partial');
    vi.advanceTimersByTime(32);
    state.flushPersistence();
    const raw = records.get(CHAT_STORAGE_KEY)!;
    TestBed.resetTestingModule();
    records.set(CHAT_STORAGE_KEY, raw);
    api = new FakeChatApi();
    await create();
    expect(state.activeSession().messages[1]).toMatchObject({
      status: 'interrupted',
      content: 'Interrupted partial',
      replyTo: state.activeSession().messages[0].id,
    });
    expect(turns.activePending()).toBe(false);
    expect(api.calls).toHaveLength(0);
    expect(turns.retry()).toBe(true);
  });

  it('retries a trailing unanswered user after reload before start without appending it again', async () => {
    state.selectPrompt(state.prompts()[0].id);
    const lockedPrompt = { ...state.activeSession().prompt! };
    send('Previous question');
    api.event(0, 'start');
    api.event(0, 'delta', 'Previous answer');
    api.event(0, 'done');
    send('Unanswered question');
    const firstRequest = api.calls[1].request;
    const userId = state.activeSession().messages.at(-1)!.id;
    state.updateDraft('Preserved next draft');
    TestBed.tick();
    const raw = records.get(CHAT_STORAGE_KEY)!;
    expect(stored().sessions[0].items.at(-1)).toMatchObject({ role: 'user', id: userId });
    TestBed.resetTestingModule();
    records.set(CHAT_STORAGE_KEY, raw);
    api = new FakeChatApi();
    await create();
    expect(api.calls).toHaveLength(0);
    expect(turns.activePending()).toBe(false);
    expect(turns.retryMessage()).toMatchObject({ role: 'user', id: userId });
    const user = state.activeSession().messages.at(-1)!;
    expect(state.activeSession().promptLocked).toBe(true);
    state.selectPrompt(null);
    state.savePrompt({ ...lockedPrompt, content: 'Changed catalogue' });
    expect(turns.retry()).toBe(true);
    expect(turns.retry()).toBe(false);
    expect(api.calls[0].request).toMatchObject({
      system_prompt: lockedPrompt.content,
      messages: [
        { role: 'user', content: 'Previous question' },
        { role: 'assistant', content: 'Previous answer' },
        { role: 'user', content: 'Unanswered question' },
      ],
    });
    expect(api.calls[0].request.request_id).not.toBe(firstRequest.request_id);
    expect(state.activeSession().messages.at(-1)).toBe(user);
    api.event(0, 'start', '', { message_id: 'recovered-reply' });
    api.event(0, 'delta', 'Recovered answer', { message_id: 'recovered-reply' });
    api.event(0, 'done', '', { message_id: 'recovered-reply' });
    expect(state.activeSession().messages.filter((message) => message.id === userId)).toHaveLength(
      1,
    );
    expect(state.activeSession().messages.at(-1)).toMatchObject({
      replyTo: userId,
      content: 'Recovered answer',
      status: 'complete',
    });
    expect(state.activeSession().draft).toBe('Preserved next draft');
    expect(state.activeSession().prompt).toEqual(lockedPrompt);
    expect(state.activeSession().promptLocked).toBe(true);
  });

  it('does not offer retry for an empty trailing legacy user message', () => {
    state.updateSession(state.activeSessionId(), (session) => ({
      ...session,
      messages: [{ id: 'empty-user', role: 'user', content: '' }],
    }));
    expect(turns.retryMessage()).toBeNull();
    expect(turns.retry()).toBe(false);
    expect(api.calls).toHaveLength(0);
  });

  it('preserves an unavailable model preference and saved history until the user selects a model', async () => {
    send();
    api.event(0, 'start');
    api.event(0, 'done');
    state.selectModel('retired-deployment');
    TestBed.tick();
    TestBed.resetTestingModule();
    api = new FakeChatApi();
    await create();
    expect(turns.selectedModel()).toBe('retired-deployment');
    expect(state.activeSession().messages).toHaveLength(2);
    expect(state.persistenceError()).toBeNull();
    state.updateDraft('Next');
    expect(turns.send()).toBe(false);
    state.selectModel('model-b');
    expect(turns.send()).toBe(true);
  });

  it('aborts all live requests on destruction and persists stopped partials', () => {
    send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Partial');
    TestBed.resetTestingModule();
    expect(api.calls[0].signal.aborted).toBe(true);
    expect(stored().sessions[0].items[1]).toMatchObject({ status: 'stopped', msg: 'Partial' });
  });
});
