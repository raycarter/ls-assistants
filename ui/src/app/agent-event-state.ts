import {
  controlOf,
  identityMatches,
  parseAgentRun,
  validateLlmProgress,
  validateResultWindow,
  type AgentEvent,
  type AgentRun,
  type RunView,
} from './agent.models';

/** Stage a loaded view without publishing anything. Headers remain authoritative for stale views. */
export function reduceRunView(view: RunView | null, event: AgentEvent): RunView | null {
  const control = event.run;
  if (!control) return view;
  const body = view?.body.id === control.id ? view.body : null;
  if (body) validateLlmProgress(controlOf(body), event);
  const plan = event.plan ?? body?.plan ?? null;
  if (
    plan === null ? control.plan_identity !== null : !identityMatches(plan, control.plan_identity)
  )
    return view;
  const request = event.request ?? body?.request;
  if (request === undefined) return view;
  if (control.status === 'awaiting_clarification') return null;
  const older = !!body && view!.window.kind === 'older';
  let results = body?.results ?? [];
  if (event.result) {
    validateResultWindow([event.result], plan, control.result_count, control.completed_steps);
    if (older) {
      if (results.some((result) => result.step_id === event.result!.step_id))
        results = results.map((result) =>
          result.step_id === event.result!.step_id ? event.result! : result,
        );
    } else {
      results = [
        ...results.filter((result) => result.step_id !== event.result!.step_id),
        event.result,
      ]
        .sort(
          (a, b) =>
            plan!.steps.findIndex((step) => step.id === a.step_id) -
            plan!.steps.findIndex((step) => step.id === b.step_id),
        )
        .slice(-20);
    }
  }
  const run: AgentRun = {
    projection_version: 2,
    id: control.id,
    request,
    created_at: control.created_at,
    updated_at: control.updated_at,
    status: control.status,
    operation_id: control.operation_id,
    plan,
    results,
    answer: event.answer ?? body?.answer ?? null,
    error: control.error,
    clarification: body?.clarification ?? null,
    version_sequence: control.version_sequence,
    result_count: control.result_count,
    completed_steps: control.completed_steps,
    results_before: older
      ? body!.results_before
      : control.result_count > results.length
        ? control.result_count - results.length
        : null,
  };
  parseAgentRun(run);
  return {
    body: run,
    window: older ? view!.window : { kind: 'recent', before: null, at_sequence: event.sequence },
  };
}
