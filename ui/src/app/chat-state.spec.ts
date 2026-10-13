import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { CHAT_STORAGE, ChatState, type ChatStorage } from './chat-state';
import type { ChatSession, PromptDraft } from './chat.models';

describe('ChatState', () => {
  let state: ChatState;

  beforeEach(() => {
    const storage: ChatStorage = { getItem: () => null, setItem: () => {} };
    TestBed.configureTestingModule({
      providers: [ChatState, { provide: CHAT_STORAGE, useValue: storage }],
    });
    state = TestBed.inject(ChatState);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function session(id: string): ChatSession {
    const found = state.sessions().find((entry) => entry.id === id);
    if (!found) throw new Error(`Missing session: ${id}`);
    return found;
  }

  function sharedPromptSessions() {
    const prompt = { ...state.prompts()[0] };
    state.selectPrompt(prompt.id);
    state.updateDraft('Lock this prompt');
    state.sendMessage();
    const locked = state.activeSession();

    state.newSession();
    state.selectPrompt(prompt.id);
    state.updateDraft('Keep this unsent draft');
    const firstUnlocked = state.activeSession();

    state.newSession();
    state.selectPrompt(prompt.id);
    const secondUnlocked = state.activeSession();

    state.newSession();
    state.selectPrompt(state.prompts()[1].id);
    const unrelated = state.activeSession();
    state.selectSession(locked.id);

    return { prompt, locked, firstUnlocked, secondUnlocked, unrelated };
  }

  describe('session history and drafts', () => {
    it('starts in an empty unlocked chat, with the Markdown demo kept in a separate session', () => {
      expect(state.activeSession()).toMatchObject({
        title: 'New session',
        mode: 'chat',
        messages: [],
        draft: '',
        prompt: null,
        promptLocked: false,
      });
      const demo = state.sessions().find((entry) => entry.id !== state.activeSessionId());
      expect(demo).toMatchObject({ title: 'Markdown preview (demo)', promptLocked: true });
      expect(demo?.messages.map((message) => message.role)).toEqual(['user', 'assistant']);
    });

    it('prepends and activates a fresh session without changing the previous session', () => {
      state.selectPrompt(state.prompts()[0].id);
      state.updateDraft('An unfinished message');
      const previous = state.activeSession();
      const previousSessions = [...state.sessions()];

      state.newSession();

      expect(state.activeSessionId()).not.toBe(previous.id);
      expect(state.sessions()[0]).toBe(state.activeSession());
      expect(state.sessions().slice(1)).toEqual(previousSessions);
      expect(session(previous.id)).toBe(previous);
      expect(state.activeSession()).toMatchObject({
        title: 'New session',
        mode: 'chat',
        messages: [],
        draft: '',
        prompt: null,
        promptLocked: false,
      });
    });

    it('preserves independent drafts and prompt selections when switching sessions', () => {
      const firstId = state.activeSessionId();
      const firstPrompt = state.prompts()[0];
      const secondPrompt = state.prompts()[1];
      state.selectPrompt(firstPrompt.id);
      state.updateDraft('First draft\nwith a second line');
      state.newSession();
      const secondId = state.activeSessionId();
      state.selectPrompt(secondPrompt.id);
      state.updateDraft('Second draft');

      state.selectSession(firstId);
      expect(state.activeSession().draft).toBe('First draft\nwith a second line');
      expect(state.activeSession().prompt).toEqual(firstPrompt);

      state.selectSession(secondId);
      expect(state.activeSession().draft).toBe('Second draft');
      expect(state.activeSession().prompt).toEqual(secondPrompt);
      expect(session(firstId).messages).toEqual([]);
      expect(session(secondId).messages).toEqual([]);
    });

    it('ignores selection and deletion of unknown session IDs', () => {
      const sessions = state.sessions();
      const activeId = state.activeSessionId();

      state.selectSession('missing-session');
      state.deleteSession('missing-session');

      expect(state.sessions()).toBe(sessions);
      expect(state.activeSessionId()).toBe(activeId);
    });

    it('keeps the active session when deleting another session and selects a survivor when deleting the active one', () => {
      const first = state.activeSession();
      state.newSession();
      const second = state.activeSession();
      state.newSession();
      const third = state.activeSession();

      state.deleteSession(second.id);

      expect(state.sessions().some((entry) => entry.id === second.id)).toBe(false);
      expect(state.activeSession()).toBe(third);
      expect(session(first.id)).toBe(first);

      state.deleteSession(third.id);

      expect(state.activeSession()).toBe(first);
      expect(state.activeSessionId()).toBe(state.sessions()[0].id);
    });

    it('replaces the last deleted session with one empty, unlocked session', () => {
      for (const entry of [...state.sessions()]) {
        if (entry.id !== state.activeSessionId()) state.deleteSession(entry.id);
      }
      state.selectPrompt(state.prompts()[0].id);
      state.updateDraft('A message in the session being deleted');
      state.sendMessage();
      state.updateDraft('Discard this draft too');
      const deletedId = state.activeSessionId();

      state.deleteSession(deletedId);

      expect(state.sessions()).toHaveLength(1);
      expect(state.activeSessionId()).not.toBe(deletedId);
      expect(state.activeSession()).toBe(state.sessions()[0]);
      expect(state.activeSession()).toMatchObject({
        title: 'New session',
        mode: 'chat',
        messages: [],
        draft: '',
        prompt: null,
        promptLocked: false,
      });

      const replacementId = state.activeSessionId();
      state.deleteSession(replacementId);
      expect(state.sessions()).toHaveLength(1);
      expect(state.activeSessionId()).not.toBe(replacementId);
      expect(state.activeSession()).toBe(state.sessions()[0]);
    });
  });

  describe('sending messages', () => {
    it.each(['', ' \n\t '])('rejects an empty or whitespace-only draft: %j', (draft) => {
      state.selectPrompt(state.prompts()[0].id);
      state.updateDraft(draft);
      const sessions = state.sessions();
      const active = state.activeSession();

      expect(state.sendMessage()).toBe(false);

      expect(state.sessions()).toBe(sessions);
      expect(state.activeSession()).toBe(active);
      expect(active.messages).toEqual([]);
      expect(active.promptLocked).toBe(false);
      expect(active.draft).toBe(draft);
      expect(active.title).toBe('New session');
    });

    it('appends exactly one trimmed user message, clears the draft and derives the first-message title', () => {
      const otherSessions = state
        .sessions()
        .filter((entry) => entry.id !== state.activeSessionId());
      state.updateDraft(' \n Hello   world!\nSecond line \t ');
      const beforeSend = Date.now();

      expect(state.sendMessage()).toBe(true);

      expect(state.activeSession().messages).toEqual([
        {
          id: expect.any(String),
          role: 'user',
          content: 'Hello   world!\nSecond line',
          time: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/),
        },
      ]);
      const time = state.activeSession().messages[0].time!;
      expect(new Date(time).toISOString()).toBe(time);
      expect(Date.parse(time)).toBeGreaterThanOrEqual(beforeSend);
      expect(Date.parse(time)).toBeLessThanOrEqual(Date.now());
      expect(state.activeSession().draft).toBe('');
      expect(state.activeSession().title).toBe('Hello world! Second line');
      for (const other of otherSessions) expect(session(other.id)).toBe(other);

      expect(state.sendMessage()).toBe(false);
      expect(state.activeSession().messages).toHaveLength(1);
    });

    it('limits the title to 60 characters without truncating the message', () => {
      const content = `A long message ${'word '.repeat(20)}end`;
      state.updateDraft(content);

      state.sendMessage();

      expect(state.activeSession().title).toBe(content.slice(0, 60));
      expect(state.activeSession().messages[0].content).toBe(content);
    });

    it('keeps the first title and earlier messages when sending again, with no generated assistant reply', () => {
      state.updateDraft('First message');
      state.sendMessage();
      const firstMessage = state.activeSession().messages[0];
      state.updateDraft('A different second message');

      expect(state.sendMessage()).toBe(true);

      expect(state.activeSession().title).toBe('First message');
      expect(state.activeSession().messages).toHaveLength(2);
      expect(state.activeSession().messages[0]).toBe(firstMessage);
      expect(state.activeSession().messages[1]).toMatchObject({
        role: 'user',
        content: 'A different second message',
      });
      expect(state.activeSession().messages[1].id).not.toBe(firstMessage.id);
      expect(state.activeSession().messages.map((message) => message.role)).toEqual([
        'user',
        'user',
      ]);
      expect(state.activeSession().draft).toBe('');
    });

    it('can append to the demo without replacing its title or adding another assistant response', () => {
      const demo = state.sessions().find((entry) => entry.title === 'Markdown preview (demo)')!;
      state.selectSession(demo.id);
      state.updateDraft('Follow-up to the local demo');

      state.sendMessage();

      expect(state.activeSession().title).toBe(demo.title);
      expect(state.activeSession().messages.slice(0, 2)).toEqual(demo.messages);
      expect(state.activeSession().messages.map((message) => message.role)).toEqual([
        'user',
        'assistant',
        'user',
      ]);
    });
  });

  describe('prompt selection and locking', () => {
    it('copies a selected catalogue entry, ignores unknown IDs and allows clearing an unlocked selection', () => {
      const prompt = state.prompts()[0];
      state.selectPrompt(prompt.id);
      const selected = state.activeSession();

      expect(selected.prompt).toEqual(prompt);
      expect(selected.prompt).not.toBe(prompt);
      state.selectPrompt('missing-prompt');
      expect(state.activeSession()).toBe(selected);

      state.selectPrompt(null);
      expect(state.activeSession().prompt).toBeNull();
      expect(state.activeSession().promptLocked).toBe(false);
    });

    it.each([true, false])(
      'locks the first-message snapshot per session (selected prompt: %s)',
      (selected) => {
        const prompt = state.prompts()[0];
        const alternative = state.prompts()[1];
        if (selected) state.selectPrompt(prompt.id);
        state.updateDraft('First message locks this choice');

        state.sendMessage();
        const locked = state.activeSession();

        expect(locked.promptLocked).toBe(true);
        expect(locked.prompt).toEqual(selected ? prompt : null);
        if (selected) expect(locked.prompt).not.toBe(prompt);
        state.selectPrompt(alternative.id);
        state.selectPrompt(null);
        expect(state.activeSession()).toBe(locked);

        state.newSession();
        expect(state.activeSession().promptLocked).toBe(false);
        state.selectPrompt(alternative.id);
        expect(state.activeSession().prompt).toEqual(alternative);

        state.selectSession(locked.id);
        expect(state.activeSession()).toBe(locked);
      },
    );
  });

  describe('prompt catalogue', () => {
    it('adds a trimmed prompt with a new ID without changing any session selection', () => {
      state.selectPrompt(state.prompts()[0].id);
      const sessions = [...state.sessions()];
      const previousPrompts = [...state.prompts()];

      state.savePrompt({ title: '  New helper  ', content: '\n First line.\nSecond line. \n' });

      expect(state.prompts()).toHaveLength(previousPrompts.length + 1);
      expect(state.prompts().slice(0, -1)).toEqual(previousPrompts);
      const added = state.prompts().at(-1)!;
      expect(added).toEqual({
        id: expect.any(String),
        title: 'New helper',
        content: 'First line.\nSecond line.',
      });
      expect(previousPrompts.some((entry) => entry.id === added.id)).toBe(false);
      for (const previous of sessions) expect(session(previous.id)).toBe(previous);
    });

    it.each<PromptDraft>([
      { title: ' \t ', content: 'Valid content' },
      { title: 'Valid title', content: ' \n ' },
      { title: '', content: '' },
      { id: 'it-assistant', title: ' ', content: 'Invalid edit' },
      { id: 'missing-prompt', title: 'Unknown edit', content: 'Valid content' },
    ])('rejects an invalid prompt draft without changing catalogue or sessions: %j', (draft) => {
      state.selectPrompt(state.prompts()[0].id);
      const prompts = state.prompts();
      const sessions = state.sessions();

      state.savePrompt(draft);

      expect(state.prompts()).toBe(prompts);
      expect(state.sessions()).toBe(sessions);
    });

    it('updates every unlocked selection on edit while preserving locked snapshots and unrelated sessions', () => {
      const { prompt, locked, firstUnlocked, secondUnlocked, unrelated } = sharedPromptSessions();
      const revised = {
        id: prompt.id,
        title: 'Revised helper',
        content: 'Use the revised guidance.',
      };

      state.savePrompt({
        ...revised,
        title: ` ${revised.title} `,
        content: `\n${revised.content}\n`,
      });

      expect(state.prompts().find((entry) => entry.id === prompt.id)).toEqual(revised);
      expect(session(locked.id)).toBe(locked);
      expect(session(locked.id).prompt).toEqual(prompt);
      for (const unlocked of [firstUnlocked, secondUnlocked]) {
        expect(session(unlocked.id).prompt).toEqual(revised);
        expect(session(unlocked.id).prompt).not.toBe(
          state.prompts().find((entry) => entry.id === prompt.id),
        );
        expect(session(unlocked.id).draft).toBe(unlocked.draft);
        expect(session(unlocked.id).messages).toBe(unlocked.messages);
        expect(session(unlocked.id).promptLocked).toBe(false);
      }
      expect(session(firstUnlocked.id).prompt).not.toBe(session(secondUnlocked.id).prompt);
      expect(session(unrelated.id)).toBe(unrelated);
      expect(state.activeSessionId()).toBe(locked.id);
    });

    it('resets every unlocked selection on deletion while preserving the locked snapshot', () => {
      const { prompt, locked, firstUnlocked, secondUnlocked, unrelated } = sharedPromptSessions();

      state.deletePrompt(prompt.id);

      expect(state.prompts().some((entry) => entry.id === prompt.id)).toBe(false);
      expect(session(locked.id)).toBe(locked);
      expect(session(locked.id).prompt).toEqual(prompt);
      expect(session(locked.id).promptLocked).toBe(true);
      for (const unlocked of [firstUnlocked, secondUnlocked]) {
        expect(session(unlocked.id).prompt).toBeNull();
        expect(session(unlocked.id).promptLocked).toBe(false);
        expect(session(unlocked.id).draft).toBe(unlocked.draft);
      }
      expect(session(unrelated.id)).toBe(unrelated);

      state.selectSession(firstUnlocked.id);
      state.selectPrompt(prompt.id);
      expect(state.activeSession().prompt).toBeNull();
      state.savePrompt({ ...prompt, title: 'Do not resurrect this deleted entry' });
      expect(state.prompts().some((entry) => entry.id === prompt.id)).toBe(false);
    });

    it('leaves catalogue entries and selections intact when deleting an unknown prompt', () => {
      state.selectPrompt(state.prompts()[0].id);
      const prompts = [...state.prompts()];
      const sessions = [...state.sessions()];

      state.deletePrompt('missing-prompt');

      expect(state.prompts()).toEqual(prompts);
      for (const previous of sessions) expect(session(previous.id)).toBe(previous);
    });
  });
});
