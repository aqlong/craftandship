import http from 'http';

// Binding the HTTP port is the one start-up step whose failure is routine
// (another copy of the server, or an unrelated program, already holds the
// port). http.Server reports it as an 'error' event; with no listener Node
// throws it as an uncaught exception and prints a stack trace. listenOn()
// turns the event into a rejected promise, and describeListenError() into one
// line a person can act on.

/**
 * Starts `handler` listening on `port` and resolves with the server once the
 * port is bound. Rejects with the server's own error (for example
 * EADDRINUSE or EACCES) when it cannot be.
 */
export function listenOn(handler: http.RequestListener, port: number): Promise<http.Server> {
  return new Promise((resolve, reject) => {
    const server = http.createServer(handler);
    const onError = (err: Error): void => {
      reject(err);
    };
    server.once('error', onError);
    server.listen(port, () => {
      // From here on, errors are no longer start-up errors: drop this
      // listener so Node's default handling for a running server applies.
      server.removeListener('error', onError);
      resolve(server);
    });
  });
}

/** One line saying why the port could not be bound and what to do about it. */
export function describeListenError(err: NodeJS.ErrnoException, port: number): string {
  switch (err.code) {
    case 'EADDRINUSE':
      return `Port ${port} is already in use (EADDRINUSE). Stop the program using it or set PORT to a free port.`;
    case 'EACCES':
      return `Permission denied binding port ${port} (EACCES). Use a port above 1023 or run with the needed privileges.`;
    default:
      return `Could not listen on port ${port}: ${err.code ? `${err.code}: ` : ''}${err.message}`;
  }
}
