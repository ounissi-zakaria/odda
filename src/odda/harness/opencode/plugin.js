import { spawn } from "node:child_process";
import { openSync } from "node:fs";
import { access } from "node:fs/promises";
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
  const logPath = join(RUNTIME_DIR, `odda-${ppid}.log`);
  const dataDir = join(directory, ".odda");
  const oddaBin = process.env.ODDA_BIN || "odda";
  let started = false;

  const startServer = async () => {
    if (started) {
      return;
    }
    started = true;

    // The log file lives in the session dir (next to the socket), not the
    // data dir. The data dir (.odda) is created lazily by the server on the
    // first state-producing write, so we must not create it here. Pre-opening
    // the fd preserves early-crash diagnostics (import errors, bad flags)
    // before the server's own logging initialises. The session dir already
    // exists (the socket lives there), so no mkdir is needed.
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
      output.env.ODDA_LOG = logPath;
    },
  };
};
