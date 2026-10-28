import { inject, Injectable } from '@angular/core';
import { FETCH } from './chat-api';
import { SseDecoder, type SseEvent } from './sse-decoder';
import { advanceResults, type ResultFrontier } from './agent-progress';
import {
  AgentApiError,
  artifactId,
  id,
  invalidAgent,
  object,
  parseArtifactPage,
  parseAgentSession,
  parseAgentEvent,
  parseAgentRun,
  parseSessionsPage,
  parseRunsPage,
  parseResultsPage,
  parseEventsPage,
  identityMatches,
  sameControl,
  isRecoveryEvent,
  settled,
  time,
  type AgentCommand,
  type AgentEvent,
  type AgentSession,
  type ArtifactPage,
  type ReviewIdentity,
  type AgentRun,
  type SessionsPage,
  type RunsPage,
  type ResultsPage,
  type EventsPage,
  type ControlPlanIdentity,
  type AgentPlan,
  type RunControl,
} from './agent.models';
import type { ReasoningEffort } from './chat.models';

const BASE = '/api/agent/sessions';
const SESSION_EVENTS = new Set([
  'session.created',
  'session.updated',
  'session.archive_pending',
  'session.archived',
]);
export interface AgentStreamRequest {
  session_id: string;
  request_id: string;
  sequence: number;
  command: AgentCommand;
  snapshot?: AgentSession;
  original_request?: string;
  reviewed_plan?: AgentPlan;
}

/** Session journal records share a sequence; only work records own the command/operation. */
export class AgentEventDecoder {
  private sequence: number;
  private signatures = new Map<number, string>();
  private runId?: string;
  private operationId?: string;
  private ownedStarted = false;
  private previous?: AgentSession;
  private readonly startingRunId: string | null;
  private frozenPlan?: ControlPlanIdentity;
  private ownedControl: RunControl | null = null;
  private frontier: ResultFrontier | null = null;
  terminal = false;

  constructor(private readonly request: AgentStreamRequest) {
    this.sequence = request.sequence;
    this.previous = request.snapshot;
    this.startingRunId = request.snapshot?.latest_run_id ?? null;
    if (request.command.action === 'execute')
      this.frozenPlan = request.snapshot?.current_run?.plan_identity ?? undefined;
    this.runId = request.command.action === 'runs' ? undefined : request.command.run_id;
  }

  decode(frame: SseEvent): AgentEvent | null {
    if (frame.event !== 'agent') return null;
    let value: unknown;
    try {
      value = JSON.parse(frame.data);
    } catch {
      return invalidAgent();
    }
    if (
      !object(value) ||
      value['schema_version'] !== 2 ||
      !/^\d+$/.test(frame.id) ||
      !Number.isSafeInteger(value['sequence']) ||
      Number(frame.id) !== value['sequence'] ||
      value['session_id'] !== this.request.session_id ||
      !id(value['request_id']) ||
      !(value['run_id'] === null || id(value['run_id'])) ||
      !(value['operation_id'] === null || id(value['operation_id'])) ||
      !id(value['type']) ||
      !time(value['time'])
    )
      return invalidAgent();
    const event = parseAgentEvent(value);
    const session = event.session;
    const sequence = value['sequence'] as number;
    if (session.id !== value['session_id'] || session.sequence !== sequence) return invalidAgent();
    const signature = frame.data;
    if (this.signatures.get(sequence) === signature) return null;
    if (this.terminal || sequence <= this.sequence) return invalidAgent();
    if (isRecoveryEvent(event)) {
      if (sequence !== this.sequence + 1) return invalidAgent();
      this.commit(sequence, signature, session);
      this.terminal = true;
      return event;
    }
    if (
      this.ownedStarted &&
      this.previous &&
      (this.previous.model !== session.model ||
        this.previous.reasoning_effort !== session.reasoning_effort)
    )
      return invalidAgent();
    if (SESSION_EVENTS.has(value['type'])) {
      if (
        sequence !== this.sequence + 1 ||
        value['run_id'] !== null ||
        value['operation_id'] !== null ||
        (this.ownedStarted &&
          !event.invalidates_runs &&
          (session.current_run?.id !== this.runId ||
            (session.current_run?.operation_id !== null &&
              session.current_run?.operation_id !== this.operationId) ||
            (this.frozenPlan &&
              !identityMatches(this.frozenPlan, session.current_run?.plan_identity ?? null))))
      )
        return invalidAgent();
      const control = session.current_run;
      if (
        this.ownedControl &&
        control &&
        !event.invalidates_runs &&
        (control.result_count !== this.ownedControl.result_count ||
          control.completed_steps !== this.ownedControl.completed_steps ||
          control.version_sequence < this.ownedControl.version_sequence)
      )
        return invalidAgent();
      this.commit(sequence, signature, session);
      this.terminal = event.invalidates_runs;
      return value as unknown as AgentEvent;
    }
    if (
      value['request_id'] !== this.request.request_id ||
      !id(value['run_id']) ||
      !id(value['operation_id']) ||
      (this.runId && value['run_id'] !== this.runId) ||
      (this.operationId && value['operation_id'] !== this.operationId)
    )
      return invalidAgent();
    const run = event.run;
    if (!run || (run.operation_id !== null && run.operation_id !== value['operation_id']))
      return invalidAgent();
    const command = this.request.command;
    if (
      event.type === 'run.planning' &&
      command.action === 'revise' &&
      this.request.original_request !== undefined &&
      event.request !== this.request.original_request
    )
      return invalidAgent();
    const admission =
      !this.ownedStarted &&
      (command.action === 'execute'
        ? value['type'] === 'run.approved' && run.status === 'approved'
        : value['type'] === 'run.planning' && run.status === 'planning');
    if (!this.ownedStarted && !admission) return invalidAgent();
    // The server starts at its admission sequence, which may follow unseen metadata commits.
    if (sequence !== this.sequence + 1 && !admission) return invalidAgent();
    if (
      command.action === 'runs' &&
      ((event.type === 'run.planning' && event.request !== command.content) ||
        (!this.runId && this.startingRunId === run.id))
    )
      return invalidAgent();
    if (command.action === 'execute') {
      if (
        !this.request.reviewed_plan ||
        !run.plan_identity ||
        run.plan_identity.id !== command.plan_id ||
        run.plan_identity.revision !== command.revision ||
        run.plan_identity.digest !== command.digest ||
        (this.request.reviewed_plan &&
          !identityMatches(this.request.reviewed_plan, run.plan_identity)) ||
        (this.frozenPlan && !identityMatches(this.frozenPlan, run.plan_identity)) ||
        ['planning', 'awaiting_approval', 'awaiting_clarification', 'rejected'].includes(run.status)
      )
        return invalidAgent();
    } else if (['approved', 'executing', 'completed'].includes(run.status)) return invalidAgent();
    else if (
      command.action === 'revise' &&
      run.status === 'awaiting_approval' &&
      (!run.plan_identity ||
        run.plan_identity.revision <= command.revision ||
        run.plan_identity.digest === command.digest)
    )
      return invalidAgent();
    const previousRun =
      this.ownedControl ??
      (this.previous?.current_run?.id === run.id ? this.previous.current_run : null);
    if (
      previousRun &&
      (previousRun.created_at !== run.created_at ||
        run.version_sequence < previousRun.version_sequence ||
        (run.version_sequence === previousRun.version_sequence && !sameControl(previousRun, run)) ||
        run.completed_steps < previousRun.completed_steps ||
        run.result_count < previousRun.result_count ||
        (previousRun.plan_identity &&
          run.plan_identity &&
          run.plan_identity.revision < previousRun.plan_identity.revision) ||
        (previousRun.plan_identity &&
          run.plan_identity &&
          previousRun.plan_identity.revision === run.plan_identity.revision &&
          !identityMatches(previousRun.plan_identity, run.plan_identity)) ||
        (previousRun.status === 'executing' && run.status === 'approved') ||
        (previousRun.status === 'cancelling' &&
          !['cancelling', 'cancelled', 'interrupted', 'failed'].includes(run.status)))
    )
      return invalidAgent();
    if (this.request.command.action === 'execute' && this.request.reviewed_plan)
      this.frontier = advanceResults(previousRun, event, this.request.reviewed_plan, this.frontier);
    this.ownedControl = run;
    this.runId = run.id;
    if (command.action === 'execute') this.frozenPlan ??= run.plan_identity ?? undefined;
    this.operationId = value['operation_id'];
    this.ownedStarted = true;
    this.commit(sequence, signature, session);
    this.terminal =
      event.invalidates_runs ||
      settled(run) ||
      ['awaiting_approval', 'awaiting_clarification'].includes(run.status);
    return value as unknown as AgentEvent;
  }

  private commit(sequence: number, signature: string, session: AgentSession): void {
    this.sequence = sequence;
    this.signatures.set(sequence, signature);
    if (this.signatures.size > 64) this.signatures.delete(this.signatures.keys().next().value!);
    this.previous = session;
  }
}

@Injectable({ providedIn: 'root' })
export class AgentApi {
  private readonly fetch = inject(FETCH);

  async list(signal: AbortSignal, cursor?: string): Promise<SessionsPage> {
    const page = parseSessionsPage(
      await this.json(
        `${BASE}?include_archived=true&limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        signal,
      ),
    );
    if (
      page.sessions.length > 25 ||
      (page.next_cursor !== null && (!page.sessions.length || page.next_cursor === cursor))
    )
      return invalidAgent();
    return page;
  }
  async get(sid: string, signal: AbortSignal): Promise<AgentSession> {
    return this.session(sid, await this.json(this.path(sid), signal));
  }
  async runs(sid: string, signal: AbortSignal, before?: number): Promise<RunsPage> {
    this.before(before);
    const page = parseRunsPage(
      await this.json(
        `${this.path(sid)}/runs?limit=10${before === undefined ? '' : `&before=${before}`}`,
        signal,
      ),
    );
    if (
      page.session_id !== sid ||
      page.runs.length > 10 ||
      (page.next_before !== null &&
        (!page.runs.length || (before !== undefined && page.next_before >= before)))
    )
      return invalidAgent();
    return page;
  }
  async detail(sid: string, rid: string, signal: AbortSignal, before?: number): Promise<AgentRun> {
    this.before(before);
    const run = parseAgentRun(
      await this.json(
        `${this.path(sid)}/runs/${encodeURIComponent(rid)}?limit=20${before === undefined ? '' : `&before=${before}`}`,
        signal,
      ),
    );
    if (run.id !== rid || run.results.length > 20) return invalidAgent();
    return run;
  }
  async results(
    sid: string,
    rid: string,
    signal: AbortSignal,
    before?: number,
  ): Promise<ResultsPage> {
    this.before(before);
    const page = parseResultsPage(
      await this.json(
        `${this.path(sid)}/runs/${encodeURIComponent(rid)}/results?limit=20${before === undefined ? '' : `&before=${before}`}`,
        signal,
      ),
    );
    if (
      page.session_id !== sid ||
      page.run_id !== rid ||
      page.results.length > 20 ||
      (page.next_before !== null &&
        (!page.results.length || (before !== undefined && page.next_before >= before)))
    )
      return invalidAgent();
    return page;
  }
  async events(
    sid: string,
    after: number,
    signal: AbortSignal,
    through?: number,
  ): Promise<EventsPage> {
    this.before(after);
    this.before(through);
    const page = parseEventsPage(
      await this.json(
        `${this.path(sid)}/events?after=${after}&limit=50${through === undefined ? '' : `&through=${through}`}`,
        signal,
      ),
    );
    if (
      page.events.length > 50 ||
      page.events.some((event) => event.session_id !== sid) ||
      (page.events.length && page.events[0].sequence !== after + 1) ||
      page.next_after < after ||
      (through !== undefined && page.through_sequence !== through) ||
      (!page.events.length && page.has_more)
    )
      return invalidAgent();
    return page;
  }
  private before(value?: number): void {
    if (value !== undefined && (!Number.isSafeInteger(value) || value < 0)) invalidAgent();
  }
  /** Reads retained evidence only; never dispatches or replays a tool. */
  async artifactPage(
    sid: string,
    artifact_id: string,
    offset: number,
    limit: number,
    signal: AbortSignal,
  ): Promise<ArtifactPage> {
    if (
      !artifactId(artifact_id) ||
      !Number.isSafeInteger(offset) ||
      offset < 0 ||
      !Number.isSafeInteger(limit) ||
      limit < 1 ||
      limit > 16000
    )
      return invalidAgent();
    const page = parseArtifactPage(
      await this.json(
        `${this.path(sid)}/artifacts/${artifact_id}?offset=${offset}&limit=${limit}`,
        signal,
      ),
    );
    signal.throwIfAborted();
    if (
      page.artifact_id !== artifact_id ||
      page.offset !== offset ||
      page.returned_chars !== Math.min(limit, page.total_chars - offset)
    )
      return invalidAgent();
    return page;
  }
  async create(
    request_id: string,
    model: string,
    reasoning_effort: ReasoningEffort,
    signal: AbortSignal,
  ): Promise<AgentSession> {
    return parseAgentSession(
      await this.json(BASE, signal, 'POST', { request_id, model, reasoning_effort }),
    );
  }
  async patch(
    sid: string,
    request_id: string,
    preferences: { model?: string; reasoning_effort?: ReasoningEffort; title?: string },
    signal: AbortSignal,
  ): Promise<AgentSession> {
    return this.session(
      sid,
      await this.json(this.path(sid), signal, 'PATCH', { request_id, ...preferences }),
    );
  }
  async reject(
    sid: string,
    rid: string,
    request_id: string,
    identity: ReviewIdentity,
    signal: AbortSignal,
  ): Promise<AgentSession> {
    return this.session(
      sid,
      await this.json(`${this.path(sid)}/runs/${encodeURIComponent(rid)}/reject`, signal, 'POST', {
        request_id,
        ...identity,
      }),
    );
  }
  async cancel(
    sid: string,
    rid: string,
    request_id: string,
    operation_id: string,
    signal: AbortSignal,
  ): Promise<AgentSession> {
    return this.session(
      sid,
      await this.json(`${this.path(sid)}/runs/${encodeURIComponent(rid)}/cancel`, signal, 'POST', {
        request_id,
        operation_id,
      }),
    );
  }
  async archive(sid: string, request_id: string, signal: AbortSignal): Promise<AgentSession> {
    return this.session(
      sid,
      await this.json(`${this.path(sid)}/archive`, signal, 'POST', { request_id }),
    );
  }
  async delete(sid: string, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    const response = await this.fetch(this.path(sid), { method: 'DELETE', signal });
    signal.throwIfAborted();
    if (response.status === 409) {
      let code: unknown;
      try {
        const body: unknown = await response.clone().json();
        if (object(body) && object(body['error'])) code = body['error']['code'];
      } catch {
        // Fall back to the normal HTTP error message for an unrecognized response.
      }
      signal.throwIfAborted();
      if (code === 'session_in_use')
        throw new AgentApiError(
          'http_409',
          'Cannot delete this session while it is in use. Wait for session readers and streams to close.',
        );
      if (code === 'operation_active')
        throw new AgentApiError(
          'http_409',
          'Cannot delete an active or settling session. Stop the run first and wait for it to settle.',
        );
    }
    await this.check(response);
    if (response.status !== 204) invalidAgent();
  }
  async stream(
    request: AgentStreamRequest,
    signal: AbortSignal,
    onEvent: (event: AgentEvent) => void,
  ): Promise<void> {
    const { action, ...fields } = request.command;
    const { run_id, ...body } = fields as typeof fields & { run_id?: string };
    const url =
      action === 'runs'
        ? `${this.path(request.session_id)}/runs`
        : `${this.path(request.session_id)}/runs/${encodeURIComponent(run_id!)}/${action}`;
    signal.throwIfAborted();
    const response = await this.fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ request_id: request.request_id, ...body }),
      signal,
    });
    signal.throwIfAborted();
    await this.check(response);
    if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body)
      return invalidAgent();
    const reader = response.body.getReader();
    const framing = new SseDecoder();
    const events = new AgentEventDecoder(request);
    const cancel = () => void reader.cancel().catch(() => {});
    signal.addEventListener('abort', cancel, { once: true });
    try {
      while (!events.terminal) {
        signal.throwIfAborted();
        const chunk = await reader.read();
        signal.throwIfAborted();
        for (const frame of chunk.done ? framing.finish() : framing.push(chunk.value)) {
          const event = events.decode(frame);
          if (event) onEvent(event);
        }
        if (chunk.done && !events.terminal)
          throw new AgentApiError(
            'interrupted',
            'Agent connection ended before settlement. Reload history to recover; execution will not be reposted.',
          );
      }
    } finally {
      signal.removeEventListener('abort', cancel);
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  private path(sid: string): string {
    return `${BASE}/${encodeURIComponent(sid)}`;
  }
  private session(sid: string, body: unknown): AgentSession {
    const session = parseAgentSession(body);
    if (session.id !== sid) return invalidAgent();
    return session;
  }
  private async json(
    url: string,
    signal: AbortSignal,
    method = 'GET',
    body?: unknown,
  ): Promise<unknown> {
    signal.throwIfAborted();
    const response = await this.fetch(url, {
      method,
      signal,
      headers: {
        Accept: 'application/json',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    signal.throwIfAborted();
    await this.check(response);
    try {
      return await response.json();
    } catch {
      return invalidAgent();
    }
  }
  private async check(response: Response): Promise<void> {
    if (response.ok) return;
    let detail = '';
    try {
      const body: unknown = await response.json();
      if (object(body)) {
        const value = body['detail'] ?? body['error'];
        if (typeof value === 'string') detail = value;
        else if (object(value) && typeof value['message'] === 'string') detail = value['message'];
        else if (Array.isArray(value))
          detail = value
            .filter(object)
            .map((entry) => entry['msg'])
            .filter((msg) => typeof msg === 'string')
            .join('; ');
      }
    } catch {
      /* Status remains actionable for non-JSON errors. */
    }
    throw new AgentApiError(
      `http_${response.status}`,
      `Agent request failed (HTTP ${response.status}). ${detail || 'Check the BFF Agent configuration and reload history.'}`,
    );
  }
}
