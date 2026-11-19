// Where the API lives.
// - Opened through Django (http://127.0.0.1:8000) or a deployment: same origin, "/api".
// - Opened through VS Code Live Server / python -m http.server on port 5500:
//   the API is on Django's port 8000.
const API_BASE =
  window.location.port === "5500" ? "http://127.0.0.1:8000/api" : "/api";

// Created by `python manage.py seed_demo`
const DEMO_ACCOUNT = { email: "demo@pockit.app", password: "pockit-demo-2026" };
const DEMO_SHOP_ACCOUNT = { email: "chaiandco@pockit.app", password: "pockit-demo-2026" };
