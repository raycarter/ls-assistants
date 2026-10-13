import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { MarkdownPipe } from './markdown.pipe';

@Component({
  selector: 'app-markdown-test-host',
  standalone: true,
  imports: [MarkdownPipe],
  template: '<div class="message-markdown" [innerHTML]="content | markdown"></div>',
})
class MarkdownTestHost {
  content = '';
}

describe('MarkdownPipe', () => {
  const pipe = new MarkdownPipe();

  function render(markdown: string): HTMLDivElement {
    const element = document.createElement('div');
    element.innerHTML = pipe.transform(markdown);
    return element;
  }

  it('returns a plain string synchronously and handles empty messages', () => {
    expect(typeof pipe.transform('# Hello')).toBe('string');
    expect(pipe.transform('')).toBe('');
    expect(pipe.transform(null)).toBe('');
    expect(pipe.transform(undefined)).toBe('');
  });

  it('renders headings, emphasis and ordered and unordered lists', () => {
    const element = render(
      '# Heading\n\n**bold** and *italic*\n\n- first\n- second\n\n1. one\n2. two',
    );

    expect(element.querySelector('h1')?.textContent).toBe('Heading');
    expect(element.querySelector('strong')?.textContent).toBe('bold');
    expect(element.querySelector('em')?.textContent).toBe('italic');
    expect(Array.from(element.querySelectorAll('ul li'), (item) => item.textContent)).toEqual([
      'first',
      'second',
    ]);
    expect(Array.from(element.querySelectorAll('ol li'), (item) => item.textContent)).toEqual([
      'one',
      'two',
    ]);
  });

  it('renders GFM tables and strikethrough', () => {
    const element = render('| Name | Status |\n| --- | --- |\n| Chat | ~~pending~~ ready |');

    expect(Array.from(element.querySelectorAll('thead th'), (cell) => cell.textContent)).toEqual([
      'Name',
      'Status',
    ]);
    expect(Array.from(element.querySelectorAll('tbody td'), (cell) => cell.textContent)).toEqual([
      'Chat',
      'pending ready',
    ]);
    expect(element.querySelector('del')?.textContent).toBe('pending');
  });

  it('renders single chat newlines as breaks and blank lines as paragraphs', () => {
    const element = render('first line\nsecond line\n\nnext paragraph');
    const paragraphs = element.querySelectorAll('p');

    expect(paragraphs.length).toBe(2);
    expect(paragraphs[0].innerHTML).toMatch(/^first line<br>\s*second line$/);
    expect(paragraphs[1].textContent).toBe('next paragraph');
  });

  it('renders accessible static markers for checked and unchecked GFM tasks', () => {
    const element = render('- [x] complete\n- [ ] pending');
    const markers = element.querySelectorAll('li span');

    expect(Array.from(element.querySelectorAll('li'), (item) => item.textContent?.trim())).toEqual([
      '☑ complete',
      '☐ pending',
    ]);
    expect(Array.from(markers, (marker) => marker.getAttribute('role'))).toEqual(['img', 'img']);
    expect(Array.from(markers, (marker) => marker.getAttribute('aria-label'))).toEqual([
      'Completed',
      'Incomplete',
    ]);
    expect(element.querySelector('input, [checked], [disabled], [type]')).toBeNull();
  });

  it.each([
    ['typescript', 'const value: number = 42;'],
    ['javascript', 'const value = 42;'],
    ['json', '{"ready": true}'],
    ['python', 'def greet():\n    return "hello"'],
    ['bash', '#!/bin/bash\necho "hello"'],
    ['sql', 'SELECT id FROM messages WHERE id = 1;'],
    ['xml', '<div class="message">hello</div>'],
    ['css', '.message { color: red; }'],
  ])('highlights %s fences while preserving their text', (language, source) => {
    const element = render(`\`\`\`${language}\n${source}\n\`\`\``);
    const code = element.querySelector('pre > code');

    expect(code?.getAttribute('class')).toBe(`hljs language-${language}`);
    expect(code?.querySelector('span[class^="hljs-"]')).not.toBeNull();
    expect(code?.textContent).toBe(`${source}\n`);
  });

  it.each([
    ['TS', 'typescript'],
    ['js', 'javascript'],
    ['py', 'python'],
    ['sh', 'bash'],
    ['html', 'xml'],
  ])('resolves the %s alias to a canonical class', (alias, language) => {
    const element = render(`\`\`\`${alias}\nexample\n\`\`\``);

    expect(element.querySelector('code')?.getAttribute('class')).toBe(`hljs language-${language}`);
  });

  it.each([
    '',
    'unknown-language',
    '__proto__',
    'constructor',
    'javascript" onmouseover="alert(1)',
    'javascript"><img/src=x/onerror=alert(1)>',
  ])('escapes unhighlighted code and rejects unsafe fence labels: %s', (language) => {
    const source =
      'const value = "<script>alert(1)</script>";\n<img src=x onerror=alert(1)> & &lt;';
    const element = render(`\`\`\`${language}\n${source}\n\`\`\``);
    const code = element.querySelector('pre > code');

    expect(code?.textContent).toBe(`${source}\n`);
    expect(code?.getAttribute('class')).toBeNull();
    expect(code?.children.length).toBe(0);
    expect(code?.innerHTML).toContain('&lt;script&gt;');
    expect(code?.innerHTML).toContain('&amp; &amp;lt;');
    expect(element.querySelector('script, img, [onmouseover], [onerror]')).toBeNull();
  });

  it('ignores fence metadata when generating highlighting attributes', () => {
    const element = render(
      '```javascript title="example" onmouseover="alert(1)"\nconst x = 1;\n```',
    );
    const code = element.querySelector('code');

    expect(code?.getAttribute('class')).toBe('hljs language-javascript');
    expect(code?.getAttributeNames()).toEqual(['class']);
  });

  it('keeps HTML-looking highlighted code inert', () => {
    const source = 'const html = "</code><img src=x onerror=alert(1)><script>alert(1)</script>";';
    const element = render(`\`\`\`javascript\n${source}\n\`\`\``);

    expect(element.querySelector('code')?.textContent).toBe(`${source}\n`);
    expect(element.querySelector('.hljs-string')).not.toBeNull();
    expect(element.querySelector('script, img, [onerror]')).toBeNull();
  });

  it.each([
    '<span class="fixed inset-0 z-30 bg-gray-900 message-markdown">spoof</span>',
    '<span class="fixed&#x20;inset-0&#9;z-30 bg-gray-900">spoof</span>',
  ])('removes raw HTML overlay and application classes: %s', (markdown) => {
    const span = render(markdown).querySelector('span');

    expect(span?.textContent).toBe('spoof');
    expect(span?.hasAttribute('class')).toBe(false);
  });

  it('filters mixed classes to highlighting tokens and registered canonical languages', () => {
    const element = render(
      '<span class="fixed hljs hljs-keyword hljs-built_in language-typescript language-ts language-rust hljs-foo:fixed bg-gray-900">safe</span>',
    );

    expect(element.querySelector('span')?.getAttribute('class')).toBe(
      'hljs hljs-keyword hljs-built_in language-typescript',
    );
  });

  it('preserves generated nested highlighting classes', () => {
    const source = 'const result = `Hello ${Math.PI}`;';
    const element = render(`\`\`\`javascript\n${source}\n\`\`\``);

    expect(element.querySelector('code')?.getAttribute('class')).toBe('hljs language-javascript');
    expect(element.querySelector('.hljs-string .hljs-subst .hljs-built_in')).not.toBeNull();
    expect(element.querySelector('code')?.textContent).toBe(`${source}\n`);
  });

  it('sanitizes dangerous raw HTML, events and styling from the final output', () => {
    const element = render(
      [
        '<script>alert(1)</script>',
        '<img src="https://example.test/image.png" onerror="alert(1)">',
        '<strong onclick="alert(1)" style="color:red" data-payload="bad">safe</strong>',
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
        '<object data="javascript:alert(1)"></object>',
        '<embed src="javascript:alert(1)">',
        '<style>body { display: none; }</style>',
        '<form action="https://example.test"><button>submit</button></form>',
        '<input type="checkbox" checked disabled>',
        '<svg onload="alert(1)"><circle></circle></svg>',
        '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
      ].join('\n'),
    );

    expect(
      element.querySelector('script, iframe, object, embed, style, form, button, input, svg, math'),
    ).toBeNull();
    expect(element.querySelector('img')?.getAttribute('src')).toBe(
      'https://example.test/image.png',
    );
    expect(element.querySelector('strong')?.textContent).toBe('safe');
    expect(element.querySelector('[style], [data-payload]')).toBeNull();

    for (const node of element.querySelectorAll('*')) {
      expect(node.getAttributeNames().some((name) => name.startsWith('on'))).toBe(false);
    }
  });

  it.each([
    '[unsafe](javascript:alert%281%29)',
    '[unsafe](JaVaScRiPt:alert%281%29)',
    '[unsafe](java&#x73;cript:alert%281%29)',
    '[unsafe](vbscript:msgbox%281%29)',
    '[unsafe](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
    '<a href="java&#x09;script:alert(1)">unsafe</a>',
    '<a href="javascript:alert(1)" onclick="alert(1)">unsafe</a>',
  ])('removes unsafe link destinations: %s', (markdown) => {
    const link = render(markdown).querySelector('a');

    expect(link).not.toBeNull();
    expect(link?.textContent).toBe('unsafe');
    expect(link?.hasAttribute('href')).toBe(false);
    expect(link?.hasAttribute('onclick')).toBe(false);
  });

  it.each([
    'https://example.test/path?one=1&two=2',
    '/help',
    '#section',
    'mailto:hello@example.test',
  ])('preserves safe links and their titles: %s', (url) => {
    const link = render(`[safe](${url} "Details")`).querySelector('a');

    expect(link?.textContent).toBe('safe');
    expect(link?.getAttribute('href')).toBe(url);
    expect(link?.getAttribute('title')).toBe('Details');
  });

  it('removes JavaScript image sources while keeping alternative text', () => {
    const image = render('![unsafe image](javascript:alert%281%29)').querySelector('img');

    expect(image).not.toBeNull();
    expect(image?.getAttribute('alt')).toBe('unsafe image');
    expect(image?.hasAttribute('src')).toBe(false);
  });
});

describe('MarkdownPipe through Angular bindings', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('retains static task states and nested highlighting without raw layout classes', async () => {
    await TestBed.configureTestingModule({ imports: [MarkdownTestHost] }).compileComponents();
    const fixture = TestBed.createComponent(MarkdownTestHost);
    fixture.componentInstance.content = [
      '- [x] complete\n- [ ] pending',
      '```javascript\nconst result = `Hello ${Math.PI}`;\n```',
      '<span class="fixed inset-0 z-30 bg-gray-900 message-markdown">spoof</span>',
    ].join('\n\n');
    fixture.detectChanges();
    await fixture.whenStable();
    const root = fixture.nativeElement as HTMLElement;
    const message = root.querySelector('.message-markdown')!;

    expect(Array.from(message.querySelectorAll('li'), (item) => item.textContent?.trim())).toEqual([
      '☑ complete',
      '☐ pending',
    ]);
    expect(
      Array.from(message.querySelectorAll('li span'), (marker) =>
        marker.getAttribute('aria-label'),
      ),
    ).toEqual(['Completed', 'Incomplete']);
    expect(message.querySelectorAll('li span[role="img"]').length).toBe(2);
    expect(message.querySelector('input')).toBeNull();
    expect(message.querySelector('code')?.getAttribute('class')).toBe('hljs language-javascript');
    expect(message.querySelector('.hljs-keyword')?.textContent).toBe('const');
    expect(message.querySelector('.hljs-string .hljs-subst .hljs-built_in')).not.toBeNull();
    expect(
      message.querySelector('.fixed, .inset-0, .z-30, .bg-gray-900, .message-markdown'),
    ).toBeNull();
    const spoof = Array.from(message.querySelectorAll('span')).find(
      (span) => span.textContent === 'spoof',
    );
    expect(spoof).toBeDefined();
    expect(spoof?.hasAttribute('class')).toBe(false);
  });
});
