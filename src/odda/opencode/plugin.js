import { spawn } from "node:child_process";
import { openSync } from "node:fs";
import { access, mkdir } from "node:fs/promises";
import { join } from "node:path";

const RUNTIME_DIR = process.env.XDG_RUNTIME_DIR || "/tmp";

async function waitForSocket(socketPath, timeoutMs = 5000) {
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

export const OddaPlugin = async ({ directory }) => {
  const ppid = process.pid;
  const socketPath = join(RUNTIME_DIR, `odda-${ppid}.sock`);
  const dataDir = join(directory, ".odda");
  const oddaBin = process.env.ODDA_BIN || "odda";
  let started = false;

  const startServer = async () => {
    if (started) {
      return;
    }
    started = true;

    await mkdir(dataDir, { recursive: true });
    const logFile = join(dataDir, "server.log");
    const out = openSync(logFile, "a");

    const server = spawn(
      oddaBin,
      [
        "server",
        "--socket", socketPath,
        "--data-dir", dataDir,
        "--parent-pid", String(ppid),
      ],
      {
        stdio: ["ignore", out, out],
        detached: false,
      }
    );

    server.on("error", (err) => {
      console.error("[odda] failed to start server:", err);
    });

    await waitForSocket(socketPath);
  };

  return {
    event: async ({ event }) => {
      if (event.type === "session.created") {
        await startServer();
      }
    },
    "shell.env": async (_input, output) => {
      if (!started) {
        await startServer();
      }
      output.env.ODDA_SOCKET = socketPath;
      output.env.ODDA_DATA_DIR = dataDir;
    },
  };
};
