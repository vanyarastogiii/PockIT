// Business page: become a merchant, see orders, refund, manage API key and webhooks.

if (!auth.access) window.location.href = `/?next=${encodeURIComponent("/merchant.html")}`;

const $ = (id) => document.getElementById(id);
let merchant = null;
let toastTimer;

$("brand").innerHTML = POCKIT_LOGO + "PockIt";

function toast(text, type = "success") {
  clearTimeout(toastTimer);
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("is-error", type === "error");
  t.hidden = false;
  toastTimer = setTimeout(() => { t.hidden = true; }, type === "error" ? 6000 : 4000);
}

function handleError(err) {
  if (err.status === 401) {
    auth.clear();
    window.location.href = "index.html";
    return;
  }
  toast(err.status ? err.message : "Network problem. Try again.", "error");
}

function textEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function when(iso) {
  return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

const ORDER_STATUS = {
  PAID: "Paid", PARTIALLY_REFUNDED: "Partly refunded", REFUNDED: "Refunded",
  ON_HOLD: "On hold", DECLINED: "Declined", CREATED: "Waiting", EXPIRED: "Expired",
};
const DELIVERY_STATUS = { DELIVERED: "PAID", PENDING: "PENDING", FAILED: "DECLINED" }; // reuse pill colours

// ---------------------------------------------------------------- account bar
(async () => {
  try {
    const { data } = await api("/wallet");
    const name = data.name || data.email;
    $("user-name").textContent = name;
    $("avatar").textContent = name.split(/\s+/).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  } catch (err) {
    handleError(err);
  }
})();
$("logout").addEventListener("click", () => { auth.clear(); window.location.href = "index.html"; });

// ---------------------------------------------------------------- load
async function load() {
  try {
    merchant = (await api("/merchant")).data;
  } catch (err) {
    if (err.status === 404) {
      $("biz-loading").hidden = true;
      $("onboard").hidden = false;
      return;
    }
    return handleError(err);
  }
  $("biz-loading").hidden = true;
  $("onboard").hidden = true;
  $("dashboard").hidden = false;
  renderProfile();
  await Promise.all([loadOrders(), loadDeliveries()]);
}

function renderProfile() {
  $("biz-name").textContent = merchant.name;
  const stats = [
    ["Received", formatRupees(merchant.stats.total_received)],
    ["Refunded", formatRupees(merchant.stats.total_refunded)],
    ["Paid orders", merchant.stats.orders_paid],
    ["On hold", merchant.stats.on_hold],
  ];
  $("biz-stats").replaceChildren(...stats.flatMap(([label, value]) => {
    const wrap = textEl("div", "biz-stat");
    wrap.append(textEl("dt", "", label), textEl("dd", "", String(value)));
    return [wrap];
  }));
  $("key-hint").textContent = merchant.api_key_hint;
  $("webhook-form").elements.webhook_url.value = merchant.webhook_url || "";
  $("webhook-secret").textContent = "whsec_••••••••";
  $("reveal-secret").textContent = "Show";
  $("code-sample").textContent =
`curl -X POST ${window.location.origin}/api/v1/orders \\
  -H "Authorization: Bearer ${merchant.api_key_hint.replace("…", "...")}" \\
  -H "Content-Type: application/json" \\
  -d '{"amount": "499.00", "description": "Order #1042",
       "return_url": "https://your-site.com/thanks"}'

# The response has a checkout_url: send your customer there.`;
}

// ---------------------------------------------------------------- orders
async function loadOrders() {
  try {
    const { data } = await api("/merchant/orders");
    $("orders-meta").textContent = data.count === 1 ? "1 order" : `${data.count} orders`;
    if (data.results.length === 0) {
      $("orders").innerHTML =
        '<li class="ledger-empty"><strong>No orders yet</strong><span>Try buying something from the demo shop, or create a checkout with your API key.</span></li>';
      return;
    }
    $("orders").replaceChildren(...data.results.map(renderOrder));
  } catch (err) {
    handleError(err);
  }
}

function renderOrder(order) {
  const li = textEl("li", "order-row");
  const main = textEl("div", "order-main");
  const info = textEl("div", "ledger-text");
  info.append(
    textEl("div", "ledger-desc", order.description),
    textEl("div", "ledger-date", `${order.customer_name || order.customer_email || "Customer"}, ${when(order.paid_at || order.created_at)}`),
  );
  main.append(info);

  const amount = textEl("div", "ledger-amount", formatRupees(order.amount));
  if (Number(order.refunded_amount) > 0) {
    amount.append(textEl("div", "ledger-date", `−${formatRupees(order.refunded_amount)} refunded`));
  }
  const statusCell = textEl("div", "");
  statusCell.append(textEl("span", `status status--${order.status}`, ORDER_STATUS[order.status] || order.status));

  const actions = textEl("div", "order-actions");
  const remaining = Number(order.amount) - Number(order.refunded_amount);
  const refundable = ["PAID", "PARTIALLY_REFUNDED"].includes(order.status) && remaining > 0;
  if (refundable) {
    const btn = textEl("button", "btn btn--quiet btn--sm", "Refund");
    btn.addEventListener("click", () => openRefund(li, order, remaining));
    actions.append(btn);
  }
  li.append(main, amount, statusCell, actions);
  return li;
}

function openRefund(li, order, remaining) {
  document.querySelectorAll(".refund-form").forEach((f) => f.remove());
  const form = textEl("form", "refund-form");
  form.innerHTML = `
    <label class="field"><span>Amount</span>
      <div class="money-input"><span class="money-prefix" aria-hidden="true">₹</span>
      <input name="amount" type="number" min="0.01" step="0.01" required></div></label>
    <label class="field"><span>Reason <em class="optional">Optional, the customer sees it</em></span>
      <input name="reason" maxlength="140" placeholder="Item arrived damaged"></label>
    <div class="refund-actions">
      <button class="btn btn--primary btn--sm" type="submit"></button>
      <button class="btn btn--ghost btn--sm" type="button" data-cancel>Cancel</button>
    </div>`;
  const amountInput = form.elements.amount;
  amountInput.max = remaining.toFixed(2);
  amountInput.value = remaining.toFixed(2);
  const submit = form.querySelector("button[type=submit]");
  const label = () => { submit.textContent = `Refund ${formatRupees(amountInput.value || 0)}`; };
  label();

  let key = null; // one idempotency key per refund attempt
  form.addEventListener("input", () => { key = null; label(); });
  form.querySelector("[data-cancel]").addEventListener("click", () => form.remove());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.disabled || !form.reportValidity()) return;
    key ??= newIdempotencyKey();
    submit.disabled = true;
    submit.textContent = "Refunding…";
    try {
      const { data } = await api(`/merchant/orders/${order.id}/refunds`, {
        method: "POST",
        body: { amount: amountInput.value, reason: form.elements.reason.value },
        headers: { "Idempotency-Key": key },
      });
      toast(`Refunded ${formatRupees(data.amount)} to ${order.customer_name || "the customer"}`);
      merchant = (await api("/merchant")).data;
      renderProfile();
      await Promise.all([loadOrders(), loadDeliveries()]);
    } catch (err) {
      if (err.status) key = null;
      handleError(err);
      submit.disabled = false;
      label();
    }
  });
  li.after(form);
  amountInput.focus();
}

// ---------------------------------------------------------------- webhooks
async function loadDeliveries() {
  try {
    const { data } = await api("/merchant/webhooks");
    const events = data.results.slice(0, 8);
    if (events.length === 0) {
      $("deliveries").innerHTML = '<li class="hint">No webhooks sent yet.</li>';
      return;
    }
    $("deliveries").replaceChildren(...events.map((ev) => {
      const li = textEl("li", "delivery");
      li.append(
        textEl("code", "", ev.event_type),
        textEl("span", `status status--${DELIVERY_STATUS[ev.status]}`,
          ev.status === "DELIVERED" ? "Delivered" : ev.status === "FAILED" ? "Failed" : "Retrying"),
        textEl("span", "hint",
          `${ev.attempts} attempt${ev.attempts === 1 ? "" : "s"}${ev.last_status_code ? `, HTTP ${ev.last_status_code}` : ""}${ev.last_error && ev.status !== "DELIVERED" ? `, ${ev.last_error}` : ""}`),
        textEl("span", "hint", when(ev.created_at)),
      );
      if (ev.status !== "DELIVERED") {
        const retry = textEl("button", "btn btn--ghost btn--sm", "Retry");
        retry.addEventListener("click", async () => {
          retry.disabled = true;
          try {
            const { data: updated } = await api(`/merchant/webhooks/${ev.id}/retry`, { method: "POST" });
            toast(updated.status === "DELIVERED" ? "Webhook delivered" : `Still failing: ${updated.last_error}`,
              updated.status === "DELIVERED" ? "success" : "error");
            loadDeliveries();
          } catch (err) {
            handleError(err);
          }
        });
        li.append(retry);
      }
      return li;
    }));
  } catch (err) {
    handleError(err);
  }
}

$("webhook-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    merchant = (await api("/merchant", { method: "PATCH", body: { webhook_url: e.target.elements.webhook_url.value } })).data;
    renderProfile();
    toast("Webhook URL saved");
  } catch (err) {
    handleError(err);
  }
});

$("reveal-secret").addEventListener("click", () => {
  const shown = $("reveal-secret").textContent === "Hide";
  $("webhook-secret").textContent = shown ? "whsec_••••••••" : merchant.webhook_secret;
  $("reveal-secret").textContent = shown ? "Show" : "Hide";
});

// ---------------------------------------------------------------- keys
function showSecret(key) {
  $("secret-key").textContent = key;
  $("secret-box").hidden = false;
  $("secret-box").scrollIntoView({ behavior: "smooth", block: "center" });
}
$("copy-key").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("secret-key").textContent);
    toast("Copied");
  } catch {
    toast("Select the key and copy it manually.", "error");
  }
});
$("secret-done").addEventListener("click", () => {
  $("secret-box").hidden = true;
  $("secret-key").textContent = "";
});

let rotateArmed = false;
$("rotate-key").addEventListener("click", async () => {
  const btn = $("rotate-key");
  if (!rotateArmed) {
    rotateArmed = true;
    btn.textContent = "Confirm: old key stops working";
    btn.classList.add("btn--confirm");
    setTimeout(() => { rotateArmed = false; btn.textContent = "Replace key"; btn.classList.remove("btn--confirm"); }, 4000);
    return;
  }
  rotateArmed = false;
  try {
    const { data } = await api("/merchant/rotate-key", { method: "POST" });
    merchant = (await api("/merchant")).data;
    renderProfile();
    showSecret(data.api_key);
  } catch (err) {
    handleError(err);
  } finally {
    btn.textContent = "Replace key";
    btn.classList.remove("btn--confirm");
  }
});

$("onboard-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const body = Object.fromEntries(new FormData(form));
    const { data } = await api("/merchant", { method: "POST", body });
    await load();
    showSecret(data.api_key);
  } catch (err) {
    handleError(err);
  } finally {
    button.disabled = false;
  }
});

load();
