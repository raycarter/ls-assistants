import { describe, expect, it } from 'vitest';
import { decodeChatState, encodeChatState } from './chat-storage';
import type {
  ChatMessage,
  ChatSnapshot,
  StoredChatState,
  StoredMessage,
  StoredSession,
  SystemPrompt,
} from './chat.models';

const prompt: SystemPrompt = {
  id: 'p1',
  title: 'Helper',
  content: 'Keep **raw** text.\n你好 🌱',
};

function storedSession(overrides: Partial<StoredSession> = {}): StoredSession {
  return {
    id: 's1',
    items: [
      { id: 'm1', time: '10:00', role: 'user', msg: '**你好**\n`x`', type: 'Message' },
      {
        id: 'm2',
        time: '10:01',
        role: 'ai',
        msg: '# Antwort 🧪\n\n- raw',
        type: 'Message',
        status: 'complete',
      },
    ],
    title: 'Tiny chat',
    type: 'Chat',
    selected: true,
    selectedSystemPromptId: 'p1',
    systemPromptSnapshot: { ...prompt },
    systemPromptLocked: true,
    ...overrides,
  };
}

function envelope(overrides: Partial<StoredChatState> = {}): StoredChatState {
  return {
    version: 1,
    sessions: [storedSession()],
    systemPrompts: [{ ...prompt }],
    drafts: { s1: ' draft\n未发送 ' },
    ...overrides,
  };
}

function snapshot(): ChatSnapshot {
  return {
    sessions: [
      {
        id: 's1',
        title: 'Tiny chat',
        mode: 'chat',
        messages: [
          { id: 'm1', role: 'user', content: '**你好**\n`x`', time: '10:00' },
          {
            id: 'm2',
            role: 'assistant',
            content: '# Antwort 🧪\n\n- raw',
            time: '10:01',
            status: 'complete',
          },
        ],
        draft: ' draft\n未发送 ',
        prompt: { ...prompt },
        promptLocked: true,
      },
    ],
    prompts: [{ ...prompt }],
    activeSessionId: 's1',
  };
}

function decode(value: unknown, fallback: readonly SystemPrompt[] = []): ChatSnapshot {
  return decodeChatState(JSON.stringify(value), fallback);
}

describe('encodeChatState', () => {
  it('writes the exact example-shaped envelope, with drafts outside sessions', () => {
    expect(encodeChatState(snapshot())).toEqual(envelope());
  });

  it.each<{ message: ChatMessage; time: string; status?: string }>([
    {
      message: { id: 'demo-answer', role: 'assistant', content: 'Demo' },
      time: '',
      status: 'complete',
    },
    {
      message: { id: 'm', role: 'assistant', content: '', time: '', status: 'streaming' },
      time: '',
      status: 'streaming',
    },
    {
      message: {
        id: 'm',
        role: 'assistant',
        content: '',
        time: '2026-01-02T03:04:05+08:00',
        status: '',
      },
      time: '2026-01-02T03:04:05+08:00',
      status: '',
    },
    { message: { id: 'm', role: 'user', content: '' }, time: '' },
    {
      message: { id: 'm', role: 'user', content: '', time: '10:11', status: 'sent' },
      time: '10:11',
      status: 'sent',
    },
    {
      message: { id: 'm', role: 'user', content: '', time: '', status: '' },
      time: '',
      status: '',
    },
  ])('preserves/defaults message metadata: $message', ({ message, time, status }) => {
    const source = snapshot();
    source.sessions[0].messages = [message];

    const encoded = encodeChatState(source).sessions[0].items[0];

    expect(encoded.time).toBe(time);
    expect(encoded.status).toBe(status);
    expect(Object.hasOwn(encoded, 'status')).toBe(status !== undefined);
  });

  it.each([
    ['chat', 'Chat'],
    ['agent', 'Agent'],
  ] as const)('maps %s mode to %s', (mode, storedType) => {
    const source = snapshot();
    source.sessions[0].mode = mode;
    expect(encodeChatState(source).sessions[0].type).toBe(storedType);
  });

  it('selects exactly the active session', () => {
    const source = snapshot();
    source.sessions.push({ ...source.sessions[0], id: 's2' });
    source.activeSessionId = 's2';

    expect(encodeChatState(source).sessions.map((session) => session.selected)).toEqual([
      false,
      true,
    ]);
  });

  it('normalizes a missing active ID to the first session', () => {
    const source = snapshot();
    source.sessions.push({ ...source.sessions[0], id: 's2' });
    source.activeSessionId = 'missing';

    expect(encodeChatState(source).sessions.map((session) => session.selected)).toEqual([
      true,
      false,
    ]);
  });

  it('stores an unlocked prompt selection without a snapshot', () => {
    const source = snapshot();
    source.sessions[0].promptLocked = false;

    expect(encodeChatState(source).sessions[0]).toMatchObject({
      selectedSystemPromptId: 'p1',
      systemPromptSnapshot: null,
      systemPromptLocked: false,
    });
  });

  it('stores a locked no-prompt choice as null', () => {
    const source = snapshot();
    source.sessions[0].prompt = null;

    expect(encodeChatState(source).sessions[0]).toMatchObject({
      selectedSystemPromptId: null,
      systemPromptSnapshot: null,
      systemPromptLocked: true,
    });
  });

  it('clones messages, catalogue entries and locked snapshots without mutating the caller', () => {
    const source = snapshot();
    const before = structuredClone(source);
    Object.freeze(source.sessions[0].prompt);
    Object.freeze(source.sessions[0].messages[0]);
    Object.freeze(source.sessions[0].messages);
    Object.freeze(source.sessions[0]);
    Object.freeze(source.sessions);
    Object.freeze(source.prompts[0]);
    Object.freeze(source.prompts);
    Object.freeze(source);

    const encoded = encodeChatState(source);
    encoded.sessions[0].items[0].msg = 'Changed';
    encoded.sessions[0].systemPromptSnapshot!.content = 'Changed snapshot';
    encoded.systemPrompts[0].content = 'Changed catalogue';
    encoded.drafts['s1'] = 'Changed draft';

    expect(source).toEqual(before);
    expect(encoded.sessions[0].systemPromptSnapshot).not.toBe(encoded.systemPrompts[0]);
  });

  it('encodes empty history without inventing a session', () => {
    expect(encodeChatState({ sessions: [], prompts: [prompt], activeSessionId: '' })).toEqual({
      version: 1,
      sessions: [],
      systemPrompts: [prompt],
      drafts: {},
    });
  });
});

describe('decodeChatState', () => {
  it('reads a bare exact example-shaped session using the fallback catalogue', () => {
    const expected = snapshot();
    expected.sessions[0].draft = '';

    expect(decode(storedSession(), [prompt])).toEqual(expected);
  });

  it('roundtrips raw Unicode, Markdown, timestamps, statuses and drafts', () => {
    const source = snapshot();
    source.sessions[0].mode = 'agent';
    source.sessions[0].messages[0].status = 'sent';
    source.sessions[0].messages[1].status = 'streaming';

    expect(decode(encodeChatState(source), [{ id: 'ignored', title: '', content: '' }])).toEqual(
      source,
    );
  });

  it('accepts empty strings and preserves absent status without inventing metadata', () => {
    const session = storedSession({
      title: '',
      items: [{ id: 'm', time: '', role: 'ai', msg: '', type: 'Message' }],
      systemPromptSnapshot: { ...prompt, title: '', content: '' },
    });
    const decoded = decode(session);

    expect(decoded.sessions[0].title).toBe('');
    expect(decoded.sessions[0].messages).toEqual([
      { id: 'm', time: '', role: 'assistant', content: '' },
    ]);
    expect(Object.hasOwn(decoded.sessions[0].messages[0], 'status')).toBe(false);
    expect(decoded.sessions[0].prompt).toEqual({ ...prompt, title: '', content: '' });
  });

  it.each([
    { selected: [false, false, false], active: 's1' },
    { selected: [false, true, false], active: 's2' },
    { selected: [false, true, true], active: 's2' },
  ])('normalizes selection $selected to $active', ({ selected, active }) => {
    const source = envelope({
      sessions: selected.map((value, index) =>
        storedSession({ id: `s${index + 1}`, selected: value }),
      ),
    });
    const decoded = decode(source);

    expect(decoded.activeSessionId).toBe(active);
    expect(encodeChatState(decoded).sessions.filter((session) => session.selected)).toHaveLength(1);
  });

  it('keeps a locked snapshot independent of an edited catalogue entry', () => {
    const edited = { ...prompt, title: 'Edited', content: 'New guidance' };
    const decoded = decode(envelope({ systemPrompts: [edited] }));

    expect(decoded.prompts).toEqual([edited]);
    expect(decoded.sessions[0].prompt).toEqual(prompt);
    decoded.prompts[0].content = 'Edited again';
    expect(decoded.sessions[0].prompt).toEqual(prompt);
  });

  it('keeps deleted catalogue prompts deleted while retaining locked snapshots', () => {
    const decoded = decode(envelope({ systemPrompts: [] }), [prompt]);

    expect(decoded.prompts).toEqual([]);
    expect(decoded.sessions[0].prompt).toEqual(prompt);
    expect(decoded.sessions[0].promptLocked).toBe(true);
    expect(encodeChatState(decoded).systemPrompts).toEqual([]);
  });

  it('keeps a locked no-prompt selection even when a catalogue exists', () => {
    const decoded = decode(
      envelope({
        sessions: [storedSession({ selectedSystemPromptId: null, systemPromptSnapshot: null })],
      }),
    );

    expect(decoded.sessions[0].prompt).toBeNull();
    expect(decoded.sessions[0].promptLocked).toBe(true);
  });

  it('resolves an unlocked selection from the live catalogue, ignoring a stale snapshot', () => {
    const edited = { ...prompt, content: 'Live guidance' };
    const decoded = decode(
      envelope({
        sessions: [storedSession({ systemPromptLocked: false })],
        systemPrompts: [edited],
      }),
    );

    expect(decoded.sessions[0].prompt).toEqual(edited);
    expect(decoded.sessions[0].prompt).not.toBe(decoded.prompts[0]);
    expect(encodeChatState(decoded).sessions[0].systemPromptSnapshot).toBeNull();
  });

  it.each<{ catalogue: SystemPrompt[] }>([
    { catalogue: [] },
    { catalogue: [{ id: 'other', title: 'Other', content: '' }] },
  ])(
    'clears a missing unlocked selection in the explicit catalogue $catalogue',
    ({ catalogue }) => {
      const decoded = decode(
        envelope({
          sessions: [storedSession({ systemPromptLocked: false })],
          systemPrompts: catalogue,
        }),
        [prompt],
      );

      expect(decoded.sessions[0].prompt).toBeNull();
      expect(decoded.prompts).toEqual(catalogue);
    },
  );

  it('preserves a custom catalogue, independent drafts and missing-draft defaults', () => {
    const custom = { id: 'custom', title: '自定义', content: '```txt\nraw\n```' };
    const decoded = decode(
      envelope({
        sessions: [
          storedSession(),
          storedSession({
            id: 's2',
            type: 'Agent',
            selectedSystemPromptId: custom.id,
            systemPromptSnapshot: null,
            systemPromptLocked: false,
          }),
          storedSession({ id: 's3' }),
        ],
        systemPrompts: [custom],
        drafts: { s1: ' \n**草稿** 🧪\t', s2: '' },
      }),
      [prompt],
    );

    expect(decoded.prompts).toEqual([custom]);
    expect(decoded.sessions.map((session) => session.draft)).toEqual([' \n**草稿** 🧪\t', '', '']);
    expect(decoded.sessions[1]).toMatchObject({ mode: 'agent', prompt: custom });
  });

  it('imports bare arrays and appends missing snapshots once for unlocked selection', () => {
    const missing = { id: 'missing', title: 'Imported', content: 'Snapshot guidance' };
    const decoded = decode(
      [
        storedSession({
          selectedSystemPromptId: missing.id,
          systemPromptSnapshot: missing,
        }),
        storedSession({
          id: 's2',
          selectedSystemPromptId: missing.id,
          systemPromptSnapshot: null,
          systemPromptLocked: false,
        }),
        storedSession({
          id: 's3',
          selectedSystemPromptId: missing.id,
          systemPromptSnapshot: missing,
        }),
      ],
      [prompt],
    );

    expect(decoded.prompts).toEqual([prompt, missing]);
    expect(decoded.sessions.map((session) => session.prompt)).toEqual([missing, missing, missing]);
    expect(decoded.sessions.map((session) => session.draft)).toEqual(['', '', '']);
  });

  it('retains fallback edits without replacing a bare locked snapshot', () => {
    const edited = { ...prompt, content: 'Live fallback' };
    const decoded = decode(storedSession(), [edited]);

    expect(decoded.prompts).toEqual([edited]);
    expect(decoded.sessions[0].prompt).toEqual(prompt);
  });

  it('uses a bare unlocked snapshot to supply a missing catalogue selection', () => {
    const decoded = decode(storedSession({ systemPromptLocked: false }));

    expect(decoded.prompts).toEqual([prompt]);
    expect(decoded.sessions[0].prompt).toEqual(prompt);
    expect(decoded.sessions[0].prompt).not.toBe(decoded.prompts[0]);
  });

  it('clones the fallback catalogue and every session prompt independently', () => {
    const fallback = Object.freeze([Object.freeze({ ...prompt })]);
    const decoded = decode(
      [
        storedSession(),
        storedSession({ id: 's2', systemPromptLocked: false, systemPromptSnapshot: null }),
        storedSession({ id: 's3', systemPromptLocked: false, systemPromptSnapshot: null }),
      ],
      fallback,
    );

    decoded.prompts[0].content = 'Catalogue changed';
    decoded.sessions[1].prompt!.content = 'Session changed';

    expect(fallback).toEqual([prompt]);
    expect(decoded.sessions[0].prompt).toEqual(prompt);
    expect(decoded.sessions[2].prompt).toEqual(prompt);
  });

  it('ignores unknown fields rather than spreading them into internal data', () => {
    const extra = Object.fromEntries([
      ['__proto__', { polluted: true }],
      ['unknown', 'extra'],
    ]);
    const source = {
      ...envelope(),
      ...extra,
      sessions: [
        {
          ...storedSession(),
          ...extra,
          draft: 'Wrong location',
          systemPromptSnapshot: { ...prompt, ...extra },
          items: storedSession().items.map((message) => ({ ...message, ...extra })),
        },
      ],
      systemPrompts: [{ ...prompt, ...extra }],
    };

    expect(decode(source)).toEqual(snapshot());
    expect(Object.hasOwn(Object.prototype, 'polluted')).toBe(false);
  });

  it('returns empty envelope history and its explicit empty catalogue', () => {
    expect(decode(envelope({ sessions: [], systemPrompts: [], drafts: {} }), [prompt])).toEqual({
      sessions: [],
      prompts: [],
      activeSessionId: '',
    });
  });

  it('returns empty bare history with a cloned fallback catalogue', () => {
    const decoded = decode([], [prompt]);

    expect(decoded).toEqual({ sessions: [], prompts: [prompt], activeSessionId: '' });
    expect(decoded.prompts[0]).not.toBe(prompt);
  });

  it('roundtrips prototype-named session, message, prompt and draft keys safely', () => {
    const keys = ['__proto__', 'constructor', 'toString'];
    const source: ChatSnapshot = {
      sessions: keys.map((id) => ({
        id,
        title: '',
        mode: 'chat',
        messages: [{ id, role: 'user', content: '', time: '' }],
        draft: `草稿 ${id}`,
        prompt: { id, title: id, content: '' },
        promptLocked: false,
      })),
      prompts: keys.map((id) => ({ id, title: id, content: '' })),
      activeSessionId: 'constructor',
    };
    const encoded = encodeChatState(source);

    expect(Object.getPrototypeOf(encoded.drafts)).toBe(Object.prototype);
    for (const id of keys) {
      expect(Object.hasOwn(encoded.drafts, id)).toBe(true);
      expect(encoded.drafts[id]).toBe(`草稿 ${id}`);
    }
    expect(decode(encoded)).toEqual(source);
  });

  it.each(['__proto__', 'constructor', 'toString'])(
    'does not read an inherited draft for session %s',
    (id) => {
      const decoded = decode(envelope({ sessions: [storedSession({ id })], drafts: {} }));
      expect(decoded.sessions[0].draft).toBe('');
    },
  );
});

describe('decodeChatState validation', () => {
  it.each(['', '{', 'null', '"text"', '3', 'true'])(
    'rejects invalid JSON or a non-object root: %j',
    (raw) => {
      expect(() => decodeChatState(raw, [])).toThrow(Error);
    },
  );

  it.each<[string, unknown]>([
    ['version', undefined],
    ['version', 0],
    ['version', 2],
    ['version', '1'],
    ['sessions', undefined],
    ['sessions', {}],
    ['sessions', [null]],
    ['systemPrompts', undefined],
    ['systemPrompts', {}],
    ['systemPrompts', [null]],
    ['drafts', undefined],
    ['drafts', null],
    ['drafts', []],
    ['drafts', { s1: 42 }],
    ['drafts', { s1: null }],
    ['drafts', { '': 'Invalid ID' }],
  ])('rejects an invalid envelope field %s = %j', (field, value) => {
    expect(() => decode({ ...envelope(), [field]: value })).toThrow(Error);
  });

  it.each<[keyof StoredSession, unknown]>([
    ['id', undefined],
    ['id', ''],
    ['id', 1],
    ['items', undefined],
    ['items', {}],
    ['items', [null]],
    ['title', undefined],
    ['title', 1],
    ['type', undefined],
    ['type', 'chat'],
    ['selected', undefined],
    ['selected', 'true'],
    ['selectedSystemPromptId', undefined],
    ['selectedSystemPromptId', ''],
    ['selectedSystemPromptId', 1],
    ['systemPromptSnapshot', undefined],
    ['systemPromptSnapshot', []],
    ['systemPromptLocked', undefined],
    ['systemPromptLocked', 'true'],
  ])('rejects an invalid session field %s = %j without dropping the session', (field, value) => {
    const bad = { ...storedSession({ id: 'bad' }), [field]: value };
    expect(() => decode(envelope({ sessions: [storedSession(), bad] as StoredSession[] }))).toThrow(
      Error,
    );
  });

  it.each<[keyof StoredMessage, unknown]>([
    ['id', undefined],
    ['id', ''],
    ['id', 1],
    ['time', undefined],
    ['time', 1],
    ['role', undefined],
    ['role', 'assistant'],
    ['msg', undefined],
    ['msg', null],
    ['type', undefined],
    ['type', 'Chat'],
    ['status', null],
    ['status', 1],
  ])('rejects an invalid message field %s = %j without dropping the message', (field, value) => {
    const good = storedSession().items[0];
    const bad = { ...good, id: 'bad', [field]: value };
    expect(() => decode(storedSession({ items: [good, bad] as StoredMessage[] }))).toThrow(Error);
  });

  it.each<[keyof SystemPrompt, unknown]>([
    ['id', undefined],
    ['id', ''],
    ['id', 1],
    ['title', undefined],
    ['title', null],
    ['content', undefined],
    ['content', []],
  ])('rejects an invalid catalogue or snapshot field %s = %j', (field, value) => {
    const bad = { ...prompt, [field]: value };
    expect(() => decode({ ...envelope(), systemPrompts: [bad] })).toThrow(Error);
    expect(() =>
      decode({ ...storedSession(), systemPromptSnapshot: bad, systemPromptLocked: false }),
    ).toThrow(Error);
  });

  it.each([
    { selectedSystemPromptId: 'p1', systemPromptSnapshot: null },
    { selectedSystemPromptId: null, systemPromptSnapshot: prompt },
    { selectedSystemPromptId: 'different', systemPromptSnapshot: prompt },
  ])('rejects a mismatched locked prompt selection %j', (selection) => {
    expect(() => decode(storedSession(selection))).toThrow(Error);
  });

  it('rejects duplicate session IDs in envelopes and bare arrays', () => {
    const sessions = [storedSession(), storedSession()];

    expect(() => decode(envelope({ sessions }))).toThrow(Error);
    expect(() => decode(sessions)).toThrow(Error);
  });

  it('rejects duplicate message IDs within one session', () => {
    const message = storedSession().items[0];

    expect(() =>
      decode(storedSession({ items: [message, { ...message, msg: 'Different' }] })),
    ).toThrow(Error);
  });

  it('rejects duplicate catalogue IDs and duplicate fallback IDs for bare imports', () => {
    const catalogue = [prompt, { ...prompt, content: 'Different' }];

    expect(() => decode(envelope({ systemPrompts: catalogue }))).toThrow(Error);
    expect(() => decode(storedSession(), catalogue)).toThrow(Error);
  });

  it('validates all messages and sessions even after finding the selected session', () => {
    const bad = { ...storedSession({ id: 's2', selected: false }), items: [{ role: 'ai' }] };

    expect(() => decode({ ...envelope(), sessions: [storedSession(), bad] })).toThrow(Error);
  });
});
