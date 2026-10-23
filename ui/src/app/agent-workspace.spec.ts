import { computed, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fixtureData from './agent-contract-v1.fixture.json';
import { AgentApi } from './agent-api';
import { AgentState } from './agent-state';
import { AgentWorkspace } from './agent-workspace';
import { settled, type ArtifactPage } from './agent.models';
import {
  workspaceFixture as parseAgentSession,
  type WorkspaceFixture as AgentSession,
} from './agent-legacy.testing';
import { FETCH } from './chat-api';

const recorded = fixtureData.streams.find((entry) => entry.name === 'fetch_execution')!;
function fetchedSession(): AgentSession {
  return parseAgentSession(structuredClone(recorded.terminal));
}
function continuation(): ArtifactPage {
  return structuredClone(recorded.artifact_page!.body);
}
function deferredResponse() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

/** Only component inputs/state are mocked. All evidence HTTP uses the actual AgentApi. */
class WorkspaceState {
  readonly sessions = signal<AgentSession[]>([]);
  readonly selectedId = signal<string | null>(null);
  readonly activeSession = computed(
    () => this.sessions().find((s) => s.id === this.selectedId()) ?? null,
  );
  readonly activeRun = computed(
    () => this.activeSession()?.runs.find((run) => !settled(run)) ?? null,
  );
  readonly visibleRuns = computed(() => this.activeSession()?.runs ?? []);
  readonly activeControl = computed(() => this.activeSession()?.current_run ?? null);
  readonly summaries = signal([]);
  readonly nextRunsBefore = signal(null);
  readonly currentView = computed(() =>
    this.visibleRuns()[0] ? { body: this.visibleRuns()[0] } : null,
  );
  readonly openedView = signal(null);
  readonly pageLoading = signal(new Set<string>());
  readonly pageErrors = signal(new Map<string, string>());
  readonly invalidating = signal(new Set<string>());
  readonly loadRuns = vi.fn();
  readonly openRun = vi.fn();
  readonly closeRun = vi.fn();
  readonly loadResults = vi.fn();
  readonly windowKind = vi.fn(() => 'recent');
  readonly canApprove = vi.fn(() => true);
  readonly stopping = signal(new Set<string>());
  readonly archivePending = signal(new Set<string>());
  readonly busy = signal(new Set<string>());
  readonly globalError = signal<string | null>(null);
  readonly sessionError = signal<string | null>(null);
  readonly loading = signal(false);
  readonly draft = signal('');
  readonly canRequest = signal(false);
  readonly feedback = vi.fn(() => '');
  readonly request = vi.fn();
  readonly approve = vi.fn();
  readonly reject = vi.fn();
  readonly revise = vi.fn();
  readonly load = vi.fn();
  readonly stop = vi.fn();
  readonly updateDraft = vi.fn();
  readonly updateFeedback = vi.fn();
}

describe('safe fetched evidence workspace', () => {
  let fixture: ComponentFixture<AgentWorkspace>;
  let state: WorkspaceState;
  let fetchDouble: ReturnType<typeof vi.fn<typeof fetch>>;
  beforeEach(() => {
    state = new WorkspaceState();
    fetchDouble = vi.fn<typeof fetch>();
    TestBed.configureTestingModule({
      imports: [AgentWorkspace],
      providers: [
        { provide: AgentState, useValue: state },
        { provide: FETCH, useValue: fetchDouble },
      ],
    });
  });
  afterEach(() => TestBed.resetTestingModule());
  function root(): HTMLElement {
    return fixture.nativeElement;
  }
  function button(label: string, within: ParentNode = root()): HTMLButtonElement {
    const found = [...within.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) throw new Error(`Missing button ${label}`);
    return found;
  }
  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }
  async function render(session = fetchedSession()): Promise<void> {
    const { runs, ...header } = session;
    fetchDouble.mockResolvedValueOnce(Response.json(header));
    // History reload goes through the strict UI decoder, without a real store or network.
    const reloaded = await TestBed.inject(AgentApi).get(session.id, new AbortController().signal);
    state.sessions.set([{ ...reloaded, runs }]);
    state.selectedId.set(reloaded.id);
    fixture = TestBed.createComponent(AgentWorkspace);
    await settle();
  }

  it('reviews the whole exact fetch scope, with quota-free presentation', async () => {
    await render(parseAgentSession(structuredClone(recorded.baseline)));
    const plan = root().querySelector('[aria-label="Reviewed plan"]')!;
    expect(plan.textContent).toContain('Fetch URL: https://example.com/public');
    expect(plan.textContent).toContain('offset 0 (zero-based)');
    expect(plan.textContent).toContain('18 characters');
    expect(
      JSON.parse(plan.querySelector('[aria-label="Exact tool arguments"] pre')!.textContent!),
    ).toEqual({
      url: 'https://example.com/public',
      offset: 0,
      limit: 18,
    });
    expect(plan.textContent).not.toContain('Workspace:');
    expect(plan.textContent).not.toContain('Lines');
    expect(plan.textContent).not.toContain('tool calls');
    expect(root().textContent).toContain('Search and grep are unavailable');
    expect(root().textContent).toContain('determined by the BFF');
    expect(root().textContent).toContain('not a complete LLM audit');
    const file = parseAgentSession(
      structuredClone(fixtureData.streams.find((entry) => entry.name === 'execution')!.baseline),
    );
    state.sessions.set([file]);
    state.selectedId.set(file.id);
    await settle();
    const filePlan = root().querySelector('[aria-label="Reviewed plan"]')!;
    expect(filePlan.textContent).toContain('Workspace: public');
    expect(filePlan.textContent).toContain('public.txt');
    expect(filePlan.textContent).toContain('Lines 1–2');
  });

  it('renders untrusted title and extracted source literally, with only safe provenance links', async () => {
    const session = fetchedSession();
    const result = session.runs[0].results[0];
    const source =
      '<img src=x onerror="alert(1)"> **untrusted** [click](javascript:alert(1)) <script>bad()</script>';
    result.content = source;
    result.fetch!.title = '<svg onload="alert(1)">Untrusted title</svg>';
    result.fetch!.returned_chars = Array.from(source).length;
    result.fetch!.total_chars = result.fetch!.returned_chars;
    result.fetch!.next_offset = null;
    const step = session.runs[0].plan!.steps[0];
    step.arguments.limit = result.fetch!.returned_chars;
    await render(session);
    const card = root().querySelector('[aria-label="Step result"]')!;
    expect(card.textContent).toContain('Fetched page');
    expect(card.querySelector('h4')!.textContent).toBe(result.fetch!.title);
    expect(card.querySelector('.source-text')!.textContent).toBe(source);
    expect(card.querySelector('img, script, svg, strong, [onload], [onerror]')).toBeNull();
    expect(card.textContent).toContain('2026-10-16T09:00:00.123456Z');
    expect(card.textContent).not.toContain('Recorded file excerpt');
    const links = [...card.querySelectorAll('a')];
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      'https://example.com/public',
      'https://example.com/public-text',
    ]);
    expect(
      links.every((link) => link.rel === 'noopener noreferrer' && link.target === '_blank'),
    ).toBe(true);
    // Defense in depth even if a future caller bypasses the decoder.
    const unsafe = structuredClone(session);
    unsafe.runs[0].results[0].fetch!.requested_url = 'javascript:alert(1)';
    unsafe.runs[0].results[0].fetch!.final_url = 'https://user:pass@example.com/';
    state.sessions.set([unsafe]);
    await settle();
    expect(root().querySelector('[aria-label="Step result"] a')).toBeNull();
  });

  it('renders every description and the complete exact plan before approval beyond old step/revision ceilings', async () => {
    const session = parseAgentSession(structuredClone(recorded.baseline));
    const run = session.runs[0];
    run.plan!.revision = 25;
    delete run.plan!.limits;
    run.plan!.steps = Array.from({ length: 12 }, (_, index) => ({
      ...run.plan!.steps[0],
      id: `step-${index}`,
      description: `Exact scope ${index}`,
    }));
    session.current_run!.plan_identity!.revision = 25;
    await render(session);
    const reviewed = root().querySelector('[aria-label="Reviewed plan"]')!;
    for (let index = 0; index < 12; index++)
      expect(reviewed.textContent).toContain(`Exact scope ${index}`);
    expect(reviewed.querySelectorAll('ol > li')).toHaveLength(12);
    expect(reviewed.querySelectorAll('details')).toHaveLength(0);
    expect(
      JSON.parse(reviewed.querySelector('[aria-label="Exact recorded plan"] pre')!.textContent!),
    ).toEqual(run.plan);
    expect(reviewed.textContent).not.toContain('Limits:');
    expect(button('Approve and run').disabled).toBe(false);
  });

  it('loads the generated continuation once via GET and makes full retained text available again after reload', async () => {
    await render();
    expect(root().textContent).toContain('Execution preview: offset 0');
    expect(root().textContent).toContain('18 of 52 retained characters');
    expect(root().textContent).toContain('only part of the retained text');
    const pending = deferredResponse();
    fetchDouble.mockReturnValueOnce(pending.promise);
    const next = button('Next retained page');
    next.click();
    next.click();
    await settle();
    expect(fetchDouble).toHaveBeenCalledTimes(2);
    expect(next.disabled).toBe(true);
    expect(root().textContent).toContain('Loading retained evidence');
    pending.resolve(Response.json(continuation()));
    await vi.waitFor(() =>
      expect(fixture.componentInstance.evidence().values().next().value?.loading).toBe(false),
    );
    await settle();
    expect(root().querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      continuation().content,
    );
    expect(root().textContent).toContain('End of retained text');
    expect(root().querySelector('[aria-label="Fetched source preview"]')!.textContent).toBe(
      'Public evidence 🌱\n',
    );
    expect(fetchDouble.mock.calls[1][0]).toBe(recorded.artifact_page!.http.url);
    fixture.destroy();
    await render();
    expect(root().querySelector('[aria-label="Retained evidence page"]')).toBeNull();
    fetchDouble.mockResolvedValueOnce(Response.json(continuation()));
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect(fixture.componentInstance.evidence().values().next().value?.loading).toBe(false),
    );
    await settle();
    expect(root().textContent).toContain(continuation().content);
    expect(
      fetchDouble.mock.calls.every(([, options]) => options?.method === 'GET' && !options.body),
    ).toBe(true);
    expect(state.approve).not.toHaveBeenCalled();
    expect(state.request).not.toHaveBeenCalled();
  });

  it('can inspect from the beginning when the execution preview started later in the artifact', async () => {
    const session = fetchedSession();
    const result = session.runs[0].results[0];
    const step = session.runs[0].plan!.steps[0];
    if (step.tool !== 'web_fetch') throw new Error('Expected fetch');
    step.arguments.offset = 18;
    step.arguments.limit = 12000;
    result.content = continuation().content;
    Object.assign(result.fetch!, { offset: 18, returned_chars: 34, next_offset: null });
    await render(session);
    expect(root().textContent).toContain('only part of the retained text');
    const full = {
      ...continuation(),
      content: 'Public evidence 🌱\n' + continuation().content,
      offset: 0,
      returned_chars: 52,
    };
    fetchDouble.mockResolvedValueOnce(Response.json(full));
    button('Inspect retained text from start').click();
    await vi.waitFor(() =>
      expect(fixture.componentInstance.evidence().values().next().value?.loading).toBe(false),
    );
    await settle();
    expect(String(fetchDouble.mock.calls[1][0]).endsWith('?offset=0&limit=12000')).toBe(true);
    expect(root().querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      full.content,
    );
  });

  it.each(['session', 'run', 'artifact', 'unmount'] as const)(
    'aborts and ignores late page success on %s change',
    async (change) => {
      await render();
      const pending = deferredResponse();
      fetchDouble.mockReturnValueOnce(pending.promise);
      button('Next retained page').click();
      const signal = fetchDouble.mock.calls[1][1]!.signal!;
      if (change === 'unmount') fixture.destroy();
      else {
        const replacement = fetchedSession();
        if (change === 'session') replacement.id = 'another-session';
        if (change === 'run') replacement.runs[0].id = 'another-run';
        if (change === 'artifact')
          replacement.runs[0].results[0].fetch!.artifact_id = 'f'.repeat(32);
        state.sessions.set([replacement]);
        state.selectedId.set(replacement.id);
        await settle();
      }
      expect(signal.aborted).toBe(true);
      pending.resolve(Response.json(continuation()));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      if (change !== 'unmount') {
        await settle();
        expect(root().querySelector('[aria-label="Retained evidence page"]')).toBeNull();
        expect(root().textContent).not.toContain('Loading retained evidence');
        expect(root().querySelector('[role="alert"]')).toBeNull();
      }
      expect(fetchDouble).toHaveBeenCalledTimes(2);
    },
  );

  it.each(['session', 'unmount'] as const)(
    'ignores late page errors after %s change',
    async (change) => {
      await render();
      const pending = deferredResponse();
      fetchDouble.mockReturnValueOnce(pending.promise);
      const result = state.activeSession()!.runs[0].results[0];
      const work = fixture.componentInstance.readEvidence(
        state.selectedId()!,
        state.activeSession()!.runs[0].id,
        result,
        18,
      );
      if (change === 'unmount') fixture.destroy();
      else {
        state.selectedId.set(null);
        await settle();
      }
      pending.reject(new Error('late private error'));
      await work;
      expect(
        [...fixture.componentInstance.evidence().values()].every((view) => view.error === null),
      ).toBe(true);
    },
  );

  it('keeps evidence results, loading, and retryable errors scoped per run/step/artifact', async () => {
    const session = fetchedSession();
    const other = structuredClone(session.runs[0]);
    other.id = 'other-run';
    other.results[0].fetch!.artifact_id = 'f'.repeat(32);
    session.runs.push(other);
    await render(session);
    const cards = root().querySelectorAll('[aria-label="Step result"]');
    const pending = deferredResponse();
    fetchDouble.mockReturnValueOnce(pending.promise);
    fetchDouble.mockResolvedValueOnce(
      Response.json({ error: { message: 'Evidence unavailable' } }, { status: 503 }),
    );
    button('Next retained page', cards[0]).click();
    button('Next retained page', cards[1]).click();
    await vi.waitFor(() =>
      expect([...fixture.componentInstance.evidence().values()].some((view) => view.error)).toBe(
        true,
      ),
    );
    await settle();
    expect(cards[0].textContent).toContain('Loading retained evidence');
    expect(cards[0].querySelector('[role="alert"]')).toBeNull();
    expect(cards[1].textContent).toContain('Evidence unavailable');
    expect(button('Next retained page', cards[1]).disabled).toBe(false);
    pending.resolve(Response.json(continuation()));
    await vi.waitFor(() =>
      expect(
        [...fixture.componentInstance.evidence().values()].every((view) => !view.loading),
      ).toBe(true),
    );
    fetchDouble.mockResolvedValueOnce(
      Response.json({ ...continuation(), artifact_id: 'f'.repeat(32), content: 'x'.repeat(34) }),
    );
    button('Next retained page', cards[1]).click();
    await vi.waitFor(() =>
      expect(
        [...fixture.componentInstance.evidence().values()].every(
          (view) => !view.loading && !view.error,
        ),
      ).toBe(true),
    );
    await settle();
    expect(cards[0].querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      continuation().content,
    );
    expect(cards[1].querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      'x'.repeat(34),
    );
    expect(fetchDouble.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
  });

  it('rejects changed artifact totals without replacing the execution preview', async () => {
    await render();
    const page = {
      ...continuation(),
      content: 'x'.repeat(35),
      returned_chars: 35,
      total_chars: 53,
      next_offset: null,
    };
    const read = vi.spyOn(TestBed.inject(AgentApi), 'artifactPage');
    fetchDouble.mockResolvedValueOnce(Response.json(page));
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect([...fixture.componentInstance.evidence().values()][0]?.error).toContain(
        'Invalid Agent',
      ),
    );
    await settle();
    expect(read).toHaveBeenCalledOnce();
    await expect(read.mock.results[0].value).resolves.toEqual(page);
    const card = root().querySelector('[aria-label="Step result"]')!;
    expect(card.querySelector('[role="alert"]')!.textContent).toContain('Invalid Agent');
    expect(state.sessionError()).toBeNull();
    expect(state.globalError()).toBeNull();
    expect(card.querySelector('[aria-label="Retained evidence page"]')).toBeNull();
    expect(card.querySelector('[aria-label="Fetched source preview"]')!.textContent).toBe(
      'Public evidence 🌱\n',
    );
    expect(fetchDouble.mock.calls.map(([, options]) => options?.method)).toEqual(['GET', 'GET']);
  });

  it('advances retained pages sequentially, preserves a page on error, and renders remote markup literally', async () => {
    const session = fetchedSession();
    const result = session.runs[0].results[0];
    const tail = '<img src=x onerror="bad()"> [link](javascript:bad())';
    const total = 18 + 12000 + Array.from(tail).length;
    result.fetch!.total_chars = total;
    await render(session);
    const firstPage: ArtifactPage = {
      artifact_id: result.fetch!.artifact_id,
      content: '🌱'.repeat(12000),
      offset: 18,
      returned_chars: 12000,
      total_chars: total,
      next_offset: 12018,
    };
    fetchDouble.mockResolvedValueOnce(Response.json(firstPage));
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect([...fixture.componentInstance.evidence().values()][0]?.loading).toBe(false),
    );
    await settle();
    fetchDouble.mockRejectedValueOnce(new Error('Read interrupted'));
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect([...fixture.componentInstance.evidence().values()][0]?.error).toBe('Read interrupted'),
    );
    await settle();
    expect(root().querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      firstPage.content,
    );
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        ...firstPage,
        content: tail,
        offset: 12018,
        returned_chars: Array.from(tail).length,
        next_offset: null,
      }),
    );
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect([...fixture.componentInstance.evidence().values()][0]?.loading).toBe(false),
    );
    await settle();
    const retained = root().querySelector('[aria-label="Retained evidence page"]')!;
    expect(retained.querySelector('pre')!.textContent).toBe(tail);
    expect(retained.querySelector('img, a, [onerror]')).toBeNull();
    expect(fetchDouble.mock.calls.slice(1).map(([url]) => String(url).split('?')[1])).toEqual([
      'offset=18&limit=12000',
      'offset=12018&limit=12000',
      'offset=12018&limit=12000',
    ]);
  });

  it('keeps two fetched steps separate from a file containing search snippets', async () => {
    const session = fetchedSession();
    const run = session.runs[0];
    const otherStep = { ...run.plan!.steps[0], id: 'second-fetch' };
    run.plan!.steps.push(otherStep, {
      id: 'file',
      title: 'Read snippets',
      description: 'Recorded search snippets in a file',
      tool: 'read_file',
      arguments: { workspace_id: 'public', path: 'snippets.txt', offset: 1, limit: 2 },
    });
    run.results.push(
      {
        ...structuredClone(run.results[0]),
        step_id: 'second-fetch',
        fetch: { ...run.results[0].fetch!, artifact_id: 'f'.repeat(32) },
      },
      {
        step_id: 'file',
        status: 'completed',
        content: 'Search snippets: https://example.com/public',
        time: run.updated_at,
        path: 'snippets.txt',
        offset: 1,
        returned_lines: 1,
      },
    );
    await render(session);
    const cards = root().querySelectorAll('[aria-label="Step result"]');
    expect(cards).toHaveLength(3);
    expect(cards[2].textContent).toContain('Recorded file excerpt');
    expect(cards[2].querySelector('[aria-label="Fetched page provenance"]')).toBeNull();
    fetchDouble.mockResolvedValueOnce(Response.json(continuation()));
    button('Next retained page', cards[0]).click();
    await vi.waitFor(() =>
      expect([...fixture.componentInstance.evidence().values()][0]?.loading).toBe(false),
    );
    await settle();
    expect(cards[0].querySelector('[aria-label="Retained evidence page"]')).not.toBeNull();
    expect(cards[1].querySelector('[aria-label="Retained evidence page"]')).toBeNull();
  });
});
