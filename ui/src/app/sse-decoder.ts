export interface SseEvent {
  event: string;
  data: string;
  id: string;
}

/** Incremental UTF-8 and SSE framing, independent of the chat protocol. */
export class SseDecoder {
  private readonly utf8 = new TextDecoder('utf-8', { fatal: true });
  private line = '';
  private afterCr = false;
  private event = '';
  private data: string[] = [];
  private id = '';

  push(bytes: Uint8Array): SseEvent[] {
    return this.consume(this.utf8.decode(bytes, { stream: true }));
  }

  finish(): SseEvent[] {
    // An unterminated event is not dispatched (SSE requires a blank line).
    return this.consume(this.utf8.decode());
  }

  private consume(text: string): SseEvent[] {
    const events: SseEvent[] = [];
    for (const character of text) {
      if (this.afterCr && character === '\n') {
        this.afterCr = false;
        continue;
      }
      this.afterCr = character === '\r';
      if (character === '\r' || character === '\n') {
        const line = this.line;
        this.line = '';
        if (!line) {
          if (this.data.length) {
            events.push({
              event: this.event || 'message',
              data: this.data.join('\n'),
              id: this.id,
            });
          }
          this.event = '';
          this.data = [];
        } else if (!line.startsWith(':')) {
          const colon = line.indexOf(':');
          const field = colon < 0 ? line : line.slice(0, colon);
          const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
          if (field === 'event') this.event = value;
          if (field === 'data') this.data.push(value);
          if (field === 'id' && !value.includes('\0')) this.id = value;
        }
      } else {
        this.line += character;
      }
    }
    return events;
  }
}
