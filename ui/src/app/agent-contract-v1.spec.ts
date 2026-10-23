import { describe, expect, it } from 'vitest';
import fixture from './agent-contract-v1.fixture.json';
import { parseLegacyAgentSession, type AgentEvent } from './agent-legacy.models';
import { parseAgentSession } from './agent.models';

describe('frozen v1 read-only compatibility (complete snapshots)', () => {
  it.each(fixture.streams)('decodes every full static snapshot for $name', (entry) => {
    expect(fixture.version).toBe(1);
    expect(parseLegacyAgentSession(entry.baseline)).toEqual(entry.baseline);
    expect(parseLegacyAgentSession(entry.terminal)).toEqual(entry.terminal);
    for (const block of entry.sse.trim().split('\n\n')) {
      const raw = block
        .split('\n')
        .find((line) => line.startsWith('data: '))!
        .slice(6);
      const event = JSON.parse(raw) as AgentEvent;
      expect(parseLegacyAgentSession(event.session)).toEqual(event.session);
    }
    for (const control of entry.controls ?? [])
      expect(parseLegacyAgentSession(control.session)).toEqual(control.session);
    expect(() => parseAgentSession(entry.terminal)).toThrow('Invalid Agent');
  });
  it('never interprets an incomplete v1 completed snapshot as a result window', () => {
    const complete = structuredClone(
      fixture.streams.find((entry) => entry.name === 'execution')!.terminal,
    );
    complete.runs[0].results = [];
    expect(() => parseLegacyAgentSession(complete)).toThrow('Invalid Agent');
  });
  it('preserves optional digit-valued legacy limits without imposing upper ceilings', () => {
    const session = structuredClone(fixture.streams[0].terminal);
    session.runs[0].plan!.limits = {
      max_steps: 100000,
      max_tool_calls: 1000000,
      timeout_seconds: 1000000,
    };
    expect(parseLegacyAgentSession(session).runs[0].plan?.limits).toEqual(
      session.runs[0].plan!.limits,
    );
    const noLimits = structuredClone(session) as unknown as {
      runs: { plan: Record<string, unknown> }[];
    };
    delete noLimits.runs[0].plan['limits'];
    expect(parseLegacyAgentSession(noLimits).runs[0].plan?.limits).toBeUndefined();
  });
});
