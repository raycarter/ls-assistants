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
