import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './app';
import { ChatApi, FETCH } from './chat-api';
import { FakeChatApi } from './chat-api.testing';
import { CHAT_STORAGE, CHAT_STORAGE_KEY, ChatState } from './chat-state';

describe('App streaming integration', () => {
  let fixture: ComponentFixture<App>;
  let api: FakeChatApi;
  let state: ChatState;
  let records: Map<string, string>;
  let observers: {
    callback: ResizeObserverCallback;
    observe: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  }[];

  beforeEach(async () => {
    api = new FakeChatApi();
    observers = [];
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe = vi.fn();
        disconnect = vi.fn();
        unobserve = vi.fn();
        constructor(readonly callback: ResizeObserverCallback) {
          observers.push(this);
        }
      },
    );
    records = new Map<string, string>();
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        { provide: ChatApi, useValue: api },
        {
          provide: CHAT_STORAGE,
          useValue: {
            getItem: (key: string) => records.get(key) ?? null,
            setItem: (key: string, value: string) => records.set(key, value),
          },
        },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(App);
    state = TestBed.inject(ChatState);
    await settle();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function root(): HTMLElement {
    return fixture.nativeElement;
  }
  function element<T extends HTMLElement>(selector: string): T {
    return root().querySelector<T>(selector)!;
  }
  function button(label: string): HTMLButtonElement {
    return [...root().querySelectorAll<HTMLButtonElement>('button')].find((candidate) => {
      if (candidate.getAttribute('aria-label') === label) return true;
      const content = candidate.cloneNode(true) as HTMLElement;
      content.querySelectorAll('[aria-hidden="true"]').forEach((hidden) => hidden.remove());
      return content.textContent?.replace(/\s+/g, ' ').trim() === label;
    })!;
  }
  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }
  async function draft(value: string): Promise<void> {
    const input = element<HTMLTextAreaElement>('#message-input');
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await settle();
  }
  async function select(selector: string, value: string): Promise<void> {
    const input = element<HTMLSelectElement>(selector);
    input.value = value;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
  }
  async function send(): Promise<void> {
    await draft('Question');
    button('Send').click();
    await settle();
  }

  it('shows header model and effort selectors, with medium default', async () => {
    const models = element<HTMLSelectElement>('#model-input');
    const thinking = element<HTMLSelectElement>('#thinking-input');
    expect([...models.options].map((option) => option.value)).toEqual(['model-a', 'model-b']);
    expect(models.value).toBe('model-a');
    expect(thinking.value).toBe('medium');
    expect([...thinking.options].map((option) => [option.value, option.disabled])).toEqual([
      ['low', false],
      ['medium', false],
      ['high', false],
      ['xhigh', false],
    ]);
    const header = element('main > header');
    const composerForm = element<HTMLTextAreaElement>('#message-input').form;
    expect(header.contains(models)).toBe(true);
    expect(header.contains(thinking)).toBe(true);
    expect(models.previousElementSibling).toBe(header.querySelector('label[for="model-input"]'));
    expect(thinking.previousElementSibling).toBe(
      header.querySelector('label[for="thinking-input"]'),
    );
    expect(models.form).toBeNull();
    expect(thinking.form).toBeNull();
    expect(composerForm).not.toBeNull();
    expect(button('Send').form).toBe(composerForm);
    await select('#model-input', 'model-b');
    await select('#thinking-input', 'xhigh');
    await send();
    expect(api.calls[0].request).toMatchObject({ model: 'model-b', reasoning_effort: 'xhigh' });
    expect([...thinking.options].every((option) => !option.disabled)).toBe(true);
  });

  it('restores per-session controls on switching, including unavailable models', async () => {
    const first = state.activeSessionId();
    await select('#model-input', 'model-b');
    await select('#thinking-input', 'high');
    fixture.componentInstance.newSession();
    await settle();
    expect(element<HTMLSelectElement>('#model-input').value).toBe('model-a');
    expect(element<HTMLSelectElement>('#thinking-input').value).toBe('medium');
    fixture.componentInstance.selectSession(first);
    await settle();
    expect(element<HTMLSelectElement>('#model-input').value).toBe('model-b');
    expect(element<HTMLSelectElement>('#thinking-input').value).toBe('high');
    state.selectModel('removed');
    await draft('Preserve history');
    expect(element<HTMLSelectElement>('#model-input').selectedOptions[0].textContent).toContain(
      'removed (unavailable)',
    );
    expect(button('Send').disabled).toBe(true);
    await select('#model-input', 'model-a');
    expect(button('Send').disabled).toBe(false);
  });

  it('guards both form and Enter while pending and keeps the textarea editable with its draft', async () => {
    await send();
    await draft('Next typed draft');
    expect(button('Send').disabled).toBe(true);
    expect(element<HTMLTextAreaElement>('#message-input').disabled).toBe(false);
    expect(button('Stop reply')).toBeTruthy();
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    element('#message-input').dispatchEvent(enter);
    element('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await settle();
    expect(enter.defaultPrevented).toBe(true);
    expect(api.calls).toHaveLength(1);
    api.event(0, 'start');
    api.event(0, 'delta', 'Reply');
    api.event(0, 'done');
    await settle();
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Next typed draft');
    expect(button('Send').disabled).toBe(false);
    expect(root().textContent).not.toContain('Receiving reply');
  });

  it('displays and persists user and reply timestamps while streaming', async () => {
    await send();
    const userTime = state.activeSession().messages[0].time!;
    const sent = element<HTMLTimeElement>('[role="log"] article:first-child time');
    expect(sent.dateTime).toBe(userTime);
    expect(sent.textContent?.trim()).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);

    const replyTime = new Date(2026, 0, 2, 3, 4, 6).toISOString();
    api.event(0, 'start', '', { time: replyTime });
    await settle();
    const received = element<HTMLTimeElement>('[role="log"] article:last-child time');
    expect(received.dateTime).toBe(replyTime);
    expect(received.textContent?.trim()).toBe('2026-01-02 03:04:06');

    api.event(0, 'delta', 'Timestamped answer');
    api.event(0, 'done', '', { time: replyTime });
    await settle();
    expect(element('[role="log"] article:last-child time')).toBe(received);
    expect(received.dateTime).toBe(replyTime);
    const saved = JSON.parse(records.get(CHAT_STORAGE_KEY)!);
    expect(saved.sessions[0].items.map((message: { time: string }) => message.time)).toEqual([
      userTime,
      replyTime,
    ]);
  });

  it('renders growing partial Markdown in a single bubble and scrolls streamed/deferred height changes', async () => {
    const log = element<HTMLDivElement>('[role="log"]');
    Object.defineProperty(log, 'scrollHeight', {
      configurable: true,
      get: () => log.textContent!.length * 10,
    });
    await send();
    api.event(0, 'start');
    api.event(0, 'delta', '## Partial heading\n\n**bold');
    await vi.waitFor(() => expect(state.activeSession().messages[1].content).toContain('Partial'));
    await settle();
    expect(element('[role="log"] article:last-child h2').textContent).toBe('Partial heading');
    expect(root().querySelectorAll('[role="log"] article')).toHaveLength(2);
    expect(log.scrollTop).toBe(log.scrollHeight);
    const bubble = element('[role="log"] article:last-child');
    api.event(0, 'delta', '**\n\n```typescript\nconst n = 1;');
    await vi.waitFor(() => expect(state.activeSession().messages[1].content).toContain('const n'));
    await settle();
    expect(element('[role="log"] article:last-child')).toBe(bubble);
    expect(element('[role="log"] article:last-child strong').textContent).toBe('bold');
    expect(element('[role="log"] article:last-child pre code').textContent).toContain('const n');
    expect(log.scrollTop).toBe(log.scrollHeight);
    expect(observers.at(-1)!.observe).toHaveBeenCalledWith(bubble);
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 4096 });
    observers.at(-1)!.callback([], {} as ResizeObserver);
    expect(log.scrollTop).toBe(4096);
    const current = observers.at(-1)!;
    fixture.destroy();
    expect(current.disconnect).toHaveBeenCalled();
    log.scrollTop = 0;
    current.callback([], {} as ResizeObserver);
    expect(log.scrollTop).toBe(0);
  });

  it('disconnects old bubble observers when switching and ignores their late notifications', async () => {
    await send();
    api.event(0, 'start');
    await settle();
    const previous = observers.at(-1)!;
    const log = element<HTMLDivElement>('[role="log"]');
    fixture.componentInstance.newSession();
    await settle();
    expect(previous.disconnect).toHaveBeenCalled();
    log.scrollTop = 23;
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 2048 });
    previous.callback([], {} as ResizeObserver);
    expect(log.scrollTop).toBe(23);
  });

  it('scrolls to the bottom on a height-only viewport resize without bubble notifications', async () => {
    await send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Completed reply');
    api.event(0, 'done');
    await settle();
    const log = element<HTMLDivElement>('[role="log"]');
    const messages = state.activeSession().messages;
    const bubble = element('[role="log"] article:last-child');
    let height = 600;
    let top = 600;
    Object.defineProperties(log, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, get: () => height },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (value: number) => {
          top = Math.min(value, log.scrollHeight - log.clientHeight);
        },
      },
    });
    const observer = observers.at(-1)!;
    expect(observer.observe).toHaveBeenCalledWith(log);
    height = 300;
    expect(log.scrollTop).toBe(600);
    observer.callback(
      [
        {
          target: log,
          contentRect: new DOMRect(0, 0, 800, height),
          borderBoxSize: [],
          contentBoxSize: [],
          devicePixelContentBoxSize: [],
        },
      ],
      {} as ResizeObserver,
    );
    expect(log.scrollTop).toBe(900);
    expect(state.activeSession().messages).toBe(messages);
    expect(element('[role="log"] article:last-child')).toBe(bubble);
  });

  it('offers UI retry after reload before start, preserving the unanswered user and draft', async () => {
    state.selectPrompt(state.prompts()[0].id);
    await send();
    await draft('Next unsent draft');
    const raw = records.get(CHAT_STORAGE_KEY)!;
    TestBed.resetTestingModule();
    records.set(CHAT_STORAGE_KEY, raw);
    api = new FakeChatApi();
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        { provide: ChatApi, useValue: api },
        {
          provide: CHAT_STORAGE,
          useValue: {
            getItem: (key: string) => records.get(key) ?? null,
            setItem: (key: string, value: string) => records.set(key, value),
          },
        },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(App);
    state = TestBed.inject(ChatState);
    await settle();
    expect(api.calls).toHaveLength(0);
    expect(button('Retry reply').disabled).toBe(false);
    expect(state.activeSession().promptLocked).toBe(true);
    button('Retry reply').click();
    await settle();
    expect(api.calls[0].request.messages).toEqual([{ role: 'user', content: 'Question' }]);
    expect(state.activeSession().messages).toHaveLength(1);
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Next unsent draft');
    expect(button('Send').disabled).toBe(true);
  });

  it('shows meaningful unsupported reasoning error with preserved partial text and accessible retry', async () => {
    await send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Keep **partial**');
    api.event(0, 'error');
    await settle();
    expect(element('[role="alert"]').textContent).toBe('This model does not support xhigh.');
    expect(element('[role="log"] article:last-child strong').textContent).toBe('partial');
    await draft('Next draft');
    button('Retry reply').click();
    await settle();
    expect(api.calls).toHaveLength(2);
    expect(api.calls[1].request.messages).toEqual([{ role: 'user', content: 'Question' }]);
    expect(root().querySelector('[role="alert"]')).toBeNull();
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Next draft');
  });

  it('Stop flushes partial content and presents retry', async () => {
    await send();
    api.event(0, 'start');
    api.event(0, 'delta', 'Preserved partial');
    button('Stop reply').click();
    await settle();
    expect(api.calls[0].signal.aborted).toBe(true);
    expect(element('[role="log"] article:last-child').textContent).toContain('Preserved partial');
    expect(element('[role="alert"]').textContent).toContain('Reply stopped');
    expect(button('Retry reply').disabled).toBe(false);
  });

  it('keeps errors and replies with their origin while another session stays active', async () => {
    const first = state.activeSessionId();
    await send();
    fixture.componentInstance.newSession();
    await draft('Independent');
    api.event(0, 'start');
    api.event(0, 'error');
    await settle();
    expect(root().querySelector('[role="alert"]')).toBeNull();
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Independent');
    fixture.componentInstance.selectSession(first);
    await settle();
    expect(element('[role="alert"]').textContent).toContain('does not support xhigh');
    expect(button('Retry reply')).toBeTruthy();
  });

  it('shows model-load errors with reload, preserving the draft', async () => {
    const models = vi
      .spyOn(api, 'models')
      .mockRejectedValueOnce(new Error('Models temporarily unavailable'));
    await fixture.componentInstance.turns.loadModels();
    await draft('Keep draft');
    expect(element('[role="alert"]').textContent).toContain('Models temporarily unavailable');
    button('Reload models').click();
    await settle();
    expect(models).toHaveBeenCalledTimes(2);
    expect(root().querySelector('[role="alert"]')).toBeNull();
    expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Keep draft');
  });
});

describe('App with literal backend SSE payloads', () => {
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  async function render(stream: string) {
    const records = new Map<string, string>();
    const fetch = vi.fn(async (url: string | URL | Request, options?: RequestInit) => {
      if (url === '/api/models') {
        return new Response('{"models":["deployment"],"default_model":"deployment"}');
      }
      const request = JSON.parse(options!.body as string);
      return new Response(stream.replaceAll('REQUEST_ID', request.request_id), {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        { provide: FETCH, useValue: fetch },
        {
          provide: CHAT_STORAGE,
          useValue: {
            getItem: (key: string) => records.get(key) ?? null,
            setItem: (key: string, value: string) => records.set(key, value),
          },
        },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(App);
    const state = TestBed.inject(ChatState);
    fixture.detectChanges();
    await fixture.whenStable();
    await vi.waitFor(() =>
      expect(fixture.componentInstance.turns.models()).toEqual(['deployment']),
    );
    state.updateDraft('Question');
    expect(fixture.componentInstance.turns.send()).toBe(true);
    await vi.waitFor(() => expect(fixture.componentInstance.turns.activePending()).toBe(false));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, state, records };
  }

  it('completes literal start/delta/done through fetch, rendering and storage without time on delta', async () => {
    const { fixture, state, records } = await render(
      [
        'id: server-reply:1',
        'event: start',
        'data: {"session_id":"initial-session","request_id":"REQUEST_ID","message_id":"server-reply","time":"2026-10-13T12:00:00.000Z"}',
        '',
        'id: server-reply:2',
        'event: delta',
        'data: {"session_id":"initial-session","request_id":"REQUEST_ID","message_id":"server-reply","text":"**Answer 世界**"}',
        '',
        'id: server-reply:3',
        'event: done',
        'data: {"session_id":"initial-session","request_id":"REQUEST_ID","message_id":"server-reply","time":"2026-10-13T12:00:00.000Z","status":"complete"}',
        '',
        '',
      ].join('\n'),
    );
    const root = fixture.nativeElement as HTMLElement;
    expect(state.activeSession().messages[1]).toMatchObject({
      id: 'server-reply',
      time: '2026-10-13T12:00:00.000Z',
      content: '**Answer 世界**',
      status: 'complete',
    });
    expect(root.querySelector('article:last-child strong')?.textContent).toBe('Answer 世界');
    expect(root.querySelector('[role="alert"]')).toBeNull();
    const saved = JSON.parse(records.get(CHAT_STORAGE_KEY)!);
    expect(saved.sessions[0].items[1]).toMatchObject({
      id: 'server-reply',
      time: '2026-10-13T12:00:00.000Z',
      msg: '**Answer 世界**',
      status: 'complete',
    });
  });

  it('surfaces literal start/error unsupported reasoning with the start time preserved', async () => {
    const { fixture, state, records } = await render(
      [
        'id: server-reply:1',
        'event: start',
        'data: {"session_id":"initial-session","request_id":"REQUEST_ID","message_id":"server-reply","time":"2026-10-13T12:00:00.000Z"}',
        '',
        'id: server-reply:2',
        'event: error',
        'data: {"session_id":"initial-session","request_id":"REQUEST_ID","message_id":"server-reply","code":"unsupported_reasoning","message":"This deployment does not support xhigh."}',
        '',
        '',
      ].join('\n'),
    );
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('[role="alert"]')?.textContent).toBe(
      'This deployment does not support xhigh.',
    );
    expect(root.textContent).toContain('Retry reply');
    expect(state.activeSession().messages[1]).toMatchObject({
      id: 'server-reply',
      time: '2026-10-13T12:00:00.000Z',
      status: 'error',
      error: { code: 'unsupported_reasoning', message: 'This deployment does not support xhigh.' },
    });
    const saved = JSON.parse(records.get(CHAT_STORAGE_KEY)!);
    expect(saved.sessions[0].items[1].error).toEqual(state.activeSession().messages[1].error);
  });
});
