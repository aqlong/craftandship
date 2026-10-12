import { describe, it, expect, vi, afterEach } from 'vitest';
import express, { Router, Request, Response, NextFunction } from 'express';
import request from 'supertest';
import { forwardAsyncErrors } from '../../src/utils/async-routes';

function buildApp(configure: (router: Router) => void): express.Express {
  const app = express();
  const router = forwardAsyncErrors(Router());
  configure(router);
  app.use(router);
  app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
    res.status(500).json({ handled: err.message });
  });
  return app;
}

describe('forwardAsyncErrors', () => {
  const unhandled = vi.fn();
  process.on('unhandledRejection', unhandled);
  afterEach(() => unhandled.mockClear());

  it('turns an async handler rejection into a 500 from the error handler (no hang, no unhandled rejection)', async () => {
    const app = buildApp((r) => {
      r.get('/boom', async () => {
        throw new Error('db exploded before the try block');
      });
    });
    const res = await request(app).get('/boom').timeout({ deadline: 2000 });
    expect(res.status).toBe(500);
    expect(res.body.handled).toBe('db exploded before the try block');
    await new Promise((resolve) => setImmediate(resolve));
    expect(unhandled).not.toHaveBeenCalled();
  });

  it('forwards a synchronous throw as well', async () => {
    const app = buildApp((r) => {
      r.post('/sync', () => {
        throw new Error('sync failure');
      });
    });
    const res = await request(app).post('/sync');
    expect(res.status).toBe(500);
    expect(res.body.handled).toBe('sync failure');
  });

  it('wraps every handler in a chain and in array form, and leaves successful handlers alone', async () => {
    const calls: string[] = [];
    const app = buildApp((r) => {
      r.get(
        '/chain',
        (_req: Request, _res: Response, next: NextFunction) => {
          calls.push('mw1');
          next();
        },
        [
          async (_req: Request, _res: Response, next: NextFunction) => {
            calls.push('mw2');
            next();
          },
        ],
        async (_req: Request, res: Response) => {
          calls.push('final');
          res.json({ ok: true });
        }
      );
      r.put('/fail-late', [async (): Promise<void> => Promise.reject(new Error('late'))]);
    });
    const ok = await request(app).get('/chain');
    expect(ok.status).toBe(200);
    expect(ok.body).toEqual({ ok: true });
    expect(calls).toEqual(['mw1', 'mw2', 'final']);

    const bad = await request(app).put('/fail-late');
    expect(bad.status).toBe(500);
  });

  it('survives a rejection with a non-Error value', async () => {
    const app = buildApp((r) => {
      r.delete('/odd', async () => {
        throw undefined;
      });
    });
    const res = await request(app).delete('/odd');
    expect(res.status).toBe(500);
  });

  it('does not touch four-argument error handlers registered on the router', async () => {
    const seen = vi.fn();
    const app = express();
    const router = forwardAsyncErrors(Router());
    router.get('/x', (_req, _res, next) => next(new Error('x')));
    // A four-argument handler registered through the wrapped method: if the
    // wrapper treated it as a normal handler it would be called as (req, res,
    // next) and never receive the error.
    router.get('/w', (_req, _res, next) => next(new Error('w')), (err: Error, _req: Request, res: Response, _next: NextFunction) => {
      res.status(418).send(`router-level:${err.message}`);
    });
    router.get('/y', (_req, res) => res.send('y'));
    router.all('/z', (_req, res) => res.send('z'));
    app.use(router);
    app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
      seen(err.message);
      res.status(500).send('handled');
    });
    expect((await request(app).get('/x')).text).toBe('handled');
    const routerLevel = await request(app).get('/w');
    expect(routerLevel.status).toBe(418);
    expect(routerLevel.text).toBe('router-level:w');
    expect((await request(app).get('/y')).text).toBe('y');
    expect((await request(app).post('/z')).text).toBe('z');
    expect(seen).toHaveBeenCalledWith('x');
    expect(seen).not.toHaveBeenCalledWith('w');
  });
});
