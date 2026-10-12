import type { Router, Request, Response, NextFunction } from 'express';

// Express 4 does not look at the promise an async route handler returns. If the
// handler rejects (a throw outside its own try/catch, a bad request body, a
// database error before the try block), the rejection is unhandled: the request
// hangs until the client gives up, and on Node 15+ the process terminates
// unless something has registered an 'unhandledRejection' listener.
//
// forwardAsyncErrors() wraps every handler registered through
// get/post/put/patch/delete/all on a router so a rejected promise or a
// synchronous throw is passed to next(err), i.e. to the app-level error
// handler, which renders the 500 page. Handlers with four parameters (error
// handlers) are left alone. Use it once where the router is created:
//
//   const router = forwardAsyncErrors(Router());

type RouteHandler = (req: Request, res: Response, next: NextFunction) => unknown;

const WRAPPED_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'all'] as const;

function wrapHandler(handler: unknown): unknown {
  if (Array.isArray(handler)) return handler.map(wrapHandler);
  if (typeof handler !== 'function' || handler.length >= 4) return handler;

  const fn = handler as RouteHandler;
  return function forwardingHandler(req: Request, res: Response, next: NextFunction): void {
    const fail = (err: unknown): void => {
      const error = err instanceof Error ? err : new Error(`Route handler rejected with ${String(err)}`);
      if (res.headersSent) {
        // Too late to render an error page; do not leave the socket hanging.
        console.error('Route handler failed after the response started:', error.message);
        res.end();
        return;
      }
      next(error);
    };
    try {
      const result = fn(req, res, next);
      if (result && typeof (result as Promise<unknown>).then === 'function') {
        (result as Promise<unknown>).then(undefined, fail);
      }
    } catch (err) {
      fail(err);
    }
  };
}

export function forwardAsyncErrors<T extends Router>(router: T): T {
  const target = router as unknown as Record<string, (...args: unknown[]) => unknown>;
  for (const method of WRAPPED_METHODS) {
    const original = target[method].bind(router);
    target[method] = (path: unknown, ...handlers: unknown[]) => original(path, ...handlers.map(wrapHandler));
  }
  return router;
}
