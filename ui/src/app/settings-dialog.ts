import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  Injector,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { PromptDraft, SystemPrompt } from './chat.models';

@Component({
  selector: 'app-settings-dialog',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './settings-dialog.html',
  styleUrl: './settings-dialog.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SettingsDialog {
  readonly prompts = input.required<readonly SystemPrompt[]>();
  readonly selectedPrompt = input<SystemPrompt | null>(null);
  readonly promptLocked = input(false);
  readonly chatSelectionContext = input(true);
  readonly closed = output<void>();
  readonly promptSelected = output<string | null>();
  readonly promptSaved = output<PromptDraft>();
  readonly promptDeleted = output<string>();

  protected readonly editor = signal<PromptDraft | null>(null);
  protected readonly submitted = signal(false);

  private readonly injector = inject(Injector);
  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');
  private readonly sectionHeading =
    viewChild.required<ElementRef<HTMLHeadingElement>>('sectionHeading');
  private readonly titleField = viewChild<ElementRef<HTMLInputElement>>('titleField');
  private readonly contentField = viewChild<ElementRef<HTMLTextAreaElement>>('contentField');
  private editorOrigin: HTMLElement | null = null;
  private closedEmitted = false;

  constructor() {
    let nativeDialog: HTMLDialogElement | undefined;
    afterNextRender(() => {
      nativeDialog = this.dialog().nativeElement;
      nativeDialog.showModal();
    });
    inject(DestroyRef).onDestroy(() => {
      this.closedEmitted = true;
      if (nativeDialog?.open) nativeDialog.close();
    });
  }

  protected closeDialog(): void {
    if (this.closedEmitted) return;
    // Close first: the browser restores focus before the parent removes this component.
    this.dialog().nativeElement.close();
    this.onNativeClose();
  }

  protected onNativeClose(): void {
    if (this.closedEmitted) return;
    this.closedEmitted = true;
    this.closed.emit();
  }

  protected onCancel(event: Event): void {
    event.preventDefault();
    this.closeDialog();
  }

  protected onBackdropClick(event: MouseEvent): void {
    const dialog = this.dialog().nativeElement;
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (
      event.clientX < bounds.left ||
      event.clientX > bounds.right ||
      event.clientY < bounds.top ||
      event.clientY > bounds.bottom
    ) {
      this.closeDialog();
    }
  }

  protected focusSection(): void {
    this.sectionHeading().nativeElement.focus();
  }

  protected isSelected(id: string | null): boolean {
    return this.chatSelectionContext() && (this.selectedPrompt()?.id ?? null) === id;
  }

  protected selectPrompt(id: string | null): void {
    if (this.chatSelectionContext() && !this.promptLocked()) this.promptSelected.emit(id);
  }

  protected addPrompt(origin: HTMLElement): void {
    this.openEditor({ title: '', content: '' }, origin);
  }

  protected editPrompt(prompt: SystemPrompt, origin: HTMLElement): void {
    this.openEditor({ id: prompt.id, title: prompt.title, content: prompt.content }, origin);
  }

  protected updateDraft(field: 'title' | 'content', value: string): void {
    this.editor.update((draft) => (draft ? { ...draft, [field]: value } : null));
  }

  protected cancelEditor(): void {
    this.editor.set(null);
    this.submitted.set(false);
    this.editorOrigin?.focus();
    this.editorOrigin = null;
  }

  protected savePrompt(): void {
    const draft = this.editor();
    if (!draft) return;
    this.submitted.set(true);
    const title = draft.title.trim();
    const content = draft.content.trim();
    if (!title || !content) {
      if (!title) this.titleField()?.nativeElement.focus();
      else this.contentField()?.nativeElement.focus();
      return;
    }

    const saved: PromptDraft = { title, content };
    if (draft.id !== undefined) saved.id = draft.id;
    this.cancelEditor();
    this.promptSaved.emit(saved);
  }

  protected deletePrompt(id: string): void {
    if (this.editor()?.id === id) this.cancelEditor();
    this.promptDeleted.emit(id);
  }

  private openEditor(draft: PromptDraft, origin: HTMLElement): void {
    this.editorOrigin = origin;
    this.submitted.set(false);
    this.editor.set(draft);
    afterNextRender(() => this.titleField()?.nativeElement.focus(), { injector: this.injector });
  }
}
