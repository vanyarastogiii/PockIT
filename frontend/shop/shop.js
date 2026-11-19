// Chai & Co. front end. It only talks to the SHOP's own server (/shop/api/...).
// The shop's server talks to PockIt; the browser never sees the shop's secret key.

const ORIGIN = window.location.port === "5500" ? "http://127.0.0.1:8000" : "";
const params = new URLSearchParams(window.location.search);
const statusBox = document.getElementById("order-status");

async function shopApi(path, options = {}) {
  const res = await fetch(`${ORIGIN}/shop/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Something went wrong.");
  return data;
}

// ---------------------------------------------------------------- buying
document.querySelectorAll(".pockit-btn").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    document.getElementById("shop-error").textContent = "";
    try {
      const data = await shopApi("/buy", { method: "POST", body: JSON.stringify({ product: button.dataset.product }) });
      window.location.href = `${ORIGIN}${data.checkout_url}`;
    } catch (err) {
      document.getElementById("shop-error").textContent = err.message;
      button.disabled = false;
    }
  });
});

// ---------------------------------------------------------------- after checkout
const MESSAGES = {
  WAITING: ["is-waiting", "Confirming your payment with PockIt…",
    "We mark orders paid only when PockIt's signed confirmation reaches our server. This takes a second."],
  PAID: ["is-paid", "Paid. Thank you!", "Your order is confirmed and on its way."],
  ON_HOLD: ["is-waiting", "PockIt is reviewing this payment",
    "Some payments get a quick security check. We'll start your order as soon as PockIt approves it."],
  DECLINED: ["is-bad", "Payment not approved", "PockIt didn't approve this payment, and your money went back to your wallet."],
  PARTIALLY_REFUNDED: ["is-paid", "Paid, partly refunded", "Part of this order was refunded to your PockIt wallet."],
  REFUNDED: ["is-paid", "Refunded", "This order was refunded to your PockIt wallet."],
};

function show(order) {
  const [cls, title, text] = MESSAGES[order.status] || MESSAGES.WAITING;
  statusBox.className = `order-status ${cls}`;
  statusBox.innerHTML = '<span class="dot" aria-hidden="true"></span><div><h2></h2><p></p></div>';
  statusBox.querySelector("h2").textContent = `Order #${order.id}, ${order.product_name}: ${title}`;
  statusBox.querySelector("p").textContent = text;
  statusBox.hidden = false;
}

async function track(orderId) {
  const started = Date.now();
  while (true) {
    let order;
    try {
      order = await shopApi(`/orders/${encodeURIComponent(orderId)}`);
    } catch {
      return;
    }
    show(order);
    const settled = !["WAITING", "ON_HOLD"].includes(order.status);
    // Poll quickly at first, then slowly (holds can take a while to review).
    if (settled || Date.now() - started > 10 * 60 * 1000) return;
    await new Promise((r) => setTimeout(r, Date.now() - started < 30000 ? 1500 : 10000));
  }
}

if (params.get("order")) {
  track(params.get("order"));
} else if (params.get("cancelled")) {
  statusBox.className = "order-status is-bad";
  statusBox.innerHTML = '<span class="dot" aria-hidden="true"></span><div><h2>Payment cancelled</h2><p>Nothing was charged. Your basket is still here.</p></div>';
  statusBox.hidden = false;
}
