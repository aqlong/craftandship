import { Router, Request, Response } from 'express';
import { requireAuth, validateCsrfToken } from '../middleware/auth';
import { boundedRateLimit, customerOrAddressKey } from '../middleware/companion-rate-limit';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import Anthropic from '@anthropic-ai/sdk';
import { generateKnowledge } from '../services/support-knowledge';
import { tryConsumeDailyCall } from '../services/llm-budget';
import { customerLlmDailyLimit } from '../middleware/llm-rate-limit';

const router = forwardAsyncErrors(Router());

const SUPPORT_MODEL = 'claude-sonnet-4-20250514';
const SUPPORT_TIMEOUT_MS = 60_000;
const SIMILARITY_THRESHOLD = 0.6;
const MAX_HISTORY_MESSAGES = 20;
const MAX_HISTORY_CHARS = 20000;

const supportRateLimit = boundedRateLimit({
  windowMs: 60 * 1000,
  max: 10,
  message: { error: 'Too many messages. Please wait a moment before sending again.' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: customerOrAddressKey,
});

function buildSystemPrompt(): string {
  const knowledge = generateKnowledge();
  return `You are a friendly customer support assistant for Craft & Ship.

${knowledge}

Guidelines:
- Be helpful, concise, and friendly
- Answer only questions related to Craft & Ship
- If asked about something unrelated, politely redirect to Craft & Ship topics
- Do not make up features or pricing that are not listed above
- Keep answers under 3 paragraphs
- Never use em dashes; use commas or semicolons instead
- For complex or account-specific issues, direct users to support@craftandship.io`;
}

function normalizeText(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function getKeywords(text: string): string[] {
  const stopWords = new Set([
    'a', 'an', 'the', 'is', 'it', 'to', 'of', 'in', 'for', 'on', 'with',
    'and', 'or', 'but', 'not', 'at', 'by', 'from', 'that', 'this', 'what',
    'how', 'do', 'does', 'can', 'i', 'my', 'me', 'you', 'your', 'we',
    'are', 'be', 'was', 'were', 'has', 'have', 'had', 'will', 'would',
    'could', 'should', 'about', 'there', 'their', 'they', 'if', 'so',
    'up', 'out', 'just', 'also', 'than', 'then', 'when', 'which',
  ]);
  return normalizeText(text)
    .split(' ')
    .filter((w) => w.length > 1 && !stopWords.has(w));
}

function calculateSimilarity(query: string, cached: string): number {
  const queryWords = getKeywords(query);
  const cachedWords = getKeywords(cached);
  if (queryWords.length === 0 || cachedWords.length === 0) return 0;

  const cachedSet = new Set(cachedWords);
  const matchCount = queryWords.filter((w) => cachedSet.has(w)).length;
  return matchCount / Math.max(queryWords.length, cachedWords.length);
}

// What may be served from support_kb, and why this route never writes to it
// ----------------------------------------------------------------------
// support_kb is shared by every customer: a row matched by word overlap is
// returned to whoever asks a similar-looking question. This route used to
// store the model's answer to each first message there. That made one
// customer's message a way to plant an answer for the next one: the answer is
// a function of text the sender controls (a question can carry instructions
// for the model), and the match is fuzzy by design (and blind to anything
// outside [A-Za-z0-9_], so the instructions need not even count as words).
//
// So answers generated for a customer are returned to that customer only and
// are not stored. The lookup below remains, over rows written by the
// operator-side jobs (the coordinator's knowledge sync and the weekly topic
// sweep in cron/topic_kb_sweep.ts), which pass their own content filters
// before inserting. Those rows are model-drafted, and they are stored as
// 'pending': only a row an operator has set to 'approved' is served (the
// status column and how to approve a row are in db/schema.sql).
async function findKnowledgeBaseAnswer(message: string): Promise<string | null> {
  // Bound the scan: this runs on the support-chat request path before the
  // model call. The most-used entries are the likeliest matches; a miss in
  // the long tail just falls through to the model.
  const result = await pool.query(
    "SELECT id, question, answer FROM support_kb WHERE status = 'approved' ORDER BY usage_count DESC LIMIT 500"
  );

  const rows = result.rows as Array<{ id: string; question: string; answer: string }>;
  let bestMatch: { id: string; answer: string; score: number } | null = null;

  for (const row of rows) {
    const score = calculateSimilarity(message, row.question);
    if (score >= SIMILARITY_THRESHOLD && (!bestMatch || score > bestMatch.score)) {
      bestMatch = { id: row.id, answer: row.answer, score };
    }
  }

  if (bestMatch) {
    await pool.query(
      'UPDATE support_kb SET usage_count = usage_count + 1, updated_at = NOW() WHERE id = $1',
      [bestMatch.id]
    );
    return bestMatch.answer;
  }

  return null;
}

interface HistoryMessage {
  role: 'user' | 'assistant';
  text: string;
}

function sanitizeHistory(rawHistory: unknown): HistoryMessage[] {
  if (!Array.isArray(rawHistory)) return [];

  const history: HistoryMessage[] = [];
  let totalChars = 0;

  // Take at most MAX_HISTORY_MESSAGES, cap total chars
  const entries = rawHistory.slice(-MAX_HISTORY_MESSAGES);
  for (const entry of entries) {
    if (
      typeof entry !== 'object' || entry === null ||
      !('role' in entry) || !('text' in entry) ||
      typeof entry.text !== 'string' ||
      (entry.role !== 'user' && entry.role !== 'assistant')
    ) {
      continue;
    }
    const text = entry.text.slice(0, 1000);
    totalChars += text.length;
    if (totalChars > MAX_HISTORY_CHARS) break;
    history.push({ role: entry.role, text });
  }

  return history;
}

async function askClaude(message: string, history: HistoryMessage[]): Promise<string> {
  // The SDK default timeout is 10 minutes; a support chat reply should never
  // keep a customer's request open that long. Retries (429/5xx/connection) stay
  // at the SDK default of 2, with exponential backoff.
  const anthropic = new Anthropic({
    apiKey: process.env.ANTHROPIC_API_KEY,
    timeout: SUPPORT_TIMEOUT_MS,
    maxRetries: 2,
  });

  // Build messages array: history + current message
  const messages: Array<{ role: 'user' | 'assistant'; content: string }> = [];
  for (const entry of history) {
    messages.push({ role: entry.role, content: entry.text });
  }
  messages.push({ role: 'user', content: message });

  const response = await anthropic.messages.create({
    model: SUPPORT_MODEL,
    max_tokens: 2048,
    system: buildSystemPrompt(),
    messages,
  });

  const content = response.content[0];
  if (content.type !== 'text') {
    throw new Error('Unexpected response type from Claude');
  }
  return content.text;
}

router.post('/chat', requireAuth, supportRateLimit, validateCsrfToken, customerLlmDailyLimit, async (req: Request, res: Response) => {
  const { message, history: rawHistory } = req.body as { message: string; history?: unknown };

  if (!message || typeof message !== 'string' || message.trim().length === 0) {
    res.status(400).json({ error: 'Message is required.' });
    return;
  }

  const sanitized = message.trim().slice(0, 1000);
  const history = sanitizeHistory(rawHistory);

  try {
    // Knowledge-base lookup only for a first message (a follow-up needs the
    // conversation, which a stored answer does not have).
    if (history.length === 0) {
      const stored = await findKnowledgeBaseAnswer(sanitized);
      if (stored) {
        res.json({ reply: stored, source: 'cache' });
        return;
      }
    }

    if (!process.env.ANTHROPIC_API_KEY) {
      res.json({
        reply: 'Thanks for reaching out! For detailed questions, please email us at support@craftandship.io.',
        source: 'fallback',
      });
      return;
    }

    // Same process-wide daily budget as the calls in services/claude.ts.
    if (!tryConsumeDailyCall()) {
      res.status(503).json({
        error: 'We are experiencing high demand right now. Please try again later, or email support@craftandship.io.',
      });
      return;
    }

    // The reply goes to the customer who asked and nowhere else: it is not
    // written to support_kb (see findKnowledgeBaseAnswer).
    const reply = await askClaude(sanitized, history);

    res.json({ reply, source: 'ai' });
  } catch (err) {
    console.error('Support chat error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to process your message. Please try again.' });
  }
});

export default router;
