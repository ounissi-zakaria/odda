import { spawn } from "node:child_process";
import { openSync } from "node:fs";
import { access } from "node:fs/promises";
import { createConnection } from "node:net";
import { join } from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const RUNTIME_DIR = process.env.XDG_RUNTIME_DIR || "/tmp";

const ppid = process.pid;
const socketPath = join(RUNTIME_DIR, `odda-${ppid}.sock`);
const logPath = join(RUNTIME_DIR, `odda-${ppid}.log`);
const sleep = (ms: number): Promise<void> => {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
};

async function waitForSocket(socketPath: string, timeoutMs = 5000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      await access(socketPath);
      return;
    } catch {
      await sleep(50);
    }
  }
  throw new Error(`Timed out waiting for odda server at ${socketPath}`);
}

/** True when a server is accepting connections on the socket. */
function socketIsLive(socketPath: string, timeoutMs = 500): Promise<boolean> {
  const { promise, resolve } = Promise.withResolvers<boolean>();
  const sock = createConnection(socketPath);
  let settled = false;
  const done = (live: boolean): void => {
    if (settled) return;
    settled = true;
    sock.destroy();
    resolve(live);
  };
  sock.once("connect", () => done(true));
  sock.once("error", () => done(false));
  sock.setTimeout(timeoutMs, () => done(false));
  return promise;
}

export default async function (pi: ExtensionAPI) {

  const startServer = async (cwd: string) => {
        const dataDir = join(cwd, ".odda");

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

  pi.on("session_start", async (_event, ctx) => {
    // Dedup across sessions in this process: the socket path is pid-derived,
    // and every session loads this plugin as a fresh module, so a module-level
    // flag cannot dedupe. Probe the socket instead: a live server answers a
    // connect; a stale socket file (dead server) fails the connect and the
    // spawn below rebinds it (server.py unlinks the stale file). No
    // process.env writes — env injection is per-call in the tool_call handler.
    if (!(await socketIsLive(socketPath))) {
      await startServer(ctx.cwd);
    }
  });

  pi.on("tool_call", (event, ctx) => {
    if (event.toolName !== "bash") return;
    return {
      input: {
        ...event.input,
        env: {
          ...(event.input.env ?? {}),
          ODDA_SOCKET: socketPath,
          ODDA_LOG: logPath,
          ODDA_DATA_DIR: join(ctx.cwd, ".odda"),
        },
      },
    };
  });
}
