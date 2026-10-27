import {
  id,
  integer,
  object,
  onlyKeys,
  parseArtifactPage,
  text,
  validPlan as validLegacyPlan,
  validResult as validLegacyResult,
  type AgentPlan as LegacyPlan,
  type AgentStep as LegacyStep,
  type StepResult as LegacyResult,
} from './agent-legacy.models';

export interface BackendGrepArguments {
  workspace_id: string;
  path: string;
  pattern: string;
  file_glob: string | null;
  offset: number;
  limit: number;
}
export type GrepArguments = BackendGrepArguments;
export interface BackendGrepMetadata {
  artifact_id: string;
  workspace_id: string;
  path: string;
  pattern: string;
  file_glob: string | null;
  offset: number;
  returned_chars: number;
  total_chars: number;
  next_offset: number | null;
  total_matches: number;
  files_searched: number;
  skipped_binary: number;
  skipped_unreadable: number;
  complete: boolean;
}
export type GrepMetadata = BackendGrepMetadata;
export type AgentStep =
  | LegacyStep
  | {
      id: string;
      title: string;
      description: string;
      tool: 'grep_in_folder';
      arguments: GrepArguments;
    };
export interface AgentPlan extends Omit<LegacyPlan, 'steps'> {
  steps: AgentStep[];
}
export interface StepResult extends LegacyResult {
  grep?: GrepMetadata | null;
}

function validGrepArguments(value: unknown): value is GrepArguments {
  return (
    object(value) &&
    onlyKeys(value, ['workspace_id', 'path', 'pattern', 'file_glob', 'offset', 'limit']) &&
    id(value['workspace_id']) &&
    text(value['path']) &&
    value['path'].length > 0 &&
    text(value['pattern']) &&
    value['pattern'].length > 0 &&
    (value['file_glob'] === null || typeof value['file_glob'] === 'string') &&
    integer(value['offset']) &&
    integer(value['limit'], 1) &&
    value['limit'] <= 16000
  );
}

/** Extend the shared plan envelope without changing the frozen v1 decoder. */
export function validPlan(value: unknown): value is AgentPlan {
  if (!object(value) || !Array.isArray(value['steps'])) return false;
  const steps = [];
  for (const step of value['steps']) {
    if (!object(step)) return false;
    if (step['tool'] !== 'grep_in_folder') {
      steps.push(step);
      continue;
    }
    if (!validGrepArguments(step['arguments'])) return false;
    steps.push({
      ...step,
      tool: 'read_file',
      arguments: {
        workspace_id: step['arguments'].workspace_id,
        // Validate the shared envelope only; grep's literal fields were checked above.
        path: '.',
        offset: 1,
        limit: 1,
      },
    });
  }
  return validLegacyPlan({ ...value, steps });
}

function validGrep(value: unknown, content: string): value is GrepMetadata {
  if (
    !object(value) ||
    !onlyKeys(value, [
      'artifact_id',
      'workspace_id',
      'path',
      'pattern',
      'file_glob',
      'offset',
      'returned_chars',
      'total_chars',
      'next_offset',
      'total_matches',
      'files_searched',
      'skipped_binary',
      'skipped_unreadable',
      'complete',
    ]) ||
    !id(value['workspace_id']) ||
    !text(value['path']) ||
    value['path'].length === 0 ||
    !text(value['pattern']) ||
    value['pattern'].length === 0 ||
    !(value['file_glob'] === null || typeof value['file_glob'] === 'string') ||
    !integer(value['total_matches']) ||
    !integer(value['files_searched']) ||
    !integer(value['skipped_binary']) ||
    !integer(value['skipped_unreadable']) ||
    typeof value['complete'] !== 'boolean' ||
    value['complete'] !== (value['skipped_binary'] === 0 && value['skipped_unreadable'] === 0)
  )
    return false;
  try {
    parseArtifactPage({
      artifact_id: value['artifact_id'],
      content,
      offset: value['offset'],
      returned_chars: value['returned_chars'],
      total_chars: value['total_chars'],
      next_offset: value['next_offset'],
    });
    return true;
  } catch {
    return false;
  }
}

export function validResult(value: unknown, plan?: AgentPlan | null): value is StepResult {
  if (!object(value)) return false;
  const grep = value['grep'];
  if (grep != null) {
    if (
      typeof value['content'] !== 'string' ||
      !validGrep(grep, value['content']) ||
      value['read'] != null ||
      value['fetch'] != null ||
      value['path'] !== null ||
      value['offset'] !== null ||
      value['returned_lines'] !== null ||
      value['truncated'] !== (grep.next_offset !== null || !grep.complete) ||
      value['status'] !== 'completed'
    )
      return false;
  }
  if (!validLegacyResult(value)) return false;
  if (plan === undefined) return true;
  const step = plan?.steps.find((entry) => entry.id === value['step_id']);
  if (!step) return false;
  if (step.tool !== 'grep_in_folder') {
    return grep == null && validLegacyResult(value, { ...plan!, steps: [step] });
  }
  if (
    value['read'] != null ||
    value['fetch'] != null ||
    value['path'] !== null ||
    value['offset'] !== null ||
    value['returned_lines'] !== null ||
    (value['status'] === 'completed' && !grep)
  )
    return false;
  return (
    !grep ||
    ((['workspace_id', 'path', 'pattern', 'file_glob', 'offset'] as const).every(
      (key) => grep[key] === step.arguments[key],
    ) &&
      grep.returned_chars === Math.min(step.arguments.limit, grep.total_chars - grep.offset))
  );
}
