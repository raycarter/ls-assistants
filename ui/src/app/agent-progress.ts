import {
  identityMatches,
  invalidAgent,
  validateResultWindow,
  type AgentEvent,
  type AgentPlan,
  type RunControl,
} from './agent.models';

export interface ResultFrontier {
  step_id: string;
  status: 'running' | 'completed' | 'failed';
}

/** Constant working state: counters plus the one current step, never retained result history. */
export function advanceResults(
  previous: RunControl | null,
  event: AgentEvent,
  plan: AgentPlan,
  frontier: ResultFrontier | null,
): ResultFrontier | null {
  const run = event.run!;
  if (!identityMatches(plan, run.plan_identity)) return invalidAgent();
  validateResultWindow(
    event.result ? [event.result] : [],
    plan,
    run.result_count,
    run.completed_steps,
  );
  if (
    run.status === 'completed' &&
    (run.completed_steps !== plan.steps.length ||
      run.result_count !== plan.steps.length ||
      frontier?.status !== 'completed')
  )
    return invalidAgent();
  const completed = previous?.completed_steps ?? 0;
  const count = previous?.result_count ?? 0;
  const result = event.result;
  if (!result) {
    if (run.result_count !== count || run.completed_steps !== completed) return invalidAgent();
    if (
      ['completed', 'cancelled', 'failed', 'interrupted'].includes(run.status) &&
      frontier?.status === 'running'
    )
      return invalidAgent();
    return frontier;
  }
  const index = plan.steps.findIndex((step) => step.id === result.step_id);
  if (index !== completed || frontier?.status === 'failed') return invalidAgent();
  if (event.type === 'tool.intent') {
    if (
      result.status !== 'running' ||
      count !== completed ||
      run.completed_steps !== completed ||
      run.result_count !== count + 1 ||
      frontier?.status === 'running'
    )
      return invalidAgent();
  } else {
    if (
      !['tool.result', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.type) ||
      result.status === 'running' ||
      frontier?.status !== 'running' ||
      frontier.step_id !== result.step_id ||
      count !== completed + 1 ||
      run.result_count !== count ||
      run.completed_steps !== completed + (result.status === 'completed' ? 1 : 0) ||
      (event.type !== 'tool.result' && result.status !== 'failed')
    )
      return invalidAgent();
  }
  return { step_id: result.step_id, status: result.status };
}
