import { parseLegacyAgentSession, type AgentSession as LegacySession } from './agent-legacy.models';
import { controlOf, settled, type AgentRun, type AgentSession } from './agent.models';

/** Test-only full legacy fixture conversion; production v2 parsers never accept this aggregate. */
export interface WorkspaceFixture extends AgentSession {
  runs: AgentRun[];
}
export function workspaceFixture(raw: unknown): WorkspaceFixture {
  const legacy: LegacySession = parseLegacyAgentSession(raw);
  const runs: AgentRun[] = legacy.runs.map((run) => ({
    ...run,
    projection_version: 2,
    clarification: run.clarification ?? null,
    version_sequence: legacy.sequence,
    result_count: run.results.length,
    completed_steps: run.results.filter((result) => result.status === 'completed').length,
    results_before: null,
  }));
  const { runs: ignored, ...header } = legacy;
  void ignored;
  return {
    ...header,
    projection_version: 2,
    archive_pending: legacy.archive_pending ?? false,
    run_count: runs.length,
    latest_run_id: runs.at(-1)?.id ?? null,
    current_run: runs.find((run) => !settled(run))
      ? controlOf(runs.find((run) => !settled(run))!)
      : null,
    runs,
  };
}
