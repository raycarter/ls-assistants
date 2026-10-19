import { computed, DestroyRef, effect, inject, Injectable, signal } from '@angular/core';
import { AgentApi, AgentEventDecoder, type AgentStreamRequest } from './agent-api';
import {
  AgentApiError,
  invalidAgent,
  object,
  parseAgentSession,
  planIdentity,
  settled,
  type AgentCommand,
  type AgentRun,
  type AgentSession,
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
  readonly activeRun = computed(
    () => this.activeSession()?.runs.find((run) => !settled(run)) ?? null,
  );
  readonly locked = computed(
    () =>
      !!this.activeRun() ||
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
      const sessions = await this.api.list(controller.signal);
      if (!this.alive(controller)) return;
      for (const session of sessions) this.apply(session);
      this.loaded = true;
      if (navigation === this.navigation && !this.legacyId()) {
        const selected = this.selectedId();
        if (!selected || !sessions.some((session) => session.id === selected)) {
          this.selectedId.set(sessions.find((session) => !session.archived)?.id ?? null);
        }
        if (this.selectedId()) await this.refresh(this.selectedId()!);
      }
    } catch (error) {
      this.fail(controller, error);
    } finally {
      if (this.listController === controller && !this.destroyed) this.loading.set(false);
      this.controllers.delete(controller);
    }
  }
  select(id: string): void {
    this.navigation++;
    this.legacyId.set(null);
    this.selectedId.set(id);
    void this.refresh(id);
  }
  selectLegacy(id: string): void {
    this.navigation++;
    this.legacyId.set(id);
    this.selectedId.set(null);
  }
  async refresh(sid = this.selectedId(), errorOwner?: AbortController): Promise<void> {
    if (!sid) return;
    this.reads.get(sid)?.abort();
    const controller = this.controller();
    this.reads.set(sid, controller);
    const owner = errorOwner ?? this.ownError(controller, sid, false);
    const archive = this.archiveCommands.get(sid);
    const recoveringArchive = archive && !archive.inFlight ? archive : undefined;
    try {
      const session = await this.api.get(sid, controller.signal);
      if (this.alive(controller) && this.reads.get(sid) === controller)
        this.apply(session, sid, recoveringArchive);
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
      this.apply(session);
      if (navigation === this.navigation) {
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
  approve(run: AgentRun): void {
    if (run.status !== 'awaiting_approval' || !run.plan || !this.currentRun(run)) return;
    this.start({ action: 'execute', run_id: run.id, ...planIdentity(run.plan) });
  }
  revise(run: AgentRun, content: string): void {
    if (run.status !== 'awaiting_approval' || !run.plan || !content.trim() || !this.currentRun(run))
      return;
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
  async stop(run: AgentRun): Promise<void> {
    const sid = this.selectedId();
    if (
      !sid ||
      !run.operation_id ||
      !this.currentRun(run) ||
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
      const current = this.sessions()
        .find((entry) => entry.id === sid)
        ?.runs.find((entry) => entry.id === run.id);
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
      this.activeSession()?.runs.find((entry) => entry.id === run.id) === run
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
    };
    void this.run(request, controller);
  }
  private async run(request: AgentStreamRequest, controller: AbortController): Promise<void> {
    const decoder = new AgentEventDecoder(request);
    let accepted = false;
    try {
      await this.api.stream(request, controller.signal, (event) => {
        if (!this.alive(controller) || this.streams.get(request.session_id) !== controller) return;
        const validated = decoder.decode({
          event: 'agent',
          id: String(event.sequence),
          data: JSON.stringify(event),
        });
        if (!validated) return;
        this.apply(validated.session, request.session_id);
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
