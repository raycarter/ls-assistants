import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fixture from './agent-contract.fixture.json';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import {
  parseAgentEvent,
  parseAgentRun,
  parseAgentSession,
  type AgentCommand,
  type AgentEvent,
  type AgentRun,
  type AgentSession,
  type ArtifactPage,
  type EventsPage,
  type RunsPage,
  type ResultsPage,
  planIdentity,
} from './agent.models';
import { FETCH } from './chat-api';
import { SseDecoder } from './sse-decoder';

interface RecordedRequest {
  method: 'POST' | 'PATCH';
  url: string;
  body: { request_id: string; [key: string]: unknown };
}
interface Exchange {
  name: string;
  baseline: AgentSession;
  command: AgentCommand;
  http: RecordedRequest;
  sse: string;
  terminal: AgentSession;
  terminal_run: AgentRun;
  controls?: { http: RecordedRequest; status: number; session: AgentSession }[];
  artifact_page?: { http: { method: 'GET'; url: string }; status: number; body: ArtifactPage };
}
interface ErrorExchange {
  name: string;
  baseline: AgentSession;
  command: AgentCommand;
  http: RecordedRequest;
  status: number;
  body: { error: { code: string; message: string } };
}
interface RecoveryExchange {
  name: string;
  baseline: AgentSession;
  baseline_run: AgentRun;
  http: { method: 'GET'; url: string };
  status: number;
  body: EventsPage;
  terminal: AgentSession;
  terminal_run: AgentRun;
  runs_page: RunsPage;
  results_page: ResultsPage;
}
const contract = fixture as unknown as {
  version: number;
  streams: Exchange[];
  errors: ErrorExchange[];
  recoveries: RecoveryExchange[];
};
function streamRequest(
  entry: Pick<Exchange, 'baseline' | 'command' | 'http'> & { terminal_run?: AgentRun },
): AgentStreamRequest {
  return {
    session_id: entry.baseline.id,
    request_id: entry.http.body.request_id,
    sequence: entry.baseline.sequence,
    snapshot: entry.baseline,
    command: entry.command,
    reviewed_plan:
      entry.command.action === 'execute' ? (entry.terminal_run?.plan ?? undefined) : undefined,
  };
}
function frames(entry: Exchange) {
  const decoder = new SseDecoder();
  return decoder.push(new TextEncoder().encode(entry.sse));
}
function chunked(raw: string): Response {
  const bytes = new TextEncoder().encode(raw);
  let offset = 0;
  return new Response(
    new ReadableStream({
      pull(controller) {
        if (offset === bytes.length) {
          controller.close();
          return;
        }
        controller.enqueue(bytes.slice(offset, (offset += 7)));
        offset = Math.min(offset, bytes.length);
      },
    }),
    { headers: { 'content-type': 'text/event-stream' } },
  );
}
function expectRequest(expected: RecordedRequest, input: RequestInfo | URL, init?: RequestInit) {
  expect(String(input)).toBe(expected.url);
  expect(init?.method).toBe(expected.method);
  expect(JSON.parse(String(init?.body))).toEqual(expected.body);
}

describe('backend-generated v2 Agent contract (static mocked FETCH)', () => {
  let fetchDouble: ReturnType<typeof vi.fn<typeof fetch>>;
  let api: AgentApi;
  beforeEach(() => {
    fetchDouble = vi.fn<typeof fetch>();
    TestBed.configureTestingModule({ providers: [{ provide: FETCH, useValue: fetchDouble }] });
    api = TestBed.inject(AgentApi);
  });
  afterEach(() => TestBed.resetTestingModule());
  it('covers ordinary and recorded LLM cases with separately loaded quota-free bodies', () => {
    expect(contract.version).toBe(2);
    expect(contract.streams.map((entry) => entry.name)).toEqual([
      'planning',
      'revision',
      'execution',
      'stale_baseline',
      'cancellation_archive',
      'backwards_clock',
      'fetch_execution',
      'recorded_planning',
      'grep_execution',
    ]);
    for (const entry of contract.streams) {
      expect(parseAgentSession(entry.baseline)).toEqual(entry.baseline);
      expect(parseAgentSession(entry.terminal)).toEqual(entry.terminal);
      expect(parseAgentRun(entry.terminal_run)).toEqual(entry.terminal_run);
      expect(entry.terminal).not.toHaveProperty('runs');
      expect(entry.terminal_run.plan).not.toHaveProperty('limits');
    }
  });
  it.each(contract.streams)(
    'decodes every chunked frame and reloads header + body for $name',
    async (entry) => {
      const expected = frames(entry).map((frame) => JSON.parse(frame.data) as AgentEvent);
      expect(expected.every((event) => event.invalidates_runs === false)).toBe(true);
      for (const event of expected)
        expect(
          () => parseAgentEvent(event),
          JSON.stringify({
            type: event.type,
            sequence: event.sequence,
            run: event.run,
            current: event.session.current_run,
            plan: event.plan,
          }),
        ).not.toThrow();
      const signal = new AbortController().signal;
      const received: AgentEvent[] = [];
      const response = chunked(entry.sse);
      fetchDouble.mockImplementationOnce(async (input, init) => {
        expectRequest(entry.http, input, init);
        return response;
      });
      const decoder = new AgentEventDecoder(streamRequest(entry));
      await api.stream(streamRequest(entry), signal, (event) => {
        expect(
          decoder.decode({
            event: 'agent',
            id: String(event.sequence),
            data: JSON.stringify(event),
          }),
        ).toEqual(event);
        received.push(event);
      });
      expect(received).toEqual(expected);
      expect(decoder.terminal).toBe(true);
      expect(response.body?.locked).toBe(false);
      fetchDouble.mockResolvedValueOnce(Response.json(entry.terminal));
      expect(await api.get(entry.baseline.id, signal)).toEqual(entry.terminal);
      fetchDouble.mockResolvedValueOnce(Response.json(entry.terminal_run));
      expect(await api.detail(entry.baseline.id, entry.terminal_run.id, signal)).toEqual(
        entry.terminal_run,
      );
      expect(fetchDouble.mock.calls.at(-1)?.[0]).toBe(
        `/api/agent/sessions/${entry.baseline.id}/runs/${entry.terminal_run.id}?limit=20`,
      );
      expect(fetchDouble.mock.calls.map(([, init]) => init?.method)).toEqual([
        'POST',
        'GET',
        'GET',
      ]);
    },
  );
  it('catches up in a finite explicit through-window with GET only', async () => {
    const entry = contract.streams.find((entry) => entry.name === 'execution')!;
    const events = frames(entry).map((frame) => JSON.parse(frame.data) as AgentEvent);
    const through = events.at(-1)!.sequence;
    const split = Math.ceil(events.length / 2);
    const signal = new AbortController().signal;
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        projection_version: 2,
        events: events.slice(0, split),
        next_after: events[split - 1].sequence,
        through_sequence: through,
        has_more: true,
      }),
    );
    const first = await api.events(entry.baseline.id, entry.baseline.sequence, signal);
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        projection_version: 2,
        events: events.slice(split),
        next_after: through,
        through_sequence: through,
        has_more: false,
      }),
    );
    const second = await api.events(
      entry.baseline.id,
      first.next_after,
      signal,
      first.through_sequence,
    );
    expect([...first.events, ...second.events]).toEqual(events);
    expect(second.has_more).toBe(false);
    expect(fetchDouble.mock.calls.every(([, init]) => init?.method === 'GET' && !init.body)).toBe(
      true,
    );
  });
  it.each(contract.recoveries)(
    'preserves $name invalidation and reloads bounded authoritative views with GET only',
    async (entry) => {
      const signal = new AbortController().signal;
      fetchDouble.mockImplementationOnce(async (input, init) => {
        expect(String(input)).toBe(entry.http.url);
        expect(init?.method).toBe('GET');
        return Response.json(entry.body, { status: entry.status });
      });
      const page = await api.events(entry.baseline.id, entry.baseline.sequence, signal);
      expect(page).toEqual(entry.body);
      expect(page.events.map((event) => event.type)).toEqual(
        entry.name === 'llm_startup_recovery'
          ? ['llm.outcome', 'run.interrupted']
          : ['run.interrupted'],
      );
      const event = page.events.at(-1)!;
      expect(event).toMatchObject({
        invalidates_runs: true,
        request_id: 'recovery',
        type: 'run.interrupted',
        run_id: null,
        operation_id: null,
        run: { id: entry.baseline_run.id, status: 'interrupted' },
        error: { code: 'outcome_unknown' },
      });
      expect(event.result).toEqual(
        entry.baseline_run.results.length ? expect.objectContaining({ status: 'failed' }) : null,
      );
      // Recovery is replayed through GET; only the final null-ID invalidation is terminal.
      const beforeInterruption = page.events.at(-2)?.session ?? entry.baseline;
      const decoder = new AgentEventDecoder({
        session_id: entry.baseline.id,
        sequence: beforeInterruption.sequence,
        request_id: 'original-execution',
        snapshot: beforeInterruption,
        reviewed_plan: entry.baseline_run.plan ?? undefined,
        command: entry.baseline_run.plan
          ? {
              action: 'execute',
              run_id: entry.baseline_run.id,
              ...planIdentity(entry.baseline_run.plan),
            }
          : { action: 'runs', content: entry.baseline_run.request },
      });
      expect(
        decoder.decode({ event: 'agent', id: String(event.sequence), data: JSON.stringify(event) }),
      ).toEqual(event);
      expect(decoder.terminal).toBe(true);
      fetchDouble.mockResolvedValueOnce(Response.json(entry.terminal));
      expect(await api.get(entry.baseline.id, signal)).toEqual(entry.terminal);
      fetchDouble.mockResolvedValueOnce(Response.json(entry.terminal_run));
      expect(await api.detail(entry.baseline.id, entry.terminal_run.id, signal)).toEqual(
        entry.terminal_run,
      );
      fetchDouble.mockResolvedValueOnce(Response.json(entry.runs_page));
      expect(await api.runs(entry.baseline.id, signal)).toEqual(entry.runs_page);
      fetchDouble.mockResolvedValueOnce(Response.json(entry.results_page));
      expect(await api.results(entry.baseline.id, entry.terminal_run.id, signal)).toEqual(
        entry.results_page,
      );
      expect(fetchDouble).toHaveBeenCalledTimes(5);
      expect(fetchDouble.mock.calls.every(([, init]) => init?.method === 'GET' && !init.body)).toBe(
        true,
      );
    },
  );
  it.each(['fetch_execution', 'grep_execution'])(
    'preserves generated metadata and reads artifact continuation for %s',
    async (name) => {
      const entry = contract.streams.find((entry) => entry.name === name)!;
      const continuation = entry.artifact_page!;
      const result = entry.terminal_run.results[0];
      const metadata = result.grep ?? result.fetch!;
      fetchDouble.mockImplementationOnce(async (input, init) => {
        expect(String(input)).toBe(continuation.http.url);
        expect(init?.method).toBe('GET');
        return Response.json(continuation.body);
      });
      expect(
        await api.artifactPage(
          entry.baseline.id,
          metadata.artifact_id,
          metadata.next_offset!,
          12000,
          new AbortController().signal,
        ),
      ).toEqual(continuation.body);
      expect(continuation.body.offset).toBe(metadata.next_offset);
      expect(continuation.body.next_offset).toBeNull();
      const retained = result.content + continuation.body.content;
      if (name === 'grep_execution') {
        expect(entry.terminal_run.plan?.steps).toHaveLength(1);
        expect(entry.terminal_run.plan?.steps[0]).toMatchObject({
          tool: 'grep_in_folder',
          arguments: {
            workspace_id: 'public',
            path: '.',
            pattern: 'alpha',
            file_glob: '*.txt',
            offset: 0,
            limit: 18,
          },
        });
        expect(result.grep).toMatchObject({
          returned_chars: 18,
          total_matches: 1,
          files_searched: 1,
          skipped_binary: 0,
          skipped_unreadable: 0,
          complete: true,
        });
        expect(
          retained
            .trim()
            .split('\n')
            .map((line) => JSON.parse(line)),
        ).toEqual([{ path: 'public.txt', line: 1, text: 'alpha 🌱' }]);
        const events = frames(entry).map((frame) => parseAgentEvent(JSON.parse(frame.data)));
        expect(events.map((event) => event.type)).toEqual([
          'run.approved',
          'run.executing',
          'tool.intent',
          'tool.result',
          'run.completed',
        ]);
        const plan = entry.terminal_run.plan!;
        expect(events[0].run?.plan_identity).toEqual({
          id: plan.id,
          revision: plan.revision,
          digest: plan.digest,
          model: plan.model,
          reasoning_effort: plan.reasoning_effort,
        });
        expect(events[3].result).toEqual(result);
        expect(events.map((event) => event.run?.result_count)).toEqual([0, 0, 1, 1, 1]);
        expect(events.map((event) => event.run?.completed_steps)).toEqual([0, 0, 0, 1, 1]);
      } else {
        expect(retained).toBe('Public evidence 🌱\nContinuation from durable storage.');
      }
      expect(fetchDouble).toHaveBeenCalledOnce();
    },
  );
  const controls = contract.streams.flatMap((entry) =>
    (entry.controls ?? []).map((control) => ({
      ...control,
      entry,
      name: control.http.body.request_id,
    })),
  );
  it.each(controls)(
    'preserves recorded mutation header/status for $name',
    async ({ entry, http, status, session }) => {
      fetchDouble.mockImplementationOnce(async (input, init) => {
        expectRequest(http, input, init);
        return Response.json(session, { status });
      });
      const signal = new AbortController().signal;
      let response: AgentSession;
      if (http.method === 'PATCH')
        response = await api.patch(
          entry.baseline.id,
          http.body.request_id,
          { title: http.body['title'] as string },
          signal,
        );
      else if (http.url.endsWith('/archive'))
        response = await api.archive(entry.baseline.id, http.body.request_id, signal);
      else {
        if (entry.command.action !== 'execute') throw new Error('Expected execution');
        response = await api.cancel(
          entry.baseline.id,
          entry.command.run_id,
          http.body.request_id,
          http.body['operation_id'] as string,
          signal,
        );
      }
      expect(response).toEqual(session);
      expect(fetchDouble).toHaveBeenCalledOnce();
    },
  );
  it.each(contract.errors)(
    'surfaces $name and recovers by GET without command replay',
    async (entry) => {
      fetchDouble.mockImplementationOnce(async (input, init) => {
        expectRequest(entry.http, input, init);
        return Response.json(entry.body, { status: entry.status });
      });
      const signal = new AbortController().signal;
      const emit = vi.fn();
      await expect(api.stream(streamRequest(entry), signal, emit)).rejects.toMatchObject({
        code: `http_${entry.status}`,
        message: expect.stringContaining(entry.body.error.message),
      });
      expect(emit).not.toHaveBeenCalled();
      fetchDouble.mockResolvedValueOnce(Response.json(entry.baseline));
      expect(await api.get(entry.baseline.id, signal)).toEqual(entry.baseline);
      expect(fetchDouble.mock.calls.map(([, init]) => init?.method)).toEqual(['POST', 'GET']);
    },
  );
});
