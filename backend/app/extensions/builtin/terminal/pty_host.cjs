"use strict";

const readline = require("node:readline");
const pty = require(process.argv[6]);

const root = process.argv[2];
const cols = Number(process.argv[3]);
const rows = Number(process.argv[4]);
const shell = process.argv[5];
const shellArgs = process.argv[7]
  ? JSON.parse(Buffer.from(process.argv[7], "base64url").toString("utf8"))
  : ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"];
const terminal = pty.spawn(shell, shellArgs, {
  name: "xterm-256color",
  cols,
  rows,
  cwd: root,
  env: process.env,
  useConpty: true,
});

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

terminal.onData((data) => send({ type: "output", data: Buffer.from(data, "utf8").toString("base64") }));
terminal.onExit(({ exitCode }) => {
  send({ type: "exit", exitCode });
  process.exit(0);
});
send({ type: "ready", pid: terminal.pid });

readline.createInterface({ input: process.stdin, crlfDelay: Infinity }).on("line", (raw) => {
  try {
    const message = JSON.parse(raw);
    if (message.type === "input") terminal.write(Buffer.from(message.data || "", "base64").toString("utf8"));
    else if (message.type === "resize") terminal.resize(Math.max(20, Number(message.cols)), Math.max(5, Number(message.rows)));
    else if (message.type === "close") terminal.kill();
  } catch (error) {
    send({ type: "error", message: error?.name || "TerminalHostError" });
  }
});

process.stdin.on("end", () => terminal.kill());
