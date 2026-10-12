import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    include: ['tests/unit/**/*.test.ts'],
    // Keeps a local web/.env out of the unit suite (see the file).
    setupFiles: ['tests/unit/setup-no-dotenv.ts'],
    exclude: ['tests/ui/**', 'node_modules/**'],
    // Several files import the whole Express app in their first test. On a
    // cold, loaded machine (a shared CI runner with every file in parallel)
    // that import alone can pass the 5-second default, so give tests and
    // hooks more room. A test that hangs still fails; it just takes longer.
    testTimeout: 20000,
    hookTimeout: 20000,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/**/*.ts'],
      exclude: ['src/server.ts', 'src/db/index.ts'],
    },
  },
});
