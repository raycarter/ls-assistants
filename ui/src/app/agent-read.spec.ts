import { describe, expect, it } from 'vitest';
import { fixturePlan, fixtureRun, normalizeRun, FIXTURE_TIME } from './agent-api.testing';
import { validResult } from './agent-legacy.models';
import {
  parseAgentRun,
  parseResultsPage,
  type ReadMetadata,
  type StepResult,
} from './agent.models';

function result(eof = false): StepResult {
  return {
    step_id: 'step-1',
    status: 'completed',
    time: FIXTURE_TIME,
    content: 'Accumulated findings, not a literal byte-for-byte file excerpt.',
    path: 'actual/example.md',
    offset: null,
    returned_lines: null,
    truncated: !eof,
    read: {
      chunks: 2,
      bytes_read: eof ? 100 : 40,
      total_bytes: 100,
      eof,
      stop_reason: eof ? 'eof' : 'enough',
      file_version: 'stable-version',
    },
  };
}
function parse(value: StepResult) {
  const run = fixtureRun('completed');
  run.results = [value];
  run.answer = 'Done';
  return parseAgentRun(normalizeRun(run, 4));
}

describe('adaptive read result contract', () => {
  it.each([false, true])('preserves accumulated findings and strict coverage (eof=%s)', (eof) => {
    const value = result(eof);
    expect(parse(value).results).toEqual([value]);
    expect(
      parseResultsPage(
        {
          projection_version: 2,
          session_id: 'session',
          run_id: 'run-1',
          at_sequence: 4,
          total: 1,
          completed_steps: 1,
          results: [value],
          next_before: null,
        },
        fixturePlan(),
      ).results,
    ).toEqual([value]);
  });

  it('accepts an empty file and preserves legacy results without read metadata', () => {
    const empty = result(true);
    Object.assign(empty.read!, { chunks: 1, bytes_read: 0, total_bytes: 0 });
    expect(() => parse(empty)).not.toThrow();
    const legacy = result();
    delete legacy.read;
    Object.assign(legacy, { offset: 1, returned_lines: 2 });
    expect(() => parse(legacy)).not.toThrow();
    legacy.read = null;
    expect(() => parse(legacy)).not.toThrow();
  });

  it.each([
    { chunks: 0 },
    { chunks: 1.5 },
    { chunks: true },
    { bytes_read: -1 },
    { bytes_read: 101 },
    { bytes_read: 1.5 },
    { total_bytes: -1 },
    { total_bytes: Number.MAX_SAFE_INTEGER + 1 },
    { eof: 'false' },
    { eof: true },
    { bytes_read: 100 },
    { stop_reason: 'eof' },
    { stop_reason: 'limit' },
    { stop_reason: null },
    { file_version: '' },
    { file_version: '  ' },
    { file_version: 1 },
    { extra: 'private' },
  ])('rejects malformed or inconsistent coverage: %j', (mutation) => {
    const value = result();
    Object.assign(value.read!, mutation);
    expect(() => parse(value)).toThrow('Invalid Agent');
    expect(validResult(value)).toBe(false);
  });

  it.each(['chunks', 'bytes_read', 'total_bytes', 'eof', 'stop_reason', 'file_version'] as const)(
    'requires read.%s when metadata is supplied',
    (key) => {
      const value = result();
      delete (value.read as Partial<ReadMetadata>)[key];
      expect(() => parse(value)).toThrow('Invalid Agent');
    },
  );

  it('rejects non-object metadata, stale line ranges, wrong truncation and nonrelative paths', () => {
    for (const mutation of [
      { read: [] },
      { read: 'private' },
      { read: false },
      { offset: 1 },
      { offset: undefined },
      { returned_lines: 2 },
      { returned_lines: undefined },
      { truncated: false },
      { truncated: undefined },
      { path: null },
      { path: '/absolute/file' },
      { path: '../outside' },
    ])
      expect(() => parse({ ...result(), ...mutation } as StepResult)).toThrow('Invalid Agent');
    const eof = result(true);
    eof.truncated = true;
    expect(() => parse(eof)).toThrow('Invalid Agent');
  });

  it('permits read metadata only for file steps and never alongside fetch provenance', () => {
    const plan = fixturePlan();
    plan.steps[0] = {
      id: 'step-1',
      title: 'Fetch',
      description: '',
      tool: 'web_fetch',
      arguments: { url: 'https://example.com/', offset: 0, limit: 10 },
    };
    expect(validResult(result(), plan)).toBe(false);
    expect(validResult({ ...result(), fetch: {} })).toBe(false);
  });
});
