import { Pipe, type PipeTransform } from '@angular/core';
import createDOMPurify, { type Config } from 'dompurify';
import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import python from 'highlight.js/lib/languages/python';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import { Marked, type Tokens } from 'marked';

const highlighter = hljs.newInstance();
const languageAliases = new Map<string, string>();
const languageClasses = new Set<string>();

for (const [name, definition] of [
  ['typescript', typescript],
  ['javascript', javascript],
  ['json', json],
  ['python', python],
  ['bash', bash],
  ['sql', sql],
  ['xml', xml],
  ['css', css],
] as const) {
  highlighter.registerLanguage(name, definition);
  languageAliases.set(name, name);
  languageClasses.add(`language-${name}`);

  for (const alias of highlighter.getLanguage(name)?.aliases ?? []) {
    languageAliases.set(alias.toLowerCase(), name);
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderCode({ text, lang }: Tokens.Code): string {
  const label = lang?.trim().split(/\s+/, 1)[0]?.toLowerCase() ?? '';
  const language = languageAliases.get(label);

  if (!language) {
    return `<pre><code>${escapeHtml(text)}\n</code></pre>\n`;
  }

  const highlighted = highlighter.highlight(text, {
    language,
    ignoreIllegals: true,
  }).value;

  // Classes use only canonical registry names, never the untrusted fence label.
  return `<pre><code class="hljs language-${language}">${highlighted}\n</code></pre>\n`;
}

const parser = new Marked({
  async: false,
  gfm: true,
  breaks: true,
  renderer: {
    code: renderCode,
    checkbox({ checked }) {
      const label = checked ? 'Completed' : 'Incomplete';
      const marker = checked ? '☑' : '☐';
      return `<span role="img" aria-label="${label}">${marker}</span> `;
    },
  },
});
const purifier = createDOMPurify();

purifier.addHook('uponSanitizeAttribute', (_node, attribute) => {
  if (attribute.attrName !== 'class') {
    return;
  }

  // Raw HTML must not gain access to application or utility layout classes.
  const classes = attribute.attrValue.split(/\s+/).filter((name) => {
    return name === 'hljs' || /^hljs-[a-z0-9_-]+$/.test(name) || languageClasses.has(name);
  });
  attribute.attrValue = classes.join(' ');
  attribute.keepAttr = classes.length > 0;
});

const sanitizeOptions: Config = {
  // Keep Markdown and static spans; exclude active HTML and form controls.
  ALLOWED_TAGS: [
    'a',
    'blockquote',
    'br',
    'code',
    'del',
    'em',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'hr',
    'img',
    'li',
    'ol',
    'p',
    'pre',
    'span',
    'strong',
    'table',
    'tbody',
    'td',
    'th',
    'thead',
    'tr',
    'ul',
  ],
  ALLOWED_ATTR: ['align', 'alt', 'aria-label', 'class', 'href', 'role', 'src', 'start', 'title'],
  ALLOW_ARIA_ATTR: false,
  ALLOW_DATA_ATTR: false,
  RETURN_TRUSTED_TYPE: false,
};

@Pipe({
  name: 'markdown',
  standalone: true,
})
export class MarkdownPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    if (!value) {
      return '';
    }

    try {
      const html = parser.parse(value, { async: false });
      // Sanitize after rendering and highlighting, with no subsequent HTML changes.
      return purifier.sanitize(html, sanitizeOptions);
    } catch {
      return `<pre>${escapeHtml(value)}</pre>`;
    }
  }
}
