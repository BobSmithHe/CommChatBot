<template>
  <div ref="host" class="terminal-host" @click="focusTerminal" />
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { getAuthToken } from "../api/client";

const props = defineProps({
  conversationId: { type: [Number, String], required: true },
  terminalId: { type: String, required: true },
});
const emit = defineEmits(["ready", "exit", "status"]);
const host = ref(null);
let terminal;
let fitAddon;
let socket;
let resizeObserver;
let dataDisposable;
let resizeDisposable;
let reconnectTimer;
let restartStatusTimer;
let intentionalClose = false;

onMounted(async () => {
  terminal = new Terminal({
    allowProposedApi: false,
    convertEol: false,
    cursorBlink: true,
    cursorStyle: "bar",
    fontFamily: '"Cascadia Mono", "JetBrains Mono", Consolas, monospace',
    fontSize: 13,
    lineHeight: 1.16,
    scrollback: 5000,
    theme: {
      background: "#090d12",
      foreground: "#d9e2ec",
      cursor: "#66d9a8",
      selectionBackground: "#335f5166",
    },
  });
  fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(host.value);
  terminal.attachCustomKeyEventHandler((event) => {
    if (event.type === "keydown" && event.ctrlKey && !event.altKey && !event.metaKey && event.key.toLowerCase() === "z") {
      // Browsers reserve Ctrl+Z for page editing and do not reliably pass it
      // through xterm. Ask the server to emit the platform-correct EOF bytes:
      // EOT on Unix/Docker, Ctrl+Z + Enter on Windows ConPTY.
      event.preventDefault();
      if (!event.repeat) send({ type: "eof" });
      return false;
    }
    return true;
  });
  dataDisposable = terminal.onData((data) => send({ type: "input", data }));
  resizeDisposable = terminal.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
  resizeObserver = new ResizeObserver(() => fit());
  resizeObserver.observe(host.value);
  await nextTick();
  fit();
  connect();
});

watch(() => [props.conversationId, props.terminalId], () => {
  if (!terminal) return;
  disconnect();
  terminal.clear();
  intentionalClose = false;
  connect();
});

onBeforeUnmount(() => {
  intentionalClose = true;
  clearTimeout(reconnectTimer);
  clearTimeout(restartStatusTimer);
  disconnect();
  resizeObserver?.disconnect();
  dataDisposable?.dispose();
  resizeDisposable?.dispose();
  terminal?.dispose();
});

function connect() {
  if (!props.conversationId || !props.terminalId || !terminal) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(
    `${protocol}//${location.host}/api/conversations/${props.conversationId}/workspace/terminals/${props.terminalId}/ws`,
  );
  socket.addEventListener("open", () => {
    fit();
    send({ type: "auth", token: getAuthToken(), cols: terminal.cols, rows: terminal.rows });
    emit("status", "connected");
  });
  socket.addEventListener("message", (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === "output") terminal.write(message.data || "");
    else if (message.type === "ready") {
      emit("ready", message.terminal);
      terminal.focus();
    } else if (message.type === "exit") {
      terminal.write("\r\n\x1b[90m[terminal process exited]\x1b[0m\r\n");
      emit("exit");
    } else if (message.type === "terminal_restarted") {
      emit("ready", message.terminal);
      emit("status", "restarted");
      clearTimeout(restartStatusTimer);
      restartStatusTimer = setTimeout(() => emit("status", "connected"), 2500);
    }
  });
  socket.addEventListener("close", (event) => {
    if (socket !== event.target) return;
    emit("status", "disconnected");
    if (!intentionalClose && event.code < 4400) reconnectTimer = setTimeout(connect, 800);
  });
  socket.addEventListener("error", () => emit("status", "error"));
}

function disconnect() {
  if (!socket) return;
  const active = socket;
  socket = null;
  active.close();
}

function send(payload) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

function fit() {
  if (!fitAddon || !host.value || host.value.clientWidth < 20 || host.value.clientHeight < 20) return;
  try { fitAddon.fit(); } catch { /* hidden panel during layout transitions */ }
}

function focusTerminal() {
  terminal?.focus();
}

function sendCtrlC() {
  send({ type: "input", data: "\u0003" });
  terminal?.focus();
}

defineExpose({ fit, focus: focusTerminal, sendCtrlC });
</script>

<style scoped>
.terminal-host {
  width: 100%;
  height: 100%;
  min-height: 0;
  padding: 5px 8px 4px;
  background: #090d12;
  overflow: hidden;
}

.terminal-host :deep(.xterm) {
  height: 100%;
}
</style>
