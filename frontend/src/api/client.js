const JSON_HEADERS = { "Content-Type": "application/json" };
const ACCESS_TOKEN_KEY = "commchatbot.access_token";
const REFRESH_TOKEN_KEY = "commchatbot.refresh_token";
let refreshPromise = null;

export function getAuthToken() {
  return localStorage.getItem(ACCESS_TOKEN_KEY) || "";
}

export function setAuthToken(token) {
  if (token) localStorage.setItem(ACCESS_TOKEN_KEY, token);
  else localStorage.removeItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_TOKEN_KEY) || "";
}

export function setAuthSession(session) {
  setAuthToken(session?.access_token || "");
  if (session?.refresh_token) localStorage.setItem(REFRESH_TOKEN_KEY, session.refresh_token);
  else localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function clearAuthSession() {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

function headers(extra = {}) {
  const token = getAuthToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

export async function apiGet(path) {
  const response = await authorizedFetch(path, { headers: headers() });
  return parseResponse(response);
}

export async function apiPost(path, body) {
  const response = await authorizedFetch(path, {
    method: "POST",
    headers: headers(JSON_HEADERS),
    body: JSON.stringify(body),
  });
  return parseResponse(response);
}

export async function apiDelete(path) {
  const response = await authorizedFetch(path, { method: "DELETE", headers: headers() });
  return parseResponse(response);
}

export async function apiPatch(path, body) {
  const response = await authorizedFetch(path, {
    method: "PATCH",
    headers: headers(JSON_HEADERS),
    body: JSON.stringify(body),
  });
  return parseResponse(response);
}

export async function uploadFile(path, file) {
  const form = new FormData();
  form.append("file", file);
  const response = await authorizedFetch(path, { method: "POST", headers: headers(), body: form });
  return parseResponse(response);
}

export async function streamChat(payload, onEvent) {
  const response = await authorizedFetch("/api/chat/stream", {
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
  const response = await authorizedFetch(path, {
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

async function authorizedFetch(path, options = {}, retry = true) {
  const nextOptions = { ...options, headers: headers(options.headers || {}) };
  let response = await fetch(path, nextOptions);
  if (response.status !== 401 || !retry || path === "/api/auth/refresh" || !getRefreshToken()) {
    return response;
  }
  const refreshed = await refreshAccessToken();
  if (!refreshed) return response;
  response = await fetch(path, { ...options, headers: headers(options.headers || {}) });
  return response;
}

async function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch("/api/auth/refresh", {
          method: "POST",
          headers: JSON_HEADERS,
          body: JSON.stringify({ refresh_token: getRefreshToken() }),
        });
        if (!response.ok) throw new Error("Session refresh failed");
        setAuthSession(await response.json());
        return true;
      } catch {
        clearAuthSession();
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
  }
  return refreshPromise;
}

async function parseResponse(response) {
  if (!response.ok) {
    const text = await response.text();
    const error = new Error(text);
    error.status = response.status;
    try { error.payload = JSON.parse(text); } catch { error.payload = null; }
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}
