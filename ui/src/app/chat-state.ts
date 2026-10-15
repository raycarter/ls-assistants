import {
  computed,
  DestroyRef,
  effect,
  inject,
  Injectable,
  InjectionToken,
  signal,
} from '@angular/core';
import type {
  ChatSession,
  ChatSnapshot,
  PromptDraft,
  ReasoningEffort,
  SystemPrompt,
} from './chat.models';
import { decodeChatState, encodeChatState } from './chat-storage';

export const CHAT_STORAGE_KEY = 'ls-assistants.chat-state';
export type ChatStorage = Pick<Storage, 'getItem' | 'setItem'>;

export const CHAT_STORAGE = new InjectionToken<ChatStorage | null>('CHAT_STORAGE', {
  providedIn: 'root',
  factory: () => {
    try {
      return typeof window === 'undefined' ? null : window.localStorage;
    } catch {
      return null;
    }
  },
});

const INITIAL_PROMPTS: SystemPrompt[] = [
  {
    id: 'it-assistant',
    title: 'IT-personal-assistant',
    content:
      'Help with software development, DevOps, and technical decisions. Be clear and practical.',
  },
  {
    id: 'language-correction',
    title: '德语英语更正',
    content: 'Correct German and English writing, and briefly explain the important changes.',
  },
  {
    id: 'translation',
    title: '语言翻译',
    content:
      'Translate the supplied text accurately, preserving its meaning, tone, and formatting.',
  },
];

function emptySession(id: string): ChatSession {
  return {
    id,
    title: 'New session',
    mode: 'chat',
    messages: [],
    draft: '',
    prompt: null,
    promptLocked: false,
  };
}

function initialSnapshot(): ChatSnapshot {
  return {
    prompts: INITIAL_PROMPTS.map((prompt) => ({ ...prompt })),
    sessions: [emptySession('initial-session')],
    activeSessionId: 'initial-session',
  };
}

function readInitialState(storage: ChatStorage | null): {
  snapshot: ChatSnapshot;
  canPersist: boolean;
  error: string | null;
} {
  const fallback = initialSnapshot();
  if (!storage) {
    return {
      snapshot: fallback,
      canPersist: false,
      error: 'Browser storage is unavailable. Changes will remain in memory only.',
    };
  }

  let raw: string | null;
  try {
    raw = storage.getItem(CHAT_STORAGE_KEY);
  } catch {
    return {
      snapshot: fallback,
      canPersist: false,
      error:
        'Saved chat data could not be read. Storage was left untouched; changes remain in memory.',
    };
  }
  if (raw === null) return { snapshot: fallback, canPersist: true, error: null };

  try {
    const snapshot = decodeChatState(raw, fallback.prompts);
    for (const session of snapshot.sessions) {
      session.messages = session.messages.map((message) =>
        message.role === 'assistant' && message.status === 'streaming'
          ? { ...message, status: 'interrupted' }
          : message,
      );
    }
    if (!snapshot.sessions.length) {
      const session = emptySession(crypto.randomUUID());
      snapshot.sessions = [session];
      snapshot.activeSessionId = session.id;
    }
    return { snapshot, canPersist: true, error: null };
  } catch {
    return {
      snapshot: fallback,
      canPersist: false,
      error:
        'Saved chat data is invalid or unsupported. Storage was left untouched; changes remain in memory.',
    };
  }
}

@Injectable({ providedIn: 'root' })
export class ChatState {
  private readonly storage = inject(CHAT_STORAGE);
  private readonly initial = readInitialState(this.storage);
  private lastStorageWarning: string | null = null;
  private persistTimer: ReturnType<typeof setTimeout> | undefined;
  private lastPersisted: ChatSnapshot | undefined;

  readonly prompts = signal<SystemPrompt[]>(this.initial.snapshot.prompts);
  readonly sessions = signal<ChatSession[]>(this.initial.snapshot.sessions);
  readonly activeSessionId = signal(this.initial.snapshot.activeSessionId);
  readonly activeSession = computed(() =>
    this.sessions().find((session) => session.id === this.activeSessionId())!,
  );
  readonly persistenceError = signal<string | null>(this.initial.error);

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      if (this.persistTimer !== undefined) this.flushPersistence();
    });
    if (this.initial.error) this.reportStorageError(this.initial.error);
    if (!this.storage || !this.initial.canPersist) return;

    // Hydration finishes before this effect runs. Signals remain the UI's source of truth.
    effect(() => {
      const snapshot = this.snapshot();
      if (
        snapshot.sessions.some((session) => session.messages.some((m) => m.status === 'streaming'))
      ) {
        // Defer encoding AND stringification, not just the storage write.
        this.persistTimer ??= setTimeout(() => this.flushPersistence(), 200);
      } else {
        this.flushPersistence();
      }
    });
  }

  flushPersistence(): void {
    clearTimeout(this.persistTimer);
    this.persistTimer = undefined;
    if (!this.storage || !this.initial.canPersist) return;
    const snapshot = this.snapshot();
    const last = this.lastPersisted;
    if (
      last?.sessions === snapshot.sessions &&
      last.prompts === snapshot.prompts &&
      last.activeSessionId === snapshot.activeSessionId
    )
      return;
    try {
      this.storage.setItem(CHAT_STORAGE_KEY, JSON.stringify(encodeChatState(snapshot)));
      this.lastPersisted = snapshot;
      this.persistenceError.set(null);
      this.lastStorageWarning = null;
    } catch {
      this.reportStorageError(
        'Chat data could not be saved. Current changes remain in memory only.',
      );
    }
  }

  private snapshot(): ChatSnapshot {
    return {
      sessions: this.sessions(),
      prompts: this.prompts(),
      activeSessionId: this.activeSessionId(),
    };
  }

  selectModel(model: string): void {
    this.updateActiveSession((session) => ({ ...session, model }));
  }

  selectReasoningEffort(reasoningEffort: ReasoningEffort): void {
    this.updateActiveSession((session) => ({ ...session, reasoningEffort }));
  }

  updateSession(id: string, update: (session: ChatSession) => ChatSession): void {
    this.sessions.update((sessions) =>
      sessions.map((session) => (session.id === id ? update(session) : session)),
    );
  }

  newSession(): void {
    const session = emptySession(crypto.randomUUID());
    this.sessions.update((sessions) => [session, ...sessions]);
    this.activeSessionId.set(session.id);
  }

  selectSession(id: string): void {
    if (this.sessions().some((session) => session.id === id)) {
      this.activeSessionId.set(id);
    }
  }

  deleteSession(id: string): void {
    if (!this.sessions().some((session) => session.id === id)) return;

    const remaining = this.sessions().filter((session) => session.id !== id);
    if (!remaining.length) remaining.push(emptySession(crypto.randomUUID()));
    this.sessions.set(remaining);
    if (this.activeSessionId() === id) this.activeSessionId.set(remaining[0].id);
  }

  updateDraft(draft: string): void {
    this.updateActiveSession((session) => ({ ...session, draft }));
  }

  sendMessage(): boolean {
    const content = this.activeSession().draft.trim();
    if (!content) return false;

    this.updateActiveSession((session) => ({
      ...session,
      title: session.messages.some((message) => message.role === 'user')
        ? session.title
        : content.replace(/\s+/g, ' ').slice(0, 60),
      draft: '',
      prompt: session.prompt ? { ...session.prompt } : null,
      promptLocked: true,
      messages: [
        ...session.messages,
        { id: crypto.randomUUID(), role: 'user', content, time: new Date().toISOString() },
      ],
    }));
    return true;
  }

  selectPrompt(id: string | null): void {
    if (this.activeSession().promptLocked) return;
    const prompt = id === null ? null : this.prompts().find((entry) => entry.id === id);
    if (prompt === undefined) return;
    this.updateActiveSession((session) => ({ ...session, prompt: prompt ? { ...prompt } : null }));
  }

  savePrompt(draft: PromptDraft): void {
    const title = draft.title.trim();
    const content = draft.content.trim();
    if (!title || !content) return;
    if (draft.id && !this.prompts().some((prompt) => prompt.id === draft.id)) return;

    const prompt: SystemPrompt = { id: draft.id ?? crypto.randomUUID(), title, content };
    this.prompts.update((prompts) =>
      draft.id
        ? prompts.map((entry) => (entry.id === draft.id ? prompt : entry))
        : [...prompts, prompt],
    );
    this.sessions.update((sessions) =>
      sessions.map((session) =>
        !session.promptLocked && session.prompt?.id === prompt.id
          ? { ...session, prompt: { ...prompt } }
          : session,
      ),
    );
  }

  deletePrompt(id: string): void {
    this.prompts.update((prompts) => prompts.filter((prompt) => prompt.id !== id));
    this.sessions.update((sessions) =>
      sessions.map((session) =>
        !session.promptLocked && session.prompt?.id === id ? { ...session, prompt: null } : session,
      ),
    );
  }

  private updateActiveSession(update: (session: ChatSession) => ChatSession): void {
    this.updateSession(this.activeSessionId(), update);
  }

  private reportStorageError(message: string): void {
    this.persistenceError.set(message);
    if (message !== this.lastStorageWarning) console.warn(message);
    this.lastStorageWarning = message;
  }
}
