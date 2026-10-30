import { describe, expect, it } from 'vitest';
import { fixturePlan, fixtureRun, normalizeRun, FIXTURE_TIME } from './agent-api.testing';
import {
  parseAgentRun,
  parseResultsPage,
  validPlan,
  validResult,
  type AgentPlan,
  type StepResult,
  type BackendGrepMetadata,
} from './agent.models';

function plan(): AgentPlan {
  return {
    ...fixturePlan(),
    steps: [
      {
        id: 'step-1',
        title: 'Grep',
        description: 'Literal folder evidence',
        tool: 'grep_in_folder',
        arguments: {
          workspace_id: 'public',
          path: '.',
          pattern: '😀',
          file_glob: null,
          offset: 0,
          limit: 2,
        },
      },
    ],
  };
}
function result(): StepResult {
  return {
    step_id: 'step-1',
    status: 'completed',
    content: '😀\n',
    time: FIXTURE_TIME,
    path: null,
    offset: null,
    returned_lines: null,
    truncated: true,
    grep: {
      artifact_id: 'a'.repeat(32),
      workspace_id: 'public',
      path: '.',
      pattern: '😀',
      file_glob: null,
      offset: 0,
      returned_chars: 2,
      total_chars: 8,
      next_offset: 2,
      total_matches: 1,
      files_searched: 2,
      skipped_binary: 0,
      skipped_unreadable: 0,
      complete: true,
    },
  };
}
function parse(value = result(), approved = plan()) {
  return parseAgentRun(
    normalizeRun(
      { ...fixtureRun('completed'), plan: approved, results: [value], answer: 'Done' },
      4,
    ),
  );
}

describe('production grep evidence contract', () => {
  it('uses Unicode character ranges and retains generic text, including split JSONL', () => {
    expect(parse().results).toEqual([result()]);
    expect(validResult(result())).toBe(true);
    expect(
      parseResultsPage(
        {
          projection_version: 2,
          session_id: 's',
          run_id: 'r',
          at_sequence: 4,
          total: 1,
          completed_steps: 1,
          results: [result()],
          next_before: null,
        },
        plan(),
      ).completed_steps,
    ).toBe(1);
  });

  it('preserves whitespace literal patterns and optional null glob without normalization', () => {
    const approved = plan();
    const value = result();
    const step = approved.steps[0];
    if (step.tool !== 'grep_in_folder') throw new Error('Expected grep');
    step.arguments.pattern = '  ';
    value.grep!.pattern = '  ';
    expect(parse(value, approved).plan!.steps[0].arguments).toEqual(step.arguments);
  });

  it.each([
    { extra: true },
    { workspace_id: 'other' },
    { path: 'other' },
    { pattern: 'other' },
    { file_glob: '*.ts' },
    { file_glob: undefined },
    { offset: 1 },
    { offset: -1 },
    { returned_chars: 3 },
    { returned_chars: 1 },
    { total_chars: 1 },
    { total_chars: 2 ** 53 },
    { next_offset: null },
    { next_offset: 0 },
    { next_offset: 3 },
    { total_matches: -1 },
    { total_matches: 0.5 },
    { files_searched: '2' },
    { skipped_binary: -1 },
    { skipped_unreadable: 0.5 },
    { skipped_binary: 1 },
    { complete: false },
    { complete: 'true' },
  ])('rejects malformed, unknown or uncorrelated metadata: %j', (mutation) => {
    const value = result();
    Object.assign(value.grep!, mutation);
    expect(() => parse(value)).toThrow('Invalid Agent');
  });

  it('requires every exact metadata field', () => {
    for (const field of Object.keys(result().grep!) as (keyof BackendGrepMetadata)[]) {
      const value = result();
      delete (value.grep as Partial<BackendGrepMetadata>)[field];
      expect(() => parse(value), field).toThrow('Invalid Agent');
    }
  });

  it.each([
    { offset: -1 },
    { offset: 0.5 },
    { offset: 2 ** 53 },
    { limit: 0 },
    { limit: 16001 },
    { limit: 1.5 },
    { pattern: null },
    { file_glob: undefined },
    { extra: true },
  ])('rejects invalid grep arguments: %j', (mutation) => {
    const value = plan();
    Object.assign(value.steps[0].arguments, mutation);
    expect(validPlan(value)).toBe(false);
  });

  it('requires completed metadata and rejects other tool metadata and stale line ranges', () => {
    for (const mutation of [
      { grep: null },
      { read: {} },
      { fetch: {} },
      { path: '.' },
      { offset: 1 },
      { returned_lines: 1 },
      { truncated: false },
      { status: 'running' },
    ])
      expect(() => parse({ ...result(), ...mutation } as StepResult)).toThrow('Invalid Agent');
    expect(validResult(result(), fixturePlan())).toBe(false);
    const fetchPlan = plan();
    fetchPlan.steps[0] = {
      id: 'step-1',
      title: '',
      description: '',
      tool: 'web_fetch',
      arguments: { url: 'https://example.com/', offset: 0, limit: 2 },
    };
    expect(validResult(result(), fetchPlan)).toBe(false);
    const short = plan();
    if (short.steps[0].tool !== 'grep_in_folder') throw new Error('Expected grep');
    short.steps[0].arguments.limit = 1;
    expect(() => parse(result(), short)).toThrow('Invalid Agent');
  });

  it('accepts no matches, skipped coverage and explicit nonzero initial evidence offsets', () => {
    const empty = result();
    empty.content = '';
    empty.truncated = false;
    Object.assign(empty.grep!, {
      returned_chars: 0,
      total_chars: 0,
      next_offset: null,
      total_matches: 0,
    });
    expect(() => parse(empty)).not.toThrow();
    empty.truncated = true;
    Object.assign(empty.grep!, { skipped_unreadable: 1, complete: false });
    expect(() => parse(empty)).not.toThrow();
    const resumed = result();
    resumed.grep!.offset = 4;
    resumed.grep!.next_offset = 6;
    const approved = plan();
    if (approved.steps[0].tool !== 'grep_in_folder') throw new Error('Expected grep');
    approved.steps[0].arguments.offset = 4;
    expect(() => parse(resumed, approved)).not.toThrow();
  });

  it('preserves sequential completed-prefix and frontier count validation', () => {
    const run = parse();
    expect(() => parseAgentRun({ ...run, completed_steps: 0 })).toThrow('Invalid Agent');
    expect(() => parseAgentRun({ ...run, result_count: 2 })).toThrow('Invalid Agent');
  });
});
