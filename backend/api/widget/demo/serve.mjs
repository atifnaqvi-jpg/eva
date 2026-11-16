/**
 * Serve the demo host page with the bot's public id substituted.
 *
 *     node demo/serve.mjs BOT_PUBLIC_ID [--port 8099]
 *
 * The page is served through the API origin in the gate run (Caddy routes `/demo/*`
 * there), because the widget's domain check compares the *host page's* Origin and a
 * `file://` or `localhost:8099` page would be correctly refused. This server exists
 * for local iteration on the markup, not as the verification path.
 */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const botId = process.argv[2];
if (!botId) {
  console.error("usage: node demo/serve.mjs BOT_PUBLIC_ID [--port 8099]");
  process.exit(2);
}

const portFlag = process.argv.indexOf("--port");
const port = portFlag === -1 ? 8099 : Number(process.argv[portFlag + 1]);

const page = readFileSync(join(HERE, "host.html"), "utf8").replace(
  "BOT_PUBLIC_ID",
  botId,
);

createServer((request, response) => {
  if (request.url === "/" || request.url.startsWith("/demo")) {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(page);
    return;
  }
  response.writeHead(404).end("not found");
}).listen(port, () => {
  console.log(`demo host page: http://localhost:${port}/ (bot ${botId})`);
});
