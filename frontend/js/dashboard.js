// Dashboard: balance, send / request / add money, requests, passbook.

if (!auth.access) window.location.href = "index.html";

const PAGE_SIZE = 20;
const REFRESH_EVERY_MS = 10000;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const el = {
  balance: document.getElementById("balance"),
  owner: document.getElementById("wallet-owner"),
  userName: document.getElementById("user-name"),
  avatar: document.getElementById("avatar"),
  toast: document.getElementById("toast"),
  history: document.getElementById("history"),
  count: document.getElementById("passbook-count"),
  prev: document.getElementById("prev-page"),
  next: document.getElementById("next-page"),
  pageInfo: document.getElementById("page-info"),
  requests: document.getElementById("requests"),
  requestList: document.getElementById("request-list"),
  requestsMeta: document.getElementById("requests-meta"),
  topupForm: document.getElementById("topup-form"),
  transferForm: document.getElementById("transfer-form"),
  requestForm: document.getElementById("request-form"),
};

let currentPage = 1;
let currentBalance = null;

// ---------------------------------------------------------------- helpers
const HOLD_TOAST =
  "Payment on hold for a quick security review. You'll see it in your passbook; " +
  "if it isn't approved, the money comes back to you.";
let toastTimer;
function toast(text, type = "success") {
  clearTimeout(toastTimer);
  el.toast.textContent = text;
  el.toast.classList.toggle("is-error", type === "error");
  el.toast.classList.toggle("is-hold", type === "hold");
  el.toast.hidden = false;
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, type === "success" ? 4000 : 7000);
}

function handleError(err) {
  if (err.status === 401) {
    auth.clear();
    window.location.href = "index.html";
    return;
  }
  toast(err.status ? err.message : "Network problem. Check your connection and try again.", "error");
}

function initials(name) {
  return (name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("");
}

function formatWhen(iso) {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit",
  });
}

// Build an element with a class and plain text (never innerHTML for user data).
function textEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- balance
// After a payment the balance counts to its new value: the one animation on the page.
function showBalance(value) {
  const target = Number(value);
  const start = currentBalance;
  currentBalance = target;

  if (start === null || start === target || reduceMotion) {
    el.balance.textContent = formatRupees(target);
    return;
  }

  const duration = 700;
  const t0 = performance.now();
  el.balance.classList.add("is-updated");
  function frame(now) {
    const p = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    el.balance.textContent = formatRupees(start + (target - start) * eased);
    if (p < 1) requestAnimationFrame(frame);
    else setTimeout(() => el.balance.classList.remove("is-updated"), 300);
  }
  requestAnimationFrame(frame);
}

async function loadWallet() {
  try {
    const { data } = await api("/wallet");
    showBalance(data.balance);
    const name = data.name || data.email;
    el.userName.textContent = name;
    el.avatar.textContent = initials(name);
    el.owner.textContent = data.email;
  } catch (err) {
    handleError(err);
  }
}

// ---------------------------------------------------------------- passbook
const ARROW_OUT = '<svg viewBox="0 0 24 24"><path d="M7 17 17 7M9 7h8v8"/></svg>';
const ARROW_IN = '<svg viewBox="0 0 24 24"><path d="M17 7 7 17M15 17H7V9"/></svg>';
const BAG = '<svg viewBox="0 0 24 24"><path d="M6 8h12l-1 12H7z"/><path d="M9 8a3 3 0 0 1 6 0"/></svg>';
const UNDO = '<svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>';
const ICONS = {
  TOPUP: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  TRANSFER_OUT: ARROW_OUT,
  TRANSFER_IN: ARROW_IN,
  PAYMENT_OUT: BAG,
  PAYMENT_IN: BAG,
  REFUND_OUT: UNDO,
  REFUND_IN: UNDO,
  HOLD_RETURN: UNDO,
};
const ICON_CLASS = {
  TOPUP: "topup", TRANSFER_OUT: "out", TRANSFER_IN: "in", PAYMENT_OUT: "out", PAYMENT_IN: "in",
  REFUND_OUT: "out", REFUND_IN: "in", HOLD_RETURN: "in",
};
const HOLD_LABEL = { HELD: "On hold for review", REJECTED: "Not approved" };

function renderRow(entry) {
  const li = textEl("li", "ledger-row");
  const isIn = Number(entry.amount) > 0;

  const details = textEl("div", "ledger-details");
  const icon = textEl("span", `ledger-icon ledger-icon--${ICON_CLASS[entry.entry_type]}`);
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = ICONS[entry.entry_type]; // static SVG, not user data

  const text = textEl("div", "ledger-text");
  const desc = textEl("div", "ledger-desc", entry.description);
  if (HOLD_LABEL[entry.hold_status]) {
    desc.append(" ", textEl("span", `status status--${entry.hold_status}`, HOLD_LABEL[entry.hold_status]));
  }
  text.append(desc);
  if (entry.note) text.append(textEl("div", "ledger-note", entry.note));
  text.append(textEl("div", "ledger-date", formatWhen(entry.created_at)));
  details.append(icon, text);

  li.append(
    details,
    textEl("div", `ledger-amount ${isIn ? "in" : "out"}`, `${isIn ? "+" : "−"}${formatRupees(Math.abs(entry.amount))}`),
    textEl("div", "ledger-balance", formatRupees(entry.balance_after)),
  );
  return li;
}

function renderEmpty() {
  el.history.innerHTML = `
    <li class="ledger-empty">
      <strong>No transactions yet</strong>
      <span>Add money to your wallet, then send some to a friend. Every movement shows up here.</span>
      <button class="btn btn--quiet" data-open="topup">Add money</button>
    </li>`;
}

async function loadHistory(page = currentPage) {
  try {
    const { data } = await api(`/transactions?page=${page}`);
    currentPage = page;
    el.history.innerHTML = "";
    if (data.results.length === 0) renderEmpty();
    else data.results.forEach((entry) => el.history.appendChild(renderRow(entry)));

    const pages = Math.max(1, Math.ceil(data.count / PAGE_SIZE));
    el.count.textContent = data.count === 1 ? "1 entry" : `${data.count} entries`;
    el.pageInfo.textContent = `Page ${page} of ${pages}`;
    el.prev.disabled = !data.previous;
    el.next.disabled = !data.next;
    el.prev.parentElement.hidden = pages === 1;
  } catch (err) {
    handleError(err);
  }
}

el.prev.addEventListener("click", () => loadHistory(currentPage - 1));
el.next.addEventListener("click", () => loadHistory(currentPage + 1));

// ---------------------------------------------------------------- requests
const STATUS_LABEL = { PENDING: "Waiting", ON_HOLD: "On hold", PAID: "Paid", DECLINED: "Declined", CANCELLED: "Cancelled" };
const MAX_OUTGOING_SHOWN = 5;

// One idempotency key per request we try to pay, kept until the server answers.
const payKeys = new Map();
let busyRequestIds = new Set();

function renderRequest(req) {
  const incoming = req.direction === "incoming";
  const who = req.other_name || req.other_email;
  const li = textEl("li", `request-item${incoming && req.status === "PENDING" ? " is-incoming" : ""}`);
  li.dataset.id = req.id;

  const avatar = textEl("span", "avatar", initials(who));
  avatar.setAttribute("aria-hidden", "true");

  const text = textEl("div", "request-text");
  const title = textEl("div", "request-title");
  if (incoming) {
    title.append(textEl("strong", "", who), ` asked you for ${formatRupees(req.amount)}`);
  } else {
    title.append(`You asked `, textEl("strong", "", who), ` for ${formatRupees(req.amount)}`);
  }
  const sub = textEl("div", "request-sub");
  if (req.note) sub.append(textEl("span", "ledger-note", req.note), "  ");
  sub.append(formatWhen(req.created_at));
  text.append(title, sub);

  const actions = textEl("div", "request-actions");
  if (req.status === "PENDING" && incoming) {
    const pay = textEl("button", "btn btn--primary btn--sm", `Pay ${formatRupees(req.amount)}`);
    pay.dataset.action = "pay";
    pay.dataset.amount = req.amount;
    pay.dataset.who = who;
    const decline = textEl("button", "btn btn--quiet btn--sm", "Decline");
    decline.dataset.action = "decline";
    actions.append(pay, decline);
  } else if (req.status === "PENDING") {
    actions.append(textEl("span", "status status--PENDING", STATUS_LABEL.PENDING));
    const cancel = textEl("button", "btn btn--ghost btn--sm", "Cancel");
    cancel.dataset.action = "cancel";
    actions.append(cancel);
  } else {
    actions.append(textEl("span", `status status--${req.status}`, STATUS_LABEL[req.status]));
  }
  if (busyRequestIds.has(req.id)) actions.querySelectorAll("button").forEach((b) => (b.disabled = true));

  li.append(avatar, text, actions);
  return li;
}

async function loadRequests() {
  try {
    const { data } = await api("/requests");
    const incoming = data.results.filter((r) => r.direction === "incoming" && r.status === "PENDING");
    const outgoing = data.results.filter((r) => r.direction === "outgoing").slice(0, MAX_OUTGOING_SHOWN);
    const shown = [...incoming, ...outgoing];

    el.requests.hidden = shown.length === 0;
    el.requestList.replaceChildren(...shown.map(renderRequest));
    el.requestsMeta.textContent =
      incoming.length === 0 ? "" : incoming.length === 1 ? "1 waiting for you" : `${incoming.length} waiting for you`;
    el.requestsMeta.classList.toggle("is-waiting", incoming.length > 0);
  } catch (err) {
    handleError(err);
  }
}

// Paying is a two-step click: "Pay ₹250.00" → "Confirm ₹250.00" (resets after 4 s).
el.requestList.addEventListener("click", async (e) => {
  const button = e.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const id = Number(button.closest(".request-item").dataset.id);
  const action = button.dataset.action;

  if (action === "pay" && !button.classList.contains("btn--confirm")) {
    button.classList.add("btn--confirm");
    button.textContent = `Confirm ${formatRupees(button.dataset.amount)}`;
    setTimeout(() => {
      if (button.isConnected && !button.disabled) {
        button.classList.remove("btn--confirm");
        button.textContent = `Pay ${formatRupees(button.dataset.amount)}`;
      }
    }, 4000);
    return;
  }

  busyRequestIds.add(id);
  button.closest(".request-actions").querySelectorAll("button").forEach((b) => (b.disabled = true));

  try {
    if (action === "pay") {
      if (!payKeys.has(id)) payKeys.set(id, newIdempotencyKey());
      const { data } = await api(`/requests/${id}/pay`, {
        method: "POST",
        headers: { "Idempotency-Key": payKeys.get(id) },
      });
      payKeys.delete(id);
      if (data.status === "ON_HOLD") toast(HOLD_TOAST, "hold");
      else toast(`Paid ${formatRupees(data.amount)} to ${data.to_name || data.to}`);
    } else if (action === "decline") {
      await api(`/requests/${id}/decline`, { method: "POST" });
      toast("Request declined");
    } else if (action === "cancel") {
      await api(`/requests/${id}/cancel`, { method: "POST" });
      toast("Request cancelled");
    }
  } catch (err) {
    if (err.status) payKeys.delete(id); // server answered: this attempt is over
    handleError(err);
  } finally {
    busyRequestIds.delete(id);
    await refreshAll();
  }
});

// ---------------------------------------------------------------- panel tabs
function openPanel(name, focus = true) {
  document.querySelectorAll(".panel .segment").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active);
  });
  document.querySelectorAll(".panel [data-panel]").forEach((form) => {
    form.hidden = form.dataset.panel !== name;
  });
  if (focus) {
    const form = document.querySelector(`.panel [data-panel="${name}"]`);
    form.querySelector("input").focus({ preventScroll: window.innerWidth > 900 });
    if (window.innerWidth <= 900) form.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
  }
}

document.querySelectorAll(".panel .segment").forEach((tab) => {
  tab.addEventListener("click", () => openPanel(tab.dataset.tab, false));
});

// Buttons anywhere on the page with data-open="topup" / "transfer" / "request"
document.addEventListener("click", (e) => {
  const opener = e.target.closest("[data-open]");
  if (opener) openPanel(opener.dataset.open);
});

// ---------------------------------------------------------------- forms
// Payment forms keep ONE idempotency key per payment attempt:
//  - created when the user first submits
//  - kept if the request fails because of the network, so clicking again
//    retries the SAME payment (the server won't charge twice)
//  - thrown away after success or when the user edits the form (= a new payment)
function setupForm(form, { path, busyLabel, buildBody, successText, idempotent = true }) {
  let idempotencyKey = null;
  const button = form.querySelector("button[type=submit]");
  const label = button.textContent;

  form.addEventListener("input", () => { idempotencyKey = null; });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (button.disabled) return; // ignore double clicks while in flight
    if (!form.reportValidity()) return;
    if (form.dataset.recipientBlocked === "true") {
      // The email check already said this account doesn't exist (or is you).
      form.querySelector("[data-recipient-input]").focus();
      return;
    }

    const headers = {};
    if (idempotent) {
      idempotencyKey ??= newIdempotencyKey();
      headers["Idempotency-Key"] = idempotencyKey;
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyLabel;

    try {
      const body = buildBody(Object.fromEntries(new FormData(form)));
      const { data, replayed } = await api(path, { method: "POST", body, headers });
      if (data.status === "ON_HOLD") toast(HOLD_TOAST, "hold");
      else toast(successText(data) + (replayed ? " (already processed)" : ""));
      form.reset();
      idempotencyKey = null;
      currentPage = 1;
      await refreshAll();
    } catch (err) {
      // A real answer from the server (e.g. insufficient balance) ends this
      // attempt; a network failure (no status) keeps the key for a safe retry.
      if (err.status) idempotencyKey = null;
      if (!err.status && idempotent) toast("Network problem. Press the button again to retry safely.", "error");
      else handleError(err);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = button.dataset.label || label;
    }
  });
}

// ---------------------------------------------------------------- recipient check
// While you type an email, ask the server who it belongs to and show their
// name, like UPI confirming the payee before you pay. Sending to an email
// with no account is blocked before any request is made.
const CHECK_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const ALERT_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5M12 16.5v.01"/><circle cx="12" cy="12" r="9"/></svg>';
const lookupCache = new Map(); // email -> { ok, name?, message? }

function setupRecipientCheck(form, fieldName, verb) {
  const input = form.elements[fieldName];
  const status = form.querySelector(`[data-recipient-for="${fieldName}"]`);
  const button = form.querySelector("button[type=submit]");
  const defaultLabel = button.textContent;
  input.setAttribute("data-recipient-input", "");
  let timer;
  let latest = "";

  function show(state, html) {
    status.className = `recipient-check${state ? " is-" + state : ""}`;
    status.innerHTML = html;
    const blocked = state === "error";
    form.dataset.recipientBlocked = blocked;
    input.setAttribute("aria-invalid", blocked);
  }

  function setButton(name) {
    button.dataset.label = name ? `${verb} ${name.split(" ")[0]}` : defaultLabel;
    if (!button.disabled) button.textContent = button.dataset.label;
  }

  function render(email, result) {
    if (email !== latest) return; // a newer email was typed meanwhile
    if (result.ok) {
      show("ok", `${CHECK_ICON}<span></span>`);
      const name = document.createElement("strong");
      name.textContent = result.name || email;
      status.querySelector("span").append(name);
      setButton(result.name || email);
    } else if (result.message) {
      show("error", `${ALERT_ICON}<span></span>`);
      status.querySelector("span").textContent = result.message;
      setButton(null);
    } else {
      show(null, "");
      setButton(null);
    }
  }

  async function check() {
    const email = input.value.trim().toLowerCase();
    latest = email;
    if (!email || !input.checkValidity() || !/@[^@]+\.[^@]+$/.test(email)) {
      show(null, "");
      setButton(null);
      return;
    }
    if (lookupCache.has(email)) return render(email, lookupCache.get(email));

    show(null, "Checking…");
    let result;
    try {
      const { data } = await api(`/users/lookup?email=${encodeURIComponent(email)}`);
      result = { ok: true, name: data.name };
    } catch (err) {
      if (err.status === 401) return handleError(err);
      if (err.status === 404) result = { ok: false, message: "No PockIt account uses this email. Check for typos." };
      else if (err.status === 400 && err.data?.code === "self") result = { ok: false, message: "That's your own email." };
      else result = { ok: false }; // rate limited or offline: let the server decide on submit
    }
    if (result.ok || result.message) lookupCache.set(email, result);
    render(email, result);
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    latest = "";
    show(null, "");
    setButton(null);
    timer = setTimeout(check, 400);
  });
  input.addEventListener("blur", () => {
    clearTimeout(timer);
    check();
  });
  form.addEventListener("reset", () => {
    latest = "";
    show(null, "");
    setButton(null);
  });
}

setupForm(el.transferForm, {
  path: "/transfers",
  busyLabel: "Sending…",
  buildBody: ({ to_email, amount, note }) => ({ to_email, amount, note }),
  successText: (data) => `Sent ${formatRupees(data.amount)} to ${data.to_name || data.to}`,
});

setupForm(el.requestForm, {
  path: "/requests",
  busyLabel: "Sending request…",
  idempotent: false, // creating a request moves no money
  buildBody: ({ from_email, amount, note }) => ({ from_email, amount, note }),
  successText: (data) => `Asked ${data.other_name || data.other_email} for ${formatRupees(data.amount)}`,
});

setupRecipientCheck(el.transferForm, "to_email", "Send to");
setupRecipientCheck(el.requestForm, "from_email", "Request from");

setupForm(el.topupForm, {
  path: "/wallet/topup",
  busyLabel: "Adding…",
  buildBody: ({ amount }) => ({ amount }),
  successText: (data) => `Added ${formatRupees(data.amount)} to your wallet`,
});

// Quick amount chips
document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const input = el.topupForm.elements.amount;
    input.value = chip.dataset.amount;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  });
});

document.getElementById("logout").addEventListener("click", () => {
  auth.clear();
  window.location.href = "index.html";
});

// ---------------------------------------------------------------- refresh
// Poll every 10 s while the tab is visible, so money and requests from
// other people show up without reloading the page.
function refreshAll() {
  return Promise.all([loadWallet(), loadRequests(), loadHistory(currentPage)]);
}

setInterval(() => {
  if (document.visibilityState === "visible") refreshAll();
}, REFRESH_EVERY_MS);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshAll();
});

el.history.innerHTML = '<li class="ledger-loading">Loading your passbook…</li>';
refreshAll();
