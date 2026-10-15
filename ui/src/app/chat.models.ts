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
  requestId?: string;
  replyTo?: string;
  error?: { code: string; message: string };
}

export const REASONING_EFFORTS = ['low', 'medium', 'high', 'xhigh'] as const;
export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

export interface ChatSession {
  id: string;
  title: string;
  mode: 'chat' | 'agent';
  messages: ChatMessage[];
  draft: string;
  prompt: SystemPrompt | null;
  promptLocked: boolean;
  model?: string;
  reasoningEffort?: ReasoningEffort;
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
  requestId?: string;
  replyTo?: string;
  error?: { code: string; message: string };
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
  model?: string;
  reasoningEffort?: ReasoningEffort;
}

export interface StoredChatState {
  version: 1;
  sessions: StoredSession[];
  systemPrompts: SystemPrompt[];
  drafts: Record<string, string>;
}
