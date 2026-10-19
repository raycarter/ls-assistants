import {
  afterRenderEffect,
  afterNextRender,
  Component,
  computed,
  DestroyRef,
  ElementRef,
  inject,
  Injector,
  signal,
  viewChild,
  viewChildren,
} from '@angular/core';
import { FormsModule, NgModel } from '@angular/forms';
import { ChatState } from './chat-state';
import { ChatTurns } from './chat-turns';
import { REASONING_EFFORTS } from './chat.models';
import { MarkdownPipe } from './markdown.pipe';
import { MessageTimePipe } from './message-time.pipe';
import { SettingsDialog } from './settings-dialog';
import { AgentState } from './agent-state';
import { AgentWorkspace } from './agent-workspace';
import type { ReasoningEffort } from './chat.models';

@Component({
  imports: [FormsModule, MarkdownPipe, MessageTimePipe, SettingsDialog, AgentWorkspace],
  selector: 'app-root',
  styleUrl: './app.css',
  templateUrl: './app.html',
  host: {
    '(document:keydown)': 'onNavigationKeydown($event)',
    '(window:resize)': 'onViewportResize()',
  },
})
export class App {
  readonly state = inject(ChatState);
  readonly turns = inject(ChatTurns);
  readonly agent = inject(AgentState);
  readonly legacy = computed(
    () =>
      this.state.legacySessions().find((session) => session.id === this.agent.legacyId()) ?? null,
  );
  readonly selectedModel = computed(() =>
    this.agent.mode() === 'chat'
      ? this.turns.selectedModel()
      : this.agent.model() || this.turns.defaultModel(),
  );
  readonly selectedEffort = computed(() =>
    this.agent.mode() === 'chat' ? this.turns.selectedEffort() : this.agent.effort(),
  );
  readonly efforts = REASONING_EFFORTS;
  readonly replyError = computed(() => {
    const last = this.messages().at(-1);
    return (
      this.turns.errors().get(this.state.activeSessionId()) ??
      last?.error?.message ??
      (last?.status === 'interrupted' ? 'The reply was interrupted. You can retry.' : null)
    );
  });
  readonly settingsOpen = signal(false);
  readonly sidebarOpen = signal(false);
  readonly messages = computed(() => this.state.activeSession().messages);
  readonly selectedPromptId = computed(() => this.state.activeSession().prompt?.id ?? null);
  private readonly messageList = viewChild<ElementRef<HTMLDivElement>>('messageList');
  private readonly messageElements = viewChildren<ElementRef<HTMLElement>>('messageBubble');
  private readonly composer = viewChild<ElementRef<HTMLTextAreaElement>>('composer');
  private readonly agentWorkspace = viewChild<unknown, ElementRef<HTMLElement>>('agentWorkspace', {
    read: ElementRef,
  });
  private readonly sidebar = viewChild<ElementRef<HTMLElement>>('sidebar');
  private readonly sidebarToggle = viewChild<ElementRef<HTMLButtonElement>>('sidebarToggle');
  private readonly newSessionButton = viewChild<ElementRef<HTMLButtonElement>>('newSessionButton');
  private readonly settingsButton = viewChild<ElementRef<HTMLButtonElement>>('settingsButton');
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);
  private navigation = 0;

  constructor() {
    if (this.agent.legacyId() && !this.legacy()) this.agent.legacyId.set(null);
    this.agent.setMode(this.agent.mode());
    afterRenderEffect((onCleanup) => {
      this.messages();
      this.messageElements();
      const list = this.messageList()?.nativeElement;
      if (list) list.scrollTop = list.scrollHeight;
      if (!list || typeof ResizeObserver === 'undefined') return;
      let alive = true;
      const observer = new ResizeObserver(() => {
        if (alive) list.scrollTop = list.scrollHeight;
      });
      observer.observe(list);
      for (const bubble of this.messageElements()) observer.observe(bubble.nativeElement);
      onCleanup(() => {
        alive = false;
        observer.disconnect();
      });
    });
    afterRenderEffect(() => {
      if (this.sidebarOpen()) this.newSessionButton()?.nativeElement.focus();
    });
  }

  newSession(): void {
    const navigation = ++this.navigation;
    if (this.agent.mode() === 'agent') {
      void this.agent.create(this.turns.defaultModel()).then((session) => {
        if (session && this.agent.mode() === 'agent' && this.agent.selectedId() === session.id)
          this.focusWorkspace(navigation);
      });
    } else this.state.newSession();
    this.sidebarOpen.set(false);
    this.focusWorkspace(navigation);
  }

  selectSession(id: string): void {
    this.navigation++;
    if (this.agent.mode() === 'agent') this.agent.select(id);
    else this.state.selectSession(id);
    this.sidebarOpen.set(false);
    this.focusWorkspace();
  }

  selectLegacy(id: string): void {
    this.navigation++;
    this.agent.selectLegacy(id);
    this.sidebarOpen.set(false);
    this.focusWorkspace();
  }

  setMode(mode: 'chat' | 'agent'): void {
    this.navigation++;
    this.agent.setMode(mode);
  }

  deleteSession(id: string): void {
    this.navigation++;
    if (this.agent.mode() === 'agent') void this.agent.archive(id);
    else this.turns.deleteSession(id);
    this.focusAfterRender(() =>
      this.sidebarOpen() ? this.newSessionButton()?.nativeElement : this.activeComposer(),
    );
  }

  closeSidebar(): void {
    this.navigation++;
    this.sidebarOpen.set(false);
    this.focusAfterRender(() => this.sidebarToggle()?.nativeElement);
  }

  toggleSidebar(): void {
    this.navigation++;
    this.sidebarOpen.set(!this.sidebarOpen());
  }

  closeSettings(): void {
    this.navigation++;
    this.settingsOpen.set(false);
    this.focusAfterRender(() =>
      window.innerWidth < 768
        ? this.sidebarToggle()?.nativeElement
        : this.settingsButton()?.nativeElement,
    );
  }

  onViewportResize(): void {
    if (window.innerWidth >= 768 && this.sidebarOpen()) {
      this.sidebarOpen.set(false);
      this.focusAfterRender(() => this.activeComposer());
    }
  }

  onNavigationKeydown(event: KeyboardEvent): void {
    if (!this.sidebarOpen()) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      this.closeSidebar();
      return;
    }
    if (event.key !== 'Tab') return;

    const buttons =
      this.sidebar()?.nativeElement.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
    if (!buttons?.length) return;
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  openSettings(): void {
    this.navigation++;
    this.sidebarOpen.set(false);
    this.settingsOpen.set(true);
  }

  sendMessage(): void {
    if (this.agent.mode() !== 'chat') return;
    if (this.turns.send()) this.composer()?.nativeElement.focus();
  }

  async selectModel(model: string, control?: NgModel): Promise<void> {
    if (this.agent.mode() === 'chat') this.state.selectModel(model);
    else if (model !== this.selectedModel()) {
      await this.agent.preferences({ model });
      this.reconcilePreference(control, this.selectedModel());
    }
  }

  async selectEffort(reasoning_effort: ReasoningEffort, control?: NgModel): Promise<void> {
    if (this.agent.mode() === 'chat') this.state.selectReasoningEffort(reasoning_effort);
    else if (reasoning_effort !== this.selectedEffort()) {
      await this.agent.preferences({ reasoning_effort });
      this.reconcilePreference(control, this.selectedEffort());
    }
  }

  onComposerKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229)
      return;
    event.preventDefault();
    this.sendMessage();
  }

  private focusAfterRender(target: () => HTMLElement | undefined): void {
    if (this.destroyRef.destroyed) return;
    afterNextRender(() => target()?.focus(), { injector: this.injector });
  }

  private reconcilePreference(control: NgModel | undefined, value: string): void {
    if (this.destroyRef.destroyed || !control) return;
    // Keep NgModel's binding cache in sync without invoking a mode-dependent user action.
    control.viewModel = value;
    control.control.setValue(value, { emitEvent: false, emitViewToModelChange: false });
  }

  private activeComposer(): HTMLElement | undefined {
    if (this.agent.mode() === 'chat') return this.composer()?.nativeElement;
    const workspace = this.agentWorkspace()?.nativeElement;
    return (
      workspace?.querySelector<HTMLElement>('#agent-request:not(:disabled)') ??
      workspace?.querySelector<HTMLElement>('h1[tabindex]') ??
      workspace?.querySelector<HTMLElement>('.agent-history') ??
      undefined
    );
  }

  private focusWorkspace(navigation = this.navigation): void {
    this.focusAfterRender(() =>
      navigation === this.navigation ? this.activeComposer() : undefined,
    );
  }
}
