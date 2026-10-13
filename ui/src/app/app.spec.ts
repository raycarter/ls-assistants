import {
  ComponentFixture,
  DeferBlockBehavior,
  DeferBlockState,
  TestBed,
} from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './app';
import { ChatState } from './chat-state';

describe('App', () => {
  let fixture: ComponentFixture<App>;
  let state: ChatState;
  let showModal: ReturnType<typeof vi.fn>;
  let close: ReturnType<typeof vi.fn>;
  let originalShowModal: PropertyDescriptor | undefined;
  let originalClose: PropertyDescriptor | undefined;
  let originalInnerWidth: PropertyDescriptor | undefined;

  beforeEach(async () => {
    originalInnerWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth');
    setViewportWidth(1024);
    // jsdom lacks native dialog methods; restore the original descriptors after each test.
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

    await renderApp();
  });

  afterEach(() => {
    // Destroy any open Settings dialog while the native-method stubs are still installed.
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
    if (originalInnerWidth) {
      Object.defineProperty(window, 'innerWidth', originalInnerWidth);
    } else {
      Reflect.deleteProperty(window, 'innerWidth');
    }
  });

  function setViewportWidth(width: number): void {
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      writable: true,
      value: width,
    });
  }

  async function renderApp(deferBlockBehavior = DeferBlockBehavior.Playthrough): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [App],
      deferBlockBehavior,
    }).compileComponents();
    fixture = TestBed.createComponent(App);
    state = TestBed.inject(ChatState);
    await settle();
  }

  function root(): HTMLElement {
    return fixture.nativeElement as HTMLElement;
  }

  function element<T extends HTMLElement>(selector: string, scope: ParentNode = root()): T {
    const found = scope.querySelector<T>(selector);
    if (!found) throw new Error(`Missing element: ${selector}`);
    return found;
  }

  function elements<T extends HTMLElement>(selector: string, scope: ParentNode = root()): T[] {
    return Array.from(scope.querySelectorAll<T>(selector));
  }

  function button(label: string, scope: ParentNode = root()): HTMLButtonElement {
    const found = elements<HTMLButtonElement>('button', scope).find(
      (candidate) =>
        candidate.getAttribute('aria-label') === label ||
        candidate.getAttribute('title') === label ||
        candidate.textContent?.replace(/\s+/g, ' ').trim() === label,
    );
    if (!found) throw new Error(`Missing button: ${label}`);
    return found;
  }

  function historyRows(): HTMLLIElement[] {
    return elements<HTMLLIElement>('section[aria-labelledby="history-heading"] li');
  }

  function sessionRow(id: string): HTMLLIElement {
    const index = state.sessions().findIndex((session) => session.id === id);
    const row = historyRows()[index];
    if (index < 0 || !row) throw new Error(`Missing session row: ${id}`);
    return row;
  }

  function sessionButton(id: string): HTMLButtonElement {
    return element<HTMLButtonElement>('button[title]', sessionRow(id));
  }

  function deleteSessionButton(id: string): HTMLButtonElement {
    return element<HTMLButtonElement>('button[aria-label]', sessionRow(id));
  }

  function promptSection(): HTMLElement {
    return element('section[aria-labelledby="prompts-heading"]');
  }

  function promptButtons(): HTMLButtonElement[] {
    return elements<HTMLButtonElement>('button', promptSection());
  }

  function promptButton(id: string | null): HTMLButtonElement {
    if (id === null) return element<HTMLButtonElement>('li:first-child button', promptSection());
    const prompt = state.prompts().find((entry) => entry.id === id);
    if (!prompt) throw new Error(`Missing prompt: ${id}`);
    return button(prompt.title, promptSection());
  }

  function expectSelectedPrompt(id: string | null): void {
    const selected = promptButton(id);
    expect(selected.getAttribute('aria-pressed')).toBe('true');
    expect(selected.querySelector('[aria-hidden="true"]')?.textContent?.trim()).toBe('✓');
    expect(elements('button[aria-pressed="true"]', promptSection())).toEqual([selected]);
    expect(elements('[aria-hidden="true"]', promptSection())).toHaveLength(1);
  }

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function click(target: HTMLElement): Promise<void> {
    target.click();
    await settle();
  }

  async function clickButton(label: string, scope: ParentNode = root()): Promise<void> {
    await click(button(label, scope));
  }

  async function fill(selector: string, value: string): Promise<void> {
    const field = element<HTMLInputElement | HTMLTextAreaElement>(selector);
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    await settle();
  }

  async function keydown(target: EventTarget, options: KeyboardEventInit): Promise<KeyboardEvent> {
    const event = new KeyboardEvent('keydown', {
      bubbles: true,
      cancelable: true,
      ...options,
    });
    target.dispatchEvent(event);
    await settle();
    return event;
  }

  async function composerKey(options: KeyboardEventInit = {}): Promise<KeyboardEvent> {
    return keydown(element('#message-input'), { key: 'Enter', ...options });
  }

  async function openSettings(): Promise<HTMLDialogElement> {
    await clickButton('Settings', element('aside'));
    return element<HTMLDialogElement>('app-settings-dialog dialog');
  }

  function settingsRadio(id: string | null): HTMLInputElement {
    const radio = elements<HTMLInputElement>('dialog input[type="radio"]').find(
      (candidate) => candidate.value === (id ?? ''),
    );
    if (!radio) throw new Error(`Missing Settings radio: ${id}`);
    return radio;
  }

  it('renders an empty conversation with an enabled prompt selection and a disabled Send button', () => {
    expect(element('[role="log"] h1').textContent).toBe('Start a conversation');
    expect(elements('[role="log"] article')).toHaveLength(0);
    expect(element('[aria-label="Current interactive mode"]').textContent).toBe('Chat');
    expect(button('Send').disabled).toBe(true);
    expect(historyRows()).toHaveLength(state.sessions().length);
    expect(sessionButton(state.activeSessionId()).getAttribute('aria-current')).toBe('true');
    expect(promptButtons().every((candidate) => !candidate.disabled)).toBe(true);
    expectSelectedPrompt(null);
  });

  describe('sidebar sessions', () => {
    it('creates and selects sessions through the sidebar while preserving their individual drafts', async () => {
      const initialId = state.activeSessionId();
      const initialCount = state.sessions().length;
      await fill('#message-input', 'First draft\nwith another line');
      await clickButton('Open sidebar');
      expect(fixture.componentInstance.sidebarOpen()).toBe(true);

      await clickButton('New session', element('aside'));
      const newId = state.activeSessionId();

      expect(newId).not.toBe(initialId);
      expect(state.sessions()).toHaveLength(initialCount + 1);
      expect(state.sessions()[0].id).toBe(newId);
      expect(historyRows()).toHaveLength(initialCount + 1);
      expect(sessionButton(newId).getAttribute('aria-current')).toBe('true');
      expect(element<HTMLTextAreaElement>('#message-input').value).toBe('');
      expect(document.activeElement).toBe(element('#message-input'));
      expect(fixture.componentInstance.sidebarOpen()).toBe(false);

      await fill('#message-input', 'Second draft');
      await clickButton('Open sidebar');
      await click(sessionButton(initialId));

      expect(state.activeSessionId()).toBe(initialId);
      expect(element<HTMLTextAreaElement>('#message-input').value).toBe(
        'First draft\nwith another line',
      );
      expect(sessionButton(initialId).getAttribute('aria-current')).toBe('true');
      expect(sessionButton(newId).hasAttribute('aria-current')).toBe(false);
      expect(fixture.componentInstance.sidebarOpen()).toBe(false);

      await click(sessionButton(newId));
      expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Second draft');
      expect(state.sessions().find((session) => session.id === initialId)?.draft).toBe(
        'First draft\nwith another line',
      );
    });

    it('deletes inactive and active sessions through their row buttons and renders the surviving selection', async () => {
      const initialId = state.activeSessionId();
      await clickButton('New session', element('aside'));
      const secondId = state.activeSessionId();
      await clickButton('New session', element('aside'));
      const thirdId = state.activeSessionId();

      await click(deleteSessionButton(secondId));

      expect(state.sessions().some((session) => session.id === secondId)).toBe(false);
      expect(state.activeSessionId()).toBe(thirdId);
      expect(sessionButton(thirdId).getAttribute('aria-current')).toBe('true');

      await click(deleteSessionButton(thirdId));

      expect(state.activeSessionId()).toBe(initialId);
      expect(sessionButton(initialId).getAttribute('aria-current')).toBe('true');
      expect(historyRows()).toHaveLength(state.sessions().length);
    });

    it('retains one fresh session after the final history entry is deleted', async () => {
      const originalId = state.activeSessionId();
      const otherId = state.sessions().find((session) => session.id !== originalId)!.id;
      await click(deleteSessionButton(otherId));
      await click(promptButton(state.prompts()[0].id));
      await fill('#message-input', 'Delete this conversation');
      await clickButton('Send');
      await fill('#message-input', 'Also discard this draft');

      await click(deleteSessionButton(originalId));

      expect(state.sessions()).toHaveLength(1);
      expect(historyRows()).toHaveLength(1);
      expect(state.activeSessionId()).not.toBe(originalId);
      expect(sessionButton(state.activeSessionId()).getAttribute('aria-current')).toBe('true');
      expect(state.activeSession().title).toBe('New session');
      expect(element<HTMLTextAreaElement>('#message-input').value).toBe('');
      expect(elements('[role="log"] article')).toHaveLength(0);
      expect(element('[role="log"] h1').textContent).toBe('Start a conversation');
      expect(promptButtons().every((candidate) => !candidate.disabled)).toBe(true);
      expectSelectedPrompt(null);
    });
  });

  describe('mobile sidebar navigation', () => {
    beforeEach(() => {
      setViewportWidth(375);
    });

    it('focuses New session on opening and makes the conversation inert', async () => {
      const toggle = button('Open sidebar');
      toggle.focus();
      expect(element('main').hasAttribute('inert')).toBe(false);

      await click(toggle);

      expect(document.activeElement).toBe(button('New session', element('aside')));
      expect(fixture.componentInstance.sidebarOpen()).toBe(true);
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(element('aside').classList.contains('hidden')).toBe(false);
      expect(element('main').getAttribute('inert')).toBe('');
      expect(button('Close sidebar')).toBeTruthy();
    });

    it.each([false, true])(
      'wraps Tab among enabled sidebar buttons (prompt locked: %s)',
      async (locked) => {
        if (locked) {
          await fill('#message-input', 'Lock prompt selection');
          await clickButton('Send');
        }
        await clickButton('Open sidebar');
        const enabled = elements<HTMLButtonElement>('button', element('aside')).filter(
          (candidate) => !candidate.disabled,
        );
        const first = enabled[0];
        const last = enabled.at(-1)!;
        expect(first).toBe(button('New session', element('aside')));
        expect(last).toBe(button('Settings', element('aside')));
        expect(promptButtons().every((candidate) => candidate.disabled === locked)).toBe(true);

        const backwards = await keydown(first, { key: 'Tab', shiftKey: true });

        expect(backwards.defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(last);
        const forwards = await keydown(last, { key: 'Tab' });
        expect(forwards.defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(first);

        const interior = enabled[1];
        interior.focus();
        expect((await keydown(interior, { key: 'Tab' })).defaultPrevented).toBe(false);
        expect((await keydown(interior, { key: 'Tab', shiftKey: true })).defaultPrevented).toBe(
          false,
        );
        expect(fixture.componentInstance.sidebarOpen()).toBe(true);
        expect(element('main').hasAttribute('inert')).toBe(true);
      },
    );

    it.each(['Escape', 'backdrop'])(
      'closes on %s, removes inert and returns focus to the mobile toggle',
      async (method) => {
        const toggle = button('Open sidebar');
        await click(toggle);

        if (method === 'Escape') {
          const event = await keydown(document.activeElement!, { key: 'Escape' });
          expect(event.defaultPrevented).toBe(true);
        } else {
          await clickButton('Close sidebar');
        }

        expect(fixture.componentInstance.sidebarOpen()).toBe(false);
        expect(element('main').hasAttribute('inert')).toBe(false);
        expect(element('aside').classList.contains('hidden')).toBe(true);
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        expect(root().querySelector('button[aria-label="Close sidebar"]')).toBeNull();
        expect(document.activeElement).toBe(toggle);
      },
    );

    it('does not intercept Tab or Escape while the sidebar is closed', async () => {
      const composer = element<HTMLTextAreaElement>('#message-input');
      composer.focus();

      for (const key of ['Tab', 'Escape']) {
        expect((await keydown(composer, { key })).defaultPrevented).toBe(false);
      }

      expect(fixture.componentInstance.sidebarOpen()).toBe(false);
      expect(element('main').hasAttribute('inert')).toBe(false);
      expect(document.activeElement).toBe(composer);
    });

    it.each(['select', 'create'])(
      'closes the drawer and focuses the rendered composer on session %s',
      async (action) => {
        const initialId = state.activeSessionId();
        await fill('#message-input', 'Retained draft');
        await clickButton('New session', element('aside'));
        const previousId = state.activeSessionId();
        const previousCount = state.sessions().length;
        await clickButton('Open sidebar');

        if (action === 'select') {
          await click(sessionButton(initialId));
          expect(state.activeSessionId()).toBe(initialId);
          expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Retained draft');
        } else {
          await clickButton('New session', element('aside'));
          expect(state.activeSessionId()).not.toBe(previousId);
          expect(state.sessions()).toHaveLength(previousCount + 1);
          expect(element<HTMLTextAreaElement>('#message-input').value).toBe('');
        }

        expect(fixture.componentInstance.sidebarOpen()).toBe(false);
        expect(element('main').hasAttribute('inert')).toBe(false);
        expect(button('Open sidebar').getAttribute('aria-expanded')).toBe('false');
        expect(document.activeElement).toBe(element('#message-input'));
      },
    );

    it('keeps focus in the drawer when a focused session delete button is removed', async () => {
      await clickButton('Open sidebar');

      for (const session of [...state.sessions()]) {
        const remove = deleteSessionButton(session.id);
        remove.focus();
        await click(remove);

        expect(document.activeElement).toBe(button('New session', element('aside')));
        expect(fixture.componentInstance.sidebarOpen()).toBe(true);
        expect(element('main').hasAttribute('inert')).toBe(true);
      }

      expect(state.sessions()).toHaveLength(1);
      expect(historyRows()).toHaveLength(1);
    });

    it('keeps the drawer open on mobile resize and closes it at the desktop breakpoint', async () => {
      await clickButton('Open sidebar');
      setViewportWidth(767);
      window.dispatchEvent(new Event('resize'));
      await settle();

      expect(fixture.componentInstance.sidebarOpen()).toBe(true);
      expect(element('main').hasAttribute('inert')).toBe(true);
      expect(document.activeElement).toBe(button('New session', element('aside')));

      setViewportWidth(768);
      window.dispatchEvent(new Event('resize'));
      await settle();

      expect(fixture.componentInstance.sidebarOpen()).toBe(false);
      expect(element('main').hasAttribute('inert')).toBe(false);
      expect(button('Open sidebar').getAttribute('aria-expanded')).toBe('false');
      expect(root().querySelector('button[aria-label="Close sidebar"]')).toBeNull();
      expect(document.activeElement).toBe(element('#message-input'));
    });
  });

  describe('composer submission', () => {
    it.each(['Send click', 'Enter'])(
      '%s appends exactly one user message, clears the draft and sets the first-message title',
      async (method) => {
        await fill('#message-input', ' \n First  message.\nSecond line \n ');
        expect(button('Send').disabled).toBe(false);

        if (method === 'Send click') {
          await clickButton('Send');
        } else {
          const event = await composerKey();
          expect(event.defaultPrevented).toBe(true);
        }

        expect(state.activeSession().messages).toEqual([
          { id: expect.any(String), role: 'user', content: 'First  message.\nSecond line' },
        ]);
        expect(state.activeSession().draft).toBe('');
        expect(state.activeSession().title).toBe('First message. Second line');
        expect(sessionButton(state.activeSessionId()).title).toBe('First message. Second line');
        expect(element<HTMLTextAreaElement>('#message-input').value).toBe('');
        expect(button('Send').disabled).toBe(true);
        expect(document.activeElement).toBe(element('#message-input'));
        expect(elements('[role="log"] article')).toHaveLength(1);
        expect(element('[role="log"] article > p').textContent?.trim()).toBe('User');
      },
    );

    it('preserves the first-message title and appends only one more user message on a later send', async () => {
      await fill('#message-input', 'First title');
      await clickButton('Send');
      const firstMessage = state.activeSession().messages[0];
      await fill('#message-input', 'Second message');

      await composerKey();

      expect(state.activeSession().title).toBe('First title');
      expect(sessionButton(state.activeSessionId()).title).toBe('First title');
      expect(state.activeSession().messages).toHaveLength(2);
      expect(state.activeSession().messages[0]).toBe(firstMessage);
      expect(state.activeSession().messages[1]).toMatchObject({
        role: 'user',
        content: 'Second message',
      });
      expect(state.activeSession().messages.map((message) => message.role)).toEqual([
        'user',
        'user',
      ]);
      expect(
        elements('[role="log"] article > p').map((label) => label.textContent?.trim()),
      ).toEqual(['User', 'User']);
    });

    it.each(['', ' \n\t '])('blocks empty or whitespace-only submissions: %j', async (draft) => {
      await fill('#message-input', draft);
      const session = state.activeSession();
      expect(button('Send').disabled).toBe(true);

      await clickButton('Send');
      await composerKey();

      expect(state.activeSession()).toBe(session);
      expect(state.activeSession().messages).toEqual([]);
      expect(state.activeSession().promptLocked).toBe(false);
      expect(state.activeSession().title).toBe('New session');
      expect(element<HTMLTextAreaElement>('#message-input').value).toBe(draft);
      expect(elements('[role="log"] article')).toHaveLength(0);
    });

    it.each([
      ['Shift+Enter', { shiftKey: true }],
      ['composing Enter', { isComposing: true }],
      ['IME keyCode 229', { keyCode: 229 }],
      ['a non-Enter key', { key: 'a' }],
    ] as const)('does not submit or prevent default for %s', async (_label, options) => {
      await fill('#message-input', 'Keep this draft');
      const session = state.activeSession();

      const event = await composerKey(options);

      expect(event.defaultPrevented).toBe(false);
      expect(state.activeSession()).toBe(session);
      expect(state.activeSession().messages).toEqual([]);
      expect(state.activeSession().promptLocked).toBe(false);
      expect(element<HTMLTextAreaElement>('#message-input').value).toBe('Keep this draft');
    });
  });

  describe('sidebar prompt selection', () => {
    it('moves the selection checkmark between prompts and No system prompt', async () => {
      const first = state.prompts()[0];
      const second = state.prompts()[1];
      expectSelectedPrompt(null);

      await click(promptButton(first.id));
      expect(state.activeSession().prompt).toEqual(first);
      expectSelectedPrompt(first.id);
      expect(promptButton(null).getAttribute('aria-pressed')).toBe('false');

      await click(promptButton(second.id));
      expect(state.activeSession().prompt).toEqual(second);
      expectSelectedPrompt(second.id);
      expect(promptButton(first.id).querySelector('[aria-hidden="true"]')).toBeNull();

      await click(promptButton(null));
      expect(state.activeSession().prompt).toBeNull();
      expectSelectedPrompt(null);
    });

    it.each([true, false])(
      'locks the choice per session after the first message (selected prompt: %s)',
      async (selected) => {
        const first = state.prompts()[0];
        const second = state.prompts()[1];
        const lockedId = state.activeSessionId();
        if (selected) await click(promptButton(first.id));
        await fill('#message-input', 'Lock this session');
        await clickButton('Send');
        const snapshot = state.activeSession().prompt;

        expect(state.activeSession().promptLocked).toBe(true);
        expect(promptButtons().every((candidate) => candidate.disabled)).toBe(true);
        expect(element('p[title]', promptSection()).textContent?.trim()).toBe(
          `Locked: ${selected ? first.title : 'No system prompt'}`,
        );
        expectSelectedPrompt(selected ? first.id : null);
        await click(promptButton(second.id));
        await click(promptButton(null));
        expect(state.activeSession().prompt).toBe(snapshot);

        await clickButton('New session', element('aside'));
        const unlockedId = state.activeSessionId();
        expect(promptButtons().every((candidate) => !candidate.disabled)).toBe(true);
        expectSelectedPrompt(null);
        await click(promptButton(second.id));
        expectSelectedPrompt(second.id);

        await click(sessionButton(lockedId));
        expect(state.activeSession().prompt).toBe(snapshot);
        expect(promptButtons().every((candidate) => candidate.disabled)).toBe(true);
        expectSelectedPrompt(selected ? first.id : null);

        await click(sessionButton(unlockedId));
        expect(state.activeSession().promptLocked).toBe(false);
        expectSelectedPrompt(second.id);
      },
    );
  });

  describe('Settings parent wiring', () => {
    it.each([
      { viewport: 'mobile', width: 375, method: 'close button' },
      { viewport: 'mobile', width: 375, method: 'Escape' },
      { viewport: 'desktop', width: 1024, method: 'close button' },
      { viewport: 'desktop', width: 1024, method: 'Escape' },
    ])(
      'returns focus to the visible $viewport control after Settings closes via $method',
      async ({ width, method }) => {
        setViewportWidth(width);
        if (width < 768) await clickButton('Open sidebar');
        const dialog = await openSettings();
        const closeButton = button('Close settings', dialog);
        closeButton.focus();
        expect(fixture.componentInstance.sidebarOpen()).toBe(false);
        expect(element('main').hasAttribute('inert')).toBe(false);

        if (method === 'Escape') {
          const event = new Event('cancel', { cancelable: true });
          dialog.dispatchEvent(event);
          await settle();
          expect(event.defaultPrevented).toBe(true);
        } else {
          await click(closeButton);
        }

        expect(fixture.componentInstance.settingsOpen()).toBe(false);
        expect(root().querySelector('app-settings-dialog')).toBeNull();
        expect(document.activeElement).toBe(
          width < 768 ? button('Open sidebar') : button('Settings', element('aside')),
        );
      },
    );

    it('opens a native Settings modal, closes it from the child and can reopen it', async () => {
      await clickButton('Open sidebar');
      const dialog = await openSettings();

      expect(fixture.componentInstance.sidebarOpen()).toBe(false);
      expect(fixture.componentInstance.settingsOpen()).toBe(true);
      expect(dialog.open).toBe(true);
      expect(showModal).toHaveBeenCalledTimes(1);
      expect(settingsRadio(null).checked).toBe(true);
      expect(elements('dialog input[type="radio"]')).toHaveLength(state.prompts().length + 1);

      await clickButton('Close settings', dialog);

      expect(close).toHaveBeenCalledTimes(1);
      expect(dialog.open).toBe(false);
      expect(fixture.componentInstance.settingsOpen()).toBe(false);
      expect(root().querySelector('app-settings-dialog')).toBeNull();

      const reopened = await openSettings();
      expect(reopened).not.toBe(dialog);
      expect(reopened.open).toBe(true);
      expect(showModal).toHaveBeenCalledTimes(2);
    });

    it.each(['cancel', 'close'])(
      'removes Settings when the child handles a native %s event',
      async (eventType) => {
        const dialog = await openSettings();

        if (eventType === 'cancel') {
          const event = new Event('cancel', { cancelable: true });
          dialog.dispatchEvent(event);
          expect(event.defaultPrevented).toBe(true);
        } else {
          dialog.close();
        }
        await settle();

        expect(dialog.open).toBe(false);
        expect(close).toHaveBeenCalledTimes(1);
        expect(fixture.componentInstance.settingsOpen()).toBe(false);
        expect(root().querySelector('app-settings-dialog')).toBeNull();
      },
    );

    it('applies child promptSelected events to the active session and reflects them in both selectors', async () => {
      const prompt = state.prompts()[0];
      await openSettings();

      await click(settingsRadio(prompt.id));

      expect(state.activeSession().prompt).toEqual(prompt);
      expect(settingsRadio(prompt.id).checked).toBe(true);
      expect(settingsRadio(null).checked).toBe(false);
      expect(element('dialog [role="status"]').textContent).toContain(prompt.title);
      expectSelectedPrompt(prompt.id);

      await click(settingsRadio(null));

      expect(state.activeSession().prompt).toBeNull();
      expect(settingsRadio(null).checked).toBe(true);
      expectSelectedPrompt(null);
    });

    it('adds, edits and deletes catalogue entries through child events, updating an unlocked selection', async () => {
      const initialCount = state.prompts().length;
      const dialog = await openSettings();
      await clickButton('Add system prompt', dialog);
      await fill('#settings-prompt-title', '  Custom helper  ');
      await fill('#settings-prompt-content', '\n Original instructions. \n');

      await clickButton('Save prompt', dialog);

      const added = state.prompts().find((prompt) => prompt.title === 'Custom helper')!;
      expect(added).toEqual({
        id: expect.any(String),
        title: 'Custom helper',
        content: 'Original instructions.',
      });
      expect(state.prompts()).toHaveLength(initialCount + 1);
      expect(state.activeSession().prompt).toBeNull();
      expect(settingsRadio(added.id).checked).toBe(false);
      expect(promptButton(added.id).title).toBe('Custom helper');

      await click(settingsRadio(added.id));
      await clickButton('Edit Custom helper', dialog);
      expect(element<HTMLInputElement>('#settings-prompt-title').value).toBe('Custom helper');
      expect(element<HTMLTextAreaElement>('#settings-prompt-content').value).toBe(
        'Original instructions.',
      );
      await fill('#settings-prompt-title', ' Revised helper ');
      await fill('#settings-prompt-content', ' Revised instructions. ');
      await clickButton('Save prompt', dialog);

      expect(state.prompts()).toHaveLength(initialCount + 1);
      expect(state.activeSession().prompt).toEqual({
        id: added.id,
        title: 'Revised helper',
        content: 'Revised instructions.',
      });
      expect(settingsRadio(added.id).checked).toBe(true);
      expect(element('dialog [role="status"]').textContent).toContain('Revised helper');
      expectSelectedPrompt(added.id);

      await clickButton('Delete Revised helper', dialog);

      expect(state.prompts()).toHaveLength(initialCount);
      expect(state.prompts().some((prompt) => prompt.id === added.id)).toBe(false);
      expect(state.activeSession().prompt).toBeNull();
      expect(settingsRadio(null).checked).toBe(true);
      expect(
        elements<HTMLInputElement>('dialog input[type="radio"]').some(
          (radio) => radio.value === added.id,
        ),
      ).toBe(false);
      expectSelectedPrompt(null);
    });

    it('keeps a locked snapshot immutable when Settings edits or deletes its catalogue entry, updating other unlocked sessions', async () => {
      const original = { ...state.prompts()[0] };
      await click(promptButton(original.id));
      await fill('#message-input', 'Lock the original prompt');
      await clickButton('Send');
      const lockedId = state.activeSessionId();
      const snapshot = state.activeSession().prompt;
      await clickButton('New session', element('aside'));
      const unlockedId = state.activeSessionId();
      await click(promptButton(original.id));
      await click(sessionButton(lockedId));
      const dialog = await openSettings();

      expect(
        elements<HTMLInputElement>('dialog input[type="radio"]').every((radio) => radio.disabled),
      ).toBe(true);
      expect(element('dialog [role="status"]').textContent).toContain('Locked for this chat');
      expect(button(`Edit ${original.title}`, dialog).disabled).toBe(false);
      await clickButton(`Edit ${original.title}`, dialog);
      await fill('#settings-prompt-title', 'Edited catalogue title');
      await fill('#settings-prompt-content', 'Edited catalogue instructions.');
      await clickButton('Save prompt', dialog);

      const revised = {
        id: original.id,
        title: 'Edited catalogue title',
        content: 'Edited catalogue instructions.',
      };
      expect(state.prompts().find((prompt) => prompt.id === original.id)).toEqual(revised);
      expect(state.sessions().find((session) => session.id === unlockedId)?.prompt).toEqual(
        revised,
      );
      expect(state.activeSession().prompt).toBe(snapshot);
      expect(state.activeSession().prompt).toEqual(original);
      expect(element('dialog [role="status"]').textContent).toContain(original.title);
      expect(element('p[title]', promptSection()).textContent).toContain(
        `Locked: ${original.title}`,
      );

      await clickButton('Delete Edited catalogue title', dialog);

      expect(state.prompts().some((prompt) => prompt.id === original.id)).toBe(false);
      expect(state.activeSession().prompt).toBe(snapshot);
      expect(state.activeSession().prompt).toEqual(original);
      expect(state.activeSession().promptLocked).toBe(true);
      expect(element('dialog [role="status"]').textContent).toContain(original.title);
      expect(root().querySelector('dialog input[type="radio"]:checked')).toBeNull();
      expect(state.sessions().find((session) => session.id === unlockedId)?.prompt).toBeNull();

      await clickButton('Close settings', dialog);
      await click(sessionButton(unlockedId));
      expect(state.activeSession().promptLocked).toBe(false);
      expect(promptButtons().every((candidate) => !candidate.disabled)).toBe(true);
      expectSelectedPrompt(null);
    });

    it('passes a locked No system prompt snapshot to Settings', async () => {
      await fill('#message-input', 'Continue without a system prompt');
      await clickButton('Send');

      await openSettings();

      expect(settingsRadio(null).checked).toBe(true);
      expect(element('dialog [role="status"]').textContent).toContain('Locked for this chat');
      expect(element('dialog [role="status"]').textContent).toContain('No system prompt');
      expect(
        elements<HTMLInputElement>('dialog input[type="radio"]').every((radio) => radio.disabled),
      ).toBe(true);
      await click(settingsRadio(state.prompts()[0].id));
      expect(state.activeSession().prompt).toBeNull();
    });
  });

  describe('controlled deferred rendering', () => {
    beforeEach(async () => {
      TestBed.resetTestingModule();
      await renderApp(DeferBlockBehavior.Manual);
    });

    it.each([
      { viewport: 'mobile', width: 375 },
      { viewport: 'desktop', width: 1024 },
    ])('dismisses the Settings deferred error and restores $viewport focus', async ({ width }) => {
      setViewportWidth(width);
      const [settingsBlock] = await fixture.getDeferBlocks();
      await settingsBlock.render(DeferBlockState.Error);
      await settle();
      expect(root().querySelector('[role="alert"]')).toBeNull();
      if (width < 768) await clickButton('Open sidebar');

      await clickButton('Settings', element('aside'));

      const error = element('[role="alert"]');
      expect(error.textContent).toContain('Settings could not load');
      expect(fixture.componentInstance.settingsOpen()).toBe(true);
      expect(fixture.componentInstance.sidebarOpen()).toBe(false);
      expect(element('main').hasAttribute('inert')).toBe(false);
      const dismiss = button('Dismiss', error);
      dismiss.focus();

      await click(dismiss);

      expect(fixture.componentInstance.settingsOpen()).toBe(false);
      expect(root().querySelector('[role="alert"]')).toBeNull();
      expect(root().querySelector('app-settings-dialog')).toBeNull();
      expect(document.activeElement).toBe(
        width < 768 ? button('Open sidebar') : button('Settings', element('aside')),
      );
      expect(showModal).not.toHaveBeenCalled();

      await clickButton('Settings', element('aside'));
      expect(element('[role="alert"]').textContent).toContain('Settings could not load');
      await clickButton('Dismiss');
      expect(root().querySelector('[role="alert"]')).toBeNull();
    });

    it.each(['user', 'assistant'] as const)(
      'keeps malicious %s content literal and inert in the Markdown error fallback',
      async (role) => {
        const alert = vi.spyOn(window, 'alert').mockImplementation(() => {});
        const payload = [
          '# Literal heading',
          '**Literal emphasis** and `literal code`',
          '<script>alert(1)</script>',
          '<img src=x onerror="alert(1)">',
          '<svg onload="alert(1)"><circle></circle></svg>',
          '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
          '<a href="javascript:alert(1)" onclick="alert(1)">unsafe</a>',
          '[unsafe](javascript:alert%281%29)',
        ].join('\n\n');
        state.sessions.update((sessions) =>
          sessions.map((session) =>
            session.id === state.activeSessionId()
              ? { ...session, messages: [{ id: 'untrusted-message', role, content: payload }] }
              : session,
          ),
        );
        await settle();
        const [conversationBlock] = await fixture.getDeferBlocks();
        await conversationBlock.render(DeferBlockState.Placeholder);
        await settle();
        expect(element('[role="log"] [role="status"]').textContent).toContain(
          'Loading conversation',
        );
        expect(elements('[role="log"] article')).toHaveLength(0);

        await conversationBlock.render(DeferBlockState.Error);
        await settle();

        const log = element('[role="log"]');
        expect(element('[role="alert"]', log).textContent).toContain('Showing plain text');
        expect(log.querySelector('[role="status"]')).toBeNull();
        const articles = elements('article', log);
        expect(articles).toHaveLength(1);
        expect(element('p:first-child', articles[0]).textContent?.trim()).toBe(
          role === 'user' ? 'User' : 'Assistant',
        );
        const content = element('p:last-child', articles[0]);
        expect(content.textContent?.trim()).toBe(payload);
        expect(content.children).toHaveLength(0);
        expect(
          log.querySelector('script, img, svg, iframe, a, h1, strong, code, [onerror], [onclick]'),
        ).toBeNull();
        expect(alert).not.toHaveBeenCalled();
        expect(state.activeSession().messages[0].content).toBe(payload);
      },
    );

    it('scrolls when deferred message bubbles render and restores the independent empty state', async () => {
      const log = element<HTMLDivElement>('[role="log"]');
      // jsdom has no layout; model a scroll height that grows only after bubbles exist.
      Object.defineProperty(log, 'scrollHeight', {
        configurable: true,
        get: () => log.querySelectorAll('article').length * 240,
      });
      expect(element('h1', log).textContent).toBe('Start a conversation');
      expect(log.querySelector('[role="status"]')).toBeNull();
      expect(await fixture.getDeferBlocks()).toHaveLength(1);
      await fill('#message-input', '# Deferred heading');
      await clickButton('Send');
      const messages = state.activeSession().messages;
      const blocks = await fixture.getDeferBlocks();
      expect(blocks).toHaveLength(2);
      await blocks[0].render(DeferBlockState.Placeholder);
      await settle();
      expect(element('[role="status"]', log).textContent).toContain('Loading conversation');
      expect(log.querySelector('article')).toBeNull();
      expect(log.scrollTop).toBe(0);

      await blocks[0].render(DeferBlockState.Complete);
      await settle();

      expect(state.activeSession().messages).toBe(messages);
      expect(elements('article', log)).toHaveLength(1);
      expect(element('article h1', log).textContent).toBe('Deferred heading');
      expect(log.querySelector('[role="status"]')).toBeNull();
      expect(log.scrollTop).toBe(240);

      await clickButton('New session', element('aside'));

      expect(element('h1', log).textContent).toBe('Start a conversation');
      expect(log.querySelector('article, [role="status"], [role="alert"]')).toBeNull();
      expect(await fixture.getDeferBlocks()).toHaveLength(1);
      expect(log.scrollTop).toBe(0);
    });
  });

  it('aligns user messages only to the end and assistant messages only to the start', async () => {
    const demo = state.sessions().find((session) => session.title === 'Markdown preview (demo)')!;
    await click(sessionButton(demo.id));
    await fill('#message-input', 'Another user message');
    await clickButton('Send');
    const articles = elements('[role="log"] article');

    expect(articles).toHaveLength(3);
    for (const userMessage of [articles[0], articles[2]]) {
      expect(userMessage.classList.contains('items-end')).toBe(true);
      expect(userMessage.classList.contains('items-start')).toBe(false);
    }
    expect(articles[1].classList.contains('items-start')).toBe(true);
    expect(articles[1].classList.contains('items-end')).toBe(false);
  });

  describe('Markdown through Angular bindings', () => {
    it('renders the separate demo conversation with real heading, emphasis and code elements', async () => {
      const demo = state.sessions().find((session) => session.title === 'Markdown preview (demo)')!;

      await click(sessionButton(demo.id));

      expect(elements('[role="log"] article')).toHaveLength(2);
      expect(element('[role="log"] h2').textContent).toBe('Markdown preview');
      expect(
        elements('[role="log"] strong').some((strong) => strong.textContent === 'local demo'),
      ).toBe(true);
      expect(element('[role="log"] li code').textContent).toBe('inline code');
      expect(element('[role="log"] pre > code').textContent).toContain('console.log(message);');
    });

    it('renders sent Markdown as actual heading, strong, inline-code and fenced-code nodes', async () => {
      const source = 'const ready: boolean = true;';
      await fill(
        '#message-input',
        `# A useful heading\n\nA **strong point** with \`inline()\`.\n\n\`\`\`typescript\n${source}\n\`\`\``,
      );

      await clickButton('Send');

      const message = element('[role="log"] article .message-markdown');
      expect(element('h1', message).textContent).toBe('A useful heading');
      expect(element('strong', message).textContent).toBe('strong point');
      expect(element('p > code', message).textContent).toBe('inline()');
      expect(element('pre > code', message).textContent).toBe(`${source}\n`);
    });

    it('keeps malicious raw HTML, event handlers, unsafe URLs and HTML-looking code inert in the bound message', async () => {
      const alert = vi.spyOn(window, 'alert').mockImplementation(() => {});
      const codePayload = '<img src=x onerror="alert(1)"><script>alert(1)</script>';
      const payload = [
        '# Safe heading',
        '**Still readable**',
        '<script>alert(1)</script>',
        '<img src="https://example.test/image.png" onerror="alert(1)">',
        '<strong onclick="alert(1)" style="color:red">Safe emphasis</strong>',
        '[unsafe](javascript:alert%281%29)',
        '<a href="java&#x09;script:alert(1)" onmouseover="alert(1)">unsafe raw link</a>',
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
        '<svg onload="alert(1)"><circle></circle></svg>',
        `\`\`\`html\n${codePayload}\n\`\`\``,
      ].join('\n\n');
      await fill('#message-input', payload);

      await clickButton('Send');

      const message = element('[role="log"] article .message-markdown');
      expect(element('h1', message).textContent).toBe('Safe heading');
      expect(elements('strong', message).map((strong) => strong.textContent)).toEqual([
        'Still readable',
        'Safe emphasis',
      ]);
      expect(
        message.querySelector('script, iframe, svg, object, embed, style, [style], [srcdoc]'),
      ).toBeNull();
      for (const node of elements('*', message)) {
        expect(node.getAttributeNames().some((name) => name.toLowerCase().startsWith('on'))).toBe(
          false,
        );
      }
      const links = elements<HTMLAnchorElement>('a', message);
      expect(links).toHaveLength(2);
      for (const link of links) expect(link.hasAttribute('href')).toBe(false);
      const image = element<HTMLImageElement>('img', message);
      expect(image.getAttribute('src')).toBe('https://example.test/image.png');
      image.dispatchEvent(new Event('error'));
      expect(alert).not.toHaveBeenCalled();
      const code = element('pre > code', message);
      expect(code.textContent).toBe(`${codePayload}\n`);
      expect(code.querySelector('img, script')).toBeNull();
      expect(elements('img', message)).toHaveLength(1);
      expect(state.activeSession().messages[0].content).toBe(payload);
    });
  });
});
