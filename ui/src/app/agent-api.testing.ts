import { vi } from 'vitest';
import type { AgentStreamRequest } from './agent-api';
import {
  controlOf,
  settled,
  type AgentEvent,
  type AgentPlan,
  type AgentRun,
  type AgentSession,
  type RunStatus,
  type SessionsPage,
  type RunsPage,
  type ResultsPage,
  type StepResult,
} from './agent.models';

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
    projection_version: 2,
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
    clarification: null,
    version_sequence: 0,
    result_count: 0,
    completed_steps: 0,
    results_before: null,
  };
}
export function fixtureSession(sequence = 0, runs: AgentRun[] = [], sid = 'agent-1'): AgentSession {
  const bodies = runs.map((run) => normalizeRun(run, sequence));
  const session: AgentSession = {
    projection_version: 2,
    id: sid,
    title: 'Fixture Agent',
    model: 'model-a',
    reasoning_effort: 'medium',
    created_at: FIXTURE_TIME,
    updated_at: FIXTURE_TIME,
    archived: false,
    archive_pending: false,
    sequence,
    run_count: bodies.length,
    latest_run_id: bodies.at(-1)?.id ?? null,
    current_run: bodies.find((run) => !settled(run))
      ? controlOf(bodies.find((run) => !settled(run))!)
      : null,
  };
  fixtureBodies.set(session, bodies);
  return session;
}
export const fixtureBodies = new WeakMap<AgentSession, AgentRun[]>();
export function normalizeRun(run: AgentRun, sequence: number): AgentRun {
  return {
    ...run,
    version_sequence: sequence,
    result_count: Math.max(run.result_count, run.results.length),
    completed_steps: Math.max(
      run.completed_steps,
      run.results.filter((result) => result.status === 'completed').length,
    ),
  };
}
export function fixtureList(
  sessions = [fixtureSession()],
  next_cursor: string | null = null,
  total = sessions.length,
): SessionsPage {
  return { projection_version: 2, sessions, next_cursor, total };
}
export function fixtureEvent(
  request: AgentStreamRequest,
  sequence: number,
  run: AgentRun,
): AgentEvent {
  return {
    schema_version: 2,
    invalidates_runs: false,
    sequence,
    time: FIXTURE_TIME,
    session_id: request.session_id,
    request_id: request.request_id,
    run_id: run.id,
    operation_id: 'operation-1',
    type: `run.${run.status}`,
    session: fixtureSession(sequence, [run], request.session_id),
    run: controlOf(normalizeRun(run, sequence)),
    request: run.status === 'planning' ? run.request : null,
    plan: run.status === 'awaiting_approval' ? run.plan : null,
    result: null,
    answer: run.status === 'completed' ? run.answer : null,
    error: run.error,
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
export function fixtureStepEvents(
  request: AgentStreamRequest,
  sequence: number,
  run: AgentRun,
  result: StepResult,
): [AgentEvent, AgentEvent] {
  const index = run.plan!.steps.findIndex((step) => step.id === result.step_id);
  const executing = {
    ...run,
    status: 'executing' as const,
    operation_id: 'operation-1',
    results: [],
    result_count: index + 1,
    completed_steps: index,
  };
  const intent = fixtureEvent(request, sequence, executing);
  intent.type = 'tool.intent';
  intent.result = { step_id: result.step_id, status: 'running', content: '', time: result.time };
  const finished = fixtureEvent(request, sequence + 1, {
    ...executing,
    completed_steps: index + (result.status === 'completed' ? 1 : 0),
  });
  finished.type = 'tool.result';
  finished.result = result;
  return [intent, finished];
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
  readonly list = vi.fn(async (_signal: AbortSignal, _cursor?: string) => fixtureList());
  readonly get = vi.fn(async (sid: string, _signal: AbortSignal) => fixtureSession(0, [], sid));
  readonly create = vi.fn(async () => fixtureSession());
  readonly patch = vi.fn(async () => fixtureSession(1));
  readonly reject = vi.fn(async () => {
    const header = fixtureSession(2, [fixtureRun('rejected')]);
    this.get.mockResolvedValue(header);
    this.readHeader = header;
    return header;
  });
  readonly cancel = vi.fn(async () => fixtureSession(2, [fixtureRun('cancelling')]));
  readonly archive = vi.fn(async () => ({ ...fixtureSession(2), archived: true }));
  readonly delete = vi.fn(async (_sid: string, _signal: AbortSignal): Promise<void> => {});
  private readHeader?: AgentSession;
  private async bodies(sid: string): Promise<AgentRun[]> {
    const header = this.readHeader ?? (await this.get.mock.results.at(-1)?.value);
    return header?.id === sid ? (fixtureBodies.get(header) ?? []) : [];
  }
  readonly detail = vi.fn(
    async (sid: string, rid: string, _signal: AbortSignal, _before?: number): Promise<AgentRun> => {
      return (await this.bodies(sid)).find((run) => run.id === rid) ?? { ...fixtureRun(), id: rid };
    },
  );
  readonly runs = vi.fn(
    async (sid: string, _signal: AbortSignal, _before?: number): Promise<RunsPage> => {
      const header = await this.get.mock.results.at(-1)?.value;
      const bodies = await this.bodies(sid);
      return {
        projection_version: 2,
        session_id: sid,
        at_sequence: header?.sequence ?? 0,
        total: bodies.length,
        runs: bodies
          .slice()
          .reverse()
          .map((run) => ({ ...controlOf(run), request_preview: run.request })),
        next_before: null,
      };
    },
  );
  readonly results = vi.fn(
    async (
      sid: string,
      rid: string,
      _signal: AbortSignal,
      _before?: number,
    ): Promise<ResultsPage> => {
      const run = (await this.bodies(sid)).find((run) => run.id === rid) ?? fixtureRun();
      const header = await this.get.mock.results.at(-1)?.value;
      return {
        projection_version: 2,
        session_id: sid,
        run_id: rid,
        at_sequence: header?.sequence ?? 0,
        total: run.result_count,
        completed_steps: run.completed_steps,
        results: run.results,
        next_before: run.results_before,
      };
    },
  );
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
