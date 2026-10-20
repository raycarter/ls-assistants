import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fixture from './agent-contract.fixture.json';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import {
  parseAgentSession,
  parseArtifactPage,
  safeHttpUrl,
  type AgentCommand,
  type AgentEvent,
  type AgentSession,
  type ArtifactPage,
  type FetchMetadata,
} from './agent.models';
import { FETCH } from './chat-api';

const recorded = fixture.streams.find((entry) => entry.name === 'fetch_execution')!;
function session(): AgentSession {
  return structuredClone(recorded.terminal) as AgentSession;
}
function page(): ArtifactPage {
  return structuredClone(recorded.artifact_page!.body);
}
const request: AgentStreamRequest = {
  session_id: recorded.baseline.id,
  request_id: recorded.http.body.request_id,
  sequence: recorded.baseline.sequence,
  snapshot: recorded.baseline as AgentSession,
  command: recorded.command as AgentCommand,
};

describe('strict fetch evidence contract', () => {
  it('keeps pre-fetch read-only history compatible when fetch is absent', () => {
    const legacy = structuredClone(
      fixture.streams.find((entry) => entry.name === 'execution')!.terminal,
    ) as AgentSession;
    delete legacy.runs[0].results[0].fetch;
    expect(parseAgentSession(legacy)).toEqual(legacy);
  });

  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'file:///tmp/public',
    '//example.com/public',
    '/relative',
    'https://user:password@example.com/public',
    'https://@example.com/public',
    'https://example.com\\@evil.example/public',
    'https://example.com/\npublic',
    ' https://example.com/public',
    'https://example.com:0/public',
    'https:///example.com/public',
    'https://%65xample.com/public',
  ])('rejects unsafe URLs in plans and every provenance URL: %s', (url) => {
    expect(safeHttpUrl(url)).toBe(false);
    const value = session();
    const step = value.runs[0].plan!.steps[0];
    if (step.tool !== 'web_fetch') throw new Error('Expected fetch step');
    step.arguments.url = url;
    expect(() => parseAgentSession(value)).toThrow('Invalid Agent');
    for (const field of ['requested_url', 'final_url', 'redirects']) {
      const forged = session();
      forged.runs[0].results[0].fetch = {
        ...forged.runs[0].results[0].fetch!,
        [field]: field === 'redirects' ? [url] : url,
      };
      expect(() => parseAgentSession(forged), field).toThrow('Invalid Agent');
    }
  });

  it.each([
    ['unknown tool', { tool: 'search' }],
    ['null tool', { tool: null, arguments: null }],
    ['read tool with fetch args', { tool: 'read_file' }],
    [
      'file args for fetch',
      { arguments: { workspace_id: 'public', path: 'a', offset: 1, limit: 2 } },
    ],
    [
      'mixed args',
      { arguments: { url: 'https://example.com/public', offset: 0, limit: 18, path: 'a' } },
    ],
    [
      'negative offset',
      { arguments: { url: 'https://example.com/public', offset: -1, limit: 18 } },
    ],
    [
      'fractional offset',
      { arguments: { url: 'https://example.com/public', offset: 0.5, limit: 18 } },
    ],
    ['string offset', { arguments: { url: 'https://example.com/public', offset: '0', limit: 18 } }],
    [
      'unsafe integer',
      { arguments: { url: 'https://example.com/public', offset: 2 ** 53, limit: 18 } },
    ],
    ['zero limit', { arguments: { url: 'https://example.com/public', offset: 0, limit: 0 } }],
    [
      'excessive limit',
      { arguments: { url: 'https://example.com/public', offset: 0, limit: 16001 } },
    ],
    ['missing page', { arguments: { url: 'https://example.com/public' } }],
  ])('rejects mismatched or malformed plan arguments: %s', (_name, change) => {
    const value = session();
    Object.assign(value.runs[0].plan!.steps[0], change);
    expect(() => parseAgentSession(value)).toThrow('Invalid Agent');
  });

  it.each([
    ['artifact_id', 'ABCDEF01234567890123456789012345'],
    ['artifact_id', '../artifact'],
    ['title', 12],
    ['title', undefined],
    ['status_code', '200'],
    ['status_code', 200.5],
    ['status_code', 99],
    ['status_code', 600],
    ['content_type', null],
    ['fetched_at', '2026-02-31T09:00:00Z'],
    ['fetched_at', '2026-10-16T09:00:00'],
    ['fetched_at', '2026-10-16T09:00:00+01:00'],
    ['redirects', 'https://example.com/public-text'],
    ['redirects', []],
    ['requested_url', 'https://example.com/forged'],
    ['final_url', 'https://example.com/forged'],
    ['total_chars', -1],
    ['total_chars', 2 ** 53],
    ['offset', -1],
    ['offset', 1],
    ['returned_chars', 19],
    ['returned_chars', '18'],
    ['next_offset', 0],
    ['next_offset', 19],
    ['next_offset', null],
    ['html', '<script>forged</script>'],
  ])('rejects forged provenance from GET and real SSE decoder: %s=%s', (field, value) => {
    const forged = session();
    forged.runs[0].results[0].fetch = {
      ...forged.runs[0].results[0].fetch!,
      [field]: value,
    } as FetchMetadata;
    expect(() => parseAgentSession(forged)).toThrow('Invalid Agent');
    const firstBlock = recorded.sse.split('\n\n')[0];
    const event = JSON.parse(firstBlock.split('data: ')[1]) as AgentEvent;
    event.session = forged;
    event.session.sequence = event.sequence;
    expect(() =>
      new AgentEventDecoder(request).decode({
        id: String(event.sequence),
        event: 'agent',
        data: JSON.stringify(event),
      }),
    ).toThrow('Invalid Agent');
  });

  it.each(['missing', 'file', 'running', 'file metadata', 'content mismatch'])(
    'rejects fetch evidence bound to %s',
    (kind) => {
      const forged = session();
      const run = forged.runs[0];
      const result = run.results[0];
      if (kind === 'missing') result.fetch = null;
      if (kind === 'file')
        run.plan!.steps[0] = {
          ...run.plan!.steps[0],
          tool: 'read_file',
          arguments: { workspace_id: 'public', path: 'a', offset: 1, limit: 18 },
        };
      if (kind === 'running') {
        run.status = 'executing';
        run.operation_id = 'operation';
        run.answer = null;
        result.status = 'running';
      }
      if (kind === 'file metadata') result.path = 'public.txt';
      if (kind === 'content mismatch') result.content += 'forged';
      expect(() => parseAgentSession(forged)).toThrow('Invalid Agent');
    },
  );

  it.each([
    ['artifact_id', 'not-an-artifact'],
    ['content', null],
    ['offset', -1],
    ['offset', 18.5],
    ['total_chars', 20],
    ['total_chars', '52'],
    ['returned_chars', -1],
    ['returned_chars', 33],
    ['next_offset', 52],
    ['next_offset', '52'],
    ['extra', true],
  ])('rejects malformed retained pages: %s=%s', (field, value) => {
    expect(() => parseArtifactPage({ ...page(), [field]: value })).toThrow('Invalid Agent');
  });
  it('accepts end-of-text empty pages but rejects non-progressing and oversized pages', () => {
    const end = { ...page(), offset: 52, returned_chars: 0, content: '' };
    expect(parseArtifactPage(end)).toEqual(end);
    expect(() => parseArtifactPage({ ...end, offset: 0, next_offset: 0 })).toThrow('Invalid Agent');
    expect(() =>
      parseArtifactPage({
        ...end,
        content: 'x'.repeat(16001),
        offset: 0,
        returned_chars: 16001,
        total_chars: 16001,
      }),
    ).toThrow('Invalid Agent');
  });
});

describe('read-only evidence HTTP transport', () => {
  let fetchDouble: ReturnType<typeof vi.fn<typeof fetch>>;
  let api: AgentApi;
  beforeEach(() => {
    fetchDouble = vi.fn<typeof fetch>();
    TestBed.configureTestingModule({ providers: [{ provide: FETCH, useValue: fetchDouble }] });
    api = TestBed.inject(AgentApi);
  });
  afterEach(() => TestBed.resetTestingModule());

  it.each([
    ['wrong artifact', { artifact_id: 'f'.repeat(32) }],
    ['wrong offset', { offset: 17, total_chars: 51 }],
    ['short page', { content: 'C', returned_chars: 1, next_offset: 19 }],
  ])('rejects response identity/range mismatch: %s', async (_name, change) => {
    fetchDouble.mockResolvedValueOnce(Response.json({ ...page(), ...change }));
    await expect(
      api.artifactPage('sid /1', page().artifact_id, 18, 12000, new AbortController().signal),
    ).rejects.toThrow('Invalid Agent');
    expect(fetchDouble).toHaveBeenCalledOnce();
    expect(fetchDouble.mock.calls[0][0]).toBe(
      `/api/agent/sessions/sid%20%2F1/artifacts/${page().artifact_id}?offset=18&limit=12000`,
    );
    expect(fetchDouble.mock.calls[0][1]?.method).toBe('GET');
    expect(fetchDouble.mock.calls[0][1]?.body).toBeUndefined();
  });
  it.each([
    ['bad artifact', '../a', 0, 12000],
    ['negative offset', 'a'.repeat(32), -1, 12000],
    ['fractional offset', 'a'.repeat(32), 0.5, 12000],
    ['zero limit', 'a'.repeat(32), 0, 0],
    ['excessive limit', 'a'.repeat(32), 0, 16001],
  ] as const)('rejects invalid local page request: %s', async (_name, artifact, offset, limit) => {
    await expect(
      api.artifactPage('sid', artifact, offset, limit, new AbortController().signal),
    ).rejects.toThrow('Invalid Agent');
    expect(fetchDouble).not.toHaveBeenCalled();
  });
});
