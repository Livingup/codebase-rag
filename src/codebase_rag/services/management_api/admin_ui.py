"""Local-only management dashboard UI for the Management API service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from codebase_rag.services.management_api.service import ManagementApiService

_LOCAL_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", "testclient"}


def require_local_request(request: Request) -> None:
    """Reject non-local requests for the passwordless admin dashboard."""
    client_host = request.client.host if request.client else ""
    if client_host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="Admin dashboard is only available from localhost")


def create_admin_router(service: ManagementApiService) -> APIRouter:
    """Create local-only routes for the browser management dashboard."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        require_local_request(request)
        if not service.is_admin_ui_enabled():
            raise HTTPException(status_code=404, detail="Admin dashboard is disabled")
        return HTMLResponse(_ADMIN_DASHBOARD_HTML)

    @router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin(request: Request):
        require_local_request(request)
        if not service.is_admin_ui_enabled():
            raise HTTPException(status_code=404, detail="Admin dashboard is disabled")
        return HTMLResponse(_ADMIN_DASHBOARD_HTML)

    return router


_ADMIN_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Codebase RAG 管理后台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #172033;
      --muted: #687386;
      --line: #e6eaf2;
      --primary: #2563eb;
      --ok: #15803d;
      --warn: #b45309;
      --bad: #b91c1c;
      --unknown: #475569;
      --shadow: 0 14px 40px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at top left, #e0ecff 0, transparent 34rem), var(--bg);
      color: var(--text);
    }
    header {
      padding: 28px 36px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.78);
      backdrop-filter: blur(16px);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: -0.02em; }
    .subtitle { color: var(--muted); font-size: 14px; }
    .layout { display: flex; min-height: calc(100vh - 94px); }
    nav {
      width: 232px;
      padding: 24px 18px;
      border-right: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.62);
    }
    .tab {
      width: 100%;
      border: 0;
      border-radius: 12px;
      padding: 12px 14px;
      background: var(--primary);
      color: #fff;
      text-align: left;
      font-size: 15px;
      font-weight: 700;
      box-shadow: 0 8px 22px rgba(37, 99, 235, 0.22);
    }
    main { flex: 1; padding: 26px 34px 48px; }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 20px;
    }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; }
    button.action {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      color: var(--text);
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 650;
      box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
    }
    button.action.primary { background: var(--primary); color: #fff; border-color: var(--primary); }
    button.action:disabled { opacity: 0.58; cursor: wait; }
    .card-actions { margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }
    button.card-action {
      border: 1px solid rgba(37, 99, 235, 0.24);
      border-radius: 9px;
      background: #eff6ff;
      color: var(--primary);
      padding: 8px 12px;
      cursor: pointer;
      font-weight: 700;
      font-size: 13px;
    }
    button.card-action:disabled { opacity: 0.58; cursor: wait; }
    .meta { color: var(--muted); font-size: 13px; }
    .summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(180px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }
    .summary-card, .client-card, .proxy-panel {
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }
    .summary-card { padding: 18px; }
    .summary-card .label { color: var(--muted); font-size: 13px; margin-bottom: 7px; }
    .summary-card .value { font-size: 24px; font-weight: 760; }
    .cards { display: grid; grid-template-columns: repeat(3, minmax(260px, 1fr)); gap: 16px; }
    .client-card { padding: 18px; min-height: 300px; }
    .client-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .client-title { font-size: 18px; font-weight: 760; margin-bottom: 5px; }
    .client-sub { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 760;
      white-space: nowrap;
      color: #fff;
    }
    .available { background: var(--ok); }
    .degraded, .timeout { background: var(--warn); }
    .unavailable, .error, .not_configured { background: var(--bad); }
    .unknown { background: var(--unknown); }
    dl { display: grid; grid-template-columns: 112px 1fr; gap: 9px 10px; margin: 18px 0 0; font-size: 13px; }
    dt { color: var(--muted); }
    dd { margin: 0; overflow-wrap: anywhere; }
    .keys { margin-top: 16px; border-top: 1px solid var(--line); padding-top: 12px; }
    .keys-title { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
    .key-row { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; padding: 5px 0; }
    .probe { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px; color: var(--muted); font-size: 13px; }
    .proxy-panel { margin-top: 18px; padding: 16px 18px; color: var(--muted); font-size: 13px; }
    .proxy-panel strong { color: var(--text); }
    .error-box { display: none; margin-bottom: 16px; padding: 12px 14px; border-radius: 12px; background: #fee2e2; color: #991b1b; }
    @media (max-width: 1100px) { .cards, .summary { grid-template-columns: 1fr; } nav { display: none; } main { padding: 20px; } header { padding: 22px; } }
  </style>
</head>
<body>
  <header>
    <h1>Codebase RAG 管理后台</h1>
    <div class="subtitle">仅限本机访问 · 当前只提供服务状态查看</div>
  </header>
  <div class="layout">
    <nav><button class="tab">服务状态</button></nav>
    <main>
      <section class="toolbar">
        <div>
          <h2 style="margin:0 0 6px;font-size:22px;">客户端与模型服务商可用情况</h2>
          <div class="meta" id="refresh-meta">等待刷新</div>
        </div>
        <div class="actions">
          <button class="action" id="refresh-btn">刷新当前状态</button>
          <button class="action primary" id="probe-btn">实时检测</button>
        </div>
      </section>
      <div class="error-box" id="error-box"></div>
      <section class="summary">
        <div class="summary-card"><div class="label">整体状态</div><div class="value" id="overall-status">-</div></div>
        <div class="summary-card"><div class="label">可用状态项</div><div class="value" id="available-count">-</div></div>
        <div class="summary-card"><div class="label">检测模式</div><div class="value" id="probe-mode">快照</div></div>
      </section>
      <section class="cards" id="client-cards"></section>
      <section class="proxy-panel" id="proxy-panel"></section>
    </main>
  </div>
  <script>
    const statusLabel = {
      available: '可用',
      degraded: '降级',
      unavailable: '不可用',
      not_configured: '未配置',
      timeout: '超时',
      error: '错误',
      unknown: '未知'
    };
    const escapeHtml = (value) => String(value ?? '')
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    const badge = (status) => `<span class="badge ${escapeHtml(status || 'unknown')}">${statusLabel[status] || status || '未知'}</span>`;
    function setLoading(loading) {
      document.getElementById('refresh-btn').disabled = loading;
      document.getElementById('probe-btn').disabled = loading;
      document.querySelectorAll('.single-probe-btn').forEach(button => { button.disabled = loading; });
    }
    function showError(message) {
      const box = document.getElementById('error-box');
      box.textContent = message;
      box.style.display = message ? 'block' : 'none';
    }
    function detailRows(details) {
      const keys = [
        'provider', 'api_format', 'model', 'models', 'model_count', 'endpoint', 'collection', 'dimension',
        'batch_size', 'healthy_keys', 'total_keys', 'initialized', 'provider_loaded', 'api_key_configured',
        'request_timeout', 'max_concurrency', 'is_default_provider', 'default_model', 'proxy_policy', 'probe_policy'
      ];
      const labels = {
        provider: 'Provider', api_format: 'API 格式', model: '模型', models: '模型列表', model_count: '模型数',
        endpoint: '端点', collection: 'Collection', dimension: '维度', batch_size: '批量', healthy_keys: '健康 Key',
        total_keys: '总 Key', initialized: '已初始化', provider_loaded: 'Provider 已加载',
        api_key_configured: 'API Key 已配置', request_timeout: '请求超时', max_concurrency: '最大并发',
        is_default_provider: '默认服务商', default_model: '默认模型', proxy_policy: '环境代理', probe_policy: '检测策略'
      };
      return keys.filter(k => details[k] !== undefined && details[k] !== null && details[k] !== '').map(k => `<dt>${labels[k] || k}</dt><dd>${escapeHtml(details[k])}</dd>`).join('');
    }
    function render(data) {
      const clients = data.clients || [];
      const kindLabel = { embedding: 'Embedding', vector_storage: '向量存储', model_provider: '模型服务商' };
      document.getElementById('overall-status').textContent = statusLabel[data.status] || data.status || '-';
      document.getElementById('available-count').textContent = `${clients.filter(c => c.status === 'available').length}/${clients.length}`;
      document.getElementById('probe-mode').textContent = data.live_probe ? '实时' : '快照';
      document.getElementById('refresh-meta').textContent = `更新时间：${data.generated_at || '-'} · ${data.live_probe ? '已执行真实心跳' : '未消耗外部 API'}`;
      document.getElementById('client-cards').innerHTML = clients.map(client => {
        const details = client.details || {};
        const keys = client.key_status || [];
        const live = client.live_probe;
        return `<article class="client-card">
          <div class="client-head">
            <div><div class="client-title">${escapeHtml(client.name)}</div><div class="client-sub">${escapeHtml(kindLabel[client.kind] || client.kind)} ${client.provider ? '· ' + escapeHtml(client.provider) : ''}</div></div>
            ${badge(client.status)}
          </div>
          <dl>${detailRows({...details, provider: client.provider, endpoint: client.endpoint})}</dl>
          <div class="card-actions"><button class="card-action single-probe-btn" data-client-id="${escapeHtml(client.id)}">单独测试</button></div>
          <div class="keys">
            <div class="keys-title">Key 状态</div>
            ${keys.length ? keys.map(k => {
              const keyLabel = k.key_suffix ? `***${escapeHtml(k.key_suffix)}` : escapeHtml(k.label || 'API Key');
              const statusText = k.status_label || (k.healthy ? '健康' : '不可用');
              return `<div class="key-row"><span>${keyLabel}</span><span>${escapeHtml(statusText)}${k.cooldown_remaining ? ' · 冷却 ' + Math.ceil(k.cooldown_remaining) + 's' : ''}</span></div>`;
            }).join('') : '<div class="meta">无 Key 明细</div>'}
          </div>
          <div class="probe">${live ? `实时检测：${escapeHtml(live.message || live.status)}${live.api_model ? ' · 模型 ' + escapeHtml(live.api_model) : ''}${live.latency_ms ? ' · ' + escapeHtml(live.latency_ms) + 'ms' : ''}` : '实时检测：未执行'}</div>
        </article>`;
      }).join('');
      const proxy = data.proxy_environment || {};
      const proxyVars = proxy.detected_variables || [];
      document.getElementById('proxy-panel').innerHTML = `<strong>代理环境变量：</strong>${proxyVars.length ? escapeHtml(proxyVars.join(', ')) : '未检测到 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY'}<br><span>这些变量不是必填项；不配置时客户端直连。</span>`;
    }
    async function load(live = false) {
      setLoading(true);
      showError('');
      try {
        const response = await fetch(`/api/v1/status/clients${live ? '?live=true' : ''}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        render(await response.json());
      } catch (err) {
        showError(`状态加载失败：${err.message || err}`);
      } finally {
        setLoading(false);
      }
    }
    async function probeOne(clientId) {
      setLoading(true);
      showError('');
      try {
        const response = await fetch(`/api/v1/status/clients/${encodeURIComponent(clientId)}/probe`, { method: 'POST', cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        render(await response.json());
      } catch (err) {
        showError(`单独测试失败：${err.message || err}`);
      } finally {
        setLoading(false);
      }
    }
    document.getElementById('refresh-btn').addEventListener('click', () => load(false));
    document.getElementById('probe-btn').addEventListener('click', () => load(true));
    document.getElementById('client-cards').addEventListener('click', (event) => {
      const button = event.target.closest('.single-probe-btn');
      if (!button) return;
      probeOne(button.dataset.clientId);
    });
    load(false);
  </script>
</body>
</html>
"""
