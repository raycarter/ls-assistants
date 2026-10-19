import { inject, Injectable } from '@angular/core';
import { FETCH } from './chat-api';
import { SseDecoder, type SseEvent } from './sse-decoder';
import {
  AgentApiError,
  id,
  invalidAgent,
  object,
  parseAgentSession,
  settled,
  time,
  type AgentCommand,
  type AgentEvent,
  type AgentSession,
  type ReviewIdentity,
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
}

/** Session journal records share a sequence; only work records own the command/operation. */
export class AgentEventDecoder {
  private sequence: number;
  private signatures = new Map<number, string>();
  private runId?: string;
  private operationId?: string;
  private ownedStarted = false;
  private previous?: AgentSession;
  private readonly startingRunIds: ReadonlySet<string>;
  terminal = false;

  constructor(private readonly request: AgentStreamRequest) {
    this.sequence = request.sequence;
    this.previous = request.snapshot;
    this.startingRunIds = new Set(request.snapshot?.runs.map((run) => run.id));
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
      value['schema_version'] !== 1 ||
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
    const session = parseAgentSession(value['session']);
    const sequence = value['sequence'] as number;
    if (session.id !== value['session_id'] || session.sequence !== sequence) return invalidAgent();
    const signature = frame.data;
    if (this.signatures.get(sequence) === signature) return null;
    if (this.terminal || sequence <= this.sequence) return invalidAgent();
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
        (this.ownedStarted && !session.runs.some((run) => run.id === this.runId))
      )
        return invalidAgent();
      this.commit(sequence, signature, session);
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
    const run = session.runs.find((entry) => entry.id === value['run_id']);
    if (!run || (run.operation_id !== null && run.operation_id !== value['operation_id']))
      return invalidAgent();
    const command = this.request.command;
    const admission =
      !this.ownedStarted &&
      (command.action === 'execute'
        ? value['type'] === 'run.approved' && run.status === 'approved'
        : value['type'] === 'run.planning' && run.status === 'planning');
    // The server starts at its admission sequence, which may follow unseen metadata commits.
    if (sequence !== this.sequence + 1 && !admission) return invalidAgent();
    if (
      command.action === 'runs' &&
      (run.request !== command.content || (!this.runId && this.startingRunIds.has(run.id)))
    )
      return invalidAgent();
    if (command.action === 'execute') {
      if (
        !run.plan ||
        run.plan.id !== command.plan_id ||
        run.plan.revision !== command.revision ||
        run.plan.digest !== command.digest ||
        ['planning', 'awaiting_approval', 'awaiting_clarification', 'rejected'].includes(run.status)
      )
        return invalidAgent();
    } else if (['approved', 'executing', 'completed'].includes(run.status)) return invalidAgent();
    else if (
      command.action === 'revise' &&
      run.status === 'awaiting_approval' &&
      (!run.plan || run.plan.revision <= command.revision || run.plan.digest === command.digest)
    )
      return invalidAgent();
    const previousRun = this.previous?.runs.find((entry) => entry.id === run.id);
    if (
      previousRun &&
      (previousRun.request !== run.request ||
        previousRun.created_at !== run.created_at ||
        (previousRun.plan && run.plan && run.plan.revision < previousRun.plan.revision) ||
        (previousRun.plan &&
          run.plan &&
          previousRun.plan.id === run.plan.id &&
          previousRun.plan.revision === run.plan.revision &&
          JSON.stringify(previousRun.plan) !== JSON.stringify(run.plan)) ||
        (previousRun.status === 'executing' && run.status === 'approved') ||
        (previousRun.status === 'cancelling' &&
          !['cancelling', 'cancelled', 'interrupted', 'failed'].includes(run.status)))
    )
      return invalidAgent();
    this.runId = run.id;
    this.operationId = value['operation_id'];
    this.ownedStarted = true;
    this.commit(sequence, signature, session);
    this.terminal =
      settled(run) || ['awaiting_approval', 'awaiting_clarification'].includes(run.status);
    return value as unknown as AgentEvent;
  }

  private commit(sequence: number, signature: string, session: AgentSession): void {
    this.sequence = sequence;
    this.signatures.set(sequence, signature);
    this.previous = session;
  }
}

@Injectable({ providedIn: 'root' })
export class AgentApi {
  private readonly fetch = inject(FETCH);

  async list(signal: AbortSignal): Promise<AgentSession[]> {
    const body = await this.json(`${BASE}?include_archived=true`, signal);
    if (!object(body) || !Array.isArray(body['sessions'])) return invalidAgent();
    const sessions = body['sessions'].map(parseAgentSession);
    if (new Set(sessions.map((session) => session.id)).size !== sessions.length)
      return invalidAgent();
    return sessions;
  }
  async get(sid: string, signal: AbortSignal): Promise<AgentSession> {
    return this.session(sid, await this.json(this.path(sid), signal));
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
