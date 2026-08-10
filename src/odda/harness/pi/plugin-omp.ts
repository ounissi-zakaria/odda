import { spawn } from "node:child_process";
import { openSync } from "node:fs";
import { access } from "node:fs/promises";
import { join } from "node:path";

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const RUNTIME_DIR = process.env.XDG_RUNTIME_DIR || "/tmp";

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
  let started = false;
  let envVars: Record<string, string> = {};

  const startServer = async (cwd: string) => {
    if (started) {
      return;
    }
    started = true;

    const ppid = process.pid;
    const socketPath = join(RUNTIME_DIR, `odda-${ppid}.sock`);
    const logPath = join(RUNTIME_DIR, `odda-${ppid}.log`);
    const dataDir = join(cwd, ".odda");
    const oddaBin = process.env.ODDA_BIN || "odda";

    envVars = {
      ODDA_SOCKET: socketPath,
      ODDA_DATA_DIR: dataDir,
      ODDA_LOG: logPath,
    };

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
    await startServer(ctx.cwd);
  });

  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return;
    const existingEnv = (event.input as { env?: Record<string, string> }).env ?? {};
    return {
      input: {
        ...event.input,
        env: { ...existingEnv, ...envVars },
      },
    };
  });
}