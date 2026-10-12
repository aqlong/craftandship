// A reader for web/src/db/schema.sql that does not use scripts/check_destructive_ddl.py.
//
// schema.sql is applied at every boot of the deployed web app, so what it may
// do to rows is checked twice: by that Python script in the pre-commit hook and
// by schema.idempotency.test.ts, which uses this module, in the web unit tests
// the pre-push hook runs. The two are separate programs on purpose, and they
// are held to the same rules by one corpus of awkward inputs
// (fixtures/sql-scan-corpus.json) that both read: what counts as a statement
// (a dollar quote with or without a tag, a string, a comment, a quoted
// identifier), which words change rows, what a "-- SAFE: <reason>" comment
// acknowledges, and the status `live`, which nothing acknowledges.
//
// What it reports, per statement and at any depth (a DO block, a function body
// and a string handed to EXECUTE are read statement by statement):
//   UPDATE, INSERT INTO, DELETE FROM, MERGE INTO, COPY ... FROM, TRUNCATE, and
//   EXECUTE of SQL that is built at run time, unless a `-- SAFE:` comment is on
//   the same line or directly above the statement it is in (a comment above the
//   DO block around it does not count);
//   LIVE: such a statement that names the string live, or a DEFAULT of live,
//   whatever comment is above it.
// UPDATE and the other words are not taken for the command where they only
// name an event or a privilege (BEFORE UPDATE, ON DELETE CASCADE, FOR UPDATE,
// GRANT UPDATE).
//
// It does not evaluate expressions, follow a value through a variable, follow a
// call (`SELECT f()`), or read PL/pgSQL beyond cutting a body at its semicolons.

export type TokenKind = 'word' | 'ident' | 'string' | 'dollar' | 'number' | 'op';

export interface Token {
  kind: TokenKind;
  text: string;
  line: number;
  /** The text between the quotes of a string or a dollar quote. */
  body?: string;
  /** The line that text starts on. */
  bodyLine?: number;
  /** Offsets of the token in the text that was cut into tokens. */
  start: number;
  end: number;
}

export class SqlError extends Error {
  constructor(message: string, public line: number) {
    super(message);
  }
}

const WORD_START = /[A-Za-z_\u0080-\ud7ff\ue000-\uffff]/;
const WORD = /[A-Za-z_\u0080-\ud7ff\ue000-\uffff][A-Za-z0-9_$\u0080-\ud7ff\ue000-\uffff]*/y;
const NUMBER = /\d+(\.\d*)?([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?/y;
const DOLLAR_OPEN = /\$([A-Za-z_\u0080-\ud7ff\ue000-\uffff][A-Za-z0-9_\u0080-\ud7ff\ue000-\uffff]*)?\$/y;
// What Python's str.isspace() calls white space (the same set the Python
// reader uses), which is not what /\s/ calls it.
const SPACE = /[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/;
const MAX_DEPTH = 6;
// The longest statement read, in tokens: the same bound as the Python reader.
const MAX_STATEMENT_TOKENS = 2000;
const LIVE_WORD = /(?<![\p{L}\p{N}_])live(?![\p{L}\p{N}_])/iu;
const SAFETY_MARKER = /--\s*SAFE:/i;

export interface Tokenized {
  tokens: Token[];
  /** line number -> the `--` comment on that line */
  comments: Map<number, string>;
  /** spans of the text that are comments */
  commentSpans: Array<[number, number]>;
}

/** The opening tag of a dollar quote that starts at text[i] ($$ or $tag$), or null. */
function dollarTagAt(text: string, i: number): string | null {
  DOLLAR_OPEN.lastIndex = i;
  const match = DOLLAR_OPEN.exec(text);
  return match === null ? null : match[0];
}

export function tokenize(text: string, firstLine = 1): Tokenized {
  const tokens: Token[] = [];
  const comments = new Map<number, string>();
  const commentSpans: Array<[number, number]> = [];
  const n = text.length;
  let i = 0;
  let line = firstLine;
  while (i < n) {
    const ch = text[i];
    if (ch === '\n') {
      line += 1;
      i += 1;
    } else if (SPACE.test(ch)) {
      i += 1;
    } else if (text.startsWith('--', i)) {
      let end = text.indexOf('\n', i);
      if (end === -1) end = n;
      comments.set(line, text.slice(i, end));
      commentSpans.push([i, end]);
      i = end;
    } else if (text.startsWith('/*', i)) {
      let depth = 1;
      let j = i + 2;
      const startLine = line;
      while (depth > 0 && j < n) {
        if (text.startsWith('/*', j)) {
          depth += 1;
          j += 2;
        } else if (text.startsWith('*/', j)) {
          depth -= 1;
          j += 2;
        } else {
          if (text[j] === '\n') line += 1;
          j += 1;
        }
      }
      if (depth > 0) throw new SqlError('a /* comment is never closed', startLine);
      commentSpans.push([i, j]);
      i = j;
    } else if (ch === "'") {
      // A one-letter prefix touching the quote (E'..', B'..', X'..', N'..')
      // belongs to the string. An E string takes backslash escapes.
      let escapes = false;
      let start = i;
      const last = tokens[tokens.length - 1];
      if (last && last.kind === 'word' && last.text.length === 1 && 'EeBbXxNn'.includes(last.text) && text[i - 1] === last.text) {
        escapes = 'Ee'.includes(last.text);
        start = last.start;
        tokens.pop();
      }
      let j = i + 1;
      const startLine = line;
      const body: string[] = [];
      for (;;) {
        if (j >= n) throw new SqlError('a string is never closed', startLine);
        const c = text[j];
        if (escapes && c === '\\' && j + 1 < n) {
          body.push(text.slice(j, j + 2));
          if (text[j + 1] === '\n') line += 1;
          j += 2;
        } else if (c === "'") {
          if (text.startsWith("''", j)) {
            body.push("'");
            j += 2;
          } else {
            break;
          }
        } else {
          if (c === '\n') line += 1;
          body.push(c);
          j += 1;
        }
      }
      tokens.push({ kind: 'string', text: text.slice(i, j + 1), line: startLine, body: body.join(''), bodyLine: startLine, start, end: j + 1 });
      i = j + 1;
    } else if (ch === '"') {
      let j = i + 1;
      const startLine = line;
      for (;;) {
        if (j >= n) throw new SqlError('a quoted identifier is never closed', startLine);
        if (text[j] === '"') {
          if (text.startsWith('""', j)) {
            j += 2;
            continue;
          }
          break;
        }
        if (text[j] === '\n') line += 1;
        j += 1;
      }
      tokens.push({ kind: 'ident', text: text.slice(i, j + 1), line: startLine, start: i, end: j + 1 });
      i = j + 1;
    } else if (ch === '$' && dollarTagAt(text, i) !== null) {
      const tag = dollarTagAt(text, i) as string;
      const bodyStart = i + tag.length;
      const close = text.indexOf(tag, bodyStart);
      if (close === -1) throw new SqlError(`a ${tag} quote is never closed`, line);
      const body = text.slice(bodyStart, close);
      tokens.push({ kind: 'dollar', text: text.slice(i, close + tag.length), line, body, bodyLine: line, start: i, end: close + tag.length });
      line += body.split('\n').length - 1;
      i = close + tag.length;
    } else if (WORD_START.test(ch)) {
      WORD.lastIndex = i;
      const word = (WORD.exec(text) as RegExpExecArray)[0];
      tokens.push({ kind: 'word', text: word, line, start: i, end: i + word.length });
      i += word.length;
    } else if ((ch >= '0' && ch <= '9') || (ch === '.' && i + 1 < n && text[i + 1] >= '0' && text[i + 1] <= '9')) {
      NUMBER.lastIndex = i;
      const num = (NUMBER.exec(text) as RegExpExecArray)[0];
      tokens.push({ kind: 'number', text: num, line, start: i, end: i + num.length });
      i += num.length;
    } else if (text.startsWith('::', i)) {
      tokens.push({ kind: 'op', text: '::', line, start: i, end: i + 2 });
      i += 2;
    } else {
      // one code point, which is two UTF-16 units outside the BMP
      const length = (text.codePointAt(i) as number) > 0xffff ? 2 : 1;
      tokens.push({ kind: 'op', text: text.slice(i, i + length), line, start: i, end: i + length });
      i += length;
    }
  }
  return { tokens, comments, commentSpans };
}

/** Cut a token list at the semicolons. Empty statements are dropped. */
export function splitStatements(tokens: Token[]): Token[][] {
  const statements: Token[][] = [];
  let current: Token[] = [];
  for (const token of tokens) {
    if (token.kind === 'op' && token.text === ';') {
      if (current.length) statements.push(current);
      current = [];
    } else {
      current.push(token);
    }
  }
  if (current.length) statements.push(current);
  return statements;
}

const isWord = (token: Token | undefined, ...words: string[]): boolean =>
  token !== undefined && token.kind === 'word' && words.includes(token.text.toLowerCase());
const isOp = (token: Token | undefined, text: string): boolean => token !== undefined && token.kind === 'op' && token.text === text;

// Words after which UPDATE is not the command: a trigger event, a foreign key
// action, a row lock, a privilege.
const NOT_THE_COMMAND_BEFORE = ['for', 'key', 'on', 'or', 'before', 'after', 'of', 'grant', 'revoke'];

function afterAPrivilegeOrEventWord(tokens: Token[], index: number): boolean {
  const previous = tokens[index - 1];
  if (index === 0 || previous === undefined) return false;
  if (isOp(previous, ',')) return true;
  return isWord(previous, ...NOT_THE_COMMAND_BEFORE);
}

function executeIsDynamic(tokens: Token[], index: number): boolean {
  const first = tokens[index + 1];
  if (first === undefined || (first.kind !== 'string' && first.kind !== 'dollar')) return true;
  const after = tokens[index + 2];
  return !(after === undefined || isWord(after, 'into', 'using'));
}

function copyReadsAFile(tokens: Token[], index: number): boolean {
  let depth = 0;
  for (const later of tokens.slice(index + 1)) {
    if (isOp(later, '(')) depth += 1;
    else if (isOp(later, ')')) depth -= 1;
    else if (depth === 0 && isWord(later, 'from')) return true;
    else if (depth === 0 && isWord(later, 'to')) return false;
  }
  return false;
}

/** The kind of row change the word at tokens[index] begins, or null. */
function rowChangeKind(tokens: Token[], index: number): string | null {
  const word = tokens[index].text.toLowerCase();
  const following = tokens[index + 1];
  switch (word) {
    case 'update':
      return afterAPrivilegeOrEventWord(tokens, index) ? null : 'UPDATE';
    case 'insert':
      return isWord(following, 'into') ? 'INSERT' : null;
    case 'delete':
      return isWord(following, 'from') ? 'DELETE' : null;
    case 'merge':
      return isWord(following, 'into') ? 'MERGE' : null;
    case 'copy':
      return copyReadsAFile(tokens, index) ? 'COPY FROM' : null;
    case 'truncate':
      return 'TRUNCATE';
    case 'execute':
      if (isWord(following, 'function', 'procedure')) return null; // a trigger names its function this way
      if (afterAPrivilegeOrEventWord(tokens, index)) return null; // GRANT EXECUTE ON FUNCTION
      return executeIsDynamic(tokens, index) ? 'EXECUTE' : null;
    default:
      return null;
  }
}

const namesLive = (token: Token): boolean => (token.kind === 'string' || token.kind === 'dollar') && LIVE_WORD.test(token.body ?? '');
const CAN_WRITE_LIVE = ['UPDATE', 'INSERT', 'MERGE', 'COPY FROM', 'EXECUTE'];

function defaultOfLive(tokens: Token[]): Token | null {
  for (let index = 0; index < tokens.length; index++) {
    if (!isWord(tokens[index], 'default')) continue;
    let following = tokens[index + 1];
    if (isOp(following, '(')) following = tokens[index + 2];
    if (following !== undefined && namesLive(following)) return following;
  }
  return null;
}

interface RawFinding {
  token: Token;
  kind: string;
}

function statementFindings(tokens: Token[]): RawFinding[] {
  const found: RawFinding[] = [];
  const seen = new Set<Token>();
  const add = (token: Token, kind: string): void => {
    if (!seen.has(token)) {
      seen.add(token);
      found.push({ token, kind });
    }
  };
  tokens.forEach((token, index) => {
    if (token.kind !== 'word') return;
    const kind = rowChangeKind(tokens, index);
    if (kind !== null) add(token, kind);
  });
  if (found.some((f) => CAN_WRITE_LIVE.includes(f.kind))) {
    const live = tokens.find(namesLive);
    if (live) add(live, 'LIVE');
  }
  const def = defaultOfLive(tokens);
  if (def !== null) add(def, 'LIVE');
  return found;
}

/** The tokens of one statement whose text is itself SQL that will run. */
function bodies(tokens: Token[]): Token[] {
  const words = tokens.filter((t) => t.kind === 'word').map((t) => t.text.toLowerCase());
  const first = words[0] ?? '';
  const text = (t: Token): boolean => t.kind === 'dollar' || t.kind === 'string';
  const head = words.slice(0, 6);
  if (first === 'do' || (first === 'create' && (head.includes('function') || head.includes('procedure')))) {
    return tokens.filter(text);
  }
  for (let index = 0; index < tokens.length; index++) {
    if (isWord(tokens[index], 'execute')) {
      if (isWord(tokens[index + 1], 'function', 'procedure')) continue;
      return tokens.slice(index + 1).filter(text);
    }
  }
  return [];
}

export interface Finding {
  line: number;
  kind: string;
  /** The `-- SAFE:` comment that acknowledged it, or null when none did. */
  acknowledgedBy: string | null;
  /** How many bodies deep it sits: 0 at the top level of the file. */
  depth: number;
  /** The first word of the top-level statement it is in (do, create, update ...). */
  container: string;
  /** Index of the top-level statement it is in. */
  statementIndex: number;
  /** The table the command names, lower case ('' when it cannot tell). */
  table: string;
}

export interface Statement {
  /** The statement as written, comments blanked out. */
  text: string;
  /** A DO block. */
  insideDoBlock: boolean;
  line: number;
}

export interface Scan {
  /** The first thing that could not be read (an unclosed quote or comment, bodies nested too deep), or null. */
  unreadable: string | null;
  statements: Statement[];
  /** Every row change and every mention of live, acknowledged or not. */
  all: Finding[];
  /** What is not acknowledged: this is what the pre-commit hook and the web suite refuse. */
  findings: Finding[];
}

class Source {
  readonly lines: string[];
  readonly comments = new Map<number, string>();
  readonly firstOnLine = new Map<number, Token>();

  constructor(text: string) {
    this.lines = text.split('\n');
  }

  add(tokens: Token[], comments: Map<number, string>): void {
    comments.forEach((value, line) => this.comments.set(line, value));
    for (const token of tokens) if (!this.firstOnLine.has(token.line)) this.firstOnLine.set(token.line, token);
  }

  safeOn(line: number): string | null {
    const comment = this.comments.get(line);
    return comment !== undefined && SAFETY_MARKER.test(comment) ? comment : null;
  }

  /** A SAFE comment in the run of comment lines directly above the line `token` begins. */
  safeAbove(token: Token): string | null {
    if (this.firstOnLine.get(token.line) !== token) return null;
    let above = token.line - 1;
    while (above >= 1 && above <= this.lines.length && this.lines[above - 1].trimStart().startsWith('--')) {
      const comment = this.safeOn(above);
      if (comment !== null) return comment;
      above -= 1;
    }
    return null;
  }
}

function targetOf(tokens: Token[], index: number): string {
  let i = index + 1;
  const skip = (...words: string[]): void => {
    while (isWord(tokens[i], ...words)) i += 1;
  };
  skip('into', 'from', 'table', 'only');
  const first = tokens[i];
  if (first === undefined || (first.kind !== 'word' && first.kind !== 'ident')) return '';
  let name = first;
  while (isOp(tokens[i + 1], '.') && tokens[i + 2] !== undefined && (tokens[i + 2].kind === 'word' || tokens[i + 2].kind === 'ident')) {
    i += 2;
    name = tokens[i];
  }
  return name.text.replace(/^"|"$/g, '').toLowerCase();
}

export function scanSql(text: string): Scan {
  const source = new Source(text);
  const all: Finding[] = [];
  let unreadable: string | null = null;
  const statements: Statement[] = [];

  // Comments to blank out of the statements' text: those of the file itself
  // and those inside dollar-quoted bodies, whose offsets in the file are known.
  const commentSpans: Array<[number, number]> = [];

  const check = (
    part: string,
    firstLine: number,
    depth: number,
    topLevel: { index: number; container: string } | null,
    base: number | null
  ): Tokenized | null => {
    let cut: Tokenized;
    try {
      cut = tokenize(part, firstLine);
    } catch (error) {
      if (!(error instanceof SqlError)) throw error;
      unreadable = unreadable ?? `${error.message} (line ${error.line})`;
      return null;
    }
    source.add(cut.tokens, cut.comments);
    if (base !== null) for (const [from, to] of cut.commentSpans) commentSpans.push([base + from, base + to]);
    splitStatements(cut.tokens).forEach((statement, position) => {
      if (statement.length > MAX_STATEMENT_TOKENS) {
        unreadable = unreadable ?? `a statement of more than ${MAX_STATEMENT_TOKENS} tokens (line ${statement[0].line})`;
        return;
      }
      const here = topLevel ?? {
        index: position,
        container: statement.find((t) => t.kind === 'word')?.text.toLowerCase() ?? '',
      };
      for (const { token, kind } of statementFindings(statement)) {
        const anchor = token;
        const comment =
          kind === 'LIVE' ? null : source.safeOn(token.line) ?? source.safeAbove(anchor) ?? source.safeAbove(statement[0]);
        all.push({
          line: token.line,
          kind,
          acknowledgedBy: comment,
          depth,
          container: here.container,
          statementIndex: here.index,
          table: kind === 'LIVE' ? '' : targetOf(statement, statement.indexOf(token)),
        });
      }
      for (const body of bodies(statement)) {
        if (depth >= MAX_DEPTH) {
          unreadable = unreadable ?? `bodies nested more than ${MAX_DEPTH} deep (line ${body.line})`;
          continue;
        }
        const opening = body.kind === 'dollar' ? body.text.indexOf('$', 1) + 1 : 0;
        check(body.body ?? '', body.bodyLine ?? body.line, depth + 1, here, base !== null && body.kind === 'dollar' ? base + body.start + opening : null);
      }
    });
    return cut;
  };

  const top = check(text, 1, 0, null, 0);
  if (top !== null) {
    // Blank the comments out of the text and cut it where the statements are.
    const chars = text.split('');
    for (const [from, to] of commentSpans) {
      for (let k = from; k < to; k++) if (chars[k] !== '\n') chars[k] = ' ';
    }
    const code = chars.join('');
    for (const statement of splitStatements(top.tokens)) {
      const slice = code.slice(statement[0].start, statement[statement.length - 1].end).trim();
      const first = statement.find((t) => t.kind === 'word');
      statements.push({ text: slice, insideDoBlock: first !== undefined && first.text.toLowerCase() === 'do', line: statement[0].line });
    }
  }
  all.sort((a, b) => a.line - b.line || (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0));
  // One finding for each kind on a line, as the Python reader reports them.
  const reported = new Set<string>();
  const findings = all.filter((f) => {
    const key = `${f.line} ${f.kind}`;
    if (f.acknowledgedBy !== null || reported.has(key)) return false;
    reported.add(key);
    return true;
  });
  return { unreadable, statements, all, findings };
}
