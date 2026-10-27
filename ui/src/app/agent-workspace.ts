import { Component, DestroyRef, effect, inject, input, signal } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentState } from './agent-state';
import { AgentApi } from './agent-api';
import {
  invalidAgent,
  safeHttpUrl,
  type AgentPlan,
  type AgentRun,
  type ArtifactPage,
  type StepResult,
} from './agent.models';
import type { ChatSession } from './chat.models';
import { MarkdownPipe } from './markdown.pipe';
import { MessageTimePipe } from './message-time.pipe';

interface EvidenceView {
  signature: string;
  page: ArtifactPage | null;
  loading: boolean;
  error: string | null;
}

@Component({
  selector: 'app-agent-workspace',
  imports: [FormsModule, JsonPipe, MarkdownPipe, MessageTimePipe],
  templateUrl: './agent-workspace.html',
  styleUrl: './agent-workspace.css',
})
export class AgentWorkspace {
  readonly state = inject(AgentState);
  private readonly api = inject(AgentApi);
  readonly legacy = input<ChatSession | null>(null);
  readonly safeHttpUrl = safeHttpUrl;
  readonly evidence = signal<ReadonlyMap<string, EvidenceView>>(new Map());
  private readonly reads = new Map<string, AbortController>();
  private destroyed = false;

  constructor() {
    effect(() => {
      const session = this.legacy() ? null : this.state.activeSession();
      const signatures = new Map(
        (session ? this.state.visibleRuns() : []).flatMap((run) =>
          run.results
            .filter((result) => result.fetch)
            .map(
              (result) =>
                [this.evidenceKey(session!.id, run.id, result), this.signature(result)] as const,
            ),
        ),
      );
      for (const [key, view] of this.evidence()) {
        if (signatures.get(key) !== view.signature) {
          this.reads.get(key)?.abort();
          this.reads.delete(key);
          this.evidence.update((views) => {
            const next = new Map(views);
            next.delete(key);
            return next;
          });
        }
      }
    });
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      for (const controller of this.reads.values()) controller.abort();
      this.reads.clear();
    });
  }

  evidenceKey(sid: string, rid: string, result: StepResult): string {
    return JSON.stringify([sid, rid, result.step_id, result.fetch?.artifact_id]);
  }
  evidenceView(sid: string, rid: string, result: StepResult): EvidenceView | undefined {
    const view = this.evidence().get(this.evidenceKey(sid, rid, result));
    return view?.signature === this.signature(result) ? view : undefined;
  }
  nextOffset(sid: string, rid: string, result: StepResult): number | null {
    const page = this.evidenceView(sid, rid, result)?.page;
    return page ? page.next_offset : (result.fetch?.next_offset ?? null);
  }
  async readEvidence(sid: string, rid: string, result: StepResult, offset: number): Promise<void> {
    const metadata = result.fetch;
    const key = this.evidenceKey(sid, rid, result);
    if (!metadata || this.reads.has(key) || !this.currentEvidence(sid, rid, result)) return;
    // Only sequential continuation or an explicit restart can read retained text.
    if (offset !== 0 && offset !== this.nextOffset(sid, rid, result)) return;
    const controller = new AbortController();
    this.reads.set(key, controller);
    const view: EvidenceView = {
      signature: this.signature(result),
      page: this.evidenceView(sid, rid, result)?.page ?? null,
      loading: true,
      error: null,
    };
    this.evidence.update((views) => new Map(views).set(key, view));
    const current = () =>
      !controller.signal.aborted &&
      this.reads.get(key) === controller &&
      this.currentEvidence(sid, rid, result);
    try {
      const page = await this.api.artifactPage(
        sid,
        metadata.artifact_id,
        offset,
        12000,
        controller.signal,
      );
      if (!current()) return;
      if (page.total_chars !== metadata.total_chars) return invalidAgent();
      this.evidence.update((views) => new Map(views).set(key, { ...view, page, loading: false }));
    } catch (error) {
      if (!current()) return;
      this.evidence.update((views) =>
        new Map(views).set(key, {
          ...view,
          loading: false,
          error: error instanceof Error ? error.message : 'Could not read retained evidence.',
        }),
      );
    } finally {
      if (this.reads.get(key) === controller) this.reads.delete(key);
    }
  }
  private signature(result: StepResult): string {
    return JSON.stringify([result.status, result.content, result.fetch]);
  }
  private currentEvidence(sid: string, rid: string, result: StepResult): boolean {
    const session = this.state.activeSession();
    const current = this.state
      .visibleRuns()
      .find((run) => run.id === rid)
      ?.results.find((entry) => entry.step_id === result.step_id);
    return (
      !this.destroyed &&
      !this.legacy() &&
      session?.id === sid &&
      !!current?.fetch &&
      this.signature(current) === this.signature(result)
    );
  }

  hasFileRead(plan: AgentPlan): boolean {
    return plan.steps.some((step) => step.tool === 'read_file');
  }

  stepStatus(run: AgentRun, stepId: string): string {
    const result = run.results.find((entry) => entry.step_id === stepId);
    if (result) return result.status;
    const index = run.plan?.steps.findIndex((step) => step.id === stepId) ?? -1;
    if (index >= 0 && index < run.completed_steps)
      return 'completed · outside loaded result window';
    return run.result_count > run.results.length ? 'outside loaded result window' : 'pending';
  }
  stopping(run: AgentRun): boolean {
    return (
      run.status === 'cancelling' ||
      (this.state.activeRun()?.id === run.id &&
        this.state.stopping().has(this.state.selectedId() ?? ''))
    );
  }
  keydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229)
      return;
    event.preventDefault();
    this.state.request();
  }
}
