import { computed, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fixtureData from './agent-contract-v1.fixture.json';
import { AgentApi } from './agent-api';
import { AgentState } from './agent-state';
import { AgentWorkspace } from './agent-workspace';
import { parseAgentRun, settled, type ArtifactPage } from './agent.models';
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

  function grepSession(
    source = '{"path":"<img src=x>","line":1,"text":"**literal** 😀"}\n',
  ): AgentSession {
    const session = fetchedSession();
    const run = session.runs[0];
    run.plan!.steps[0] = {
      id: run.results[0].step_id,
      title: 'Grep',
      description: 'Folder evidence',
      tool: 'grep_in_folder',
      arguments: {
        workspace_id: 'public',
        path: '<svg onload=bad()>',
        pattern: '**literal**',
        file_glob: '*.md',
        offset: 0,
        limit: Array.from(source).length,
      },
    };
    const result = run.results[0];
    result.content = source;
    result.fetch = null;
    result.read = null;
    result.path = null;
    result.offset = null;
    result.returned_lines = null;
    result.truncated = true;
    result.grep = {
      artifact_id: 'b'.repeat(32),
      workspace_id: 'public',
      path: '<svg onload=bad()>',
      pattern: '**literal**',
      file_glob: '*.md',
      offset: 0,
      returned_chars: Array.from(source).length,
      total_chars: Array.from(source).length + 4,
      next_offset: Array.from(source).length,
      total_matches: 3,
      files_searched: 4,
      skipped_binary: 1,
      skipped_unreadable: 2,
      complete: false,
    };
    parseAgentRun(run);
    return session;
  }

  it('reviews and renders untrusted search snippets with safe links only and no automatic fetch', async () => {
    const session = fetchedSession();
    const run = session.runs[0];
    run.plan!.steps[0] = {
      id: run.results[0].step_id,
      title: '<img src=x>',
      description: 'Search',
      tool: 'web_search',
      arguments: { query: '  <svg onload=bad()>  ', max_results: 2 },
    };
    Object.assign(run.results[0], {
      content: '',
      fetch: null,
      read: null,
      path: null,
      offset: null,
      returned_lines: null,
      truncated: false,
      search: {
        query: '  <svg onload=bad()>  ',
        max_results: 2,
        results: [
          {
            title: '<script>bad()</script>',
            url: 'https://example.com/a',
            snippet: '<img src=x onerror=bad()> **text**',
            published_date: '2026-01-01',
          },
          { title: 'Unsafe', url: 'javascript:alert(1)', snippet: 'snippet', published_date: null },
        ],
      },
    });
    parseAgentRun(run);
    await render(session);
    const plan = root().querySelector('[aria-label="Reviewed plan"]')!;
    expect(
      JSON.parse(plan.querySelector('[aria-label="Exact tool arguments"] pre')!.textContent!),
    ).toEqual(run.plan!.steps[0].arguments);
    const card = root().querySelector('[aria-label="Web search results"]')!;
    expect(card.textContent).toContain('search results, not fetched pages');
    expect(card.textContent).toContain('<img src=x onerror=bad()> **text**');
    expect(card.textContent).toContain('javascript:alert(1)');
    expect(card.querySelectorAll('a')).toHaveLength(1);
    expect(card.querySelector('a')!.getAttribute('href')).toBe('https://example.com/a');
    expect(card.querySelector('a')!.getAttribute('rel')).toBe('noopener noreferrer');
    expect(card.querySelector('script, img, svg, strong')).toBeNull();
    expect(plan.querySelector('img, svg')).toBeNull();
    expect(fetchDouble).toHaveBeenCalledOnce();
  });

  it('reviews literal grep scope and initial evidence arguments, rendering JSONL as text', async () => {
    const session = grepSession();
    await render(session);
    const plan = root().querySelector('[aria-label="Reviewed plan"]')!;
    expect(plan.textContent).toContain('Workspace: public');
    expect(plan.textContent).toContain('Folder: <svg onload=bad()>');
    expect(plan.textContent).toContain('Literal pattern: **literal**');
    expect(plan.textContent).toContain('File glob: *.md');
    expect(
      JSON.parse(plan.querySelector('[aria-label="Exact tool arguments"] pre')!.textContent!),
    ).toEqual(session.runs[0].plan!.steps[0].arguments);
    const card = root().querySelector('[aria-label="Step result"]')!;
    expect(card.querySelector('pre')!.textContent).toBe(session.runs[0].results[0].content);
    expect(card.textContent).toContain('3 matches · 4 files searched');
    expect(card.textContent).toContain('Skipped binary: 1 · Skipped unreadable: 2');
    expect(card.textContent).toContain('Partial search coverage');
    expect(card.textContent).toContain('Partial evidence preview');
    expect(
      card.querySelector('img, svg, script, strong, a, .message-markdown, .fetch-provenance'),
    ).toBeNull();
    expect(plan.querySelector('svg, [onload]')).toBeNull();
    expect(fetchDouble).toHaveBeenCalledOnce();
    expect(state.openRun).not.toHaveBeenCalled();
    expect(state.approve).not.toHaveBeenCalled();
  });

  it('pages grep evidence by read-only retained GET, one page at a time, without file opening', async () => {
    const session = grepSession();
    await render(session);
    const result = session.runs[0].results[0];
    const metadata = result.grep!;
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        artifact_id: metadata.artifact_id,
        content: 'tail',
        offset: metadata.next_offset,
        returned_chars: 4,
        total_chars: metadata.total_chars,
        next_offset: null,
      }),
    );
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect(fixture.componentInstance.evidence().values().next().value?.loading).toBe(false),
    );
    await settle();
    expect(fetchDouble).toHaveBeenCalledTimes(2);
    expect(fetchDouble.mock.calls[1][0]).toBe(
      `/api/agent/sessions/${session.id}/artifacts/${metadata.artifact_id}?offset=${metadata.next_offset}&limit=12000`,
    );
    expect(fetchDouble.mock.calls[1][1]?.method).toBe('GET');
    expect(fetchDouble.mock.calls[1][1]?.body).toBeUndefined();
    expect(root().querySelectorAll('[aria-label="Retained evidence page"]')).toHaveLength(1);
    expect(root().querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      'tail',
    );
    expect(root().textContent).toContain('End of retained text.');
    const restartText = 'x'.repeat(metadata.total_chars);
    fetchDouble.mockResolvedValueOnce(
      Response.json({
        artifact_id: metadata.artifact_id,
        content: restartText,
        offset: 0,
        returned_chars: metadata.total_chars,
        total_chars: metadata.total_chars,
        next_offset: null,
      }),
    );
    button('Inspect retained text from start').click();
    await vi.waitFor(() =>
      expect(fixture.componentInstance.evidence().values().next().value?.loading).toBe(false),
    );
    await settle();
    expect(root().querySelectorAll('[aria-label="Retained evidence page"]')).toHaveLength(1);
    expect(root().querySelector('[aria-label="Retained evidence page"] pre')!.textContent).toBe(
      restartText,
    );
    expect(state.openRun).not.toHaveBeenCalled();
    expect(state.request).not.toHaveBeenCalled();
    expect(state.approve).not.toHaveBeenCalled();
  });

  it('supports no-match grep and a resumed initial character window', async () => {
    const session = grepSession();
    const result = session.runs[0].results[0];
    result.content = '';
    Object.assign(result.grep!, {
      returned_chars: 0,
      total_chars: 0,
      next_offset: null,
      total_matches: 0,
      complete: true,
      skipped_binary: 0,
      skipped_unreadable: 0,
    });
    result.truncated = false;
    const initialStep = session.runs[0].plan!.steps[0];
    if (initialStep.tool !== 'grep_in_folder') throw new Error('Expected grep');
    initialStep.arguments.limit = 20;
    parseAgentRun(session.runs[0]);
    await render(session);
    expect(root().textContent).toContain('0 matches');
    expect(root().textContent).toContain('Complete search coverage');
    expect(root().querySelector('[aria-label="Step result"] button')).toBeNull();
    const resumed = grepSession();
    const metadata = resumed.runs[0].results[0].grep!;
    Object.assign(metadata, {
      offset: 4,
      total_chars: metadata.total_chars + 4,
      next_offset: metadata.next_offset! + 4,
    });
    const resumedStep = resumed.runs[0].plan!.steps[0];
    if (resumedStep.tool !== 'grep_in_folder') throw new Error('Expected grep');
    resumedStep.arguments.offset = 4;
    parseAgentRun(resumed.runs[0]);
    state.sessions.set([resumed]);
    await settle();
    expect(root().textContent).toContain('Execution preview: offset 4');
    expect(button('Inspect retained text from start')).toBeTruthy();
    expect(fetchDouble).toHaveBeenCalledOnce();
  });

  it('guards duplicate grep reads, scopes page errors and fences aborted stale responses', async () => {
    const session = grepSession();
    await render(session);
    const result = session.runs[0].results[0];
    const pending = deferredResponse();
    fetchDouble.mockReturnValueOnce(pending.promise);
    button('Next retained page').click();
    await settle();
    await fixture.componentInstance.readEvidence(
      session.id,
      session.runs[0].id,
      result,
      result.grep!.next_offset!,
    );
    expect(fetchDouble).toHaveBeenCalledTimes(2);
    const signal = fetchDouble.mock.calls[1][1]!.signal!;
    const updated = structuredClone(session);
    updated.runs[0].results[0].grep!.skipped_unreadable = 3;
    state.sessions.set([updated]);
    await settle();
    expect(signal.aborted).toBe(true);
    pending.reject(new Error('STALE grep failure'));
    await settle();
    expect(root().textContent).not.toContain('STALE');
    fetchDouble.mockRejectedValueOnce(new Error('Scoped grep failure'));
    button('Next retained page').click();
    await vi.waitFor(() =>
      expect(fixture.componentInstance.evidence().values().next().value?.loading).toBe(false),
    );
    await settle();
    expect(
      root().querySelector('[aria-label="Step result"] [role="alert"]')!.textContent,
    ).toContain('Scoped grep failure');
    expect(state.sessionError()).toBeNull();
    expect(state.globalError()).toBeNull();
  });

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
    expect(root().textContent).toContain('Search returns snippets');
    expect(root().textContent).toContain('Web search is available only when configured by the BFF');
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
    expect(filePlan.textContent).not.toContain('Lines 1–2');
    expect(filePlan.querySelector('[aria-label="Exact tool arguments"]')).toBeNull();
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
    if (step.tool !== 'web_fetch') throw new Error('Expected fetch');
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

  it('renders fetch descriptions and exact arguments before approval beyond old step/revision ceilings', async () => {
    const session = parseAgentSession(structuredClone(recorded.baseline));
    const run = session.runs[0];
    run.plan!.objective = 'Fetch-only objective';
    run.plan!.assumptions = ['Fetch-only assumption'];
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
    expect(reviewed.querySelector('h2')!.textContent).toBe('Fetch-only objective');
    expect(reviewed.textContent).toContain('Fetch-only assumption');
    for (let index = 0; index < 12; index++)
      expect(reviewed.textContent).toContain(`Exact scope ${index}`);
    expect(reviewed.querySelectorAll('ol > li')).toHaveLength(12);
    expect(reviewed.querySelectorAll('details')).toHaveLength(0);
    expect(reviewed.querySelector('[aria-label="Exact recorded plan"]')).toBeNull();
    expect(reviewed.querySelectorAll('[aria-label="Exact tool arguments"]')).toHaveLength(12);
    expect(reviewed.textContent).not.toContain('Limits:');
    expect(button('Approve and run').disabled).toBe(false);
  });

  it.each(['recent', 'older'])(
    'hides model-authored read scope and raw paging in the %s window',
    async (window) => {
      state.windowKind.mockReturnValue(window);
      const session = parseAgentSession(
        structuredClone(fixtureData.streams.find((entry) => entry.name === 'execution')!.baseline),
      );
      const run = session.runs[0];
      run.plan!.objective = 'LEAK-OBJECTIVE: read the whole file using paging';
      run.plan!.assumptions = ['LEAK-ASSUMPTION: 9 chunks cover the full file'];
      const step = run.plan!.steps[0];
      if (step.tool !== 'read_file') throw new Error('Expected read');
      step.title = 'LEAK-TITLE: read full file in 9 chunks';
      step.description = 'LEAK-DESCRIPTION: offset 719 limit 823';
      step.arguments.offset = 719;
      step.arguments.limit = 823;
      const original = structuredClone(run.plan);
      await render(session);
      const reviewed = root().querySelector('[aria-label="Reviewed plan"]')!;
      expect(reviewed.querySelector('h2')!.textContent).toBe('Plan for review');
      expect(reviewed.querySelector('ol h3')!.textContent).toBe('Read file · public.txt');
      expect(reviewed.textContent).toContain('Workspace: public');
      for (const leak of ['LEAK-', '719', '823', 'chunks', 'full file', 'offset', 'limit', 'Lines'])
        expect(reviewed.textContent).not.toContain(leak);
      expect(reviewed.querySelector('[aria-label="Exact tool arguments"]')).toBeNull();
      expect(reviewed.querySelector('[aria-label="Exact recorded plan"]')).toBeNull();
      expect(reviewed.textContent).toContain(run.plan!.digest);
      button('Approve and run').click();
      expect(state.approve).toHaveBeenCalledWith(run);
      expect(run.plan).toEqual(original);
    },
  );

  it('hides model-authored file scope in mixed plans while preserving fetch arguments and approval identity', async () => {
    const session = parseAgentSession(structuredClone(recorded.baseline));
    const run = session.runs[0];
    const plan = run.plan!;
    plan.objective = 'LEAK-OBJECTIVE: read the whole file with paging';
    plan.assumptions = ['LEAK-ASSUMPTION: the full file fits in 9 chunks'];
    plan.steps.push({
      id: 'file-read',
      tool: 'read_file',
      title: 'LEAK-TITLE: read 9 chunks',
      description: 'LEAK-DESCRIPTION: offset 719 limit 823',
      arguments: { workspace_id: 'public', path: 'public.txt', offset: 719, limit: 823 },
    });
    run.request = 'Original request stays visible';
    const original = structuredClone(plan);
    const identity = structuredClone(session.current_run!.plan_identity);
    await render(session);
    const reviewed = root().querySelector('[aria-label="Reviewed plan"]')!;
    expect(reviewed.querySelector('h2')!.textContent).toBe('Plan for review');
    for (const leak of [
      'LEAK-',
      'whole file',
      'full file',
      'chunks',
      'paging',
      '719',
      '823',
      'Unverified assumptions',
    ])
      expect(reviewed.textContent).not.toContain(leak);
    expect(reviewed.textContent).toContain('Workspace: public');
    expect(reviewed.textContent).toContain('Read file · public.txt');
    expect(root().querySelector('.request')!.textContent).toContain(run.request);
    const argumentsViews = reviewed.querySelectorAll('[aria-label="Exact tool arguments"] pre');
    expect(argumentsViews).toHaveLength(1);
    expect(JSON.parse(argumentsViews[0].textContent!)).toEqual(plan.steps[0].arguments);
    expect(reviewed.textContent).toContain(plan.digest);
    expect(reviewed.textContent).toContain(`Revision ${plan.revision}`);
    expect(reviewed.querySelector('[aria-label="Exact recorded plan"]')).toBeNull();
    button('Approve and run').click();
    expect(state.approve).toHaveBeenCalledWith(run);
    expect(run.plan).toEqual(original);
    expect(session.current_run!.plan_identity).toEqual(identity);
  });

  it.each([false, true])('shows adaptive coverage only in results (eof=%s)', async (eof) => {
    const session = parseAgentSession(
      structuredClone(fixtureData.streams.find((entry) => entry.name === 'execution')!.terminal),
    );
    const result = session.runs[0].results[0];
    Object.assign(result, {
      content: 'Accumulated findings from the file.',
      path: 'actual/public.txt',
      offset: null,
      returned_lines: null,
      truncated: !eof,
      read: {
        chunks: 3,
        bytes_read: eof ? 100 : 60,
        total_bytes: 100,
        eof,
        stop_reason: eof ? 'eof' : 'enough',
        file_version: 'version-abc',
      },
    });
    await render(session);
    const reviewed = root().querySelector('[aria-label="Reviewed plan"]')!;
    expect(reviewed.querySelector('[aria-label="File read coverage"]')).toBeNull();
    expect(reviewed.textContent).not.toContain('version-abc');
    const card = root().querySelector('[aria-label="Step result"]')!;
    expect(card.textContent).toContain('actual/public.txt');
    expect(card.textContent).toContain(`${eof ? 100 : 60} of 100 bytes`);
    expect(card.textContent).toContain('3 chunks');
    expect(card.textContent).toContain('version-abc');
    expect(card.textContent).toContain(
      eof ? 'End of file reached' : 'Stopped early: enough findings',
    );
    expect(card.textContent).toContain(result.content);
    expect(card.textContent).not.toContain('Read range:');
    expect(card.textContent).not.toContain('Returned lines:');
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
