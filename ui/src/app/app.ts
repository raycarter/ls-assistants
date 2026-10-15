import {
  afterRenderEffect,
  afterNextRender,
  Component,
  computed,
  ElementRef,
  inject,
  Injector,
  signal,
  viewChild,
  viewChildren,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ChatState } from './chat-state';
import { ChatTurns } from './chat-turns';
import { REASONING_EFFORTS } from './chat.models';
import { MarkdownPipe } from './markdown.pipe';
import { MessageTimePipe } from './message-time.pipe';
import { SettingsDialog } from './settings-dialog';

@Component({
  imports: [FormsModule, MarkdownPipe, MessageTimePipe, SettingsDialog],
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
  readonly modeLabel = computed(() =>
    this.state.activeSession().mode === 'chat' ? 'Chat' : 'Agent',
  );
  private readonly messageList = viewChild<ElementRef<HTMLDivElement>>('messageList');
  private readonly messageElements = viewChildren<ElementRef<HTMLElement>>('messageBubble');
  private readonly composer = viewChild<ElementRef<HTMLTextAreaElement>>('composer');
  private readonly sidebar = viewChild<ElementRef<HTMLElement>>('sidebar');
  private readonly sidebarToggle = viewChild<ElementRef<HTMLButtonElement>>('sidebarToggle');
  private readonly newSessionButton = viewChild<ElementRef<HTMLButtonElement>>('newSessionButton');
  private readonly settingsButton = viewChild<ElementRef<HTMLButtonElement>>('settingsButton');
  private readonly injector = inject(Injector);

  constructor() {
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
    this.state.newSession();
    this.sidebarOpen.set(false);
    this.focusAfterRender(() => this.composer()?.nativeElement);
  }

  selectSession(id: string): void {
    this.state.selectSession(id);
    this.sidebarOpen.set(false);
    this.focusAfterRender(() => this.composer()?.nativeElement);
  }

  deleteSession(id: string): void {
    this.turns.deleteSession(id);
    this.focusAfterRender(() =>
      this.sidebarOpen() ? this.newSessionButton()?.nativeElement : this.composer()?.nativeElement,
    );
  }

  closeSidebar(): void {
    this.sidebarOpen.set(false);
    this.focusAfterRender(() => this.sidebarToggle()?.nativeElement);
  }

  closeSettings(): void {
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
      this.focusAfterRender(() => this.composer()?.nativeElement);
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
    this.sidebarOpen.set(false);
    this.settingsOpen.set(true);
  }

  sendMessage(): void {
    if (this.turns.send()) this.composer()?.nativeElement.focus();
  }

  onComposerKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229)
      return;
    event.preventDefault();
    this.sendMessage();
  }

  private focusAfterRender(target: () => HTMLElement | undefined): void {
    afterNextRender(() => target()?.focus(), { injector: this.injector });
  }
}
