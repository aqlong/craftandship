import type { Response } from 'express';

// Every chat in the product (intake, revision, feature scoping and the Haven
// conversation) is answered by a model, which needs ANTHROPIC_API_KEY. A fresh
// checkout has none, so the first message a visitor sends must end in a plain
// explanation, not a raw 500 or a message that silently disappears.

export const ASSISTANT_NOT_CONFIGURED_MESSAGE =
  'The assistant is not configured on this server: no ANTHROPIC_API_KEY is set, so it cannot reply yet. ' +
  'The rest of the site works. Whoever runs this server can add the key to web/.env and restart it.';

export class AssistantNotConfiguredError extends Error {
  constructor(message: string = ASSISTANT_NOT_CONFIGURED_MESSAGE) {
    super(message);
    this.name = 'AssistantNotConfiguredError';
  }
}

export function assistantConfigured(): boolean {
  return Boolean(process.env.ANTHROPIC_API_KEY);
}

/** Throw AssistantNotConfiguredError when no model key is set. */
export function assertAssistantConfigured(): void {
  if (!assistantConfigured()) throw new AssistantNotConfiguredError();
}

/** The status and body of the "no model key" answer, for callers that build their response elsewhere. */
export const ASSISTANT_NOT_CONFIGURED_STATUS = 503;
export function assistantNotConfiguredBody(): { error: string; message: string; code: string } {
  return {
    error: ASSISTANT_NOT_CONFIGURED_MESSAGE,
    message: ASSISTANT_NOT_CONFIGURED_MESSAGE,
    code: 'assistant_not_configured',
  };
}

/**
 * Answer a chat request that cannot be served because no model key is set:
 * 503 with the explanation under `error` (what the legacy chat clients display)
 * and `message` (what the Haven client displays), plus a stable `code`.
 */
export function respondAssistantNotConfigured(res: Response): void {
  res.status(ASSISTANT_NOT_CONFIGURED_STATUS).json(assistantNotConfiguredBody());
}
