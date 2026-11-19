// Log in / sign up page.

// After logging in, go back where the user came from (e.g. a checkout page).
// Only same-site paths are allowed, so this can't be used to redirect elsewhere.
function nextPage() {
  const next = new URLSearchParams(window.location.search).get("next") || "";
  return next.startsWith("/") && !next.startsWith("//") ? next : "dashboard.html";
}

if (auth.access) window.location.href = nextPage();

const loginForm = document.getElementById("login-form");
const signupForm = document.getElementById("signup-form");
const message = document.getElementById("auth-message");
const title = document.getElementById("auth-title");

const TITLES = { login: "Log in to your wallet", signup: "Create your wallet" };

function showError(text) {
  message.textContent = text;
}

function switchTab(name) {
  document.querySelectorAll(".segment").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active);
  });
  loginForm.hidden = name !== "login";
  signupForm.hidden = name !== "signup";
  title.textContent = TITLES[name];
  showError("");
  (name === "login" ? loginForm : signupForm).querySelector("input").focus();
}

document.querySelectorAll(".segment").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

async function handleSubmit(form, path) {
  if (!form.reportValidity()) return;
  const button = form.querySelector("button[type=submit]");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = path.endsWith("signup") ? "Creating wallet…" : "Logging in…";
  showError("");
  try {
    const body = Object.fromEntries(new FormData(form));
    const { data } = await api(path, { method: "POST", body });
    auth.save(data);
    window.location.href = nextPage();
  } catch (err) {
    showError(err.status ? err.message : "Can't reach the server. Check that Django is running on port 8000.");
    button.disabled = false;
    button.textContent = label;
  }
}

loginForm.addEventListener("submit", (e) => {
  e.preventDefault();
  handleSubmit(loginForm, "/auth/login");
});

signupForm.addEventListener("submit", (e) => {
  e.preventDefault();
  handleSubmit(signupForm, "/auth/signup");
});

// Demo buttons: fill in a demo login and submit it.
function demoLogin(account, destination) {
  switchTab("login");
  loginForm.elements.email.value = account.email;
  loginForm.elements.password.value = account.password;
  const next = new URLSearchParams(window.location.search).get("next");
  if (destination && !next) history.replaceState(null, "", `?next=${encodeURIComponent(destination)}`);
  handleSubmit(loginForm, "/auth/login").then(() => {
    if (message.textContent.startsWith("Invalid")) {
      showError("The demo accounts aren't set up on this server. Run: python manage.py seed_demo");
    }
  });
}

document.getElementById("demo-login").addEventListener("click", () => demoLogin(DEMO_ACCOUNT));
document.getElementById("demo-shop-login").addEventListener("click", () => demoLogin(DEMO_SHOP_ACCOUNT, "/merchant.html"));
