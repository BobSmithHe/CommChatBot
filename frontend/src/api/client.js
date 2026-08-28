const JSON_HEADERS = { "Content-Type": "application/json" };
const TOKEN_KEY = "commchatbot.access_token";

export function getAuthToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setAuthToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function headers(extra = {}) {
  const token = getAuthToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

export async function apiGet(path) {
  const response = await fetch(path, { headers: headers() });
  return parseResponse(response);
}

export async function apiPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: headers(JSON_HEADERS),
    body: JSON.stringify(body),
  });
  return parseResponse(response);
}

export async function apiDelete(path) {
  const response = await fetch(path, { method: "DELETE", headers: headers() });
  return parseResponse(response);
}

export async function apiPatch(path, body) {
  const response = await fetch(path, {
    method: "PATCH",
    headers: headers(JSON_HEADERS),
    body: JSON.stringify(body),
  });
  return parseResponse(response);
}

export async function uploadFile(path, file) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(path, { method: "POST", headers: headers(), body: form });
  return parseResponse(response);
}

export async function streamChat(payload, onEvent) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: headers(JSON_HEADERS),
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) {
    throw new Error(await response.text());
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const line = frame.split("\n").find((item) => item.startsWith("data: "));
      if (!line) continue;
      onEvent(JSON.parse(line.slice(6)));
    }
  }
}

export async function streamTerminal(path, payload, onEvent) {
  const response = await fetch(path, {
    method: "POST",
    headers: headers(JSON_HEADERS),
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) throw new Error(await response.text());
  await consumeSse(response.body, onEvent);
}

async function consumeSse(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const line = frame.split("\n").find((item) => item.startsWith("data: "));
      if (line) await onEvent(JSON.parse(line.slice(6)));
    }
  }
}

async function parseResponse(response) {
  if (!response.ok) {
    throw new Error(await response.text());
  }
  if (response.status === 204) return null;
  return response.json();
}
