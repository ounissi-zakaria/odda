import { spawn } from "node:child_process";
import { openSync } from "node:fs";
import { access } from "node:fs/promises";
import { join } from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const RUNTIME_DIR = process.env.XDG_RUNTIME_DIR || "/tmp";

const ppid = process.pid;
const socketPath = join(RUNTIME_DIR, `odda-${ppid}.sock`);
const logPath = join(RUNTIME_DIR, `odda-${ppid}.log`);
process.env.ODDA_SOCKET = socketPath;
process.env.ODDA_LOG = logPath;

async function waitForSocket(socketPath: string, timeoutMs = 5000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      await access(socketPath);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }
  throw new Error(`Timed out waiting for odda server at ${socketPath}`);
}

export default async function (pi: ExtensionAPI) {

  const startServer = async (cwd: string) => {

        const dataDir = join(cwd, ".odda");
        process.env.ODDA_DATA_DIR = dataDir;

        const oddaBin = process.env.ODDA_BIN || "odda";
        const out = openSync(logPath, "a");
        
        const server = spawn(
          oddaBin,
          [
            "server",
            "--socket", socketPath,
            "--data-dir", dataDir,
            "--log", logPath,
            "--parent-pid", String(ppid),
          ],
          {
            stdio: ["ignore", out, out],
            detached: false,
          },
        );

        server.on("error", (err) => {
          console.error("[odda] failed to start server:", err);
        });

        await waitForSocket(socketPath);
  };

  pi.on("session_start", async (event, ctx) => {
    console.error(`[odda] session_start reason=${event.reason} cwd=${ctx.cwd}`);
    // The server is owned by the harness process (it dies with
    // --parent-pid), so only the process-boot session_start spawns it.
    // Subagent runtimes fire their own session_start; if that reason is
    // also "startup", the socket probe dedupes the spawn.
    if (event.reason === "startup") {
      await startServer(ctx.cwd);
    }
  });
}