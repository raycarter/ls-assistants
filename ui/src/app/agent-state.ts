import { computed, DestroyRef, effect, inject, Injectable, signal } from '@angular/core';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import { reduceRunView } from './agent-event-state';
import {
  AgentApiError,
  invalidAgent,
  object,
  parseAgentSession,
  parseAgentRun,
  parseResultsPage,
  parseRunsPage,
  parseSessionsPage,
  identityMatches,
  controlOf,
  sameControl,
  validateResultWindow,
  planIdentity,
  settled,
  type AgentCommand,
  type AgentRun,
  type AgentSession,
  type AgentEvent,
  type RunControl,
  type RunSummary,
  type RunView,
} from './agent.models';
import { CHAT_STORAGE } from './chat-state';
import type { ReasoningEffort } from './chat.models';

export const AGENT_UI_STORAGE_KEY = 'ls-assistants.agent-ui-state';
interface AgentUiState {
  version: 1;
  mode: 'chat' | 'agent';
  selectedId: string | null;
  legacyId: string | null;
  drafts: Record<string, string>;
}
const EMPTY: AgentUiState = {
  version: 1,
  mode: 'chat',
  selectedId: null,
  legacyId: null,
  drafts: {},
};
interface ArchiveCommand {
  controller: AbortController;
  inFlight: boolean;
}
interface PageTask {
  controller: AbortController;
  sessionId: string;
  sequence: number;
  intent: string;
  promise: Promise<boolean>;
}
interface RecentSummaryRequest {
  sessionId: string;
  navigation: number;
  intent: number;
}

@Injectable({ providedIn: 'root' })
export class AgentState {
  private readonly api = inject(AgentApi);
  private readonly storage = inject(CHAT_STORAGE);
  private canPersist = true;
  private warned = false;
  private readonly initial = this.restore();
  private destroyed = false;
  private loaded = false;
  private navigation = 0;
  private listController?: AbortController;
  private readonly controllers = new Set<AbortController>();
  private readonly reads = new Map<string, AbortController>();
  private readonly streams = new Map<string, AbortController>();
  private readonly errorOwners = new Map<string, AbortController>();
  private readonly commandErrorOwners = new WeakSet<AbortController>();
  private readonly archiveCommands = new Map<string, ArchiveCommand>();
  private globalErrorOwner?: AbortController;
  readonly mode = signal<'chat' | 'agent'>(this.initial.mode);
  readonly selectedId = signal<string | null>(this.initial.selectedId);
  readonly legacyId = signal<string | null>(this.initial.legacyId);
  readonly drafts = signal(this.initial.drafts);
  readonly sessions = signal<AgentSession[]>([]);
  readonly total = signal<number | null>(null);
  readonly nextCursor = signal<string | null>(null);
  readonly loadingMore = signal(false);
  readonly summaries = signal<RunSummary[]>([]);
  readonly nextRunsBefore = signal<number | null>(null);
  private summariesBefore: number | null = null;
  private summariesDirty = false;
  private summaryIntent = 0;
  private recentAutoAllowed = true;
  private recentSummaryRequest: RecentSummaryRequest | null = null;
  readonly invalidating = signal<ReadonlySet<string>>(new Set());
  private readonly invalidationSequences = new Map<string, number>();
  readonly currentView = signal<RunView | null>(null);
  readonly openedView = signal<RunView | null>(null);
  readonly pageLoading = signal<ReadonlySet<string>>(new Set());
  readonly pageErrors = signal<ReadonlyMap<string, string>>(new Map());
  private readonly pageReads = new Map<string, AbortController>();
  private readonly pageTasks = new Map<string, PageTask>();
  readonly visibleRuns = computed(() => {
    const current = this.currentView()?.body;
    const opened = this.openedView()?.body;
    return [current, opened].filter(
      (run, index, all): run is AgentRun =>
        !!run && all.findIndex((entry) => entry?.id === run.id) === index,
    );
  });
  readonly loading = signal(false);
  readonly creating = signal(false);
  readonly busy = signal<ReadonlySet<string>>(new Set());
  readonly stopping = signal<ReadonlySet<string>>(new Set());
  readonly archivePending = signal<ReadonlySet<string>>(new Set());
  readonly globalError = signal<string | null>(null);
  readonly sessionErrors = signal<ReadonlyMap<string, string>>(new Map());
  readonly sessionError = computed(() => this.sessionErrors().get(this.selectedId() ?? '') ?? null);
  readonly error = computed(() => this.sessionError() ?? this.globalError());
  readonly preferredModel = signal('');
  readonly preferredEffort = signal<ReasoningEffort>('medium');
  readonly activeSession = computed(
    () => this.sessions().find((session) => session.id === this.selectedId()) ?? null,
  );
  readonly activeControl = computed(() => this.activeSession()?.current_run ?? null);
  readonly activeRun = computed(
    () =>
      this.visibleRuns().find((run) => run.id === this.activeControl()?.id && !settled(run)) ??
      null,
  );
  readonly locked = computed(
    () =>
      !!this.activeControl() ||
      this.invalidating().has(this.selectedId() ?? '') ||
      !!this.activeSession()?.archived ||
      this.busy().has(this.selectedId() ?? '') ||
      this.archivePending().has(this.selectedId() ?? '') ||
      !!this.legacyId(),
  );
  readonly model = computed(() => this.activeSession()?.model ?? this.preferredModel());
  readonly effort = computed(
    () => this.activeSession()?.reasoning_effort ?? this.preferredEffort(),
  );
  readonly draft = computed(() => this.storedDraft(this.selectedId() ?? '$new'));
  readonly canRequest = computed(
    () => !!this.activeSession() && !this.locked() && !!this.draft().trim(),
  );

  constructor() {
    effect(() =>
      this.persist({
        version: 1,
        mode: this.mode(),
        selectedId: this.selectedId(),
        legacyId: this.legacyId(),
        drafts: this.drafts(),
      }),
    );
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      for (const controller of this.controllers) controller.abort();
    });
  }

  setMode(mode: 'chat' | 'agent'): void {
    this.mode.set(mode);
    if (mode === 'agent' && !this.loaded) void this.load();
  }
  async load(): Promise<void> {
    this.listController?.abort();
    const controller = this.controller();
    this.listController = controller;
    this.loading.set(true);
    this.ownError(controller);
    const navigation = this.navigation;
    try {
      const page = parseSessionsPage(await this.api.list(controller.signal));
      if (!this.alive(controller)) return;
      const sessions = page.sessions;
      for (const session of sessions) this.apply(session);
      this.total.set(page.total);
      this.nextCursor.set(page.next_cursor);
      const pinned = this.selectedId();
      this.sessions.update((old) =>
        old.filter(
          (entry) =>
            entry.id === pinned ||
            sessions.some((session) => session.id === entry.id) ||
            this.busy().has(entry.id),
        ),
      );
      this.loaded = true;
      if (navigation === this.navigation && !this.legacyId()) {
        const selected = this.selectedId();
        if (selected) {
          // A restored archived selection can be outside page one. Resolve it directly first.
          const found = await this.refresh(selected);
          if (navigation !== this.navigation) return;
          if (!found && !this.activeSession())
            this.selectedId.set(sessions.find((session) => !session.archived)?.id ?? null);
        } else {
          this.selectedId.set(sessions.find((session) => !session.archived)?.id ?? null);
        }
        if (this.selectedId() && this.selectedId() !== selected)
          await this.refresh(this.selectedId()!);
      }
    } catch (error) {
      this.fail(controller, error);
    } finally {
      if (this.listController === controller && !this.destroyed) this.loading.set(false);
      this.controllers.delete(controller);
    }
  }
  async loadMoreSessions(): Promise<void> {
    const cursor = this.nextCursor();
    if (!cursor || this.loadingMore() || this.loading()) return;
    const controller = this.controller();
    this.loadingMore.set(true);
    const baseline = this.listController;
    this.ownError(controller);
    try {
      const page = parseSessionsPage(await this.api.list(controller.signal, cursor));
      if (
        !this.alive(controller) ||
        baseline !== this.listController ||
        cursor !== this.nextCursor()
      )
        return;
      if (page.next_cursor === cursor) return invalidAgent();
      page.sessions.forEach((session) => this.apply(session));
      this.total.set(page.total);
      this.nextCursor.set(page.next_cursor);
    } catch (error) {
      this.fail(controller, error);
    } finally {
      this.controllers.delete(controller);
      if (!this.destroyed) this.loadingMore.set(false);
    }
  }
  private clearWorkspace(): void {
    for (const controller of this.pageReads.values()) controller.abort();
    this.pageReads.clear();
    this.pageTasks.clear();
    this.pageLoading.set(new Set());
    this.pageErrors.set(new Map());
    this.currentView.set(null);
    this.openedView.set(null);
    this.summaries.set([]);
    this.nextRunsBefore.set(null);
    this.summariesBefore = null;
    this.summariesDirty = false;
    this.summaryIntent++;
    this.recentAutoAllowed = true;
    this.recentSummaryRequest = null;
  }
  select(id: string): void {
    this.navigation++;
    if (this.selectedId() !== id || this.legacyId()) this.clearWorkspace();
    this.legacyId.set(null);
    this.selectedId.set(id);
    void this.refresh(id);
  }
  selectLegacy(id: string): void {
    this.navigation++;
    this.clearWorkspace();
    this.legacyId.set(id);
    this.selectedId.set(null);
  }
  async refresh(
    sid = this.selectedId(),
    errorOwner?: AbortController,
    reloadOpened = false,
  ): Promise<boolean> {
    if (!sid) return false;
    this.reads.get(sid)?.abort();
    const controller = this.controller();
    this.reads.set(sid, controller);
    const owner = errorOwner ?? this.ownError(controller, sid, false);
    const archive = this.archiveCommands.get(sid);
    const recoveringArchive = archive && !archive.inFlight ? archive : undefined;
    try {
      const session = await this.api.get(sid, controller.signal);
      if (this.alive(controller) && this.reads.get(sid) === controller) {
        this.apply(session, sid, recoveringArchive);
        if (this.sessions().find((entry) => entry.id === sid)?.sequence !== session.sequence)
          return false;
        let workspaceAdmitted = true;
        if (this.selectedId() === sid && !this.legacyId())
          workspaceAdmitted = await this.loadWorkspace(
            reloadOpened || this.invalidating().has(sid),
          );
        if (!this.alive(controller) || this.reads.get(sid) !== controller) return false;
        const invalidatedAt = this.invalidationSequences.get(sid);
        if (
          invalidatedAt !== undefined &&
          workspaceAdmitted &&
          session.sequence >= invalidatedAt &&
          this.sessions().find((entry) => entry.id === sid)?.sequence === session.sequence &&
          (this.selectedId() !== sid || !this.legacyId())
        ) {
          this.invalidationSequences.delete(sid);
          this.remove(this.invalidating, sid);
        }
        return workspaceAdmitted;
      }
    } catch (error) {
      const failure = errorOwner
        ? new Error(
            `${this.sessionErrors().get(sid) ?? 'Agent operation failed.'} History recovery failed: ${error instanceof Error ? error.message : 'Could not fetch the session.'} Reload Agent history to recover; no command was repeated.`,
          )
        : error;
      this.fail(controller, failure, sid, owner);
    } finally {
      if (this.reads.get(sid) === controller) this.reads.delete(sid);
      this.controllers.delete(controller);
    }
    return false;
  }
  private async loadWorkspace(reloadOpened = false): Promise<boolean> {
    const session = this.activeSession();
    if (!session) return false;
    const rid = session.current_run?.id ?? session.latest_run_id;
    const opened = reloadOpened ? this.openedView()?.body.id : null;
    this.cancelPage('results:current');
    if (reloadOpened) this.cancelPage('results:opened');
    const admitted = await Promise.all([
      this.readRuns(reloadOpened ? this.summariesBefore : null),
      rid ? this.openRun(rid, true) : Promise.resolve(true),
      opened && opened !== rid ? this.openRun(opened) : Promise.resolve(true),
    ]);
    if (!admitted.every(Boolean) || !this.sameOrigin(session)) return false;
    for (const view of [this.currentView(), reloadOpened ? this.openedView() : null]) {
      if (!view || (view.body.id === opened && opened === rid)) continue;
      const hint = this.summaries().find((run) => run.id === view.body.id);
      if (
        view.window.at_sequence !== session.sequence ||
        (hint && !sameControl(controlOf(view.body), hint))
      )
        return false;
    }
    if (opened === rid) this.clearOpenedRun();
    return true;
  }
  async loadRuns(older = false, reloadWindow = false): Promise<boolean> {
    const session = this.activeSession();
    const before = older ? this.nextRunsBefore() : reloadWindow ? this.summariesBefore : null;
    if (!session || (older && before === null)) return false;
    const pending = this.matchingPage('runs', this.runsIntent(before), session);
    if (pending) return pending;
    this.summaryIntent++;
    this.recentAutoAllowed = before === null;
    this.recentSummaryRequest = null;
    return this.readRuns(before);
  }
  private runsIntent(before: number | null): string {
    return `runs:${before ?? 'recent'}`;
  }
  private async readRuns(before: number | null): Promise<boolean> {
    const session = this.activeSession();
    if (!session) return false;
    return this.readPage('runs', this.runsIntent(before), async (signal) => {
      const page = parseRunsPage(await this.api.runs(session.id, signal, before ?? undefined));
      if (signal.aborted || !this.sameOrigin(session) || page.at_sequence !== session.sequence)
        return false;
      if (
        page.session_id !== session.id ||
        page.total !== session.run_count ||
        (before !== null && page.next_before !== null && page.next_before >= before)
      )
        return invalidAgent();
      this.summaries.set(page.runs);
      this.summariesBefore = before;
      if (before === null) this.summariesDirty = false;
      this.nextRunsBefore.set(page.next_before);
      return true;
    });
  }
  private requestRecentSummaries(): void {
    const session = this.activeSession();
    if (
      !session ||
      !this.summariesDirty ||
      !this.recentAutoAllowed ||
      this.invalidating().has(session.id)
    )
      return;
    this.recentSummaryRequest = {
      sessionId: session.id,
      navigation: this.navigation,
      intent: this.summaryIntent,
    };
    if (!this.pageReads.has('runs')) this.drainRecentSummaries();
  }
  private drainRecentSummaries(): void {
    // Consume one requested replacement, never retry merely because a read was stale.
    const request = this.recentSummaryRequest;
    this.recentSummaryRequest = null;
    if (
      !request ||
      request.sessionId !== this.selectedId() ||
      request.navigation !== this.navigation ||
      request.intent !== this.summaryIntent ||
      !this.summariesDirty ||
      !this.recentAutoAllowed ||
      this.invalidating().has(request.sessionId)
    )
      return;
    void this.readRuns(null);
  }
  async openRun(rid: string, current = false): Promise<boolean> {
    const session = this.activeSession();
    if (!session) return false;
    const pinned = session.current_run?.id ?? session.latest_run_id;
    current = current || rid === pinned;
    if (
      !current &&
      !this.summaries().some((run) => run.id === rid) &&
      this.openedView()?.body.id !== rid
    )
      return false;
    if (current && rid !== pinned) return false;
    const target = current ? this.currentView : this.openedView;
    const key = current ? 'current' : 'opened';
    return this.readPage(key, `run:${rid}`, async (signal) => {
      const run = parseAgentRun(await this.api.detail(session.id, rid, signal));
      if (signal.aborted || !this.sameOrigin(session) || run.version_sequence > session.sequence)
        return false;
      if (run.id !== rid) return invalidAgent();
      if (current && !session.current_run && !settled(run)) return invalidAgent();
      const previous = target()?.body;
      if (previous?.id === rid && previous.version_sequence > run.version_sequence) return false;
      const control = session.current_run?.id === rid ? session.current_run : null;
      const hint = this.summaries().find((entry) => entry.id === rid);
      if (control && !sameControl(controlOf(run), control)) return invalidAgent();
      if (
        !control &&
        hint &&
        (run.version_sequence < hint.version_sequence ||
          run.created_at !== hint.created_at ||
          run.completed_steps < hint.completed_steps ||
          run.result_count < hint.result_count ||
          (hint.plan_identity &&
            (!run.plan ||
              run.plan.revision < hint.plan_identity.revision ||
              (run.plan.revision === hint.plan_identity.revision &&
                !identityMatches(run.plan, hint.plan_identity)))) ||
          (run.version_sequence === hint.version_sequence && !sameControl(controlOf(run), hint)))
      )
        return invalidAgent();
      this.reconcileSummary(controlOf(run));
      target.set({
        body: run,
        window: { kind: 'recent', before: null, at_sequence: session.sequence },
      });
      return true;
    });
  }
  closeRun(): void {
    const closed = this.clearOpenedRun();
    const sid = this.selectedId();
    // The optional view set changed: give the remaining views one new reconciliation owner.
    if (closed && sid && !this.legacyId() && this.invalidating().has(sid))
      void this.refresh(sid, undefined, true);
  }
  private clearOpenedRun(): boolean {
    const existed =
      this.openedView() !== null ||
      this.pageReads.has('opened') ||
      this.pageReads.has('results:opened');
    this.cancelPage('opened');
    this.cancelPage('results:opened');
    this.openedView.set(null);
    return existed;
  }
  private cancelPage(key: string): void {
    this.pageReads.get(key)?.abort();
    this.pageReads.delete(key);
    this.pageTasks.delete(key);
    this.pageLoading.update((old) => {
      const next = new Set(old);
      next.delete(key);
      return next;
    });
    this.pageErrors.update((old) => {
      const next = new Map(old);
      next.delete(key);
      return next;
    });
  }
  async loadResults(run: AgentRun, recent = false): Promise<boolean> {
    const session = this.activeSession();
    const target =
      this.currentView()?.body === run
        ? this.currentView
        : this.openedView()?.body === run
          ? this.openedView
          : null;
    if (
      !session ||
      !target ||
      this.invalidating().has(session.id) ||
      (!recent && run.results_before === null)
    )
      return false;
    const view = target()!;
    const before = recent ? undefined : run.results_before!;
    return this.readPage(
      `results:${target === this.currentView ? 'current' : 'opened'}`,
      `results:${run.id}:${run.version_sequence}:${before ?? 'recent'}`,
      async (signal) => {
        const page = parseResultsPage(
          await this.api.results(session.id, run.id, signal, before),
          run.plan,
        );
        if (
          signal.aborted ||
          !this.sameOrigin(session) ||
          target() !== view ||
          page.at_sequence !== session.sequence
        )
          return false;
        if (
          page.session_id !== session.id ||
          page.run_id !== run.id ||
          page.total !== run.result_count ||
          page.completed_steps !== run.completed_steps
        )
          return invalidAgent();
        validateResultWindow(page.results, run.plan, page.total, page.completed_steps);
        const body = parseAgentRun({
          ...run,
          results: page.results,
          results_before: page.next_before,
        });
        target.set({
          body,
          window: {
            kind: recent ? 'recent' : 'older',
            before: before ?? null,
            at_sequence: page.at_sequence,
          },
        });
        return true;
      },
    );
  }
  windowKind(run: AgentRun): 'recent' | 'older' {
    return (
      [this.currentView(), this.openedView()].find((view) => view?.body === run)?.window.kind ??
      'recent'
    );
  }
  private sameOrigin(session: AgentSession): boolean {
    return (
      !this.legacyId() &&
      this.selectedId() === session.id &&
      this.activeSession()?.sequence === session.sequence
    );
  }
  private matchingPage(key: string, intent: string, origin: AgentSession): Promise<boolean> | null {
    const task = this.pageTasks.get(key);
    return task &&
      this.alive(task.controller) &&
      task.sessionId === origin.id &&
      task.sequence === origin.sequence &&
      task.intent === intent
      ? task.promise
      : null;
  }
  private readPage(
    key: string,
    intent: string,
    work: (signal: AbortSignal) => Promise<boolean>,
  ): Promise<boolean> {
    const origin = this.activeSession();
    if (!origin) return Promise.resolve(false);
    const pending = this.matchingPage(key, intent, origin);
    if (pending) return pending;
    // A matching read is shared; a different origin or intent gets a new owner.
    if (this.pageReads.has(key)) this.cancelPage(key);
    const controller = this.controller();
    let resolve!: (admitted: boolean) => void;
    const promise = new Promise<boolean>((done) => {
      resolve = done;
    });
    this.pageReads.set(key, controller);
    this.pageTasks.set(key, {
      controller,
      sessionId: origin.id,
      sequence: origin.sequence,
      intent,
      promise,
    });
    this.pageLoading.update((old) => new Set(old).add(key));
    this.pageErrors.update((old) => {
      const next = new Map(old);
      next.delete(key);
      return next;
    });
    void this.performPageRead(key, controller, origin, work).then(resolve);
    return promise;
  }
  private async performPageRead(
    key: string,
    controller: AbortController,
    origin: AgentSession,
    work: (signal: AbortSignal) => Promise<boolean>,
  ): Promise<boolean> {
    try {
      const admitted = await work(controller.signal);
      return (
        admitted &&
        this.alive(controller) &&
        this.pageReads.get(key) === controller &&
        this.sameOrigin(origin)
      );
    } catch (error) {
      if (
        this.alive(controller) &&
        this.pageReads.get(key) === controller &&
        origin &&
        this.sameOrigin(origin)
      )
        this.pageErrors.update((old) =>
          new Map(old).set(
            key,
            error instanceof Error ? error.message : 'Could not load Agent page.',
          ),
        );
      return false;
    } finally {
      if (this.pageReads.get(key) === controller) {
        this.pageReads.delete(key);
        this.pageTasks.delete(key);
        this.pageLoading.update((old) => {
          const next = new Set(old);
          next.delete(key);
          return next;
        });
        if (key === 'runs') this.drainRecentSummaries();
      }
      this.controllers.delete(controller);
    }
  }
  async create(defaultModel: string): Promise<AgentSession | null> {
    if (this.creating()) return null;
    const model = this.preferredModel() || defaultModel;
    if (!model) {
      this.globalErrorOwner = undefined;
      this.globalError.set('Load available models before creating an Agent session.');
      return null;
    }
    const navigation = ++this.navigation;
    const controller = this.controller();
    this.creating.set(true);
    this.ownError(controller);
    try {
      const session = await this.api.create(
        crypto.randomUUID(),
        model,
        this.preferredEffort(),
        controller.signal,
      );
      if (!this.alive(controller)) return null;
      const existed = this.sessions().some((entry) => entry.id === session.id);
      this.apply(session);
      if (!existed && this.total() !== null) this.total.update((total) => total! + 1);
      if (navigation === this.navigation) {
        this.clearWorkspace();
        this.legacyId.set(null);
        this.selectedId.set(session.id);
      }
      return session;
    } catch (error) {
      this.fail(controller, error);
      return null;
    } finally {
      this.controllers.delete(controller);
      if (!this.destroyed) this.creating.set(false);
    }
  }
  updateDraft(content: string): void {
    if (
      this.legacyId() ||
      this.activeSession()?.archived ||
      this.archivePending().has(this.selectedId() ?? '')
    )
      return;
    this.drafts.update((drafts) => ({ ...drafts, [this.selectedId() ?? '$new']: content }));
  }
  feedback(run: AgentRun): string {
    return this.storedDraft(this.feedbackKey(run));
  }
  updateFeedback(run: AgentRun, content: string): void {
    if (!this.currentRun(run) || run.status !== 'awaiting_approval') return;
    this.drafts.update((drafts) => ({ ...drafts, [this.feedbackKey(run)]: content }));
  }
  async preferences(value: { model?: string; reasoning_effort?: ReasoningEffort }): Promise<void> {
    if (this.locked()) return;
    const session = this.activeSession();
    if (!session) {
      if (value.model !== undefined) this.preferredModel.set(value.model);
      if (value.reasoning_effort !== undefined) this.preferredEffort.set(value.reasoning_effort);
      return;
    }
    await this.mutate(session.id, (signal) =>
      this.api.patch(session.id, crypto.randomUUID(), value, signal),
    );
  }
  request(): void {
    if (!this.canRequest()) return;
    const content = this.draft().trim();
    this.start({ action: 'runs', content });
  }
  canApprove(run: AgentRun): boolean {
    const control = this.activeControl();
    return (
      this.currentRun(run) &&
      this.currentView()?.body === run &&
      run.status === 'awaiting_approval' &&
      control?.status === 'awaiting_approval' &&
      run.version_sequence === control.version_sequence &&
      identityMatches(run.plan, control.plan_identity) &&
      run.plan?.model === this.model() &&
      run.plan.reasoning_effort === this.effort()
    );
  }
  approve(run: AgentRun): void {
    if (!this.canApprove(run) || !run.plan) return;
    this.start({ action: 'execute', run_id: run.id, ...planIdentity(run.plan) });
  }
  revise(run: AgentRun, content: string): void {
    if (!this.canApprove(run) || !run.plan || !content.trim()) return;
    this.start({
      action: 'revise',
      run_id: run.id,
      kind: 'feedback',
      content: content.trim(),
      ...planIdentity(run.plan),
    });
  }
  async reject(run: AgentRun): Promise<void> {
    const sid = this.selectedId();
    if (
      !sid ||
      !this.currentRun(run) ||
      !['awaiting_approval', 'awaiting_clarification'].includes(run.status)
    )
      return;
    const identity =
      run.status === 'awaiting_clarification' && run.clarification
        ? { kind: 'clarification' as const, clarification_id: run.clarification.id }
        : run.plan
          ? { kind: 'plan' as const, ...planIdentity(run.plan) }
          : null;
    if (!identity) return;
    await this.mutate(sid, (signal) =>
      this.api.reject(sid, run.id, crypto.randomUUID(), identity, signal),
    );
  }
  async stop(run: RunControl | AgentRun): Promise<void> {
    const sid = this.selectedId();
    if (
      !sid ||
      !run.operation_id ||
      this.activeControl()?.id !== run.id ||
      this.activeControl()?.operation_id !== run.operation_id ||
      this.activeControl()?.version_sequence !== run.version_sequence ||
      !!this.legacyId() ||
      !!this.activeSession()?.archived ||
      !['planning', 'approved', 'executing'].includes(run.status) ||
      this.stopping().has(sid)
    )
      return;
    this.add(this.stopping, sid);
    const controller = this.controller();
    this.ownError(controller, sid);
    const operationId = run.operation_id;
    try {
      const session = await this.api.cancel(
        sid,
        run.id,
        crypto.randomUUID(),
        operationId,
        controller.signal,
      );
      if (!this.alive(controller)) return;
      const current = this.sessions().find((entry) => entry.id === sid)?.current_run;
      if (current?.operation_id && current.operation_id !== operationId) return;
      this.apply(session, sid);
    } catch (error) {
      this.fail(controller, error, sid);
    } finally {
      this.remove(this.stopping, sid);
      this.controllers.delete(controller);
    }
  }
  async archive(sid: string): Promise<void> {
    const session = this.sessions().find((entry) => entry.id === sid);
    if (!session || session.archived || this.archivePending().has(sid)) return;
    const controller = this.controller();
    const command: ArchiveCommand = { controller, inFlight: true };
    this.archiveCommands.set(sid, command);
    this.add(this.archivePending, sid);
    this.ownError(controller, sid);
    try {
      const session = await this.api.archive(sid, crypto.randomUUID(), controller.signal);
      if (this.alive(controller)) this.apply(session, sid, command);
    } catch (error) {
      this.fail(controller, error, sid);
    } finally {
      command.inFlight = false;
      this.controllers.delete(controller);
    }
  }
  private currentRun(run: AgentRun): boolean {
    return (
      !this.legacyId() &&
      !this.activeSession()?.archived &&
      !this.archivePending().has(this.selectedId() ?? '') &&
      !this.invalidating().has(this.selectedId() ?? '') &&
      this.activeControl()?.id === run.id &&
      this.activeControl()?.version_sequence === run.version_sequence &&
      this.currentView()?.body === run
    );
  }
  private feedbackKey(run: AgentRun): string {
    return `feedback:${this.selectedId()}:${run.id}:${run.plan?.digest}`;
  }
  private storedDraft(key: string): string {
    const drafts = this.drafts();
    return Object.hasOwn(drafts, key) ? drafts[key] : '';
  }
  private start(command: AgentCommand): void {
    const session = this.activeSession();
    if (
      !session ||
      session.archived ||
      this.busy().has(session.id) ||
      this.archivePending().has(session.id) ||
      this.invalidating().has(session.id) ||
      this.legacyId()
    )
      return;
    const controller = this.controller();
    this.streams.set(session.id, controller);
    this.add(this.busy, session.id);
    this.ownError(controller, session.id);
    const request = {
      session_id: session.id,
      request_id: crypto.randomUUID(),
      sequence: session.sequence,
      command,
      snapshot: session,
      original_request: this.currentView()?.body.request,
      reviewed_plan:
        command.action === 'execute' ? (this.currentView()?.body.plan ?? undefined) : undefined,
    };
    void this.run(request, controller);
  }
  private async run(request: AgentStreamRequest, controller: AbortController): Promise<void> {
    const decoder = new AgentEventDecoder(request);
    let accepted = false;
    let finalRunSequence = 0;
    let invalidated = false;
    try {
      await this.api.stream(request, controller.signal, (event) => {
        if (!this.alive(controller) || this.streams.get(request.session_id) !== controller) return;
        const validated = decoder.decode({
          event: 'agent',
          id: String(event.sequence),
          data: JSON.stringify(event),
        });
        if (!validated) return;
        invalidated ||= validated.invalidates_runs;
        if (validated.run) finalRunSequence = validated.run.version_sequence;
        this.applyEvent(validated);
        if (!accepted && validated.run_id !== null && request.command.action === 'runs') {
          accepted = true;
          const content = request.command.content;
          this.drafts.update((drafts) =>
            Object.hasOwn(drafts, request.session_id) &&
            drafts[request.session_id].trim() === content
              ? { ...drafts, [request.session_id]: '' }
              : drafts,
          );
        }
      });
      if (this.alive(controller) && !decoder.terminal)
        throw new AgentApiError(
          'interrupted',
          'Agent connection ended before settlement. Reload history to recover; no command was repeated.',
        );
      if (
        !invalidated &&
        this.alive(controller) &&
        this.selectedId() === request.session_id &&
        !this.legacyId()
      ) {
        const run = this.currentView()?.body;
        if (
          !run ||
          run.version_sequence !== finalRunSequence ||
          run.status === 'awaiting_clarification'
        )
          await this.loadWorkspace();
        else this.requestRecentSummaries();
      }
    } catch (error) {
      if (this.alive(controller)) {
        this.fail(controller, error, request.session_id);
        await this.refresh(request.session_id, controller);
      }
    } finally {
      if (this.streams.get(request.session_id) === controller) {
        this.streams.delete(request.session_id);
        this.remove(this.busy, request.session_id);
      }
      this.controllers.delete(controller);
    }
  }
  private applyEvent(event: AgentEvent): void {
    const oldHeader = this.sessions().find((session) => session.id === event.session_id);
    const header = parseAgentSession(event.session);
    if (
      oldHeader?.sequence === header.sequence &&
      JSON.stringify(oldHeader) !== JSON.stringify(header)
    )
      return invalidAgent();
    const selected =
      this.selectedId() === event.session_id &&
      !this.legacyId() &&
      (!oldHeader || oldHeader.sequence < event.sequence);
    // Reduce and validate every affected loaded body before publishing even the header.
    const current =
      selected && !event.invalidates_runs
        ? reduceRunView(this.currentView(), event)
        : this.currentView();
    const opened =
      selected && !event.invalidates_runs && this.openedView()?.body.id === event.run?.id
        ? reduceRunView(this.openedView(), event)
        : this.openedView();
    this.apply(header, event.session_id);
    if (event.invalidates_runs) {
      this.recentSummaryRequest = null;
      this.invalidationSequences.set(
        event.session_id,
        Math.max(event.sequence, this.invalidationSequences.get(event.session_id) ?? 0),
      );
      this.add(this.invalidating, event.session_id);
      if (this.selectedId() === event.session_id && !this.legacyId())
        for (const key of ['runs', 'current', 'opened', 'results:current', 'results:opened'])
          this.cancelPage(key);
      void this.refresh(event.session_id, undefined, true);
      return;
    }
    if (!selected) return;
    const pinned = header.current_run?.id ?? header.latest_run_id;
    if (current === null || current.body.id === pinned) this.currentView.set(current);
    this.openedView.set(opened);
    if (event.run) this.reconcileSummary(event.run);
    if (oldHeader && event.session.run_count > oldHeader.run_count) {
      this.summariesDirty = true;
      this.summaryIntent++;
      this.recentAutoAllowed = true;
      this.requestRecentSummaries();
    }
  }
  private reconcileSummary(control: RunControl): void {
    this.summaries.update((runs) =>
      runs.map((run) =>
        run.id === control.id && run.version_sequence <= control.version_sequence
          ? { ...control, request_preview: run.request_preview }
          : run,
      ),
    );
  }
  private async mutate(
    sid: string,
    work: (signal: AbortSignal) => Promise<AgentSession>,
  ): Promise<boolean> {
    if (this.busy().has(sid) || this.archivePending().has(sid)) return false;
    const ownedBusy = !this.busy().has(sid);
    if (ownedBusy) this.add(this.busy, sid);
    const controller = this.controller();
    this.ownError(controller, sid);
    try {
      const session = await work(controller.signal);
      if (!this.alive(controller)) return false;
      this.apply(session, sid);
      if (
        this.selectedId() === sid &&
        !this.legacyId() &&
        session.latest_run_id &&
        !session.current_run
      )
        await this.openRun(session.latest_run_id, true);
      return true;
    } catch (error) {
      this.fail(controller, error, sid);
      return false;
    } finally {
      if (ownedBusy) this.remove(this.busy, sid);
      this.controllers.delete(controller);
    }
  }
  private apply(value: AgentSession, sid?: string, archiveAcknowledgement?: ArchiveCommand): void {
    const session = parseAgentSession(value);
    if (sid && session.id !== sid) return invalidAgent();
    const old = this.sessions().find((entry) => entry.id === session.id);
    if (old && old.sequence === session.sequence && JSON.stringify(old) !== JSON.stringify(session))
      return invalidAgent();
    if (archiveAcknowledgement && this.archiveCommands.get(session.id) === archiveAcknowledgement)
      this.archiveCommands.delete(session.id);
    if (old && old.sequence > session.sequence) {
      this.reconcileArchive(old);
      return;
    }
    if (!old || old.sequence !== session.sequence) {
      this.sessions.update((sessions) =>
        old
          ? sessions.map((entry) => (entry.id === session.id ? session : entry))
          : [...sessions, session],
      );
      if (this.selectedId() === session.id && !this.legacyId()) {
        const pinned = session.current_run?.id ?? session.latest_run_id;
        if (this.currentView()?.body.id !== pinned) this.currentView.set(null);
      }
    }
    this.reconcileArchive(session);
  }
  private reconcileArchive(session: AgentSession): void {
    if (this.archiveCommands.has(session.id)) this.add(this.archivePending, session.id);
    else if (session.archived || session.archive_pending === false)
      this.remove(this.archivePending, session.id);
    else if (session.archive_pending === true) this.add(this.archivePending, session.id);
  }
  private controller(): AbortController {
    const controller = new AbortController();
    this.controllers.add(controller);
    return controller;
  }
  private alive(controller: AbortController): boolean {
    return !this.destroyed && !controller.signal.aborted;
  }
  private ownError(controller: AbortController, sid?: string, clear = true): AbortController {
    if (sid) {
      const current = this.errorOwners.get(sid);
      if (
        !clear &&
        current &&
        this.commandErrorOwners.has(current) &&
        this.controllers.has(current)
      )
        return current;
      if (clear) this.commandErrorOwners.add(controller);
      this.errorOwners.set(sid, controller);
      if (clear)
        this.sessionErrors.update((errors) => {
          const next = new Map(errors);
          next.delete(sid);
          return next;
        });
    } else {
      this.globalErrorOwner = controller;
      this.globalError.set(null);
    }
    return controller;
  }
  private fail(
    controller: AbortController,
    error: unknown,
    sid?: string,
    owner = controller,
  ): void {
    if (
      !this.alive(controller) ||
      (sid ? this.errorOwners.get(sid) !== owner : this.globalErrorOwner !== owner)
    )
      return;
    const message =
      error instanceof Error
        ? error.message
        : 'Agent request failed. Check the BFF connection and reload history.';
    if (sid) this.sessionErrors.update((errors) => new Map(errors).set(sid, message));
    else this.globalError.set(message);
  }
  private add(target: typeof this.busy, sid: string): void {
    target.update((ids) => new Set(ids).add(sid));
  }
  private remove(target: typeof this.busy, sid: string): void {
    target.update((ids) => {
      const next = new Set(ids);
      next.delete(sid);
      return next;
    });
  }
  private restore(): AgentUiState {
    try {
      if (!this.storage) throw new Error();
      const raw = this.storage.getItem(AGENT_UI_STORAGE_KEY);
      if (raw === null) return { ...EMPTY, drafts: {} };
      const value: unknown = JSON.parse(raw);
      if (
        !object(value) ||
        value['version'] !== 1 ||
        !['chat', 'agent'].includes(value['mode'] as string) ||
        !(value['selectedId'] === null || typeof value['selectedId'] === 'string') ||
        !(
          value['legacyId'] === undefined ||
          value['legacyId'] === null ||
          typeof value['legacyId'] === 'string'
        ) ||
        !object(value['drafts']) ||
        !Object.values(value['drafts']).every((draft) => typeof draft === 'string')
      )
        throw new Error();
      return { ...value, legacyId: value['legacyId'] ?? null } as unknown as AgentUiState;
    } catch {
      this.canPersist = false;
      this.warn();
      return { ...EMPTY, drafts: {} };
    }
  }
  private persist(value: AgentUiState): void {
    if (!this.canPersist) return;
    try {
      this.storage!.setItem(AGENT_UI_STORAGE_KEY, JSON.stringify(value));
      this.warned = false;
    } catch {
      this.warn();
    }
  }
  private warn(): void {
    if (!this.warned)
      console.warn(
        'Agent UI storage is unavailable or unsupported. Existing data was left untouched; changes remain in memory.',
      );
    this.warned = true;
  }
}
