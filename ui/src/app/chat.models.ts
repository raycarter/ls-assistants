export interface SystemPrompt {
  id: string;
  title: string;
  content: string;
}

export interface PromptDraft {
  id?: string;
  title: string;
  content: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  time?: string;
  status?: string;
}

export interface ChatSession {
  id: string;
  title: string;
  mode: 'chat' | 'agent';
  messages: ChatMessage[];
  draft: string;
  prompt: SystemPrompt | null;
  promptLocked: boolean;
}

export interface ChatSnapshot {
  sessions: ChatSession[];
  prompts: SystemPrompt[];
  activeSessionId: string;
}

// Per-session storage follows dev-plans-archive/localstorage-data-example.json.
export interface StoredMessage {
  id: string;
  time: string;
  role: 'user' | 'ai';
  msg: string;
  type: 'Message';
  status?: string;
}

export interface StoredSession {
  id: string;
  items: StoredMessage[];
  title: string;
  type: 'Chat' | 'Agent';
  selected: boolean;
  selectedSystemPromptId: string | null;
  systemPromptSnapshot: SystemPrompt | null;
  systemPromptLocked: boolean;
}

export interface StoredChatState {
  version: 1;
  sessions: StoredSession[];
  systemPrompts: SystemPrompt[];
  drafts: Record<string, string>;
}
