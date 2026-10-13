import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CHAT_STORAGE, CHAT_STORAGE_KEY, ChatState, type ChatStorage } from './chat-state';
import type { ChatSnapshot, StoredChatState, StoredSession, SystemPrompt } from './chat.models';

const UNRELATED_KEY = 'ls-assistants.synthetic-preference';
const UNRELATED_VALUE = 'Keep this unrelated synthetic setting.';
const MEMORY_MESSAGE = 'Synthetic sensitive message 世界 🧪';
const MEMORY_DRAFT = ' Unsent synthetic draft\n草稿 🌱 ';
const EXCEPTION_CONTENT = 'Synthetic private exception detail';
const savedPrompt: SystemPrompt = {
  id: 'synthetic-saved-prompt',
  title: 'Original synthetic helper 世界',
  content: 'Original **synthetic** instructions.\n保留格式 🌱',
};
const editedPrompt: SystemPrompt = {
  ...savedPrompt,
  title: 'Edited synthetic helper',
  content: 'Revised synthetic catalogue instructions.',
};

function memoryStorage() {
  const records = new Map<string, string>([[UNRELATED_KEY, UNRELATED_VALUE]]);
  return {
    records,
    getItem: vi.fn((key: string): string | null => records.get(key) ?? null),
    setItem: vi.fn((key: string, value: string): void => {
      records.set(key, value);
    }),
  };
}

function savedSession(overrides: Partial<StoredSession> = {}): StoredSession {
  return {
    id: 'synthetic-saved-session',
    title: 'Synthetic saved history 世界 🌱',
    type: 'Agent',
    selected: true,
    selectedSystemPromptId: savedPrompt.id,
    systemPromptSnapshot: { ...savedPrompt },
    systemPromptLocked: true,
    items: [
      {
        id: 'synthetic-user-message',
        time: '2026-02-03T04:05:06+08:00',
        role: 'user',
        msg: 'Hallo **世界** 🌱\n\n`synthetic()`',
        type: 'Message',
        status: 'sent',
      },
      {
        id: 'synthetic-ai-message',
        time: '11:22:33',
        role: 'ai',
        msg: '## Synthetic Antwort 🧪\n\n- **原文**\n\n```txt\nraw <tag>\n```',
        type: 'Message',
        status: 'streaming',
      },
      {
        id: 'synthetic-empty-metadata',
        time: '',
        role: 'ai',
        msg: '',
        type: 'Message',
        status: '',
      },
    ],
    ...overrides,
  };
}

function savedEnvelope(overrides: Partial<StoredChatState> = {}): StoredChatState {
  return {
    version: 1,
    sessions: [savedSession()],
    systemPrompts: [{ ...editedPrompt }],
    drafts: { 'synthetic-saved-session': ' Saved draft\n未发送 🧪\t' },
    ...overrides,
  };
}

function snapshotOf(state: ChatState): ChatSnapshot {
  return structuredClone({
    sessions: state.sessions(),
    prompts: state.prompts(),
    activeSessionId: state.activeSessionId(),
  });
}

describe('ChatState storage integration', () => {
  let storage: ReturnType<typeof memoryStorage>;

  beforeEach(() => {
    storage = memoryStorage();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function createState(target: ChatStorage | null = storage): ChatState {
    TestBed.configureTestingModule({
      providers: [ChatState, { provide: CHAT_STORAGE, useValue: target }],
    });
    return TestBed.inject(ChatState);
  }

  function recreateState(target: ChatStorage | null = storage): ChatState {
    TestBed.resetTestingModule();
    return createState(target);
  }

  function emptyState(): ChatState {
    storage.records.set(
      CHAT_STORAGE_KEY,
      JSON.stringify(savedEnvelope({ sessions: [], systemPrompts: [], drafts: {} })),
    );
    return createState();
  }

  function storedState(): StoredChatState {
    const raw = storage.records.get(CHAT_STORAGE_KEY);
    if (raw === undefined) throw new Error('Missing saved synthetic state');
    return JSON.parse(raw) as StoredChatState;
  }

  function expectOnlyChatKeyWritten(): void {
    expect(storage.setItem.mock.calls.every(([key]) => key === CHAT_STORAGE_KEY)).toBe(true);
    expect(storage.records.get(UNRELATED_KEY)).toBe(UNRELATED_VALUE);
    expect([...storage.records.keys()].sort()).toEqual([CHAT_STORAGE_KEY, UNRELATED_KEY].sort());
  }

  function exerciseInMemory(state: ChatState): void {
    const previous = state.activeSession();
    state.newSession();
    const id = state.activeSessionId();
    state.savePrompt({ title: 'Synthetic private helper', content: 'Synthetic private guidance.' });
    const prompt = state.prompts().at(-1)!;
    state.selectPrompt(prompt.id);
    state.updateDraft(` ${MEMORY_MESSAGE} `);
    expect(state.sendMessage()).toBe(true);
    state.updateDraft(MEMORY_DRAFT);
    state.selectSession(previous.id);
    expect(state.activeSession()).toBe(previous);
    state.selectSession(id);
    expect(state.activeSession()).toMatchObject({
      id,
      prompt,
      promptLocked: true,
      draft: MEMORY_DRAFT,
      messages: [{ id: expect.any(String), role: 'user', content: MEMORY_MESSAGE }],
    });
  }

  function expectWarningsWithoutChatContent(): void {
    expect(console.warn).toHaveBeenCalled();
    for (const call of vi.mocked(console.warn).mock.calls) {
      expect(call).toHaveLength(1);
      expect(typeof call[0]).toBe('string');
      for (const content of [
        MEMORY_MESSAGE,
        MEMORY_DRAFT,
        EXCEPTION_CONTENT,
        savedPrompt.content,
        savedSession().items[0].msg,
        'Synthetic private guidance.',
      ]) {
        expect(call[0]).not.toContain(content);
      }
    }
  }

  it('initializes and persists only one empty session when storage has no saved history', () => {
    const state = createState();
    expect(state.sessions()).toEqual([
      {
        id: 'initial-session',
        title: 'New session',
        mode: 'chat',
        messages: [],
        draft: '',
        prompt: null,
        promptLocked: false,
      },
    ]);
    expect(state.activeSessionId()).toBe('initial-session');
    const expected = snapshotOf(state);
    TestBed.tick();

    expect(storedState().sessions).toHaveLength(1);
    expect(storedState().sessions[0]).toMatchObject({
      id: 'initial-session',
      selected: true,
      items: [],
    });
    expect(snapshotOf(recreateState())).toEqual(expected);
    expectOnlyChatKeyWritten();
  });

  it('writes the exact example-shaped fields after signal changes and an effect flush', () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-04-05T06:07:08.009Z'));
    const state = emptyState();
    const initialRaw = storage.records.get(CHAT_STORAGE_KEY);
    const id = state.activeSessionId();
    state.savePrompt({ title: ' Synthetic helper 世界 ', content: '\nKeep **raw** text 🌱.\n' });
    const prompt = state.prompts()[0];
    state.selectPrompt(prompt.id);
    state.updateDraft(' \nHello  世界 🌱\nSecond line\n ');
    expect(state.sendMessage()).toBe(true);
    const sentId = state.activeSession().messages[0].id;
    state.updateDraft(' Next draft\n未发送\t');
    state.sessions.update((sessions) =>
      sessions.map((session) => ({
        ...session,
        mode: 'agent',
        messages: [
          ...session.messages,
          { id: 'synthetic-new-answer', role: 'assistant', content: '## 合成 answer\n\n`raw()`' },
        ],
      })),
    );

    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.records.get(CHAT_STORAGE_KEY)).toBe(initialRaw);
    TestBed.tick();

    expect(storedState()).toEqual({
      version: 1,
      sessions: [
        {
          id,
          items: [
            {
              id: sentId,
              time: '2026-04-05T06:07:08.009Z',
              role: 'user',
              msg: 'Hello  世界 🌱\nSecond line',
              type: 'Message',
            },
            {
              id: 'synthetic-new-answer',
              time: '',
              role: 'ai',
              msg: '## 合成 answer\n\n`raw()`',
              type: 'Message',
              status: 'complete',
            },
          ],
          title: 'Hello 世界 🌱 Second line',
          type: 'Agent',
          selected: true,
          selectedSystemPromptId: prompt.id,
          systemPromptSnapshot: {
            id: prompt.id,
            title: 'Synthetic helper 世界',
            content: 'Keep **raw** text 🌱.',
          },
          systemPromptLocked: true,
        },
      ],
      systemPrompts: [
        { id: prompt.id, title: 'Synthetic helper 世界', content: 'Keep **raw** text 🌱.' },
      ],
      drafts: { [id]: ' Next draft\n未发送\t' },
    });
    expect(storage.setItem).toHaveBeenCalledTimes(1);
    expect(state.persistenceError()).toBeNull();
    expectOnlyChatKeyWritten();
    expect(console.warn).not.toHaveBeenCalled();
  });

  it('hydrates synchronously and roundtrips snapshots, raw Markdown, Unicode, times and statuses', () => {
    const saved = savedEnvelope();
    const raw = JSON.stringify(saved);
    storage.records.set(CHAT_STORAGE_KEY, raw);
    const expected: ChatSnapshot = {
      activeSessionId: 'synthetic-saved-session',
      prompts: [{ ...editedPrompt }],
      sessions: [
        {
          id: 'synthetic-saved-session',
          title: 'Synthetic saved history 世界 🌱',
          mode: 'agent',
          draft: ' Saved draft\n未发送 🧪\t',
          prompt: { ...savedPrompt },
          promptLocked: true,
          messages: [
            {
              id: 'synthetic-user-message',
              time: '2026-02-03T04:05:06+08:00',
              role: 'user',
              content: 'Hallo **世界** 🌱\n\n`synthetic()`',
              status: 'sent',
            },
            {
              id: 'synthetic-ai-message',
              time: '11:22:33',
              role: 'assistant',
              content: '## Synthetic Antwort 🧪\n\n- **原文**\n\n```txt\nraw <tag>\n```',
              status: 'streaming',
            },
            {
              id: 'synthetic-empty-metadata',
              time: '',
              role: 'assistant',
              content: '',
              status: '',
            },
          ],
        },
      ],
    };

    const state = createState();

    expect(snapshotOf(state)).toEqual(expected);
    expect(state.activeSession()).toBe(state.sessions()[0]);
    expect(state.activeSession().prompt).not.toBe(state.prompts()[0]);
    expect(storage.getItem).toHaveBeenCalledExactlyOnceWith(CHAT_STORAGE_KEY);
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.records.get(CHAT_STORAGE_KEY)).toBe(raw);
    TestBed.tick();
    expect(storedState()).toEqual(saved);

    const restored = recreateState();
    expect(snapshotOf(restored)).toEqual(expected);
    TestBed.tick();
    expect(storedState()).toEqual(saved);
    expectOnlyChatKeyWritten();
    expect(console.warn).not.toHaveBeenCalled();
  });

  it('persists active selection and session deletion with exactly one selected survivor', () => {
    let state = emptyState();
    const firstId = state.activeSessionId();
    state.newSession();
    const secondId = state.activeSessionId();
    state.newSession();
    const thirdId = state.activeSessionId();
    state.selectSession(secondId);
    TestBed.tick();

    expect(storedState().sessions.map((session) => [session.id, session.selected])).toEqual([
      [thirdId, false],
      [secondId, true],
      [firstId, false],
    ]);
    state = recreateState();
    expect(state.activeSessionId()).toBe(secondId);
    expect(state.activeSession().id).toBe(secondId);
    state.deleteSession(firstId);
    TestBed.tick();
    const selected = storedState().sessions.filter((session) => session.selected);
    expect(selected.map((session) => session.id)).toEqual([secondId]);
    state.deleteSession(secondId);
    TestBed.tick();

    state = recreateState();
    expect(state.sessions().map((session) => session.id)).toEqual([thirdId]);
    expect(state.activeSessionId()).toBe(thirdId);
    TestBed.tick();
    expect(storedState().sessions.map((session) => session.selected)).toEqual([true]);
  });

  it.each([
    { flags: [false, false], active: 'synthetic-a' },
    { flags: [false, true], active: 'synthetic-b' },
    { flags: [true, true], active: 'synthetic-a' },
  ])('normalizes saved selections $flags to one active session on write', ({ flags, active }) => {
    storage.records.set(
      CHAT_STORAGE_KEY,
      JSON.stringify(
        savedEnvelope({
          sessions: [
            savedSession({ id: 'synthetic-a', selected: flags[0] }),
            savedSession({ id: 'synthetic-b', selected: flags[1] }),
          ],
          drafts: {},
        }),
      ),
    );
    const state = createState();
    expect(state.activeSessionId()).toBe(active);
    TestBed.tick();
    const selected = storedState().sessions.filter((session) => session.selected);
    expect(selected).toHaveLength(1);
    expect(selected[0].id).toBe(active);
    expect(recreateState().activeSessionId()).toBe(active);
  });

  it('preserves drafts and a custom catalogue through edits/deletes without changing locked snapshots', () => {
    let state = emptyState();
    state.savePrompt({ title: 'Original custom helper 世界', content: 'Original guidance 🌱.' });
    const original = { ...state.prompts()[0] };
    state.savePrompt({ title: 'Kept custom helper', content: 'Keep this synthetic guidance.' });
    const kept = { ...state.prompts()[1] };
    const lockedId = state.activeSessionId();
    state.selectPrompt(original.id);
    state.updateDraft('Lock the synthetic prompt snapshot');
    state.sendMessage();
    state.updateDraft(MEMORY_DRAFT);
    state.newSession();
    const unlockedId = state.activeSessionId();
    state.selectPrompt(original.id);
    state.updateDraft(' Independent draft\n未发送\t');
    const revised = { ...original, title: 'Revised custom helper', content: 'Edited guidance 🧪.' };
    state.savePrompt(revised);
    state.selectSession(lockedId);
    TestBed.tick();

    state = recreateState();
    expect(state.prompts()).toEqual([revised, kept]);
    expect(state.activeSession()).toMatchObject({
      id: lockedId,
      prompt: original,
      promptLocked: true,
      draft: MEMORY_DRAFT,
    });
    const lockedMessage = { ...state.activeSession().messages[0] };
    expect(state.sessions().find((session) => session.id === unlockedId)).toMatchObject({
      prompt: revised,
      promptLocked: false,
      draft: ' Independent draft\n未发送\t',
    });
    state.selectPrompt(kept.id);
    expect(state.activeSession().prompt).toEqual(original);
    state.deletePrompt(original.id);
    TestBed.tick();

    state = recreateState();
    expect(state.prompts()).toEqual([kept]);
    expect(state.activeSessionId()).toBe(lockedId);
    expect(state.activeSession()).toMatchObject({
      prompt: original,
      promptLocked: true,
      draft: MEMORY_DRAFT,
      messages: [lockedMessage],
    });
    expect(state.sessions().find((session) => session.id === unlockedId)).toMatchObject({
      prompt: null,
      promptLocked: false,
      draft: ' Independent draft\n未发送\t',
    });
    state.deletePrompt(kept.id);
    TestBed.tick();

    state = recreateState();
    expect(state.prompts()).toEqual([]);
    expect(state.activeSession().prompt).toEqual(original);
    TestBed.tick();
    expect(storedState().systemPrompts).toEqual([]);
    const lockedSession = storedState().sessions.find((session) => session.id === lockedId);
    expect(lockedSession?.systemPromptSnapshot).toEqual(original);
  });

  it('restores a locked No system prompt choice even after adding a catalogue entry', () => {
    let state = emptyState();
    state.updateDraft('Synthetic chat without a prompt');
    state.sendMessage();
    state.savePrompt({ title: 'Later synthetic prompt', content: 'Later synthetic instructions.' });
    const promptId = state.prompts()[0].id;
    TestBed.tick();

    state = recreateState();
    expect(state.activeSession()).toMatchObject({ prompt: null, promptLocked: true });
    state.selectPrompt(promptId);
    expect(state.activeSession().prompt).toBeNull();
    expect(storedState().sessions[0]).toMatchObject({
      selectedSystemPromptId: null,
      systemPromptSnapshot: null,
      systemPromptLocked: true,
    });
  });

  it('does not restore a deleted session or replace the saved final-session fallback on recreation', () => {
    let state = createState();
    const originalId = state.activeSessionId();
    state.newSession();
    const deletedId = state.activeSessionId();
    state.selectSession(originalId);
    state.deleteSession(deletedId);
    TestBed.tick();

    state = recreateState();
    expect(state.sessions().map((session) => session.id)).toEqual([originalId]);
    expect(state.activeSession().messages).toEqual([]);
    state.deleteSession(originalId);
    const replacementId = state.activeSessionId();
    expect(replacementId).not.toBe(originalId);
    TestBed.tick();

    state = recreateState();
    expect(state.sessions()).toHaveLength(1);
    expect(state.activeSession()).toMatchObject({
      id: replacementId,
      title: 'New session',
      messages: [],
      draft: '',
      prompt: null,
      promptLocked: false,
    });
  });

  it.each(['v1 envelope', 'bare array'] as const)(
    'creates one blank session from valid empty %s',
    (format) => {
      storage.records.set(
        CHAT_STORAGE_KEY,
        format === 'bare array'
          ? '[]'
          : JSON.stringify(savedEnvelope({ sessions: [], systemPrompts: [], drafts: {} })),
      );
      const state = createState();
      expect(state.sessions()).toEqual([
        {
          id: expect.any(String),
          title: 'New session',
          mode: 'chat',
          messages: [],
          draft: '',
          prompt: null,
          promptLocked: false,
        },
      ]);
      expect(state.activeSessionId()).toBe(state.sessions()[0].id);
      if (format === 'v1 envelope') expect(state.prompts()).toEqual([]);
      else expect(state.prompts().length).toBeGreaterThan(0);
      const expected = snapshotOf(state);
      TestBed.tick();
      expect(storedState().sessions).toHaveLength(1);
      expect(storedState().sessions[0].selected).toBe(true);
      expect(snapshotOf(recreateState())).toEqual(expected);
      expect(console.warn).not.toHaveBeenCalled();
    },
  );

  it.each([
    { name: 'malformed JSON', raw: `{"synthetic":"${EXCEPTION_CONTENT}"` },
    {
      name: 'invalid schema',
      raw: JSON.stringify({
        ...savedEnvelope(),
        sessions: [{ ...savedSession(), items: [{ ...savedSession().items[0], time: 42 }] }],
      }),
    },
    { name: 'future version', raw: JSON.stringify({ ...savedEnvelope(), version: 99 }) },
  ])('leaves $name raw data untouched and disables writes for the service lifetime', ({ raw }) => {
    storage.records.set(CHAT_STORAGE_KEY, raw);
    let state!: ChatState;
    expect(() => {
      state = createState();
    }).not.toThrow();
    expect(state.persistenceError()).not.toBeNull();
    expect(state.activeSession().messages).toEqual([]);
    expect(() => exerciseInMemory(state)).not.toThrow();
    expect(() => TestBed.tick()).not.toThrow();
    state.updateDraft('Another synthetic draft after an effect flush');
    expect(() => TestBed.tick()).not.toThrow();

    expect(storage.records.get(CHAT_STORAGE_KEY)).toBe(raw);
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.getItem).toHaveBeenCalledExactlyOnceWith(CHAT_STORAGE_KEY);
    expect(console.warn).toHaveBeenCalledTimes(1);
    expectWarningsWithoutChatContent();
    expectOnlyChatKeyWritten();

    // Even an external repair must not enable writes in this already-failed instance.
    const repairedRaw = JSON.stringify(savedEnvelope());
    storage.records.set(CHAT_STORAGE_KEY, repairedRaw);
    state.updateDraft('Synthetic draft after external repair');
    TestBed.tick();
    expect(storage.records.get(CHAT_STORAGE_KEY)).toBe(repairedRaw);
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(state.persistenceError()).not.toBeNull();
  });

  it.each(['Error', 'SecurityError'] as const)(
    'keeps unreadable saved data untouched and memory operations functional on a read %s',
    (name) => {
      const raw = JSON.stringify(savedEnvelope());
      storage.records.set(CHAT_STORAGE_KEY, raw);
      storage.getItem.mockImplementation(() => {
        throw name === 'Error'
          ? new Error(EXCEPTION_CONTENT)
          : new DOMException(EXCEPTION_CONTENT, name);
      });
      let state!: ChatState;
      expect(() => {
        state = createState();
      }).not.toThrow();
      expect(state.persistenceError()).not.toBeNull();
      expect(() => exerciseInMemory(state)).not.toThrow();
      expect(() => TestBed.tick()).not.toThrow();

      storage.getItem.mockImplementation((key) => storage.records.get(key) ?? null);
      state.updateDraft('Synthetic draft after read access recovers');
      TestBed.tick();
      expect(state.activeSession().messages[0].content).toBe(MEMORY_MESSAGE);
      expect(storage.records.get(CHAT_STORAGE_KEY)).toBe(raw);
      expect(storage.getItem).toHaveBeenCalledExactlyOnceWith(CHAT_STORAGE_KEY);
      expect(storage.setItem).not.toHaveBeenCalled();
      expect(state.persistenceError()).not.toBeNull();
      expectWarningsWithoutChatContent();
      expectOnlyChatKeyWritten();
    },
  );

  it('supports memory-only operations when storage is unavailable', () => {
    const state = createState(null);
    expect(state.persistenceError()).not.toBeNull();
    expect(() => exerciseInMemory(state)).not.toThrow();
    expect(() => TestBed.tick()).not.toThrow();
    expect(storage.getItem).not.toHaveBeenCalled();
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.records.get(UNRELATED_KEY)).toBe(UNRELATED_VALUE);
    expectWarningsWithoutChatContent();
  });

  it.each(['QuotaExceededError', 'SecurityError'] as const)(
    'retries failed %s writes on later changes and clears the error after a successful save',
    (name) => {
      const raw = JSON.stringify(savedEnvelope());
      storage.records.set(CHAT_STORAGE_KEY, raw);
      storage.setItem.mockImplementation(() => {
        throw new DOMException(EXCEPTION_CONTENT, name);
      });
      const state = createState();
      expect(state.persistenceError()).toBeNull();
      expect(() => exerciseInMemory(state)).not.toThrow();
      const firstSnapshot = snapshotOf(state);
      expect(() => TestBed.tick()).not.toThrow();

      expect(snapshotOf(state)).toEqual(firstSnapshot);
      expect(state.persistenceError()).not.toBeNull();
      expect(storage.records.get(CHAT_STORAGE_KEY)).toBe(raw);
      expect(storage.setItem).toHaveBeenCalledTimes(1);
      state.updateDraft('Second synthetic unsaved draft');
      expect(() => TestBed.tick()).not.toThrow();
      expect(state.activeSession().draft).toBe('Second synthetic unsaved draft');
      expect(state.activeSession().messages[0].content).toBe(MEMORY_MESSAGE);
      expect(storage.setItem).toHaveBeenCalledTimes(2);
      expect(console.warn).toHaveBeenCalledTimes(1);
      expectWarningsWithoutChatContent();

      storage.setItem.mockImplementation((key, value) => {
        storage.records.set(key, value);
      });
      state.updateDraft(' Successful retry draft\n已保存 🌱 ');
      const expected = snapshotOf(state);
      TestBed.tick();

      expect(storage.setItem).toHaveBeenCalledTimes(3);
      expect(state.persistenceError()).toBeNull();
      expect(storedState().drafts[state.activeSessionId()]).toBe(
        ' Successful retry draft\n已保存 🌱 ',
      );
      expect(snapshotOf(recreateState())).toEqual(expected);
      expectOnlyChatKeyWritten();
    },
  );

  it('uses the default token factory safely when browser localStorage access throws', () => {
    const access = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException(EXCEPTION_CONTENT, 'SecurityError');
    });
    TestBed.configureTestingModule({ providers: [ChatState] });
    expect(TestBed.inject(CHAT_STORAGE)).toBeNull();
    let state!: ChatState;
    expect(() => {
      state = TestBed.inject(ChatState);
    }).not.toThrow();
    expect(() => exerciseInMemory(state)).not.toThrow();
    expect(() => TestBed.tick()).not.toThrow();
    expect(state.persistenceError()).not.toBeNull();
    expect(access).toHaveBeenCalledTimes(1);
    expectWarningsWithoutChatContent();
  });

  it('roundtrips through actual window.localStorage using the default token factory with scoped cleanup', () => {
    const browserStorage = window.localStorage;
    const previousRaw = browserStorage.getItem(CHAT_STORAGE_KEY);
    const unrelatedKey = `${CHAT_STORAGE_KEY}.spec.${crypto.randomUUID()}`;
    try {
      browserStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(savedEnvelope()));
      browserStorage.setItem(unrelatedKey, UNRELATED_VALUE);
      TestBed.configureTestingModule({ providers: [ChatState] });
      expect(TestBed.inject(CHAT_STORAGE)).toBe(browserStorage);
      const state = TestBed.inject(ChatState);
      expect(state.activeSessionId()).toBe('synthetic-saved-session');
      state.updateDraft(' Browser roundtrip draft\n合成 🌱 ');
      state.newSession();
      const newId = state.activeSessionId();
      state.updateDraft('Another synthetic browser draft');
      state.selectSession('synthetic-saved-session');
      const expected = snapshotOf(state);
      TestBed.tick();

      const persisted = JSON.parse(browserStorage.getItem(CHAT_STORAGE_KEY)!) as StoredChatState;
      expect(persisted.drafts).toEqual({
        'synthetic-saved-session': ' Browser roundtrip draft\n合成 🌱 ',
        [newId]: 'Another synthetic browser draft',
      });
      const selected = persisted.sessions.filter((session) => session.selected);
      expect(selected.map((session) => session.id)).toEqual(['synthetic-saved-session']);
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({ providers: [ChatState] });
      expect(TestBed.inject(CHAT_STORAGE)).toBe(browserStorage);
      const restored = TestBed.inject(ChatState);
      expect(snapshotOf(restored)).toEqual(expected);
      TestBed.tick();
      expect(browserStorage.getItem(unrelatedKey)).toBe(UNRELATED_VALUE);
      expect(console.warn).not.toHaveBeenCalled();
    } finally {
      TestBed.resetTestingModule();
      if (previousRaw === null) browserStorage.removeItem(CHAT_STORAGE_KEY);
      else browserStorage.setItem(CHAT_STORAGE_KEY, previousRaw);
      browserStorage.removeItem(unrelatedKey);
    }
  });
});
