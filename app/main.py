from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import init_db
from app.routers import admin, events, policy, trust


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        init_db()
    yield


APP_VERSION = "1.0.0"

APP_DESCRIPTION = """
Agent Trust Bureau is the policy and trust layer for AI-agent systems.

Use this API to:
- ingest agent behavior events
- compute explainable trust scores
- evaluate allow/review/block policy decisions
- manage tenant-level policy config, keys, and webhooks
- monitor async webhook jobs and delivery outcomes
""".strip()

OPENAPI_TAGS = [
    {"name": "intake", "description": "Agent behavior event ingestion and retrieval."},
    {"name": "trust", "description": "Trust score computation and score history."},
    {"name": "policy", "description": "Policy decisioning over trust scores."},
    {"name": "admin", "description": "Tenant-scoped admin operations and observability."},
]

app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    description=APP_DESCRIPTION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_tags=OPENAPI_TAGS,
)
app.include_router(events.router, prefix=settings.api_prefix)
app.include_router(trust.router, prefix=settings.api_prefix)
app.include_router(policy.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)


def _landing_page_html() -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{settings.app_name} v{APP_VERSION}</title>
  <style>
    :root {{
      --ink: #0f172a;
      --muted: #475569;
      --paper: #f8fafc;
      --line: #cbd5e1;
      --accent: #0369a1;
      --accent-soft: #e0f2fe;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Manrope", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% -10%, #e0f2fe 0%, transparent 36%),
        radial-gradient(circle at 95% 0%, #cffafe 0%, transparent 30%),
        var(--paper);
      min-height: 100vh;
    }}
    .wrap {{
      max-width: 960px;
      margin: 0 auto;
      padding: 48px 24px 80px;
    }}
    .brand {{
      display: inline-block;
      font-size: 12px;
      letter-spacing: .14em;
      text-transform: uppercase;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(2rem, 6vw, 3rem);
      line-height: 1.05;
    }}
    .sub {{
      margin: 0 0 26px;
      color: var(--muted);
      max-width: 68ch;
      font-size: 1.02rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin: 22px 0 34px;
    }}
    .card {{
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
    }}
    .card h3 {{
      margin: 0 0 6px;
      font-size: 0.9rem;
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .05em;
    }}
    .card .v {{
      margin: 0;
      font-size: 1.4rem;
      font-weight: 700;
    }}
    .links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 6px;
    }}
    .btn {{
      border-radius: 999px;
      text-decoration: none;
      padding: 10px 16px;
      font-weight: 700;
      border: 1px solid var(--accent);
      color: var(--accent);
      background: #fff;
      transition: all .16s ease;
    }}
    .btn:hover {{
      background: var(--accent-soft);
    }}
    .btn.primary {{
      background: var(--accent);
      color: #fff;
    }}
    .btn.primary:hover {{
      filter: brightness(1.04);
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <span class="brand">Trust Infrastructure</span>
    <h1>{settings.app_name}</h1>
    <p class="sub">
      A production-ready trust and policy API for AI agents. Ingest events, compute explainable trust scores,
      enforce allow/review/block policy, and audit webhook jobs and deliveries.
    </p>

    <section class="grid">
      <article class="card">
        <h3>Version</h3>
        <p class="v">{APP_VERSION}</p>
      </article>
      <article class="card">
        <h3>Environment</h3>
        <p class="v">{settings.environment}</p>
      </article>
      <article class="card">
        <h3>API Base</h3>
        <p class="v">{settings.api_prefix}</p>
      </article>
    </section>

    <nav class="links">
      <a class="btn primary" href="/console">Open Product Console</a>
      <a class="btn primary" href="/docs">Open API Console</a>
      <a class="btn" href="/guide">Open API Reference</a>
      <a class="btn" href="/health">Health Check</a>
    </nav>
  </main>
</body>
</html>
"""


def _console_page_html() -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{settings.app_name} Operator Console</title>
  <style>
    :root {{
      --ink: #0f172a;
      --muted: #64748b;
      --paper: #f8fafc;
      --line: #cbd5e1;
      --brand: #0f766e;
      --brand-soft: #ccfbf1;
      --panel: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Manrope", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 100% -5%, #cffafe 0%, transparent 33%),
        radial-gradient(circle at 0% 0%, #e0f2fe 0%, transparent 32%),
        var(--paper);
    }}
    .wrap {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 30px 20px 42px;
    }}
    .head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 20px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: clamp(1.7rem, 4vw, 2.45rem);
      line-height: 1.06;
    }}
    .sub {{
      margin: 0;
      color: var(--muted);
      max-width: 66ch;
    }}
    .badge {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
      background: #fff;
      font-size: 12px;
      font-weight: 700;
      color: var(--brand);
      white-space: nowrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 14px;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel);
      padding: 14px;
    }}
    .panel h2 {{
      margin: 0 0 10px;
      font-size: .98rem;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: var(--muted);
    }}
    .stack {{ display: grid; gap: 9px; }}
    label {{
      font-size: 12px;
      color: var(--muted);
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      color: var(--ink);
      background: #fff;
    }}
    .row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    button, .link {{
      border: 1px solid var(--brand);
      background: #fff;
      color: var(--brand);
      border-radius: 999px;
      padding: 8px 12px;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    button.primary {{
      background: var(--brand);
      color: #fff;
    }}
    button:hover, .link:hover {{
      background: var(--brand-soft);
    }}
    button.primary:hover {{
      filter: brightness(1.04);
      background: var(--brand);
    }}
    pre {{
      margin: 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #0b1724;
      color: #cde7ff;
      padding: 12px;
      min-height: 290px;
      max-height: 520px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.42;
    }}
    .muted {{ color: var(--muted); font-size: 12px; margin: 0; }}
    @media (max-width: 760px) {{
      .head {{ flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="head">
      <div>
        <h1>ATB Operator Console</h1>
        <p class="sub">
          A lightweight product surface for daily operations: trust decisions, webhook health,
          queue visibility, and delivery diagnostics.
        </p>
      </div>
      <span class="badge">v{APP_VERSION} • {settings.environment}</span>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>Request Setup</h2>
        <div class="stack">
          <div>
            <label for="apiKey">X-API-Key</label>
            <input id="apiKey" placeholder="atb_..." />
          </div>
          <div class="row">
            <button id="saveKey" class="primary">Save Key</button>
            <button id="clearKey">Clear Key</button>
            <a class="link" href="/docs">Swagger</a>
            <a class="link" href="/guide">Reference</a>
          </div>
          <p class="muted">Key is stored in your browser localStorage on this device only.</p>
        </div>
      </article>

      <article class="panel">
        <h2>Quick Actions</h2>
        <div class="stack">
          <div>
            <label for="agentId">Agent ID</label>
            <input id="agentId" value="agent-1" />
          </div>
          <div>
            <label for="webhookId">Webhook ID</label>
            <input id="webhookId" value="1" />
          </div>
          <div class="row">
            <button data-action="health">Health</button>
            <button data-action="decision" class="primary">Policy Decision</button>
            <button data-action="webhooks">List Webhooks</button>
            <button data-action="jobs">Jobs</button>
            <button data-action="deliveries">Deliveries</button>
            <button data-action="stats">Stats</button>
          </div>
        </div>
      </article>
    </section>

    <section class="panel" style="margin-top:14px;">
      <h2>Response</h2>
      <pre id="output">Ready. Set key and run an action.</pre>
    </section>
  </main>

  <script>
    const base = window.location.origin;
    const keyInput = document.getElementById("apiKey");
    const agentInput = document.getElementById("agentId");
    const webhookInput = document.getElementById("webhookId");
    const out = document.getElementById("output");

    const storageKey = "atb_console_api_key";
    const existing = localStorage.getItem(storageKey);
    if (existing) keyInput.value = existing;

    function setOutput(v) {{
      out.textContent = typeof v === "string" ? v : JSON.stringify(v, null, 2);
    }}

    function headers() {{
      const h = {{"Content-Type": "application/json"}};
      const k = keyInput.value.trim();
      if (k) h["X-API-Key"] = k;
      return h;
    }}

    async function req(method, path, body=null) {{
      const cfg = {{ method, headers: headers() }};
      if (body !== null) cfg.body = JSON.stringify(body);
      const r = await fetch(base + path, cfg);
      let data = null;
      try {{
        data = await r.json();
      }} catch (e) {{
        data = await r.text();
      }}
      return {{ status: r.status, ok: r.ok, data }};
    }}

    async function run(action) {{
      setOutput("Loading...");
      try {{
        const aid = encodeURIComponent(agentInput.value.trim() || "agent-1");
        const wid = encodeURIComponent(webhookInput.value.trim() || "1");
        let res = null;

        if (action === "health") res = await req("GET", "/health");
        if (action === "decision") res = await req("GET", `/v1/policy/decision/${{aid}}`);
        if (action === "webhooks") res = await req("GET", "/v1/admin/policy/webhooks");
        if (action === "jobs") res = await req("GET", `/v1/admin/policy/webhooks/${{wid}}/jobs?limit=20`);
        if (action === "deliveries") res = await req("GET", `/v1/admin/policy/webhooks/${{wid}}/deliveries?limit=20`);
        if (action === "stats") res = await req("GET", `/v1/admin/policy/webhooks/${{wid}}/stats`);

        setOutput(res);
      }} catch (err) {{
        setOutput({{ error: String(err) }});
      }}
    }}

    document.getElementById("saveKey").addEventListener("click", () => {{
      localStorage.setItem(storageKey, keyInput.value.trim());
      setOutput("API key saved in localStorage.");
    }});

    document.getElementById("clearKey").addEventListener("click", () => {{
      localStorage.removeItem(storageKey);
      keyInput.value = "";
      setOutput("API key cleared.");
    }});

    document.querySelectorAll("button[data-action]").forEach((btn) => {{
      btn.addEventListener("click", () => run(btn.dataset.action));
    }});
  </script>
</body>
</html>
"""


@app.get("/", include_in_schema=False, response_model=None)
def root(request: Request) -> object:
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(content=_landing_page_html())
    return {
        "name": settings.app_name,
        "version": APP_VERSION,
        "status": "ok",
        "docs": "/docs",
        "console": "/console",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION, "environment": settings.environment}


@app.get("/console", include_in_schema=False)
def console_html() -> HTMLResponse:
    return HTMLResponse(content=_console_page_html())


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui_html() -> HTMLResponse:
    response = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{settings.app_name} - API Console",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_ui_parameters={
            "persistAuthorization": True,
            "docExpansion": "none",
            "defaultModelsExpandDepth": -1,
            "displayRequestDuration": True,
            "syntaxHighlight.theme": "obsidian",
        },
    )
    style = b"""
<style>
  body { background: #f8fafc !important; }
  .swagger-ui .topbar { background: linear-gradient(90deg, #0b132b, #1c2541) !important; }
  .swagger-ui .topbar .download-url-wrapper { display: none !important; }
  .swagger-ui .info { margin: 24px 0 !important; }
  .swagger-ui .info .title { color: #0f172a !important; font-weight: 800; }
  .swagger-ui .scheme-container {
    border: 1px solid #cbd5e1 !important;
    box-shadow: none !important;
    border-radius: 10px !important;
    background: #ffffff !important;
  }
  .swagger-ui .opblock.opblock-get { border-color: #0ea5e9 !important; }
  .swagger-ui .opblock.opblock-post { border-color: #22c55e !important; }
</style>
"""
    response.body = response.body.replace(b"</head>", style + b"</head>")
    response.headers["content-length"] = str(len(response.body))
    return response


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
def swagger_ui_redirect() -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/guide", include_in_schema=False)
def redoc_html() -> HTMLResponse:
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{settings.app_name} - API Reference",
    )
