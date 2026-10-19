import { Component, inject, input } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentState } from './agent-state';
import type { AgentRun } from './agent.models';
import type { ChatSession } from './chat.models';
import { MarkdownPipe } from './markdown.pipe';
import { MessageTimePipe } from './message-time.pipe';

@Component({
  selector: 'app-agent-workspace',
  imports: [FormsModule, JsonPipe, MarkdownPipe, MessageTimePipe],
  templateUrl: './agent-workspace.html',
  styleUrl: './agent-workspace.css',
})
export class AgentWorkspace {
  readonly state = inject(AgentState);
  readonly legacy = input<ChatSession | null>(null);
  stepStatus(run: AgentRun, stepId: string): string {
    return run.results.find((result) => result.step_id === stepId)?.status ?? 'pending';
  }
  stopping(run: AgentRun): boolean {
    return (
      run.status === 'cancelling' ||
      (this.state.activeRun()?.id === run.id &&
        this.state.stopping().has(this.state.selectedId() ?? ''))
    );
  }
  keydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229)
      return;
    event.preventDefault();
    this.state.request();
  }
}
