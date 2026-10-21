# 管理面板 UI 开发模式参考

本文件记录 free-router 管理面板开发中遇到的关键模式、坑和最佳实践。

## 1. 单页瀑布流布局

- **结构**：顶部固定导航栏 + 8 个 `flow-section` 垂直排列 + `toast-layer` + `modal-layer`
- **无侧边栏**：所有功能区在单页内垂直流式浏览
- **CSS 变量驱动**：深空色系 (`--bg`, `--card`, `--border`) + 荧光强调色 (`--accent-cyan`, `--accent-magenta`, `--accent-green`, `--accent-amber`)
- **响应式断点**：`@media (max-width: 900px)` 简化内边距，`@media (max-width: 600px)` 隐藏次要列、单列卡片

## 2. `el()` 工具函数模式

```js
function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === 'style' && typeof v === 'object') {
      Object.entries(v).forEach(([sk, sv]) => { if (isNaN(sk)) e.style[sk] = sv; });
    } else if (k.startsWith('on') && typeof v === 'function') {
      e.addEventListener(k.slice(2).toLowerCase(), v);
    } else {
      e.setAttribute(k, v);
    }
  });
  // 关键：递归扁平化 children
  const flat = (arr) => arr.flatMap(c => Array.isArray(c) ? flat(c) : c);
  flat(children).forEach(c => e.append(c instanceof Node ? c : document.createTextNode(String(c))));
  return e;
}
```

**关键坑**：
- 不扁平化 → 渲染出 `[object HTMLDivElement]`
- `style` 赋值不滤数字键 → `Indexed property setter is not supported`
- 事件绑定用 `onClick` → 需转小写 `onclick`

## 3. 提供商分组折叠显示

**数据结构**：
```js
const grouped = {};
models.forEach(m => {
  if (!grouped[m.provider]) grouped[m.provider] = [];
  grouped[m.provider].push(m);
});
```

**渲染模式**：
```js
function toggleProviderSection(id) {
  const content = $('#' + id);
  const toggle = document.querySelector(`[onclick="toggleProviderSection('${id}')"] .provider-section-toggle`);
  const isOpen = content.style.display !== 'none';
  content.style.display = isOpen ? 'none' : 'block';
  if (toggle) toggle.textContent = isOpen ? '▶' : '▼';
}
```

**DOM 结构**：
```html
<div class="provider-section">
  <div class="provider-section-header" onclick="toggleProviderSection('provider-id')">
    <span class="provider-section-title">提供商名 (N 个模型)</span>
    <span class="provider-section-toggle">▼</span>
  </div>
  <div class="provider-section-content" id="provider-id">
    <!-- 表格/卡片/列表 -->
  </div>
</div>
```

**CSS 关键**：
```css
.provider-section-header { cursor: pointer; display: flex; justify-content: space-between; }
.provider-section-content { display: block; }  /* 默认展开 */
.provider-section-content.collapsed { display: none; }
```

## 4. 一键滚动按钮

```js
function scrollToTop() { window.scrollTo({ top: 0, behavior: 'smooth' }); }
function scrollToBottom() { window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); }

function initScrollButtons() {
  if ($('#scrollTopBtn')) return;
  const btnTop = el('button', { class: 'scroll-btn scroll-top', id: 'scrollTopBtn', onclick: scrollToTop, title: '回到顶部' }, '⬆');
  const btnBottom = el('button', { class: 'scroll-btn scroll-bottom', id: 'scrollBottomBtn', onclick: scrollToBottom, title: '跳到底部' }, '⬇');
  document.body.appendChild(btnTop);
  document.body.appendChild(btnBottom);
  
  window.addEventListener('scroll', () => {
    const st = window.scrollY;
    btnTop.style.display = st > 200 ? 'flex' : 'none';
    btnBottom.style.display = st < document.body.scrollHeight - window.innerHeight - 200 ? 'flex' : 'none';
  });
}
```

**CSS 关键**：
```css
.scroll-btn { position: fixed; right: 24px; width: 40px; height: 40px; border-radius: 50%; display: none; align-items: center; justify-content: center; z-index: 1000; }
.scroll-top { bottom: 80px; }
.scroll-bottom { bottom: 24px; }
```

## 5. ECharts 优雅降级

**HTML 引入**：
```html
<script>
  (function() {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js';
    s.onload = () => { window._echartsLoaded = true; };
    s.onerror = () => { console.warn('[ECharts] CDN load failed'); };
    document.head.appendChild(s);
  })();
</script>
```

**JS 渲染前检查**：
```js
function safeInitChart(domId, option) {
  if (!window._echartsLoaded || !window.echarts) {
    console.warn('[ECharts] not loaded, skipping chart:', domId);
    return null;
  }
  const dom = document.getElementById(domId);
  if (!dom) return null;
  if (dom._chart) dom._chart.dispose();
  const chart = echarts.init(dom);
  dom._chart = chart;
  chart.setOption(option);
  return chart;
}
```

## 6. 模型三视图切换

- **表格视图**：`renderModelsTable` - 紧凑、排序、分组折叠
- **卡片墙**：`renderModelsCards` - 视觉化、适合移动端
- **分组树**：`renderModelsTree` - 层级清晰、可展开收起

**状态管理**：
```js
let currentView = 'table'; // 'table' | 'cards' | 'tree'
function switchView(view) { currentView = view; renderModels(); }
```

## 7. 表格视图复选框列移除

需同步删除 4 处：
1. 表头 `<th>` (全选复选框)
2. tbody 每行 `<td>` (单选复选框)
3. 全选事件监听 (`thead input[type=checkbox]` change)
4. 单选事件监听 (`tbody input[type=checkbox]` change)
5. 全选按钮相关逻辑变量

## 8. 模型 ID 编码处理

```js
// 含 '/' 的 model ID 需双重编码
const encoded = encodeURIComponent(encodeURIComponent(model));
fetch(`/admin/models/${provider}/${encoded}`)

// 或用 POST + body 避免路径参数问题
fetch('/admin/models', {
  method: 'PATCH',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ provider, model, ...updates })
})
```

## 9. 多租户键兼容显示

```js
function parseUsageKey(key) {
  // 格式: 'provider/model' 或 'tenant:provider/model'
  if (key.includes(':')) {
    const [tenant, rest] = key.split(':', 2);
    return { tenant, ...parseProviderModel(rest) };
  }
  return { tenant: 'default', ...parseProviderModel(key) };
}
```

## 10. 初始化时序

```js
document.addEventListener('DOMContentLoaded', async () => {
  try {
    await loadDashboard();
    await loadModels();
    await loadPolicy();
    await loadCache();
    renderAll();
    initScrollButtons();
    console.log('[free-router] Initialization complete');
  } catch (e) {
    console.error('[free-router] Init failed:', e);
    showToast('初始化失败: ' + e.message, 'error');
  }
});
```

**必须**：等待 `DOMContentLoaded` 再执行，否则 DOM 元素查不到。

## 11. 状态对象结构

```js
const state = {
  dashboard: {},      // /admin/status
  models: [],         // /admin/models
  policy: {},         // /admin/policy
  cache: {},          // /admin/cache
  quota: {},          // quota.usage 等
  providers: [],      // 派生自 dashboard.models.by_provider
  currentView: 'table',
  searchQuery: '',
  typeFilter: 'all',
  providerFilter: 'all',
};
```

## 12. Toast 与模态框模式

```js
function showToast(msg, type = 'info') {
  const layer = $('#toastLayer');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  layer.appendChild(el);
  setTimeout(() => el.classList.add('show'), 10);
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 3000);
}

function showModal(content, { title = '', onConfirm, onCancel } = {}) {
  const layer = $('#modalLayer');
  layer.innerHTML = '';
  layer.appendChild(el('div', { class: 'modal-backdrop', onclick: () => hideModal() }, [
    el('div', { class: 'modal' }, [
      title && el('div', { class: 'modal-header' }, title),
      el('div', { class: 'modal-body' }, content),
      el('div', { class: 'modal-footer' }, [
        onCancel && el('button', { class: 'btn', onclick: () => { onCancel(); hideModal(); } }, '取消'),
        onConfirm && el('button', { class: 'btn primary', onclick: () => { onConfirm(); hideModal(); } }, '确定'),
      ])
    ])
  ]));
  layer.style.display = 'flex';
}
```

## 13. 调试日志规范

统一前缀 `[free-router]`，关键步骤打印：
```js
console.log('[free-router] Loading dashboard...');
console.log('[free-router] Dashboard loaded:', data);
console.log('[free-router] Providers derived:', state.providers);
console.log('[free-router] Rendering models, count:', models.length);
```

## 14. 常见 CSS 变量

```css
:root {
  --bg: #0a0e17;
  --card: #111827;
  --border: #1f2937;
  --fg: #e5e7eb;
  --muted: #9ca3af;
  --accent-cyan: #06b6d4;
  --accent-magenta: #d946ef;
  --accent-green: #22c55e;
  --accent-amber: #f59e0b;
  --radius: 12px;
  --shadow: 0 8px 32px rgba(0,0,0,0.4);
  --shadow-glow: 0 0 24px rgba(6,182,212,0.15);
  --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
}
```
