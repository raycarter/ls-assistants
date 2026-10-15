import { computed, DestroyRef, effect, inject, Injectable, signal, untracked } from '@angular/core';
import { ChatApi, ChatApiError, type ChatEvent, type ChatRequest } from './chat-api';
import type { ChatMessage, ChatSession } from './chat.models';
import { ChatState } from './chat-state';

interface Turn {
  request: ChatRequest;
  userId: string;
  controller: AbortController;
  replyId?: string;
  buffer: string;
  timer?: ReturnType<typeof setTimeout>;
}

function isFailed(message: ChatMessage): boolean {
  return ['error', 'stopped', 'interrupted'].includes(message.status ?? '');
}

function history(session: ChatSession, end = session.messages.length): ChatRequest['messages'] {
  return session.messages
    .slice(0, end)
    .filter(
      (message) =>
        message.content.length > 0 &&
        (message.role === 'user' || (!isFailed(message) && message.status !== 'streaming')),
    )
    .map(({ role, content }) => ({ role, content }));
}

@Injectable({ providedIn: 'root' })
export class ChatTurns {
  private readonly state = inject(ChatState);
  private readonly api = inject(ChatApi);
  private readonly turns = new Map<string, Turn>();
  private modelController?: AbortController;
  private destroyed = false;
  readonly models = signal<string[]>([]);
  readonly defaultModel = signal('');
  readonly modelsLoading = signal(false);
  readonly modelsError = signal<string | null>(null);
  readonly pending = signal<ReadonlySet<string>>(new Set());
  readonly errors = signal<ReadonlyMap<string, string>>(new Map());
  readonly activePending = computed(() => this.pending().has(this.state.activeSessionId()));
  readonly selectedModel = computed(() => this.state.activeSession().model ?? this.defaultModel());
  readonly selectedEffort = computed(() => this.state.activeSession().reasoningEffort ?? 'medium');
  readonly canSend = computed(
    () =>
      !this.activePending() &&
      this.models().includes(this.selectedModel()) &&
      !!this.state.activeSession().draft.trim(),
  );
  readonly retryMessage = computed(() => {
    if (this.activePending()) return null;
    const session = this.state.activeSession();
    const last = session.messages.at(-1);
    if (last?.role === 'user') return last.content.length > 0 ? last : null;
    return last?.role === 'assistant' &&
      isFailed(last) &&
      last.replyTo &&
      session.messages.some(
        (message) =>
          message.id === last.replyTo && message.role === 'user' && message.content.length > 0,
      )
      ? last
      : null;
  });

  constructor() {
    effect(() => {
      const ids = new Set(this.state.sessions().map((session) => session.id));
      untracked(() => {
        for (const [id, turn] of this.turns) {
          if (!ids.has(id)) this.discard(id, turn);
        }
        const errors = this.errors();
        if ([...errors.keys()].some((id) => !ids.has(id))) {
          this.errors.set(new Map([...errors].filter(([id]) => ids.has(id))));
        }
      });
    });
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      this.modelController?.abort();
      for (const id of [...this.turns.keys()]) this.stop(id);
    });
    void this.loadModels();
  }

  async loadModels(): Promise<void> {
    this.modelController?.abort();
    const controller = new AbortController();
    this.modelController = controller;
    this.modelsLoading.set(true);
    this.modelsError.set(null);
    try {
      const response = await this.api.models(controller.signal);
      if (controller.signal.aborted || this.destroyed) return;
      this.models.set(response.models);
      this.defaultModel.set(response.default_model);
    } catch (error) {
      if (!controller.signal.aborted && !this.destroyed) {
        this.modelsError.set(error instanceof Error ? error.message : 'Could not load models.');
      }
    } finally {
      if (this.modelController === controller && !this.destroyed) this.modelsLoading.set(false);
    }
  }

  send(): boolean {
    if (!this.canSend()) return false;
    const model = this.selectedModel();
    const effort = this.selectedEffort();
    if (!this.state.sendMessage()) return false;
    const session = this.state.activeSession();
    // Save the resolved defaults as the session's preference on its first turn.
    this.state.updateSession(session.id, (entry) => ({ ...entry, model, reasoningEffort: effort }));
    this.begin(
      {
        session_id: session.id,
        request_id: crypto.randomUUID(),
        model,
        reasoning_effort: effort,
        system_prompt: session.prompt?.content ?? null,
        messages: history(session),
      },
      session.messages.at(-1)!.id,
    );
    return true;
  }

  retry(): boolean {
    const target = this.retryMessage();
    const session = this.state.activeSession();
    if (!target || this.activePending() || !this.models().includes(this.selectedModel()))
      return false;
    const userId = target.role === 'user' ? target.id : target.replyTo!;
    const userIndex = session.messages.findIndex((message) => message.id === userId);
    const request: ChatRequest = {
      session_id: session.id,
      request_id: crypto.randomUUID(),
      model: this.selectedModel(),
      reasoning_effort: this.selectedEffort(),
      system_prompt: session.prompt?.content ?? null,
      messages: history(session, userIndex + 1),
    };
    this.state.updateSession(session.id, (entry) => ({
      ...entry,
      model: request.model,
      reasoningEffort: request.reasoning_effort,
      promptLocked: true,
      messages:
        target.role === 'assistant'
          ? entry.messages.filter((message) => message.id !== target.id)
          : entry.messages,
    }));
    this.begin(request, userId);
    return true;
  }

  stop(id = this.state.activeSessionId()): void {
    const turn = this.turns.get(id);
    if (!turn) return;
    this.finish(turn, 'stopped', { code: 'cancelled', message: 'Reply stopped. You can retry.' });
    turn.controller.abort();
  }

  deleteSession(id: string): void {
    const turn = this.turns.get(id);
    if (turn) this.discard(id, turn);
    this.errors.update((errors) => {
      const next = new Map(errors);
      next.delete(id);
      return next;
    });
    this.state.deleteSession(id);
  }

  private begin(request: ChatRequest, userId: string): void {
    const turn: Turn = { request, userId, controller: new AbortController(), buffer: '' };
    this.turns.set(request.session_id, turn);
    this.pending.set(new Set(this.turns.keys()));
    this.errors.update((errors) => {
      const next = new Map(errors);
      next.delete(request.session_id);
      return next;
    });
    void this.run(turn);
  }

  private async run(turn: Turn): Promise<void> {
    try {
      await this.api.stream(turn.request, turn.controller.signal, (event) =>
        this.receive(turn, event),
      );
      if (this.current(turn)) {
        this.finish(turn, 'error', {
          code: 'interrupted',
          message: 'The reply ended unexpectedly. Please retry.',
        });
      }
    } catch (error) {
      if (!this.current(turn)) return;
      this.finish(turn, 'error', {
        code: error instanceof ChatApiError ? error.code : 'network_error',
        message:
          error instanceof Error ? error.message : 'The reply could not be received. Please retry.',
      });
    }
  }

  private current(turn: Turn): boolean {
    return (
      this.turns.get(turn.request.session_id) === turn &&
      this.state.sessions().some((session) => session.id === turn.request.session_id)
    );
  }

  private receive(turn: Turn, event: ChatEvent): void {
    if (!this.current(turn)) return;
    // The transport validates IDs; retain a guard at the state boundary for injected clients.
    if (
      event.session_id !== turn.request.session_id ||
      event.request_id !== turn.request.request_id ||
      (turn.replyId && turn.replyId !== event.message_id)
    ) {
      throw new ChatApiError('protocol_error', 'The reply stream was invalid. Please retry.');
    }
    if (!turn.replyId) {
      if (event.type !== 'start' && event.type !== 'error') {
        throw new ChatApiError('protocol_error', 'The reply stream was invalid. Please retry.');
      }
      this.createReply(
        turn,
        event.message_id,
        event.type === 'start' ? event.time : new Date().toISOString(),
      );
    }
    if (event.type === 'delta') {
      turn.buffer += event.text;
      turn.timer ??= setTimeout(() => this.flushText(turn), 32);
    } else if (event.type === 'done') {
      this.finish(turn, 'complete');
    } else if (event.type === 'error') {
      this.finish(turn, 'error', { code: event.code, message: event.message });
    }
  }

  private createReply(turn: Turn, id: string, time: string): void {
    this.state.updateSession(turn.request.session_id, (session) => {
      if (session.messages.some((message) => message.id === id)) {
        throw new ChatApiError(
          'protocol_error',
          'The reply message ID was already used. Please retry.',
        );
      }
      return {
        ...session,
        messages: [
          ...session.messages,
          {
            id,
            time,
            role: 'assistant',
            content: '',
            status: 'streaming',
            requestId: turn.request.request_id,
            replyTo: turn.userId,
          },
        ],
      };
    });
    turn.replyId = id;
  }

  private flushText(turn: Turn): void {
    clearTimeout(turn.timer);
    turn.timer = undefined;
    if (!this.current(turn) || !turn.buffer) return;
    const text = turn.buffer;
    turn.buffer = '';
    this.state.updateSession(turn.request.session_id, (session) => ({
      ...session,
      messages: session.messages.map((message) =>
        message.id === turn.replyId ? { ...message, content: message.content + text } : message,
      ),
    }));
  }

  private finish(turn: Turn, status: string, error?: ChatMessage['error']): void {
    if (!this.current(turn)) return;
    if (!turn.replyId) this.createReply(turn, crypto.randomUUID(), new Date().toISOString());
    this.flushText(turn);
    this.state.updateSession(turn.request.session_id, (session) => ({
      ...session,
      messages: session.messages.map((message) =>
        message.id === turn.replyId ? { ...message, status, ...(error ? { error } : {}) } : message,
      ),
    }));
    if (error)
      this.errors.update((errors) => new Map(errors).set(turn.request.session_id, error.message));
    this.turns.delete(turn.request.session_id);
    this.pending.set(new Set(this.turns.keys()));
    this.state.flushPersistence();
  }

  private discard(id: string, turn: Turn): void {
    this.turns.delete(id);
    clearTimeout(turn.timer);
    turn.controller.abort();
    this.pending.set(new Set(this.turns.keys()));
  }
}
