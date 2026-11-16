/**
 * Build both bundles with esbuild and enforce the §12.2 size budget.
 *
 * The budget is enforced here, at build time, rather than measured in a test after
 * the fact: a loader that quietly grows past 10 KB should fail the build, not a
 * review. The number printed is gzipped size, which is what the constraint means.
 */

import { build } from "esbuild";
import { gzipSync } from "node:zlib";
import { readFileSync, mkdirSync, copyFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { writeFileSync } from "node:fs";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, "dist");

const LOADER_BUDGET = 10 * 1024;

mkdirSync(OUT, { recursive: true });

const common = {
  bundle: true,
  minify: true,
  target: ["es2019", "chrome80", "firefox78", "safari13"],
  legalComments: "none",
  logLevel: "warning",
};

await build({
  ...common,
  entryPoints: [join(HERE, "src/loader.ts")],
  outfile: join(OUT, "loader.js"),
  format: "iife",
});

await build({
  ...common,
  entryPoints: [join(HERE, "src/app/app.tsx")],
  outfile: join(OUT, "app.js"),
  format: "iife",
  jsx: "automatic",
  jsxImportSource: "preact",
  // Preact's production branch; without this the dev build ships its warnings.
  define: { "process.env.NODE_ENV": '"production"' },
});

copyFileSync(join(HERE, "src/app/index.html"), join(OUT, "index.html"));

// The gate's host page ships with the bundle it embeds, so the served snippet and
// the served loader are always the same build.
mkdirSync(join(OUT, "demo"), { recursive: true });
copyFileSync(join(HERE, "demo/host.html"), join(OUT, "demo/host.html"));

const loader = readFileSync(join(OUT, "loader.js"));
const app = readFileSync(join(OUT, "app.js"));
const gz = (buffer) => gzipSync(buffer, { level: 9 }).length;

const loaderGz = gz(loader);
const appGz = gz(app);

// Recorded for the Python-side size assertion and for the M3 gate evidence, so the
// number quoted in docs comes from the same build that ships.
writeFileSync(
  join(OUT, "sizes.json"),
  `${JSON.stringify(
    {
      loader: { raw: loader.length, gzip: loaderGz, budget: LOADER_BUDGET },
      app: { raw: app.length, gzip: appGz },
    },
    null,
    2,
  )}\n`,
);
console.log(
  `loader.js ${loader.length} B raw / ${loaderGz} B gz  (budget ${LOADER_BUDGET} B)`,
);
console.log(`app.js    ${app.length} B raw / ${appGz} B gz`);

if (loaderGz > LOADER_BUDGET) {
  console.error(
    `\nFAIL: loader is ${loaderGz} B gzipped, over the ${LOADER_BUDGET} B budget (§12.2).`,
  );
  process.exit(1);
}
