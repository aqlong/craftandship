import { describe, it, expect, afterEach } from 'vitest';
import http from 'http';
import net from 'net';
import type { AddressInfo } from 'net';
import { describeListenError, listenOn } from '../../src/utils/listen';

const opened: Array<{ close(cb?: () => void): unknown }> = [];

function closeAll(): Promise<void[]> {
  return Promise.all(
    opened.splice(0).map((server) => new Promise<void>((resolve) => server.close(() => resolve()))),
  );
}

function occupyAnyPort(): Promise<number> {
  return new Promise((resolve) => {
    const blocker = net.createServer();
    opened.push(blocker);
    blocker.listen(0, () => resolve((blocker.address() as AddressInfo).port));
  });
}

const respondOk: http.RequestListener = (_req, res) => {
  res.end('ok');
};

afterEach(closeAll);

describe('listenOn', () => {
  it('resolves with a server that is already bound and answering', async () => {
    const free = await occupyAnyPort();
    await closeAll();
    const server = await listenOn(respondOk, free);
    opened.push(server);
    expect(server.listening).toBe(true);
    const body = await new Promise<string>((resolve, reject) => {
      http.get({ port: free, host: '127.0.0.1' }, (res) => {
        let text = '';
        res.on('data', (chunk) => (text += chunk));
        res.on('end', () => resolve(text));
      }).on('error', reject);
    });
    expect(body).toBe('ok');
  });

  it('rejects with EADDRINUSE instead of raising an unhandled error event', async () => {
    const taken = await occupyAnyPort();
    const err = await listenOn(respondOk, taken).then(
      () => null,
      (e: NodeJS.ErrnoException) => e,
    );
    expect(err).not.toBeNull();
    expect(err?.code).toBe('EADDRINUSE');
  });
});

describe('describeListenError', () => {
  it('names the port and the way out for EADDRINUSE, on one line', () => {
    const message = describeListenError(
      Object.assign(new Error('listen EADDRINUSE: address already in use :::4412'), { code: 'EADDRINUSE' }),
      4412,
    );
    expect(message).toContain('4412');
    expect(message).toMatch(/already in use/);
    expect(message).toMatch(/PORT/);
    expect(message).not.toContain('\n');
  });

  it('explains EACCES', () => {
    const message = describeListenError(
      Object.assign(new Error('listen EACCES: permission denied 0.0.0.0:80'), { code: 'EACCES' }),
      80,
    );
    expect(message).toMatch(/Permission denied/);
    expect(message).toContain('80');
  });

  it('falls back to the error text for anything else', () => {
    const message = describeListenError(new Error('boom'), 4000);
    expect(message).toContain('4000');
    expect(message).toContain('boom');
  });
});
