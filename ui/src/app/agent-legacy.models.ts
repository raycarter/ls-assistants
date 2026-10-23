import { REASONING_EFFORTS, type ReasoningEffort } from './chat.models';

export const RUN_STATUSES = [
  'planning',
  'awaiting_clarification',
  'awaiting_approval',
  'approved',
  'executing',
  'cancelling',
  'completed',
  'rejected',
  'failed',
  'cancelled',
  'interrupted',
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];
export interface ReadArguments {
  workspace_id: string;
  path: string;
  offset: number;
  limit: number;
}
export interface FetchArguments {
  url: string;
  offset: number;
  limit: number;
}
export type AgentStep = {
  id: string;
  title: string;
  description: string;
} & (
  { tool: 'read_file'; arguments: ReadArguments } | { tool: 'web_fetch'; arguments: FetchArguments }
);
export interface ArtifactPage {
  artifact_id: string;
  content: string;
  offset: number;
  returned_chars: number;
  total_chars: number;
  next_offset: number | null;
}
export interface FetchMetadata extends Omit<ArtifactPage, 'content'> {
  requested_url: string;
  final_url: string;
  title: string | null;
  status_code: number;
  content_type: string;
  fetched_at: string;
  redirects: string[];
}
export interface AgentPlan {
  id: string;
  revision: number;
  digest: string;
  objective: string;
  assumptions: string[];
  model: string;
  reasoning_effort: ReasoningEffort;
  limits?: { max_steps: number; max_tool_calls: number; timeout_seconds: number };
  steps: AgentStep[];
  search_policy?: unknown;
}
export interface StepResult {
  step_id: string;
  status: 'running' | 'completed' | 'failed';
  content: string;
  time: string;
  error?: { code: string; message: string } | null;
  path?: string | null;
  offset?: number | null;
  returned_lines?: number | null;
  truncated?: boolean;
  fetch?: FetchMetadata | null;
}
export interface AgentRun {
  id: string;
  request: string;
  created_at: string;
  updated_at: string;
  status: RunStatus;
  operation_id: string | null;
  plan: AgentPlan | null;
  results: StepResult[];
  answer: string | null;
  error: { code: string; message: string } | null;
  clarification?: { id: string; questions: string[] } | null;
}
export interface AgentSession {
  id: string;
  title: string;
  model: string;
  reasoning_effort: ReasoningEffort;
  created_at: string;
  updated_at: string;
  archived: boolean;
  archive_pending?: boolean;
  sequence: number;
  runs: AgentRun[];
}
export interface AgentEvent {
  schema_version: 1;
  sequence: number;
  time: string;
  session_id: string;
  request_id: string;
  run_id: string | null;
  operation_id: string | null;
  type: string;
  session: AgentSession;
}
export type PlanIdentity = Pick<AgentPlan, 'revision' | 'digest'> & { plan_id: string };
export type ReviewIdentity =
  (PlanIdentity & { kind: 'plan' }) | { kind: 'clarification'; clarification_id: string };
export type Revision = PlanIdentity & { kind: 'feedback'; content: string };
export type AgentCommand =
  | { action: 'runs'; content: string }
  | ({ action: 'execute'; run_id: string } & PlanIdentity)
  | ({ action: 'revise'; run_id: string } & Revision);

export function settled(run: { status: RunStatus }): boolean {
  return ['completed', 'rejected', 'failed', 'cancelled', 'interrupted'].includes(run.status);
}
export function planIdentity(plan: AgentPlan): PlanIdentity {
  return { plan_id: plan.id, revision: plan.revision, digest: plan.digest };
}
export class AgentApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}
export function invalidAgent(): never {
  throw new AgentApiError(
    'protocol_error',
    'Invalid Agent response. Reload Agent history to recover; no command was retried.',
  );
}
export function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function text(value: unknown): value is string {
  return typeof value === 'string';
}
export function id(value: unknown): value is string {
  return text(value) && value.trim().length > 0;
}
export function time(value: unknown): value is string {
  if (!text(value)) return false;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(
      value,
    );
  if (!match) return false;
  const [, year, month, day, hour, minute, second, zoneHour = '0', zoneMinute = '0'] = match;
  return (
    +month >= 1 &&
    +month <= 12 &&
    +day >= 1 &&
    +day <= new Date(Date.UTC(+year, +month, 0)).getUTCDate() &&
    +hour <= 23 &&
    +minute <= 59 &&
    +second <= 59 &&
    +zoneHour <= 23 &&
    +zoneMinute <= 59 &&
    Number.isFinite(Date.parse(value))
  );
}
export function integer(value: unknown, min = 0): value is number {
  return Number.isSafeInteger(value) && (value as number) >= min;
}
export function onlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every((key) => keys.includes(key));
}
export function artifactId(value: unknown): value is string {
  return text(value) && /^[a-f0-9]{32}$/.test(value);
}
/** Link safety only. Public-network/DNS authorization remains the BFF's responsibility. */
export function safeHttpUrl(value: unknown): value is string {
  if (!text(value) || /[\s\u0000-\u001f\u007f\\]/u.test(value)) return false;
  const authority = /^https?:\/\/([^/?#]+)/i.exec(value)?.[1];
  if (!authority || /[@%]/.test(authority) || authority.endsWith(':')) return false;
  try {
    const url = new URL(value);
    return (
      ['http:', 'https:'].includes(url.protocol) &&
      !!url.hostname &&
      !url.username &&
      !url.password &&
      url.port !== '0'
    );
  } catch {
    return false;
  }
}
function validPage(value: Record<string, unknown>, content: string): boolean {
  if (
    !artifactId(value['artifact_id']) ||
    !integer(value['offset']) ||
    !integer(value['total_chars']) ||
    !integer(value['returned_chars']) ||
    value['returned_chars'] > 16000 ||
    value['returned_chars'] !== Array.from(content).length
  )
    return false;
  const end = value['offset'] + value['returned_chars'];
  return (
    Number.isSafeInteger(end) &&
    end <= value['total_chars'] &&
    (end < value['total_chars']
      ? value['returned_chars'] > 0 && value['next_offset'] === end
      : value['next_offset'] === null)
  );
}
export function parseArtifactPage(value: unknown): ArtifactPage {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'artifact_id',
      'content',
      'offset',
      'returned_chars',
      'total_chars',
      'next_offset',
    ]) ||
    !text(value['content']) ||
    !validPage(value, value['content'])
  )
    return invalidAgent();
  return value as unknown as ArtifactPage;
}
function validFetch(value: unknown, content: string): value is FetchMetadata {
  return (
    object(value) &&
    onlyKeys(value, [
      'artifact_id',
      'requested_url',
      'final_url',
      'title',
      'status_code',
      'content_type',
      'fetched_at',
      'redirects',
      'offset',
      'returned_chars',
      'total_chars',
      'next_offset',
    ]) &&
    validPage(value, content) &&
    safeHttpUrl(value['requested_url']) &&
    safeHttpUrl(value['final_url']) &&
    (value['title'] === null || text(value['title'])) &&
    integer(value['status_code'], 100) &&
    value['status_code'] <= 599 &&
    id(value['content_type']) &&
    time(value['fetched_at']) &&
    /(?:Z|\+00:00)$/.test(value['fetched_at']) &&
    Array.isArray(value['redirects']) &&
    value['redirects'].every(safeHttpUrl) &&
    (value['redirects'].length
      ? value['redirects'].at(-1) === value['final_url']
      : value['requested_url'] === value['final_url'])
  );
}
export function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(text);
}
export function errorShape(value: unknown): boolean {
  return value === null || (object(value) && id(value['code']) && id(value['message']));
}
export function effort(value: unknown): boolean {
  return REASONING_EFFORTS.includes(value as ReasoningEffort);
}
export function unique(values: unknown[], key: string): boolean {
  return (
    new Set(values.map((value) => (object(value) ? value[key] : undefined))).size === values.length
  );
}
export function validPlan(value: unknown): value is AgentPlan {
  if (!object(value)) return false;
  const limits = value['limits'];
  const steps = value['steps'];
  return (
    id(value['id']) &&
    integer(value['revision'], 1) &&
    id(value['digest']) &&
    text(value['objective']) &&
    strings(value['assumptions']) &&
    id(value['model']) &&
    effort(value['reasoning_effort']) &&
    (limits === undefined ||
      (object(limits) &&
        integer(limits['max_steps'], 1) &&
        integer(limits['max_tool_calls']) &&
        integer(limits['timeout_seconds'], 1))) &&
    Array.isArray(steps) &&
    steps.length > 0 &&
    unique(steps, 'id') &&
    steps.every((step: unknown) => {
      if (!object(step) || !id(step['id']) || !text(step['title']) || !text(step['description']))
        return false;
      const args = step['arguments'];
      if (!object(args)) return false;
      if (step['tool'] === 'read_file')
        return (
          onlyKeys(args, ['workspace_id', 'path', 'offset', 'limit']) &&
          id(args['workspace_id']) &&
          id(args['path']) &&
          integer(args['offset'], 1) &&
          integer(args['limit'], 1)
        );
      return (
        step['tool'] === 'web_fetch' &&
        onlyKeys(args, ['url', 'offset', 'limit']) &&
        safeHttpUrl(args['url']) &&
        integer(args['offset']) &&
        integer(args['limit'], 1) &&
        args['limit'] <= 16000
      );
    })
  );
}
function validRun(value: unknown): value is AgentRun {
  if (!object(value)) return false;
  const plan = value['plan'];
  const results = value['results'];
  const clarification = value['clarification'];
  if (!(
    id(value['id']) &&
    text(value['request']) &&
    time(value['created_at']) &&
    time(value['updated_at']) &&
    RUN_STATUSES.includes(value['status'] as RunStatus) &&
    (value['operation_id'] === null || id(value['operation_id'])) &&
    (plan === null || validPlan(plan)) &&
    Array.isArray(results) &&
    unique(results, 'step_id') &&
    results.every(
      (result: unknown) =>
        object(result) &&
        id(result['step_id']) &&
        ['running', 'completed', 'failed'].includes(result['status'] as string) &&
        text(result['content']) &&
        time(result['time']) &&
        (result['error'] === undefined || errorShape(result['error'])) &&
        (result['path'] === undefined || result['path'] === null || id(result['path'])) &&
        (result['offset'] === undefined ||
          result['offset'] === null ||
          integer(result['offset'], 1)) &&
        (result['returned_lines'] === undefined ||
          result['returned_lines'] === null ||
          integer(result['returned_lines'])) &&
        (result['truncated'] === undefined || typeof result['truncated'] === 'boolean') &&
        (result['fetch'] === undefined ||
          result['fetch'] === null ||
          validFetch(result['fetch'], result['content'])) &&
        plan !== null &&
        validPlan(plan) &&
        plan.steps.some((step) => step.id === result['step_id']),
    ) &&
    (value['answer'] === null || text(value['answer'])) &&
    errorShape(value['error']) &&
    (clarification === undefined ||
      clarification === null ||
      (object(clarification) &&
        id(clarification['id']) &&
        strings(clarification['questions']) &&
        clarification['questions'].length > 0))
  ))
    return false;
  const status = value['status'] as RunStatus;
  const checkedResults = results as StepResult[];
  if (plan !== null && validPlan(plan)) {
    for (const result of checkedResults) {
      const step = plan.steps.find((entry) => entry.id === result.step_id)!;
      if (step.tool === 'read_file') {
        if (result.fetch != null) return false;
      } else {
        if (
          result.path != null ||
          result.offset != null ||
          result.returned_lines != null ||
          result.truncated === true ||
          (result.status === 'completed' && !result.fetch)
        )
          return false;
        if (
          result.fetch &&
          (result.status !== 'completed' ||
            result.fetch.requested_url !== step.arguments.url ||
            result.fetch.offset !== step.arguments.offset ||
            result.fetch.returned_chars !==
              Math.min(step.arguments.limit, result.fetch.total_chars - result.fetch.offset))
        )
          return false;
      }
    }
  }
  if (['awaiting_approval', 'approved', 'executing', 'completed'].includes(status) && plan === null)
    return false;
  if (value['status'] === 'awaiting_clarification' && !clarification) return false;
  if (
    ['planning', 'awaiting_clarification', 'awaiting_approval', 'approved', 'rejected'].includes(
      status,
    ) &&
    (checkedResults.length > 0 || value['answer'] !== null)
  )
    return false;
  if (
    [
      'completed',
      'rejected',
      'failed',
      'cancelled',
      'interrupted',
      'awaiting_approval',
      'awaiting_clarification',
    ].includes(status) &&
    value['operation_id'] !== null
  )
    return false;
  if (
    ['completed', 'rejected', 'failed', 'cancelled', 'interrupted'].includes(status) &&
    checkedResults.some((result) => result.status === 'running')
  )
    return false;
  if (checkedResults.filter((result) => result.status === 'running').length > 1) return false;
  if (checkedResults.some((result) => result.status !== 'failed' && result.error != null))
    return false;
  if (status === 'rejected' && plan === null && !clarification) return false;
  if (
    status === 'completed' &&
    (value['answer'] === null ||
      value['error'] !== null ||
      !validPlan(plan) ||
      checkedResults.length !== plan.steps.length ||
      checkedResults.some((result) => result.status !== 'completed'))
  )
    return false;
  if (
    ['planning', 'approved', 'executing', 'cancelling'].includes(value['status'] as string) &&
    !id(value['operation_id'])
  )
    return false;
  return true;
}
/** Frozen v1 is a complete snapshot, never a v2 loaded window. */
export function parseLegacyAgentSession(value: unknown): AgentSession {
  if (
    !object(value) ||
    !id(value['id']) ||
    !text(value['title']) ||
    !id(value['model']) ||
    !effort(value['reasoning_effort']) ||
    !time(value['created_at']) ||
    !time(value['updated_at']) ||
    typeof value['archived'] !== 'boolean' ||
    (value['archive_pending'] !== undefined && typeof value['archive_pending'] !== 'boolean') ||
    !integer(value['sequence']) ||
    !Array.isArray(value['runs']) ||
    !unique(value['runs'], 'id') ||
    !value['runs'].every(validRun) ||
    value['runs'].filter((run) => !settled(run)).length > 1 ||
    (value['archived'] &&
      (value['archive_pending'] === true || value['runs'].some((run) => !settled(run))))
  )
    return invalidAgent();
  return value as unknown as AgentSession;
}

export function validResult(value: unknown, plan?: AgentPlan | null): value is StepResult {
  if (
    !object(value) ||
    !id(value['step_id']) ||
    !['running', 'completed', 'failed'].includes(value['status'] as string) ||
    !text(value['content']) ||
    !time(value['time']) ||
    !(value['error'] === undefined || errorShape(value['error'])) ||
    (value['status'] !== 'failed' && value['error'] != null) ||
    !(value['path'] === undefined || value['path'] === null || id(value['path'])) ||
    !(value['offset'] === undefined || value['offset'] === null || integer(value['offset'], 1)) ||
    !(
      value['returned_lines'] === undefined ||
      value['returned_lines'] === null ||
      integer(value['returned_lines'])
    ) ||
    !(value['truncated'] === undefined || typeof value['truncated'] === 'boolean') ||
    !(
      value['fetch'] === undefined ||
      value['fetch'] === null ||
      validFetch(value['fetch'], value['content'])
    )
  )
    return false;
  if (plan === undefined) return true;
  const result = value as unknown as StepResult;
  const step = plan?.steps.find((entry) => entry.id === result.step_id);
  if (!step) return false;
  if (step.tool === 'read_file') return result.fetch == null;
  return (
    result.path == null &&
    result.offset == null &&
    result.returned_lines == null &&
    result.truncated !== true &&
    (result.status !== 'completed' || !!result.fetch) &&
    (!result.fetch ||
      (result.status === 'completed' &&
        result.fetch.requested_url === step.arguments.url &&
        result.fetch.offset === step.arguments.offset &&
        result.fetch.returned_chars ===
          Math.min(step.arguments.limit, result.fetch.total_chars - result.fetch.offset)))
  );
}
