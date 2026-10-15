import { TestBed } from '@angular/core/testing';
import { Component, signal } from '@angular/core';
import { Lexer } from 'marked';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MarkdownPipe } from './markdown.pipe';

vi.mock('highlight.js/lib/core', () => ({
  default: {
    newInstance: () => ({
      registerLanguage: () => {},
      getLanguage: () => ({ aliases: [] }),
      highlight: () => {
        throw new Error('Highlight interrupted');
      },
    }),
  },
}));

@Component({ imports: [MarkdownPipe], template: '<div [innerHTML]="content() | markdown"></div>' })
class Host {
  readonly content = signal('');
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('silent partial Markdown fallback', () => {
  it.each(['parser', 'highlighter'])(
    'escapes malicious partial text when the %s throws, through Angular',
    async (stage) => {
      const warn = vi.spyOn(console, 'warn');
      const error = vi.spyOn(console, 'error');
      if (stage === 'parser')
        vi.spyOn(Lexer, 'lex').mockImplementationOnce(() => {
          throw new Error('Parsing interrupted');
        });
      await TestBed.configureTestingModule({ imports: [Host] }).compileComponents();
      const fixture = TestBed.createComponent(Host);
      const partial =
        stage === 'parser'
          ? '# Heading\n\n**unfinished <img src=x onerror=alert(1)> & <script>bad</script>'
          : '```typescript\nconst value = "<img src=x onerror=alert(1)> & <script>bad</script>";\n**unfinished';
      fixture.componentInstance.content.set(partial);
      fixture.detectChanges();
      await fixture.whenStable();
      const root = fixture.nativeElement as HTMLElement;
      expect(root.querySelector('pre')?.textContent).toBe(partial);
      expect(root.querySelector('script, img, [onerror], [role="alert"]')).toBeNull();
      expect(root.querySelector('pre')?.children).toHaveLength(0);
      expect(warn).not.toHaveBeenCalled();
      expect(error).not.toHaveBeenCalled();
      expect(new MarkdownPipe().transform('**Recovered**')).toContain('<strong>Recovered</strong>');
      fixture.componentInstance.content.set('**Recovered**');
      fixture.detectChanges();
      await fixture.whenStable();
      expect(root.querySelector('strong')?.textContent).toBe('Recovered');
    },
  );

  it.each([
    '## Partial',
    '**unfinished',
    '```typescript\nconst n = 1;',
    '[link](https://example.test',
    '| a | b |\n| --- | --- |\n| partial',
  ])('renders growing incomplete Markdown safely: %s', (partial) => {
    const pipe = new MarkdownPipe();
    expect(() => pipe.transform(partial)).not.toThrow();
    const element = document.createElement('div');
    element.innerHTML = pipe.transform(partial);
    expect(element.querySelector('script, [onerror]')).toBeNull();
    expect(element.textContent?.length).toBeGreaterThan(0);
  });
});
