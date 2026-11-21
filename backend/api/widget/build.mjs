/**
 * Build both bundles with esbuild and enforce the §12.2 size budget.
 *
 * The budget is enforced here, at build time, rather than measured in a test after
 * the fact: a loader that quietly grows past 10 KB should fail the build, not a
 * review. The number printed is gzipped size, which is what the constraint means.
 */

import { build } from "esbuild";
import { gzipSync } from "node:zlib";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, renameSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

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

// The shell must reference the bundle it was built WITH. A browser that cached the old
// shell keeps asking for /widget/app.js and gets whatever the server now serves -- a
// mismatch that reads as "my change did nothing" although the new bytes are live.
//
// So the app bundle is written under a content hash and the shell points at that name:
// a new build is a new URL, which no cache can answer with stale bytes.
const appHash = createHash("sha256").update(app).digest("hex").slice(0, 12);
const appFile = `app.${appHash}.js`;
renameSync(join(OUT, "app.js"), join(OUT, appFile));

const shell = readFileSync(join(HERE, "src/app/index.html"), "utf8");
if (!shell.includes("/widget/app.js")) {
  throw new Error(
    "index.html no longer references /widget/app.js; the hash rewrite below would do nothing",
  );
}
writeFileSync(join(OUT, "index.html"), shell.replace("/widget/app.js", `/widget/${appFile}`));
console.log(`shell references ${appFile}`);
