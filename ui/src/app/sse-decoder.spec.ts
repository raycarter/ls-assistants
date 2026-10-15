import { describe, expect, it } from 'vitest';
import { SseDecoder } from './sse-decoder';

const utf8 = new TextEncoder();

describe('SseDecoder', () => {
  it('decodes split UTF-8 and CRLF at every byte boundary, with several events per packet', () => {
    const bytes = utf8.encode(
      ': heartbeat\r\nid: reply:1\r\nevent: delta\r\ndata: 世界 🌱\r\n\r\nid: reply:2\nevent: done\ndata: {}\n\n',
    );
    for (let split = 0; split <= bytes.length; split++) {
      const decoder = new SseDecoder();
      expect([
        ...decoder.push(bytes.slice(0, split)),
        ...decoder.push(bytes.slice(split)),
        ...decoder.finish(),
      ]).toEqual([
        { event: 'delta', data: '世界 🌱', id: 'reply:1' },
        { event: 'done', data: '{}', id: 'reply:2' },
      ]);
    }
  });

  it('supports byte-at-a-time packets, CR line endings, multiline data and ignored fields', () => {
    const decoder = new SseDecoder();
    const events = [
      ...utf8.encode('event: delta\rid: m:1\rretry: 500\rdata: first\rdata:  second\r\r'),
    ].flatMap((byte) => decoder.push(new Uint8Array([byte])));
    expect(events).toEqual([{ event: 'delta', id: 'm:1', data: 'first\n second' }]);
  });

  it('ignores comments and empty events; retains IDs and defaults the event name', () => {
    const decoder = new SseDecoder();
    expect(
      decoder.push(utf8.encode(': hi\n\nid: m:1\ndata: a\n\nid: bad\0id\ndata: b\n\n')),
    ).toEqual([
      { event: 'message', id: 'm:1', data: 'a' },
      { event: 'message', id: 'm:1', data: 'b' },
    ]);
  });

  it('never dispatches an incomplete final event and rejects incomplete UTF-8', () => {
    const decoder = new SseDecoder();
    expect(decoder.push(utf8.encode('data: unfinished\n'))).toEqual([]);
    expect(decoder.finish()).toEqual([]);
    const bad = new SseDecoder();
    bad.push(new Uint8Array([0xf0, 0x9f]));
    expect(() => bad.finish()).toThrow();
  });
});
