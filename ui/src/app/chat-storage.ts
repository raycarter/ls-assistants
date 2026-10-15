import type {
  ChatMessage,
  ChatSnapshot,
  StoredChatState,
  StoredMessage,
  StoredSession,
  SystemPrompt,
} from './chat.models';
import { REASONING_EFFORTS, type ReasoningEffort } from './chat.models';

function messageMetadata(message: ChatMessage | StoredMessage) {
  return {
    ...(message.requestId === undefined ? {} : { requestId: message.requestId }),
    ...(message.replyTo === undefined ? {} : { replyTo: message.replyTo }),
    ...(message.error === undefined ? {} : { error: { ...message.error } }),
  };
}

export function encodeChatState(snapshot: ChatSnapshot): StoredChatState {
  const activeId = snapshot.sessions.some((session) => session.id === snapshot.activeSessionId)
    ? snapshot.activeSessionId
    : (snapshot.sessions[0]?.id ?? '');

  return {
    version: 1,
    sessions: snapshot.sessions.map((session) => ({
      id: session.id,
      items: session.messages.map((message): StoredMessage => {
        const status = message.status ?? (message.role === 'assistant' ? 'complete' : undefined);
        return {
          id: message.id,
          time: message.time ?? '',
          role: message.role === 'assistant' ? 'ai' : 'user',
          msg: message.content,
          type: 'Message',
          ...(status === undefined ? {} : { status }),
          ...messageMetadata(message),
        };
      }),
      title: session.title,
      type: session.mode === 'agent' ? 'Agent' : 'Chat',
      selected: session.id === activeId,
      selectedSystemPromptId: session.prompt?.id ?? null,
      systemPromptSnapshot:
        session.promptLocked && session.prompt ? clonePrompt(session.prompt) : null,
      systemPromptLocked: session.promptLocked,
      ...(session.model === undefined ? {} : { model: session.model }),
      ...(session.reasoningEffort === undefined
        ? {}
        : { reasoningEffort: session.reasoningEffort }),
    })),
    systemPrompts: snapshot.prompts.map(clonePrompt),
    drafts: Object.fromEntries(snapshot.sessions.map((session) => [session.id, session.draft])),
  };
}

export function decodeChatState(
  raw: string,
  fallbackPrompts: readonly SystemPrompt[],
): ChatSnapshot {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    invalid('JSON', 'could not be parsed');
  }

  let sessions: StoredSession[];
  let prompts: SystemPrompt[];
  let drafts: Record<string, string> = Object.fromEntries([]);
  let bare = true;

  if (Array.isArray(parsed)) {
    sessions = parseSessions(parsed);
    prompts = parsePrompts(fallbackPrompts, 'fallbackPrompts');
  } else {
    const root = requireRecord(parsed, 'root');
    if (Object.hasOwn(root, 'version') || Object.hasOwn(root, 'sessions')) {
      bare = false;
      if (readField(root, 'version', 'root') !== 1) {
        invalid('version', 'is unsupported');
      }
      sessions = parseSessions(readField(root, 'sessions', 'root'));
      prompts = parsePrompts(readField(root, 'systemPrompts', 'root'), 'systemPrompts');
      drafts = parseDrafts(readField(root, 'drafts', 'root'));
    } else {
      sessions = [parseSession(root, 'session')];
      prompts = parsePrompts(fallbackPrompts, 'fallbackPrompts');
    }
  }

  // Bare imports have no catalogue; an envelope's catalogue remains authoritative.
  if (bare) {
    const promptIds = new Set(prompts.map((prompt) => prompt.id));
    for (const session of sessions) {
      const prompt = session.systemPromptSnapshot;
      if (prompt && !promptIds.has(prompt.id)) {
        prompts.push(clonePrompt(prompt));
        promptIds.add(prompt.id);
      }
    }
  }

  const catalogue = new Map(prompts.map((prompt) => [prompt.id, prompt]));
  return {
    sessions: sessions.map((session) => {
      const prompt = session.systemPromptLocked
        ? session.systemPromptSnapshot
        : session.selectedSystemPromptId === null
          ? null
          : (catalogue.get(session.selectedSystemPromptId) ?? null);
      return {
        id: session.id,
        title: session.title,
        mode: session.type === 'Agent' ? 'agent' : 'chat',
        messages: session.items.map((message): ChatMessage => ({
          id: message.id,
          role: message.role === 'ai' ? 'assistant' : 'user',
          content: message.msg,
          time: message.time,
          ...(message.status === undefined ? {} : { status: message.status }),
          ...messageMetadata(message),
        })),
        draft: Object.hasOwn(drafts, session.id) ? drafts[session.id] : '',
        prompt: prompt ? clonePrompt(prompt) : null,
        promptLocked: session.systemPromptLocked,
        ...(session.model === undefined ? {} : { model: session.model }),
        ...(session.reasoningEffort === undefined
          ? {}
          : { reasoningEffort: session.reasoningEffort }),
      };
    }),
    prompts,
    activeSessionId: sessions.find((session) => session.selected)?.id ?? sessions[0]?.id ?? '',
  };
}

function clonePrompt(prompt: SystemPrompt): SystemPrompt {
  return { id: prompt.id, title: prompt.title, content: prompt.content };
}

function parseSessions(value: unknown): StoredSession[] {
  return uniqueIds(
    requireArray(value, 'sessions').map((session, index) =>
      parseSession(session, `sessions[${index}]`),
    ),
    'sessions',
  );
}

function parseSession(value: unknown, path: string): StoredSession {
  const record = requireRecord(value, path);
  const type = readField(record, 'type', path);
  if (type !== 'Chat' && type !== 'Agent') invalid(`${path}.type`, 'must be Chat or Agent');

  const selectedId = readField(record, 'selectedSystemPromptId', path);
  const selectedSystemPromptId =
    selectedId === null ? null : requireId(selectedId, `${path}.selectedSystemPromptId`);
  const snapshot = readField(record, 'systemPromptSnapshot', path);
  const systemPromptSnapshot =
    snapshot === null ? null : parsePrompt(snapshot, `${path}.systemPromptSnapshot`);
  const systemPromptLocked = requireBoolean(
    readField(record, 'systemPromptLocked', path),
    `${path}.systemPromptLocked`,
  );
  if (systemPromptLocked && (systemPromptSnapshot?.id ?? null) !== selectedSystemPromptId) {
    invalid(`${path}.systemPromptSnapshot`, 'must match the locked selectedSystemPromptId');
  }

  return {
    id: requireId(readField(record, 'id', path), `${path}.id`),
    items: uniqueIds(
      requireArray(readField(record, 'items', path), `${path}.items`).map((message, index) =>
        parseMessage(message, `${path}.items[${index}]`),
      ),
      `${path}.items`,
    ),
    title: requireString(readField(record, 'title', path), `${path}.title`),
    type,
    selected: requireBoolean(readField(record, 'selected', path), `${path}.selected`),
    selectedSystemPromptId,
    systemPromptSnapshot,
    systemPromptLocked,
    // Preferences are additive; unusable preferences must not discard a saved history.
    ...(typeof record['model'] === 'string' && record['model'].length
      ? { model: record['model'] }
      : {}),
    ...(REASONING_EFFORTS.includes(record['reasoningEffort'] as ReasoningEffort)
      ? { reasoningEffort: record['reasoningEffort'] as ReasoningEffort }
      : {}),
  };
}

function parseMessage(value: unknown, path: string): StoredMessage {
  const record = requireRecord(value, path);
  const role = readField(record, 'role', path);
  if (role !== 'user' && role !== 'ai') invalid(`${path}.role`, 'must be user or ai');
  if (readField(record, 'type', path) !== 'Message') invalid(`${path}.type`, 'must be Message');

  return {
    id: requireId(readField(record, 'id', path), `${path}.id`),
    time: requireString(readField(record, 'time', path), `${path}.time`),
    role,
    msg: requireString(readField(record, 'msg', path), `${path}.msg`),
    type: 'Message',
    ...(Object.hasOwn(record, 'status')
      ? { status: requireString(record['status'], `${path}.status`) }
      : {}),
    ...(Object.hasOwn(record, 'requestId')
      ? { requestId: requireId(record['requestId'], `${path}.requestId`) }
      : {}),
    ...(Object.hasOwn(record, 'replyTo')
      ? { replyTo: requireId(record['replyTo'], `${path}.replyTo`) }
      : {}),
    ...(Object.hasOwn(record, 'error')
      ? { error: parseError(record['error'], `${path}.error`) }
      : {}),
  };
}

function parseError(value: unknown, path: string): { code: string; message: string } {
  const error = requireRecord(value, path);
  return {
    code: requireString(readField(error, 'code', path), `${path}.code`),
    message: requireString(readField(error, 'message', path), `${path}.message`),
  };
}

function parsePrompts(value: unknown, path: string): SystemPrompt[] {
  return uniqueIds(
    requireArray(value, path).map((prompt, index) => parsePrompt(prompt, `${path}[${index}]`)),
    path,
  );
}

function parsePrompt(value: unknown, path: string): SystemPrompt {
  const record = requireRecord(value, path);
  return {
    id: requireId(readField(record, 'id', path), `${path}.id`),
    title: requireString(readField(record, 'title', path), `${path}.title`),
    content: requireString(readField(record, 'content', path), `${path}.content`),
  };
}

function parseDrafts(value: unknown): Record<string, string> {
  return Object.fromEntries(
    Object.entries(requireRecord(value, 'drafts')).map(([id, draft]) => [
      requireId(id, 'drafts key'),
      requireString(draft, `drafts[${JSON.stringify(id)}]`),
    ]),
  );
}

function uniqueIds<T extends { id: string }>(entries: T[], path: string): T[] {
  const ids = new Set<string>();
  for (const entry of entries) {
    if (ids.has(entry.id))
      invalid(`${path}.id`, `contains duplicate ID ${JSON.stringify(entry.id)}`);
    ids.add(entry.id);
  }
  return entries;
}

function readField(record: Record<string, unknown>, key: string, path: string): unknown {
  if (!Object.hasOwn(record, key)) invalid(`${path}.${key}`, 'is required');
  return record[key];
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    invalid(path, 'must be an object');
  }
  return value as Record<string, unknown>;
}

function requireArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) invalid(path, 'must be an array');
  return value;
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== 'string') invalid(path, 'must be a string');
  return value;
}

function requireId(value: unknown, path: string): string {
  const id = requireString(value, path);
  if (id.length === 0) invalid(path, 'must be nonempty');
  return id;
}

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') invalid(path, 'must be a boolean');
  return value;
}

function invalid(path: string, detail: string): never {
  throw new Error(`Invalid chat state: ${path} ${detail}.`);
}
