import { vi } from 'vitest';
import type { AgentStreamRequest } from './agent-api';
import type { AgentEvent, AgentPlan, AgentRun, AgentSession, RunStatus } from './agent.models';

export const FIXTURE_TIME = '2026-10-16T09:00:00.000Z';
export const FIXTURE_DIGEST = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
export const READ_FILE_FIXTURE = '1: # Public fixture\n2: A bounded, approved example.';
export function fixturePlan(): AgentPlan {
  return {
    id: 'plan-1',
    revision: 1,
    digest: FIXTURE_DIGEST,
    objective: 'Read the public fixture',
    assumptions: ['The configured fixture workspace is unverified until approved.'],
    model: 'model-a',
    reasoning_effort: 'medium',
    limits: { max_steps: 1, max_tool_calls: 1, timeout_seconds: 30 },
    steps: [
      {
        id: 'step-1',
        title: 'Read fixture',
        description: 'Read only the first two lines.',
        tool: 'read_file',
        arguments: { workspace_id: 'public-fixtures', path: 'example.md', offset: 1, limit: 2 },
      },
    ],
  };
}
export function fixtureRun(status: RunStatus = 'awaiting_approval'): AgentRun {
  return {
    id: 'run-1',
    request: 'Read the fixture',
    created_at: FIXTURE_TIME,
    updated_at: FIXTURE_TIME,
    status,
    operation_id: ['planning', 'approved', 'executing', 'cancelling'].includes(status)
      ? 'operation-1'
      : null,
    plan: status === 'planning' ? null : fixturePlan(),
    results: [],
    answer: null,
    error: null,
  };
}
export function fixtureSession(sequence = 0, runs: AgentRun[] = [], sid = 'agent-1'): AgentSession {
  return {
    id: sid,
    title: 'Fixture Agent',
    model: 'model-a',
    reasoning_effort: 'medium',
    created_at: FIXTURE_TIME,
    updated_at: FIXTURE_TIME,
    archived: false,
    archive_pending: false,
    sequence,
    runs,
  };
}
export function fixtureEvent(
  request: AgentStreamRequest,
  sequence: number,
  run: AgentRun,
): AgentEvent {
  return {
    schema_version: 1,
    sequence,
    time: FIXTURE_TIME,
    session_id: request.session_id,
    request_id: request.request_id,
    run_id: run.id,
    operation_id: 'operation-1',
    type: `run.${run.status}`,
    session: fixtureSession(sequence, [run], request.session_id),
  };
}
export function sseResponse(events: AgentEvent[]): Response {
  return new Response(
    events
      .map((event) => `event: agent\nid: ${event.sequence}\ndata: ${JSON.stringify(event)}\n\n`)
      .join(''),
    {
      headers: { 'Content-Type': 'text/event-stream' },
    },
  );
}
export function memoryAgentStorage() {
  const records = new Map<string, string>();
  return {
    records,
    getItem: vi.fn((key: string) => records.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      records.set(key, value);
    }),
  };
}
export class FakeAgentApi {
  readonly list = vi.fn(async (_signal: AbortSignal) => [fixtureSession()]);
  readonly get = vi.fn(async (sid: string, _signal: AbortSignal) => fixtureSession(0, [], sid));
  readonly create = vi.fn(async () => fixtureSession());
  readonly patch = vi.fn(async () => fixtureSession(1));
  readonly reject = vi.fn(async () => fixtureSession(2, [fixtureRun('rejected')]));
  readonly cancel = vi.fn(async () => fixtureSession(2, [fixtureRun('cancelling')]));
  readonly archive = vi.fn(async () => ({ ...fixtureSession(2), archived: true }));
  readonly calls: {
    request: AgentStreamRequest;
    signal: AbortSignal;
    emit: (event: AgentEvent) => void;
    resolve: () => void;
    reject: (error: Error) => void;
  }[] = [];
  stream(
    request: AgentStreamRequest,
    signal: AbortSignal,
    emit: (event: AgentEvent) => void,
  ): Promise<void> {
    return new Promise((resolve, reject) =>
      this.calls.push({ request, signal, emit, resolve, reject }),
    );
  }
  event(index: number, sequence: number, run: AgentRun): void {
    const call = this.calls[index];
    call.emit(fixtureEvent(call.request, sequence, run));
  }
}
