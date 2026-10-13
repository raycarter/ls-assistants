import { computed, Injectable, signal } from '@angular/core';
import { ChatSession, PromptDraft, SystemPrompt } from './chat.models';

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

function demoSession(): ChatSession {
  return {
    ...emptySession('markdown-demo'),
    title: 'Markdown preview (demo)',
    prompt: { ...INITIAL_PROMPTS[0] },
    promptLocked: true,
    messages: [
      {
        id: 'demo-question',
        role: 'user',
        content: 'Show me a **Markdown** example with a list, a table, and some code.',
      },
      {
        id: 'demo-answer',
        role: 'assistant',
        content: [
          '## Markdown preview',
          '',
          'This is a **local demo**, not a live AI response.',
          '',
          '- **Bold**, *italic*, and `inline code`',
          '- Lists, links, and fenced code blocks',
          '',
          '> Choose a system prompt before sending the first message in a new session.',
          '',
          '| Action | Keyboard shortcut |',
          '| --- | --- |',
          '| Send a message | Enter |',
          '| Add a new line | Shift + Enter |',
          '',
          '```typescript',
          'const message = "Hello, assistant!";',
          'console.log(message);',
          '```',
          '',
          'Learn more about [Markdown](https://www.markdownguide.org/).',
        ].join('\n'),
      },
    ],
  };
}

@Injectable({ providedIn: 'root' })
export class ChatState {
  readonly prompts = signal<SystemPrompt[]>(INITIAL_PROMPTS.map((prompt) => ({ ...prompt })));
  readonly sessions = signal<ChatSession[]>([emptySession('initial-session'), demoSession()]);
  readonly activeSessionId = signal('initial-session');
  readonly activeSession = computed(() =>
    this.sessions().find((session) => session.id === this.activeSessionId())!,
  );

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
      messages: [...session.messages, { id: crypto.randomUUID(), role: 'user', content }],
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
    this.sessions.update((sessions) =>
      sessions.map((session) =>
        session.id === this.activeSessionId() ? update(session) : session,
      ),
    );
  }
}
