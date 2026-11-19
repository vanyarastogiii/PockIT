// PockIt hosted checkout: /checkout.html?order=<order id>
// The shop sends the customer here; PockIt takes the payment and sends them back.

const orderId = new URLSearchParams(window.location.search).get("order");
const $ = (id) => document.getElementById(id);

const ICON_OK = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const ICON_HOLD = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
const ICON_STOP = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 8l8 8"/></svg>';

let order = null;
let idempotencyKey = null; // one per payment attempt; kept on network errors so a retry can't pay twice

$("checkout-logo").innerHTML = POCKIT_LOGO + "PockIt";

function initials(name) {
  return (name || "?").split(/\s+/).filter((w) => /\w/.test(w)).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

function returnUrl() {
  return order?.return_url || order?.cancel_url || "/";
}

function finish({ icon, kind, title, text, link = returnUrl(), linkText = `Back to ${order?.merchant_name || "the shop"}`, redirect = false }) {
  $("checkout-loading").hidden = true;
  $("checkout-main").hidden = true;
  const done = $("checkout-done");
  done.hidden = false;
  done.className = `checkout-state checkout-state--${kind}`;
  $("done-icon").innerHTML = icon;
  $("done-title").textContent = title;
  $("done-text").textContent = text;
  $("done-link").href = link;
  $("done-link").textContent = linkText;
  if (redirect) setTimeout(() => { window.location.href = link; }, 3000);
}

const FINAL_STATES = {
  PAID: { icon: ICON_OK, kind: "ok", title: "Already paid", text: "This order has been paid. Nothing more to do." },
  PARTIALLY_REFUNDED: { icon: ICON_OK, kind: "ok", title: "Already paid", text: "This order has been paid." },
  REFUNDED: { icon: ICON_OK, kind: "ok", title: "Already paid and refunded", text: "This order was paid and then refunded." },
  ON_HOLD: { icon: ICON_HOLD, kind: "hold", title: "Payment under review", text: "A payment for this order is being reviewed." },
  DECLINED: { icon: ICON_STOP, kind: "stop", title: "Payment declined", text: "Go back to the shop to start a new checkout." },
  EXPIRED: { icon: ICON_STOP, kind: "stop", title: "This checkout expired", text: "Checkout links last 30 minutes. Go back to the shop and try again." },
};

async function load() {
  if (!orderId) {
    return finish({ icon: ICON_STOP, kind: "stop", title: "No order", text: "This link is missing its order.", link: "/", linkText: "Go to PockIt" });
  }
  try {
    order = (await api(`/checkout/${encodeURIComponent(orderId)}`)).data;
  } catch (err) {
    return finish({
      icon: ICON_STOP, kind: "stop", title: "Order not found",
      text: err.status === 404 ? "This checkout link isn't valid." : "Couldn't load this checkout. Try again.",
      link: "/", linkText: "Go to PockIt",
    });
  }

  if (FINAL_STATES[order.status]) return finish(FINAL_STATES[order.status]);

  $("merchant-name").textContent = order.merchant_name;
  $("merchant-initials").textContent = initials(order.merchant_name);
  $("order-desc").textContent = order.description;
  $("order-amount").textContent = formatRupees(order.amount);
  $("cancel-link").href = order.cancel_url || order.return_url || "/";
  $("cancel-link").textContent = `Cancel and go back to ${order.merchant_name}`;
  $("checkout-loading").hidden = true;
  $("checkout-main").hidden = false;

  let wallet = null;
  if (auth.access) {
    try {
      wallet = (await api("/wallet")).data;
    } catch {
      auth.clear();
    }
  }

  if (!wallet) {
    const here = window.location.pathname + window.location.search;
    $("login-link").href = `/?next=${encodeURIComponent(here)}`;
    $("checkout-login").hidden = false;
    return;
  }

  $("payer-name").textContent = wallet.name || wallet.email;
  $("payer-balance").textContent = `Balance ${formatRupees(wallet.balance)}`;
  $("pay-button").textContent = `Pay ${formatRupees(order.amount)}`;
  $("checkout-pay").hidden = false;
}

$("pay-button").addEventListener("click", async () => {
  const button = $("pay-button");
  if (button.disabled) return;
  idempotencyKey ??= newIdempotencyKey();
  button.disabled = true;
  button.textContent = "Paying…";
  $("pay-error").textContent = "";

  try {
    const { data } = await api(`/checkout/${encodeURIComponent(orderId)}/pay`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    });
    if (data.status === "ON_HOLD") {
      finish({
        icon: ICON_HOLD, kind: "hold", title: "Payment on hold for review",
        text: `For your security, this payment is being checked. ${order.merchant_name} will be told as soon as it's approved; if it isn't, the money comes back to your wallet.`,
      });
    } else {
      finish({
        icon: ICON_OK, kind: "ok", title: `Paid ${formatRupees(data.amount)}`,
        text: `Your payment to ${order.merchant_name} is complete. Taking you back to the shop…`,
        redirect: true,
      });
    }
  } catch (err) {
    if (err.status === 401) {
      auth.clear();
      return load();
    }
    if (err.status) idempotencyKey = null; // a real answer ends this attempt
    let message = err.status ? err.message : "Network problem. Press Pay again; you won't be charged twice.";
    if (err.data?.detail?.code === "insufficient_funds" || /Insufficient/.test(message)) {
      message = "Not enough money in your wallet. Add money on your dashboard, then come back to this page.";
    }
    $("pay-error").textContent = message;
    if (err.status === 409) return load(); // already paid / expired: show the final state
    button.disabled = false;
    button.textContent = `Pay ${formatRupees(order.amount)}`;
  }
});

load();
