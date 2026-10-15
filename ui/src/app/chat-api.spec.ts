import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChatApi, ChatEventDecoder, FETCH, type ChatRequest } from './chat-api';

const request: ChatRequest = {
  session_id: 'session',
  request_id: 'request',
  model: 'deployment',
  reasoning_effort: 'xhigh',
  system_prompt: 'Locked prompt',
  messages: [{ role: 'user', content: 'Hello 世界' }],
};
const identity = {
  session_id: 'session',
  request_id: 'request',
  message_id: 'reply',
};
const utf8 = new TextEncoder();

function frame(event: string, sequence: number, extra = {}) {
  const timestamp = event === 'start' || event === 'done' ? { time: '2026-10-13T12:00:00Z' } : {};
  return {
    event,
    id: `reply:${sequence}`,
    data: JSON.stringify({ ...identity, ...timestamp, ...extra }),
  };
}

function wire(event: string, sequence: number, extra = {}): string {
  const value = frame(event, sequence, extra);
  return `id: ${value.id}\r\nevent: ${event}\r\ndata: ${value.data}\r\n\r\n`;
}

function response(text: string, packetSize = 1): Response {
  const bytes = utf8.encode(text);
  return new Response(
    new ReadableStream({
      start(controller) {
        for (let i = 0; i < bytes.length; i += packetSize)
          controller.enqueue(bytes.slice(i, i + packetSize));
        controller.close();
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  );
}

afterEach(() => TestBed.resetTestingModule());

describe('ChatEventDecoder', () => {
  it('ignores unknown events without parsing their data', () => {
    expect(
      new ChatEventDecoder(request).decode({ event: 'future', id: '', data: 'not JSON' }),
    ).toBeNull();
  });

  it('deduplicates an identical SSE sequence and never duplicates text', () => {
    const decoder = new ChatEventDecoder(request);
    decoder.decode(frame('start', 1));
    expect(decoder.decode(frame('delta', 2, { text: 'hello' }))?.type).toBe('delta');
    expect(decoder.decode(frame('delta', 2, { text: 'hello' }))).toBeNull();
    expect(() => decoder.decode(frame('delta', 2, { text: 'changed' }))).toThrow(/invalid/);
  });

  it.each([
    { event: 'start', id: 'reply:1', data: '{' },
    { event: 'start', id: 'reply:1', data: 'null' },
    frame('start', 1, { session_id: 'other' }),
    frame('start', 1, { request_id: 'other' }),
    frame('start', 1, { message_id: '' }),
    frame('start', 1, { time: null }),
    frame('start', 1, { time: 'invalid time' }),
    { ...frame('start', 1), id: 'wrong:1' },
    { ...frame('start', 1), id: 'reply:NaN' },
    frame('delta', 1, { text: 'before start' }),
    frame('done', 1, { status: 'complete' }),
    frame('error', 1, { code: 'error', message: 42 }),
  ])('rejects malformed or uncorrelated event %#', (event) => {
    expect(() => new ChatEventDecoder(request).decode(event)).toThrow(/invalid/);
  });

  it.each([
    frame('start', 2),
    frame('delta', 2, { text: null }),
    frame('delta', 2, { text: 'x', message_id: 'other' }),
    frame('done', 2, { status: 'complete', time: '2026-10-13T12:01:00Z' }),
    frame('done', 2, { status: 'complete', time: undefined }),
    frame('done', 2, { status: 'complete', time: null }),
    frame('done', 2, { status: 'complete', time: 'invalid time' }),
    frame('done', 2, { status: 'incomplete' }),
    frame('delta', 0, { text: 'out of order' }),
  ])('rejects changed identity, invalid order or payload after start %#', (event) => {
    const decoder = new ChatEventDecoder(request);
    decoder.decode(frame('start', 1));
    expect(() => decoder.decode(event)).toThrow(/invalid/);
  });

  it('accepts terminal errors before start and rejects events after a terminal', () => {
    const decoder = new ChatEventDecoder(request);
    expect(
      decoder.decode(
        frame('error', 1, { code: 'unsupported_reasoning', message: 'Unsupported xhigh' }),
      ),
    ).toEqual({
      ...identity,
      type: 'error',
      code: 'unsupported_reasoning',
      message: 'Unsupported xhigh',
    });
    expect(decoder.terminal).toBe(true);
    expect(() => decoder.decode(frame('start', 2))).toThrow();
  });
});

describe('ChatApi native fetch transport', () => {
  function api(fetch: typeof globalThis.fetch): ChatApi {
    TestBed.configureTestingModule({ providers: [{ provide: FETCH, useValue: fetch }] });
    return TestBed.inject(ChatApi);
  }

  it('sends the exact POST contract and streams Unicode deltas with no final content', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response(
          wire('start', 1) +
            wire('delta', 2, { text: '**世界 🌱' }) +
            wire('delta', 3, { text: '**' }) +
            wire('done', 4, { status: 'complete' }),
        ),
      );
    const events = vi.fn();
    const controller = new AbortController();
    await api(fetch).stream(request, controller.signal, events);
    expect(fetch).toHaveBeenCalledWith('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    expect(events.mock.calls.map(([event]) => event.type)).toEqual([
      'start',
      'delta',
      'delta',
      'done',
    ]);
    expect(events.mock.calls[1][0].text).toBe('**世界 🌱');
    expect(events.mock.calls[1][0]).not.toHaveProperty('time');
    expect(events.mock.calls[3][0]).not.toHaveProperty('content');
  });

  it('loads and validates models using GET and forwards the abort signal', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ models: ['a', 'b'], default_model: 'b' })));
    const signal = new AbortController().signal;
    expect(await api(fetch).models(signal)).toEqual({ models: ['a', 'b'], default_model: 'b' });
    expect(fetch).toHaveBeenCalledWith('/api/models', { signal });
  });

  it.each([
    { models: [], default_model: '' },
    { models: [1], default_model: 'a' },
    { models: ['a'], default_model: 'b' },
  ])('rejects invalid model lists %#', async (body) => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)));
    await expect(api(fetch).models(new AbortController().signal)).rejects.toThrow(/model list/);
  });

  it.each(['<!doctype html><html><body><app-root></app-root></body></html>', '{invalid json'])(
    'provides a proxy diagnostic when the models endpoint returns invalid JSON %#',
    async (body) => {
      const fetch = vi.fn().mockResolvedValue(new Response(body));
      await expect(api(fetch).models(new AbortController().signal)).rejects.toMatchObject({
        code: 'models_response_error',
        message: expect.stringMatching(/restart the frontend dev server.*API proxy/),
      });
    },
  );

  it('preserves cancellation while reading the model list', async () => {
    const response = new Response('{}');
    const error = new DOMException('The request was aborted.', 'AbortError');
    vi.spyOn(response, 'json').mockRejectedValue(error);
    const fetch = vi.fn().mockResolvedValue(response);
    await expect(api(fetch).models(new AbortController().signal)).rejects.toBe(error);
  });

  it('preserves HTTP errors instead of reporting an invalid model-list response', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response('<html>Unavailable</html>', { status: 503 }));
    await expect(api(fetch).models(new AbortController().signal)).rejects.toMatchObject({
      code: 'http_503',
      message: expect.stringMatching(/HTTP 503/),
    });
  });

  it.each([
    {
      status: 422,
      body: JSON.stringify({ detail: [{ loc: ['body', 'model'], msg: 'Model unavailable' }] }),
      text: 'Model unavailable',
    },
    {
      status: 400,
      body: JSON.stringify({ detail: 'Unsupported reasoning effort xhigh' }),
      text: 'Unsupported reasoning effort xhigh',
    },
    { status: 503, body: '<html>Unavailable</html>', text: 'HTTP 503' },
  ])(
    'surfaces useful HTTP $status errors before stream callbacks',
    async ({ status, body, text }) => {
      const fetch = vi.fn().mockResolvedValue(new Response(body, { status }));
      const events = vi.fn();
      await expect(
        api(fetch).stream(request, new AbortController().signal, events),
      ).rejects.toThrow(text);
      expect(events).not.toHaveBeenCalled();
    },
  );

  it.each([wire('start', 1), wire('start', 1) + wire('delta', 2, { text: 'partial' }), ''])(
    'rejects EOF without a terminal event %#',
    async (text) => {
      await expect(
        api(vi.fn().mockResolvedValue(response(text, 1000))).stream(
          request,
          new AbortController().signal,
          vi.fn(),
        ),
      ).rejects.toThrow(/unexpectedly/);
    },
  );

  it('rejects a non-SSE response', async () => {
    await expect(
      api(vi.fn().mockResolvedValue(new Response('{}'))).stream(
        request,
        new AbortController().signal,
        vi.fn(),
      ),
    ).rejects.toThrow(/invalid/);
  });

  it('cancels a blocked reader on AbortController and ignores late bytes', async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(utf8.encode(wire('start', 1)));
      },
      cancel,
    });
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }),
      );
    const controller = new AbortController();
    const events = vi.fn();
    const promise = api(fetch).stream(request, controller.signal, events);
    await vi.waitFor(() => expect(events).toHaveBeenCalledTimes(1));
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(events).toHaveBeenCalledTimes(1);
  });

  it('does not fetch an already aborted request', async () => {
    const fetch = vi.fn();
    const controller = new AbortController();
    controller.abort();
    await expect(api(fetch).stream(request, controller.signal, vi.fn())).rejects.toMatchObject({
      name: 'AbortError',
    });
    expect(fetch).not.toHaveBeenCalled();
  });
});
