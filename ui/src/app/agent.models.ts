import type { ReasoningEffort } from './chat.models';
import {
  RUN_STATUSES,
  effort,
  errorShape,
  id,
  integer,
  invalidAgent,
  object,
  onlyKeys,
  settled,
  strings,
  text,
  time,
  unique,
  type RunStatus,
} from './agent-legacy.models';
import { validPlan, validResult, type AgentPlan, type StepResult } from './agent-tool.models';
export { validPlan, validResult } from './agent-tool.models';
export type {
  AgentPlan,
  AgentStep,
  StepResult,
  GrepArguments,
  GrepMetadata,
  BackendGrepArguments,
  BackendGrepMetadata,
} from './agent-tool.models';

export {
  AgentApiError,
  RUN_STATUSES,
  artifactId,
  id,
  integer,
  invalidAgent,
  object,
  parseArtifactPage,
  safeHttpUrl,
  settled,
  text,
  time,
} from './agent-legacy.models';
export function planIdentity(plan: Pick<AgentPlan, 'id' | 'revision' | 'digest'>) {
  return { plan_id: plan.id, revision: plan.revision, digest: plan.digest };
}
export type {
  AgentCommand,
  ArtifactPage,
  FetchMetadata,
  FetchArguments,
  ReadArguments,
  ReadMetadata,
  PlanIdentity,
  ReviewIdentity,
  Revision,
  RunStatus,
} from './agent-legacy.models';

export interface AgentError {
  code: string;
  message: string;
}
export type ControlPlanIdentity = Pick<
  AgentPlan,
  'id' | 'revision' | 'digest' | 'model' | 'reasoning_effort'
>;
export interface RunControl {
  id: string;
  status: RunStatus;
  operation_id: string | null;
  created_at: string;
  updated_at: string;
  version_sequence: number;
  plan_identity: ControlPlanIdentity | null;
  result_count: number;
  completed_steps: number;
  error: AgentError | null;
}
export interface RunSummary extends RunControl {
  request_preview: string;
}
/** The production public session shape is a header, with no embedded history. */
export interface AgentSession {
  projection_version: 2;
  id: string;
  title: string;
  model: string;
  reasoning_effort: ReasoningEffort;
  created_at: string;
  updated_at: string;
  archived: boolean;
  archive_pending: boolean;
  sequence: number;
  run_count: number;
  latest_run_id: string | null;
  current_run: RunControl | null;
}
export interface AgentRun {
  projection_version: 2;
  id: string;
  request: string;
  created_at: string;
  updated_at: string;
  status: RunStatus;
  operation_id: string | null;
  plan: AgentPlan | null;
  results: StepResult[];
  answer: string | null;
  error: AgentError | null;
  clarification: { id: string; questions: string[] } | null;
  version_sequence: number;
  result_count: number;
  completed_steps: number;
  results_before: number | null;
}
export interface SessionsPage {
  projection_version: 2;
  sessions: AgentSession[];
  next_cursor: string | null;
  total: number;
}
export interface RunsPage {
  projection_version: 2;
  session_id: string;
  at_sequence: number;
  total: number;
  runs: RunSummary[];
  next_before: number | null;
}
export interface ResultsPage {
  projection_version: 2;
  session_id: string;
  run_id: string;
  at_sequence: number;
  total: number;
  completed_steps: number;
  results: StepResult[];
  next_before: number | null;
}
export interface AgentEvent {
  schema_version: 2;
  invalidates_runs: boolean;
  sequence: number;
  time: string;
  session_id: string;
  request_id: string;
  run_id: string | null;
  operation_id: string | null;
  type: string;
  session: AgentSession;
  run: RunControl | null;
  request: string | null;
  plan: AgentPlan | null;
  result: StepResult | null;
  answer: string | null;
  error: AgentError | null;
}
export interface EventsPage {
  projection_version: 2;
  events: AgentEvent[];
  next_after: number;
  through_sequence: number;
  has_more: boolean;
}
/** Internal rendering state: a body and its explicitly loaded result window. */
export interface RunView {
  body: AgentRun;
  window: { kind: 'recent' | 'older'; before: number | null; at_sequence: number };
}
const CONTROL_KEYS = [
  'id',
  'status',
  'operation_id',
  'created_at',
  'updated_at',
  'version_sequence',
  'plan_identity',
  'result_count',
  'completed_steps',
  'error',
];
function nullableId(value: unknown): boolean {
  return value === null || id(value);
}
function cursor(value: unknown): boolean {
  return value === null || integer(value);
}
export function identityMatches(
  plan: ControlPlanIdentity | null,
  identity: ControlPlanIdentity | null,
): boolean {
  return (
    !!plan &&
    !!identity &&
    ['id', 'revision', 'digest', 'model', 'reasoning_effort'].every(
      (key) =>
        plan[key as keyof ControlPlanIdentity] === identity[key as keyof ControlPlanIdentity],
    )
  );
}
export function sameControl(a: RunControl, b: RunControl): boolean {
  return CONTROL_KEYS.every((key) => {
    if (key === 'plan_identity')
      return a.plan_identity === null
        ? b.plan_identity === null
        : identityMatches(a.plan_identity, b.plan_identity);
    if (key === 'error')
      return a.error?.code === b.error?.code && a.error?.message === b.error?.message;
    return a[key as keyof RunControl] === b[key as keyof RunControl];
  });
}
export function isLlmEvent(event: AgentEvent): boolean {
  return event.type.startsWith('llm.');
}
/** Private observations may change recording clocks, never the loaded coordinator controls. */
export function validateLlmProgress(previous: RunControl, event: AgentEvent): void {
  if (
    (isLlmEvent(event) || event.type === 'tool.chunk') &&
    (!event.run ||
      !sameControl(
        {
          ...previous,
          updated_at: event.run.updated_at,
          version_sequence: event.run.version_sequence,
        },
        event.run,
      ))
  )
    return invalidAgent();
}
export function controlOf(run: AgentRun): RunControl {
  const {
    id,
    status,
    operation_id,
    created_at,
    updated_at,
    version_sequence,
    result_count,
    completed_steps,
    error,
    plan,
  } = run;
  return {
    id,
    status,
    operation_id,
    created_at,
    updated_at,
    version_sequence,
    result_count,
    completed_steps,
    error,
    plan_identity: plan
      ? {
          id: plan.id,
          revision: plan.revision,
          digest: plan.digest,
          model: plan.model,
          reasoning_effort: plan.reasoning_effort,
        }
      : null,
  };
}
function validIdentity(value: unknown): boolean {
  return (
    object(value) &&
    onlyKeys(value, ['id', 'revision', 'digest', 'model', 'reasoning_effort']) &&
    id(value['id']) &&
    integer(value['revision'], 1) &&
    text(value['digest']) &&
    /^[a-f0-9]{64}$/.test(value['digest']) &&
    id(value['model']) &&
    effort(value['reasoning_effort'])
  );
}
export function parseRunControl(value: unknown, summary = false): RunControl {
  if (
    !object(value) ||
    !onlyKeys(value, summary ? [...CONTROL_KEYS, 'request_preview'] : CONTROL_KEYS) ||
    !id(value['id']) ||
    !RUN_STATUSES.includes(value['status'] as RunStatus) ||
    !nullableId(value['operation_id']) ||
    !time(value['created_at']) ||
    !time(value['updated_at']) ||
    !integer(value['version_sequence']) ||
    !integer(value['result_count']) ||
    !integer(value['completed_steps']) ||
    value['completed_steps'] > value['result_count'] ||
    !errorShape(value['error']) ||
    !(value['plan_identity'] === null || validIdentity(value['plan_identity'])) ||
    (summary && !text(value['request_preview']))
  )
    return invalidAgent();
  const run = value as unknown as RunControl;
  if (
    ['planning', 'approved', 'executing', 'cancelling'].includes(run.status) !==
      (run.operation_id !== null) ||
    (['awaiting_approval', 'approved', 'executing', 'completed'].includes(run.status) &&
      !run.plan_identity) ||
    (['planning', 'awaiting_clarification', 'awaiting_approval', 'approved', 'rejected'].includes(
      run.status,
    ) &&
      run.result_count !== 0) ||
    (run.status === 'completed' && (run.completed_steps !== run.result_count || run.error !== null))
  )
    return invalidAgent();
  return run;
}
export function parseAgentSession(value: unknown): AgentSession {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'projection_version',
      'id',
      'title',
      'model',
      'reasoning_effort',
      'created_at',
      'updated_at',
      'archived',
      'archive_pending',
      'sequence',
      'run_count',
      'latest_run_id',
      'current_run',
    ]) ||
    value['projection_version'] !== 2 ||
    !id(value['id']) ||
    !text(value['title']) ||
    !id(value['model']) ||
    !effort(value['reasoning_effort']) ||
    !time(value['created_at']) ||
    !time(value['updated_at']) ||
    typeof value['archived'] !== 'boolean' ||
    typeof value['archive_pending'] !== 'boolean' ||
    !integer(value['sequence']) ||
    !integer(value['run_count']) ||
    !nullableId(value['latest_run_id'])
  )
    return invalidAgent();
  const session = value as unknown as AgentSession;
  const run = session.current_run === null ? null : parseRunControl(session.current_run);
  if (
    (session.run_count === 0) !== (session.latest_run_id === null) ||
    (session.archived && (session.archive_pending || run !== null)) ||
    (run &&
      (settled(run) ||
        session.run_count === 0 ||
        run.version_sequence > session.sequence ||
        (run.plan_identity &&
          (run.plan_identity.model !== session.model ||
            run.plan_identity.reasoning_effort !== session.reasoning_effort))))
  )
    return invalidAgent();
  return session;
}
export function parseAgentRun(value: unknown): AgentRun {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'projection_version',
      'id',
      'request',
      'created_at',
      'updated_at',
      'status',
      'operation_id',
      'plan',
      'results',
      'answer',
      'error',
      'clarification',
      'version_sequence',
      'result_count',
      'completed_steps',
      'results_before',
    ]) ||
    value['projection_version'] !== 2 ||
    !text(value['request']) ||
    !(value['plan'] === null || validPlan(value['plan'])) ||
    !Array.isArray(value['results']) ||
    value['results'].length > 20 ||
    !unique(value['results'], 'step_id') ||
    !value['results'].every((result) => validResult(result, value['plan'] as AgentPlan | null)) ||
    !(value['answer'] === null || text(value['answer'])) ||
    !cursor(value['results_before']) ||
    !(
      value['clarification'] === null ||
      (object(value['clarification']) &&
        id(value['clarification']['id']) &&
        strings(value['clarification']['questions']) &&
        value['clarification']['questions'].length > 0)
    )
  )
    return invalidAgent();
  const run = value as unknown as AgentRun;
  parseRunControl(controlOf(run));
  if (
    run.results.length > run.result_count ||
    run.completed_steps > (run.plan?.steps.length ?? 0) ||
    (run.plan && run.result_count > run.plan.steps.length) ||
    (run.results_before !== null &&
      (run.results_before < 1 || run.results_before >= run.result_count)) ||
    run.results.filter((result) => result.status === 'completed').length > run.completed_steps ||
    (settled(run) && run.results.some((result) => result.status === 'running')) ||
    run.results.filter((result) => result.status === 'running').length > 1 ||
    (run.status === 'awaiting_clarification' && !run.clarification) ||
    (run.status === 'rejected' && !run.plan && !run.clarification) ||
    (['planning', 'awaiting_clarification', 'awaiting_approval', 'approved', 'rejected'].includes(
      run.status,
    ) &&
      run.answer !== null) ||
    (run.status === 'completed' &&
      (!run.plan ||
        run.answer === null ||
        run.completed_steps !== run.plan.steps.length ||
        run.results.some((result) => result.status !== 'completed')))
  )
    return invalidAgent();
  validateResultWindow(run.results, run.plan, run.result_count, run.completed_steps);
  return run;
}
export function validateResultOrder(results: StepResult[], plan: AgentPlan | null): void {
  let previous = -1;
  for (const result of results) {
    if (!validResult(result, plan)) return invalidAgent();
    const index = plan!.steps.findIndex((step) => step.id === result.step_id);
    if (index <= previous) return invalidAgent();
    previous = index;
  }
}
/** Sequential execution has a completed prefix and at most one running/failed frontier. */
export function validateResultWindow(
  results: StepResult[],
  plan: AgentPlan | null,
  total: number,
  completed: number,
): void {
  validateResultOrder(results, plan);
  if (completed > total || total > completed + 1 || total > (plan?.steps.length ?? 0))
    return invalidAgent();
  for (const result of results) {
    const index = plan!.steps.findIndex((step) => step.id === result.step_id);
    if (
      index >= total ||
      (index < completed ? result.status !== 'completed' : result.status === 'completed')
    )
      return invalidAgent();
  }
}
export function parseSessionsPage(value: unknown): SessionsPage {
  if (
    !object(value) ||
    !onlyKeys(value, ['projection_version', 'sessions', 'next_cursor', 'total']) ||
    value['projection_version'] !== 2 ||
    !Array.isArray(value['sessions']) ||
    value['sessions'].length > 25 ||
    !unique(value['sessions'], 'id') ||
    !integer(value['total']) ||
    value['sessions'].length > value['total'] ||
    !(value['next_cursor'] === null || id(value['next_cursor']))
  )
    return invalidAgent();
  value['sessions'].forEach(parseAgentSession);
  return value as unknown as SessionsPage;
}
export function parseRunsPage(value: unknown): RunsPage {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'projection_version',
      'session_id',
      'at_sequence',
      'total',
      'runs',
      'next_before',
    ]) ||
    value['projection_version'] !== 2 ||
    !id(value['session_id']) ||
    !integer(value['at_sequence']) ||
    !integer(value['total']) ||
    !Array.isArray(value['runs']) ||
    value['runs'].length > 10 ||
    !unique(value['runs'], 'id') ||
    value['runs'].length > value['total'] ||
    !cursor(value['next_before'])
  )
    return invalidAgent();
  for (const run of value['runs']) {
    const control = parseRunControl(run, true);
    if (control.version_sequence > value['at_sequence']) return invalidAgent();
  }
  return value as unknown as RunsPage;
}
export function parseResultsPage(value: unknown, plan?: AgentPlan | null): ResultsPage {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'projection_version',
      'session_id',
      'run_id',
      'at_sequence',
      'total',
      'completed_steps',
      'results',
      'next_before',
    ]) ||
    value['projection_version'] !== 2 ||
    !id(value['session_id']) ||
    !id(value['run_id']) ||
    !integer(value['at_sequence']) ||
    !integer(value['total']) ||
    !integer(value['completed_steps']) ||
    value['completed_steps'] > value['total'] ||
    value['total'] > value['completed_steps'] + 1 ||
    !Array.isArray(value['results']) ||
    value['results'].length > 20 ||
    !unique(value['results'], 'step_id') ||
    value['results'].length > value['total'] ||
    !value['results'].every((result) => validResult(result)) ||
    !cursor(value['next_before'])
  )
    return invalidAgent();
  const page = value as unknown as ResultsPage;
  if (plan !== undefined)
    validateResultWindow(page.results, plan, page.total, page.completed_steps);
  return page;
}
export function parseAgentEvent(value: unknown): AgentEvent {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'schema_version',
      'invalidates_runs',
      'sequence',
      'time',
      'session_id',
      'request_id',
      'run_id',
      'operation_id',
      'type',
      'session',
      'run',
      'request',
      'plan',
      'result',
      'answer',
      'error',
    ]) ||
    value['schema_version'] !== 2 ||
    typeof value['invalidates_runs'] !== 'boolean' ||
    !integer(value['sequence'], 1) ||
    !time(value['time']) ||
    !id(value['session_id']) ||
    !id(value['request_id']) ||
    !nullableId(value['run_id']) ||
    !nullableId(value['operation_id']) ||
    !id(value['type']) ||
    !(value['request'] === null || text(value['request'])) ||
    !(value['plan'] === null || validPlan(value['plan'])) ||
    !(value['result'] === null || validResult(value['result'])) ||
    !(value['answer'] === null || text(value['answer'])) ||
    !errorShape(value['error'])
  )
    return invalidAgent();
  const event = value as unknown as AgentEvent;
  const session = parseAgentSession(event.session);
  const run = event.run === null ? null : parseRunControl(event.run);
  if (
    event.type === 'tool.chunk' &&
    (event.invalidates_runs ||
      !run ||
      !['executing', 'cancelling'].includes(run.status) ||
      event.error?.code !== run.error?.code ||
      event.error?.message !== run.error?.message)
  )
    return invalidAgent();
  if (
    isLlmEvent(event) &&
    (!['llm.intent', 'llm.dispatched', 'llm.output', 'llm.outcome', 'llm.validation'].includes(
      event.type,
    ) ||
      event.invalidates_runs ||
      !run ||
      !(
        ['llm.intent', 'llm.dispatched'].includes(event.type)
          ? ['planning', 'approved', 'executing']
          : ['planning', 'approved', 'executing', 'cancelling']
      ).includes(run.status) ||
      event.error?.code !== run.error?.code ||
      event.error?.message !== run.error?.message)
  )
    return invalidAgent();
  if (
    session.id !== event.session_id ||
    session.sequence !== event.sequence ||
    (event.type === 'run.planning' ? event.request === null : event.request !== null) ||
    (event.type === 'run.awaiting_approval' ? !event.plan : event.plan !== null) ||
    (event.type === 'run.completed' ? event.answer === null : event.answer !== null) ||
    (event.result !== null &&
      !['tool.intent', 'tool.result', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(
        event.type,
      )) ||
    (['tool.intent', 'tool.result'].includes(event.type) && event.result === null)
  )
    return invalidAgent();
  if (isRecoveryEvent(event)) {
    if (run === null) {
      if (
        event.result !== null ||
        event.error !== null ||
        event.plan !== null ||
        event.request !== null ||
        event.answer !== null ||
        !session.archived ||
        session.archive_pending
      )
        return invalidAgent();
    } else if (
      run.status !== 'interrupted' ||
      run.operation_id !== null ||
      run.version_sequence !== event.sequence ||
      run.error?.code !== 'outcome_unknown' ||
      event.error?.code !== run.error.code ||
      event.error.message !== run.error.message ||
      (event.result !== null &&
        (event.result.status !== 'failed' || event.result.error?.code !== 'outcome_unknown')) ||
      session.current_run?.id === run.id
    )
      return invalidAgent();
  } else if (event.type.startsWith('session.')) {
    if (
      ![
        'session.created',
        'session.updated',
        'session.archive_pending',
        'session.archived',
      ].includes(event.type) ||
      event.run_id !== null ||
      event.operation_id !== null ||
      run !== null ||
      event.error !== null ||
      event.result !== null
    )
      return invalidAgent();
  } else {
    if (
      !run ||
      run.id !== event.run_id ||
      !id(event.operation_id) ||
      run.version_sequence > event.sequence ||
      ((event.type.startsWith('run.') ||
        (event.type.startsWith('tool.') && event.type !== 'tool.chunk')) &&
        run.version_sequence !== event.sequence) ||
      (run.operation_id !== null && run.operation_id !== event.operation_id) ||
      (event.type.startsWith('run.') && event.type !== `run.${run.status}`) ||
      (['planner.attempt', 'planner.repair'].includes(event.type) && run.status !== 'planning') ||
      (event.type.startsWith('tool.') &&
        event.type !== 'tool.chunk' &&
        run.status !== 'executing') ||
      (settled(run)
        ? session.current_run?.id === run.id
        : !session.current_run || !sameControl(session.current_run, run)) ||
      (event.plan &&
        (!identityMatches(event.plan, run.plan_identity) ||
          event.plan.model !== session.model ||
          event.plan.reasoning_effort !== session.reasoning_effort))
    )
      return invalidAgent();
  }
  return event;
}
/** Only this journal envelope may name an affected run outside its null command IDs. */
export function isRecoveryEvent(event: AgentEvent): boolean {
  return (
    event.invalidates_runs &&
    event.type === 'run.interrupted' &&
    event.request_id === 'recovery' &&
    event.run_id === null &&
    event.operation_id === null
  );
}
export function parseEventsPage(value: unknown): EventsPage {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'projection_version',
      'events',
      'next_after',
      'through_sequence',
      'has_more',
    ]) ||
    value['projection_version'] !== 2 ||
    !Array.isArray(value['events']) ||
    value['events'].length > 50 ||
    !integer(value['next_after']) ||
    !integer(value['through_sequence']) ||
    value['next_after'] > value['through_sequence'] ||
    typeof value['has_more'] !== 'boolean'
  )
    return invalidAgent();
  const page = value as unknown as EventsPage;
  let previous = page.events[0]?.sequence ?? page.next_after;
  for (const event of page.events) {
    parseAgentEvent(event);
    if (event.sequence !== previous++ || event.sequence > page.through_sequence)
      return invalidAgent();
  }
  if (
    (page.events.length && page.events.at(-1)!.sequence !== page.next_after) ||
    page.has_more !== page.next_after < page.through_sequence
  )
    return invalidAgent();
  return page;
}
