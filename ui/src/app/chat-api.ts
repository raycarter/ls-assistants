import { inject, Injectable, InjectionToken } from '@angular/core';
import type { ReasoningEffort } from './chat.models';
import { SseDecoder, type SseEvent } from './sse-decoder';

export const FETCH = new InjectionToken<typeof fetch>('FETCH', {
  providedIn: 'root',
  factory: () => globalThis.fetch.bind(globalThis),
});

export interface ModelsResponse {
  models: string[];
  default_model: string;
}

export interface ChatRequest {
  session_id: string;
  request_id: string;
  model: string;
  reasoning_effort: ReasoningEffort;
  system_prompt: string | null;
  messages: { role: 'user' | 'assistant'; content: string }[];
}

interface ReplyIdentity {
  session_id: string;
  request_id: string;
  message_id: string;
}

export type ChatEvent = ReplyIdentity &
  (
    | { type: 'start'; time: string }
    | { type: 'delta'; text: string }
    | { type: 'done'; time: string; status: 'complete' }
    | { type: 'error'; code: string; message: string }
  );

export class ChatApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function protocolError(): never {
  throw new ChatApiError('protocol_error', 'The reply stream was invalid. Please retry.');
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function nonempty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function validTime(value: unknown): value is string {
  return nonempty(value) && Number.isFinite(Date.parse(value));
}

/** Validate correlation before exposing any event to state. Unknown event types are ignored. */
export class ChatEventDecoder {
  private identity: ReplyIdentity | undefined;
  private startTime: string | undefined;
  private sequence = -1;
  private lastFrame = '';
  terminal = false;

  constructor(private readonly request: Pick<ChatRequest, 'session_id' | 'request_id'>) {}

  decode(frame: SseEvent): ChatEvent | null {
    if (!['start', 'delta', 'done', 'error'].includes(frame.event)) return null;
    let payload: unknown;
    try {
      payload = JSON.parse(frame.data);
    } catch {
      return protocolError();
    }
    if (!record(payload)) return protocolError();
    const { session_id, request_id, message_id, time } = payload;
    if (
      session_id !== this.request.session_id ||
      request_id !== this.request.request_id ||
      !nonempty(message_id)
    ) {
      return protocolError();
    }
    if (this.identity && message_id !== this.identity.message_id) {
      return protocolError();
    }
    const prefix = `${message_id}:`;
    const suffix = frame.id.slice(prefix.length);
    if (!frame.id.startsWith(prefix) || !/^\d+$/.test(suffix)) return protocolError();
    const sequence = Number(suffix);
    if (!Number.isSafeInteger(sequence)) return protocolError();
    const signature = `${frame.event}\n${frame.data}`;
    if (sequence === this.sequence && signature === this.lastFrame) return null;
    if (sequence <= this.sequence || this.terminal) return protocolError();

    const identity: ReplyIdentity = {
      session_id: this.request.session_id,
      request_id: this.request.request_id,
      message_id,
    };
    let event: ChatEvent;
    switch (frame.event) {
      case 'start':
        if (this.startTime !== undefined || !validTime(time)) return protocolError();
        this.startTime = time;
        event = { ...identity, type: 'start', time };
        break;
      case 'delta':
        if (this.startTime === undefined || typeof payload['text'] !== 'string')
          return protocolError();
        event = { ...identity, type: 'delta', text: payload['text'] };
        break;
      case 'done':
        if (
          this.startTime === undefined ||
          !validTime(time) ||
          time !== this.startTime ||
          payload['status'] !== 'complete'
        ) {
          return protocolError();
        }
        event = { ...identity, type: 'done', time, status: 'complete' };
        this.terminal = true;
        break;
      default:
        if (!nonempty(payload['code']) || !nonempty(payload['message'])) return protocolError();
        event = { ...identity, type: 'error', code: payload['code'], message: payload['message'] };
        this.terminal = true;
    }
    this.identity = identity;
    this.sequence = sequence;
    this.lastFrame = signature;
    return event;
  }
}

async function checkResponse(response: Response): Promise<void> {
  if (response.ok) return;
  let detail = '';
  try {
    const body: unknown = await response.json();
    if (record(body)) {
      if (typeof body['detail'] === 'string') detail = body['detail'];
      if (Array.isArray(body['detail'])) {
        detail = body['detail']
          .filter(record)
          .map((entry) => entry['msg'])
          .filter((message): message is string => typeof message === 'string')
          .join('; ');
      }
    }
  } catch {
    // Non-JSON HTTP errors still have an actionable status.
  }
  throw new ChatApiError(
    `http_${response.status}`,
    `Request failed (HTTP ${response.status}).${detail ? ` ${detail}` : ' Please retry.'}`,
  );
}

@Injectable({ providedIn: 'root' })
export class ChatApi {
  private readonly fetch = inject(FETCH);

  async models(signal: AbortSignal): Promise<ModelsResponse> {
    const response = await this.fetch('/api/models', { signal });
    await checkResponse(response);
    let body: unknown;
    try {
      body = await response.json();
    } catch (error) {
      if (signal.aborted || (record(error) && error['name'] === 'AbortError')) throw error;
      throw new ChatApiError(
        'models_response_error',
        'The models endpoint did not return valid JSON. Check the BFF connection and restart the frontend dev server to reload its API proxy.',
      );
    }
    if (
      !record(body) ||
      !Array.isArray(body['models']) ||
      !body['models'].length ||
      !body['models'].every(nonempty) ||
      !nonempty(body['default_model']) ||
      !body['models'].includes(body['default_model'])
    ) {
      throw new ChatApiError('models_error', 'The model list was invalid. Please reload models.');
    }
    return { models: [...new Set(body['models'])], default_model: body['default_model'] };
  }

  async stream(
    request: ChatRequest,
    signal: AbortSignal,
    onEvent: (event: ChatEvent) => void,
  ): Promise<void> {
    signal.throwIfAborted();
    const response = await this.fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(request),
      signal,
    });
    signal.throwIfAborted();
    await checkResponse(response);
    if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
      return protocolError();
    }
    const reader = response.body.getReader();
    const decoder = new SseDecoder();
    const events = new ChatEventDecoder(request);
    const cancel = () => void reader.cancel().catch(() => {});
    signal.addEventListener('abort', cancel, { once: true });
    try {
      while (!events.terminal) {
        signal.throwIfAborted();
        const chunk = await reader.read();
        signal.throwIfAborted();
        const frames = chunk.done ? decoder.finish() : decoder.push(chunk.value);
        for (const frame of frames) {
          const event = events.decode(frame);
          if (event) onEvent(event);
          if (events.terminal) break;
        }
        if (chunk.done && !events.terminal) {
          throw new ChatApiError('interrupted', 'The reply ended unexpectedly. Please retry.');
        }
      }
    } finally {
      signal.removeEventListener('abort', cancel);
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
