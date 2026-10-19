import type { ChatEvent, ChatRequest, ModelsResponse } from './chat-api';

export class FakeChatApi {
  readonly calls: {
    request: ChatRequest;
    signal: AbortSignal;
    emit: (event: ChatEvent) => void;
    resolve: () => void;
    reject: (error: Error) => void;
  }[] = [];
  modelsResponse: ModelsResponse = { models: ['model-a', 'model-b'], default_model: 'model-a' };

  async models(_signal: AbortSignal): Promise<ModelsResponse> {
    return this.modelsResponse;
  }

  stream(
    request: ChatRequest,
    signal: AbortSignal,
    emit: (event: ChatEvent) => void,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      this.calls.push({ request, signal, emit, resolve, reject });
    });
  }

  event(index: number, type: ChatEvent['type'], text = '', overrides = {}): void {
    const call = this.calls[index];
    const identity = {
      session_id: call.request.session_id,
      request_id: call.request.request_id,
      message_id: `reply-${index}`,
    };
    const extra =
      type === 'session_title'
        ? { title: text }
        : type === 'delta'
          ? { text }
          : type === 'done'
            ? { status: 'complete', time: '2026-10-13T12:00:00.000Z' }
            : type === 'error'
              ? { code: 'unsupported_reasoning', message: 'This model does not support xhigh.' }
              : { time: '2026-10-13T12:00:00.000Z' };
    call.emit({ ...identity, type, ...extra, ...overrides } as ChatEvent);
    if (type === 'done' || type === 'error') call.resolve();
  }
}

export const completedChatApi = {
  models: async () => ({ models: ['model-a', 'model-b'], default_model: 'model-a' }),
  stream: async (request: ChatRequest, _signal: AbortSignal, emit: (event: ChatEvent) => void) => {
    const identity = {
      session_id: request.session_id,
      request_id: request.request_id,
      message_id: crypto.randomUUID(),
      time: new Date().toISOString(),
    };
    emit({ ...identity, type: 'start' });
    emit({ ...identity, type: 'done', status: 'complete' });
  },
};
