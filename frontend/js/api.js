// Small wrapper around fetch(): adds the JWT, parses JSON, refreshes an
// expired token once, and turns API errors into readable messages.

const auth = {
  get access() { return localStorage.getItem("access"); },
  get refresh() { return localStorage.getItem("refresh"); },
  save({ access, refresh, user }) {
    if (access) localStorage.setItem("access", access);
    if (refresh) localStorage.setItem("refresh", refresh);
    if (user) localStorage.setItem("user", JSON.stringify(user));
  },
  user() { return JSON.parse(localStorage.getItem("user") || "null"); },
  clear() { localStorage.removeItem("access"); localStorage.removeItem("refresh"); localStorage.removeItem("user"); },
};

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

// DRF errors come in several shapes; flatten them into one sentence.
function errorMessage(data) {
  if (!data) return "Something went wrong.";
  if (typeof data === "string") return data;
  if (data.detail) return data.detail;
  return Object.entries(data)
    .map(([field, msgs]) => {
      const text = Array.isArray(msgs) ? msgs.join(" ") : msgs;
      return field === "non_field_errors" ? text : `${field}: ${text}`;
    })
    .join(" ");
}

async function tryRefreshToken() {
  if (!auth.refresh) return false;
  const res = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh: auth.refresh }),
  });
  if (!res.ok) return false;
  auth.save(await res.json());
  return true;
}

async function api(path, { method = "GET", body, headers = {}, retry = true } = {}) {
  const opts = { method, headers: { ...headers } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (auth.access) opts.headers["Authorization"] = `Bearer ${auth.access}`;

  const res = await fetch(`${API_BASE}${path}`, opts);

  // Access token expired? Refresh once and repeat the same request.
  if (res.status === 401 && retry && (await tryRefreshToken())) {
    return api(path, { method, body, headers, retry: false });
  }

  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(errorMessage(data), res.status, data);
  return { data, replayed: res.headers.get("Idempotent-Replayed") === "true" };
}

// A fresh idempotency key for each payment attempt.
function newIdempotencyKey() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatRupees(value) {
  return "₹" + Number(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Shared PockIt logo (coin dropping into a pocket).
const POCKIT_LOGO =
  '<svg class="brand-mark" viewBox="0 0 48 48" aria-hidden="true">' +
  '<rect class="bm-tile" width="48" height="48" rx="12"/>' +
  '<circle cx="24" cy="15" r="7.5" fill="#E3B04B"/>' +
  '<circle cx="24" cy="15" r="4.2" fill="none" stroke="#0B1F3A" stroke-width="1.4" opacity=".5"/>' +
  '<path d="M10.5 20.5h27v11L24 39.5l-13.5-8z" fill="#FFFFFF"/>' +
  '<path d="M14 24.5h20" stroke="#0B1F3A" stroke-width="1.5" stroke-dasharray="2.2 1.8" opacity=".55"/></svg>';
