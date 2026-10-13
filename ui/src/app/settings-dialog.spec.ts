import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SystemPrompt } from './chat.models';
import { SettingsDialog } from './settings-dialog';

const assistantPrompt: SystemPrompt = {
  id: 'assistant',
  title: 'IT-personal-assistant',
  content: 'Help with IT questions.',
};
const translationPrompt: SystemPrompt = {
  id: 'translator',
  title: '语言翻译',
  content: 'Translate the user message.',
};
const catalogue: readonly SystemPrompt[] = [assistantPrompt, translationPrompt];

describe('SettingsDialog', () => {
  let fixture: ComponentFixture<SettingsDialog>;
  let showModal: ReturnType<typeof vi.fn>;
  let close: ReturnType<typeof vi.fn>;
  let originalShowModal: PropertyDescriptor | undefined;
  let originalClose: PropertyDescriptor | undefined;

  beforeEach(async () => {
    // jsdom does not implement native dialog behaviour. Keep these stubs in tests only.
    originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
    originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close');
    showModal = vi.fn(function (this: HTMLDialogElement) {
      this.open = true;
    });
    close = vi.fn(function (this: HTMLDialogElement) {
      if (!this.open) return;
      this.open = false;
      this.dispatchEvent(new Event('close'));
    });
    Object.defineProperties(HTMLDialogElement.prototype, {
      showModal: { configurable: true, writable: true, value: showModal },
      close: { configurable: true, writable: true, value: close },
    });
    await TestBed.configureTestingModule({ imports: [SettingsDialog] }).compileComponents();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
    }
    if (originalClose) {
      Object.defineProperty(HTMLDialogElement.prototype, 'close', originalClose);
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, 'close');
    }
  });

  async function render(selected: SystemPrompt | null = null, locked = false): Promise<void> {
    fixture = TestBed.createComponent(SettingsDialog);
    fixture.componentRef.setInput('prompts', catalogue);
    fixture.componentRef.setInput('selectedPrompt', selected);
    fixture.componentRef.setInput('promptLocked', locked);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function element<T extends HTMLElement>(selector: string): T {
    const found = (fixture.nativeElement as HTMLElement).querySelector<T>(selector);
    if (!found) throw new Error(`Missing element: ${selector}`);
    return found;
  }

  function button(label: string): HTMLButtonElement {
    const buttons = (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>(
      'button',
    );
    const found = Array.from(buttons).find(
      (candidate) =>
        candidate.textContent?.trim() === label || candidate.getAttribute('aria-label') === label,
    );
    if (!found) throw new Error(`Missing button: ${label}`);
    return found;
  }

  async function clickButton(label: string): Promise<void> {
    button(label).click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function fill(selector: string, value: string): void {
    const field = element<HTMLInputElement | HTMLTextAreaElement>(selector);
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    fixture.detectChanges();
  }

  async function submit(): Promise<void> {
    element<HTMLFormElement>('form').dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('opens a native modal after rendering and shows the catalogue count and default selection', async () => {
    await render();

    expect(showModal).toHaveBeenCalledTimes(1);
    expect(element<HTMLDialogElement>('dialog').open).toBe(true);
    expect(element('h1').textContent).toBe('Settings');
    expect(element('fieldset legend').textContent).toContain('Choose a system prompt');
    expect(element('h3').textContent).toContain('Available prompts');
    expect(element('h3').textContent).toContain('(2)');
    expect(element<HTMLInputElement>('input[type="radio"][value=""]').checked).toBe(true);
    expect(element('[role="status"]').textContent).toContain('Selected for this chat');
    expect(element('[role="status"]').textContent).toContain('No system prompt');
  });

  it('closes before notifying the parent, emitting once despite the native close event', async () => {
    await render();
    const closed = vi.fn(() => expect(element<HTMLDialogElement>('dialog').open).toBe(false));
    fixture.componentInstance.closed.subscribe(closed);

    await clickButton('Close settings');
    element('dialog').dispatchEvent(new Event('close'));

    expect(close).toHaveBeenCalledTimes(1);
    expect(closed).toHaveBeenCalledTimes(1);
    expect(button('Close settings').type).toBe('button');
  });

  it('handles the native Escape cancel event through the same close path', async () => {
    await render();
    const closed = vi.fn();
    fixture.componentInstance.closed.subscribe(closed);
    const cancel = new Event('cancel', { cancelable: true });

    element('dialog').dispatchEvent(cancel);

    expect(cancel.defaultPrevented).toBe(true);
    expect(close).toHaveBeenCalledTimes(1);
    expect(closed).toHaveBeenCalledTimes(1);
    expect(element<HTMLDialogElement>('dialog').open).toBe(false);
  });

  it('closes on backdrop clicks, but keeps clicks on the dialog surface and its content open', async () => {
    await render();
    const dialog = element<HTMLDialogElement>('dialog');
    vi.spyOn(dialog, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 100,
      left: 100,
      top: 100,
      right: 700,
      bottom: 600,
      width: 600,
      height: 500,
      toJSON: () => ({}),
    });
    const closed = vi.fn();
    fixture.componentInstance.closed.subscribe(closed);

    dialog.dispatchEvent(new MouseEvent('click', { clientX: 150, clientY: 150 }));
    element('h1').dispatchEvent(
      new MouseEvent('click', { bubbles: true, clientX: 50, clientY: 50 }),
    );
    expect(closed).not.toHaveBeenCalled();

    dialog.dispatchEvent(new MouseEvent('click', { clientX: 50, clientY: 50 }));
    expect(close).toHaveBeenCalledTimes(1);
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it('closes an open dialog when the parent destroys the component', async () => {
    await render();
    const dialog = element<HTMLDialogElement>('dialog');
    const closed = vi.fn();
    fixture.componentInstance.closed.subscribe(closed);

    fixture.destroy();

    expect(dialog.open).toBe(false);
    expect(close).toHaveBeenCalledTimes(1);
    expect(closed).not.toHaveBeenCalled();
  });

  it('emits prompt IDs or null for selection while leaving selection state to the parent', async () => {
    await render();
    const selected = vi.fn();
    fixture.componentInstance.promptSelected.subscribe(selected);

    element<HTMLInputElement>('input[value="translator"]').click();
    fixture.detectChanges();
    expect(selected).toHaveBeenLastCalledWith('translator');
    expect(fixture.componentInstance.selectedPrompt()).toBeNull();

    fixture.componentRef.setInput('selectedPrompt', translationPrompt);
    fixture.detectChanges();
    expect(element<HTMLInputElement>('input[value="translator"]').checked).toBe(true);
    expect(
      element<HTMLInputElement>('input[value="translator"]').closest('li')?.textContent,
    ).toContain('Selected');

    element<HTMLInputElement>('input[type="radio"][value=""]').click();
    expect(selected).toHaveBeenLastCalledWith(null);
    expect(selected).toHaveBeenCalledTimes(2);
    expect(fixture.componentInstance.selectedPrompt()).toBe(translationPrompt);
  });

  it('disables only prompt selection in a locked chat', async () => {
    await render(translationPrompt, true);
    const selected = vi.fn();
    fixture.componentInstance.promptSelected.subscribe(selected);
    const radios = (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLInputElement>(
      'input[type="radio"]',
    );

    for (const radio of radios) {
      expect(radio.disabled).toBe(true);
      radio.click();
      radio.dispatchEvent(new Event('change', { bubbles: true }));
    }
    expect(selected).not.toHaveBeenCalled();
    expect(button('Add system prompt').disabled).toBe(false);
    expect(button('Edit 语言翻译').disabled).toBe(false);
    expect(button('Delete 语言翻译').disabled).toBe(false);
  });

  it('uses the locked snapshot even when its catalogue entry changes or is removed', async () => {
    const snapshot = { ...translationPrompt, title: 'Original locked translation' };
    await render(snapshot, true);
    expect(element('[role="status"]').textContent).toContain('Original locked translation');

    fixture.componentRef.setInput('prompts', [assistantPrompt]);
    fixture.detectChanges();

    expect(element('[role="status"]').textContent).toContain('Locked for this chat');
    expect(element('[role="status"]').textContent).toContain('Original locked translation');
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('input[type="radio"]:checked'),
    ).toBeNull();
    expect(element('h3').textContent).toContain('(1)');
  });

  it('represents a locked null snapshot as No system prompt', async () => {
    await render(null, true);

    expect(element('[role="status"]').textContent).toContain('Locked for this chat');
    expect(element('[role="status"]').textContent).toContain('No system prompt');
    expect(element<HTMLInputElement>('input[type="radio"][value=""]').checked).toBe(true);
  });

  it('rejects whitespace-only title and content with labelled field errors', async () => {
    await render();
    const saved = vi.fn();
    fixture.componentInstance.promptSaved.subscribe(saved);
    await clickButton('Add system prompt');
    fill('#settings-prompt-title', '  \t ');
    fill('#settings-prompt-content', ' \n ');

    await submit();

    expect(saved).not.toHaveBeenCalled();
    expect(element('#settings-prompt-title').getAttribute('aria-invalid')).toBe('true');
    expect(element('#settings-prompt-content').getAttribute('aria-invalid')).toBe('true');
    expect(element('#settings-title-error').textContent).toContain('Enter a title');
    expect(element('#settings-content-error').textContent).toContain('Enter prompt content');
    expect(document.activeElement).toBe(element('#settings-prompt-title'));

    fill('#settings-prompt-title', 'Valid title');
    await submit();
    expect(saved).not.toHaveBeenCalled();
    expect(element('#settings-prompt-title').getAttribute('aria-invalid')).toBeNull();
    expect(element('#settings-prompt-content').getAttribute('aria-invalid')).toBe('true');
    expect(document.activeElement).toBe(element('#settings-prompt-content'));
  });

  it('emits a trimmed new draft without an ID and allows adding in locked chats', async () => {
    await render(translationPrompt, true);
    const saved = vi.fn();
    const selected = vi.fn();
    fixture.componentInstance.promptSaved.subscribe(saved);
    fixture.componentInstance.promptSelected.subscribe(selected);
    await clickButton('Add system prompt');
    fill('#settings-prompt-title', '  New prompt  ');
    fill('#settings-prompt-content', '\n First line.\nSecond line. \n');

    await submit();

    expect(saved).toHaveBeenCalledExactlyOnceWith({
      title: 'New prompt',
      content: 'First line.\nSecond line.',
    });
    expect(selected).not.toHaveBeenCalled();
    expect(fixture.componentInstance.prompts()).toBe(catalogue);
    expect((fixture.nativeElement as HTMLElement).querySelector('form')).toBeNull();
    expect(document.activeElement).toBe(button('Add system prompt'));
  });

  it('cancels without saving and resets the next transient draft and validation', async () => {
    await render();
    const saved = vi.fn();
    fixture.componentInstance.promptSaved.subscribe(saved);
    await clickButton('Add system prompt');
    fill('#settings-prompt-content', 'Unsaved draft');
    await submit();

    await clickButton('Cancel');
    expect(saved).not.toHaveBeenCalled();
    expect((fixture.nativeElement as HTMLElement).querySelector('form')).toBeNull();

    await clickButton('Add system prompt');
    expect(element<HTMLInputElement>('#settings-prompt-title').value).toBe('');
    expect(element<HTMLTextAreaElement>('#settings-prompt-content').value).toBe('');
    expect(element('#settings-prompt-title').getAttribute('aria-invalid')).toBeNull();
  });

  it('edits a copied catalogue entry and emits its ID without modifying the locked snapshot', async () => {
    await render(translationPrompt, true);
    const saved = vi.fn();
    fixture.componentInstance.promptSaved.subscribe(saved);
    await clickButton('Edit 语言翻译');
    expect(element<HTMLInputElement>('#settings-prompt-title').value).toBe(translationPrompt.title);
    expect(element<HTMLTextAreaElement>('#settings-prompt-content').value).toBe(
      translationPrompt.content,
    );
    fill('#settings-prompt-title', ' Revised translation ');
    fill('#settings-prompt-content', ' Translate into English. ');

    await submit();

    expect(saved).toHaveBeenCalledExactlyOnceWith({
      id: 'translator',
      title: 'Revised translation',
      content: 'Translate into English.',
    });
    expect(translationPrompt.title).toBe('语言翻译');
    expect(translationPrompt.content).toBe('Translate the user message.');
    expect(element('[role="status"]').textContent).toContain('语言翻译');
  });

  it('opens the editor on a row double-click, including while prompt selection is locked', async () => {
    await render(translationPrompt, true);
    const row = element<HTMLInputElement>('input[value="assistant"]').closest('li');
    row?.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(element('#settings-editor-title').textContent?.trim()).toBe('Edit system prompt');
    expect(element<HTMLInputElement>('#settings-prompt-title').value).toBe(assistantPrompt.title);
  });

  it('emits deletion while locked and discards an editor for that entry without changing selection', async () => {
    await render(translationPrompt, true);
    const deleted = vi.fn();
    const selected = vi.fn();
    const saved = vi.fn();
    fixture.componentInstance.promptDeleted.subscribe(deleted);
    fixture.componentInstance.promptSelected.subscribe(selected);
    fixture.componentInstance.promptSaved.subscribe(saved);
    await clickButton('Edit 语言翻译');

    await clickButton('Delete 语言翻译');

    expect(deleted).toHaveBeenCalledExactlyOnceWith('translator');
    expect(selected).not.toHaveBeenCalled();
    expect(saved).not.toHaveBeenCalled();
    expect((fixture.nativeElement as HTMLElement).querySelector('form')).toBeNull();
    expect(fixture.componentInstance.prompts()).toBe(catalogue);
    expect(fixture.componentInstance.selectedPrompt()).toBe(translationPrompt);
    expect(element('[role="status"]').textContent).toContain('语言翻译');
  });
});
