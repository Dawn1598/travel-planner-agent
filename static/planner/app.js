'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {conversation: null, conversations: [], busy: false, run: null, day: 0, pollTimer: null};
const debugState = {enabled: false, run: null, cursor: 0, count: 0, loading: false, generation: 0};
let toastTimer, lastLoginError = '';

function element(tag, className = '', text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  svg.setAttribute('aria-hidden', 'true');
  return svg;
}
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6500);
}
async function api(path, data) {
  const csrf = document.cookie.split('; ').find(c => c.startsWith('travel_csrftoken='))?.split('=')[1] || '';
  const response = await fetch(path, {
    method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf)},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  let result;
  try { result = await response.json(); } catch (_) { throw new Error('服务暂时不可用，请稍后重试。'); }
  if (!response.ok) throw new Error(result.error || '请求失败，请重试。');
  return result;
}
function setBusy(value) {
  state.busy = value;
  $('#send-button').disabled = value;
  $('#new-button').disabled = value;
  $('#history').disabled = value;
  $('#message-input').disabled = value;
  $('#message-input').placeholder = value ? '正在规划，请稍候…' : '补充信息，或告诉我想调整什么';
  $('#trip-form').querySelectorAll('input, textarea, button').forEach(node => {node.disabled = value;});
  $('#plan-button').textContent = value ? '正在规划…' : (state.conversation?.messages?.length ? '更新并规划 ↗' : '开始规划 ↗');
}
function updateDates() {
  const start = $('#start-date').value, end = $('#end-date').value;
  $('#end-date').min = start;
  $('#end-date').setCustomValidity(start && end && end < start ? '结束日期不能早于开始日期' : '');
  const days = start && end ? Math.round((Date.parse(end) - Date.parse(start)) / 86400000) + 1 : 0;
  $('#trip-duration').textContent = days > 0 ? `共 ${days} 天 · 包含开始和结束当天` : '选择有效的出行日期';
}
function fillTrip(trip = {}) {
  $('#start-location').value = trip.start_location || '';
  $('#start-date').value = trip.start_date || '';
  $('#end-date').value = trip.end_date || '';
  $('#requirements-input').value = trip.requirements || '';
  updateDates();
}
$('#start-date').addEventListener('input', updateDates);
$('#end-date').addEventListener('input', updateDates);
function setView(view) {
  $('#workspace').classList.toggle('show-plan', view === 'plan');
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
}
function renderMessages(messages = []) {
  const container = $('#messages');
  container.replaceChildren();
  container.hidden = !messages.length;
  $('#followup-area').hidden = !messages.length;
  if (!messages.length) {
    const welcome = element('div', 'message welcome');
    welcome.append(element('div', 'welcome-title', '一起安排下一段旅程。'));
    welcome.append(element('div', 'message-body', '告诉我目的地、天数和你的想法。\n也可以边聊边补充，我会接着规划。'));
    container.append(welcome);
  }
  messages.forEach(m => {
    const row = element('div', `message ${m.role === 'user' ? 'user' : 'assistant'}`);
    const role = element('div', 'message-role');
    const avatar = element('span', 'avatar');
    if (m.role === 'user') avatar.textContent = '你'; else avatar.append(icon('route'));
    role.append(avatar, document.createTextNode(m.role === 'user' ? '你' : '行间'));
    row.append(role, element('div', 'message-body', m.content));
    container.append(row);
  });
  container.scrollTop = container.scrollHeight;
}
function safeUrl(value, hosts) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && hosts.some(h => url.hostname === h || url.hostname.endsWith('.' + h)) ? url.href : '';
  } catch (_) { return ''; }
}
function externalLink(text, href, className = '') {
  const a = element('a', className, text);
  a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer';
  return a;
}
function markerUrl(activity) {
  return /^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$/.test(activity.location || '')
    ? `https://uri.amap.com/marker?position=${encodeURIComponent(activity.location)}&name=${encodeURIComponent(activity.name)}` : '';
}
function renderPlan(plan) {
  const root = $('#plan');
  root.replaceChildren();
  root.hidden = !plan?.days?.length;
  $('#empty-plan').hidden = !root.hidden;
  if (root.hidden) return;
  state.day = Math.min(state.day, plan.days.length - 1);
  const overline = element('div', 'plan-overline'); overline.append(icon('pin'), document.createTextNode(plan.city || '你的旅程'));
  root.append(overline, element('h1', '', plan.title), element('p', 'plan-summary', plan.summary));
  const meta = element('div', 'plan-meta');
  const duration = element('span', 'meta-chip'); duration.append(icon('clock'), document.createTextNode(`${plan.days.length} 天行程`)); meta.append(duration);
  if (plan.estimated_budget) meta.append(element('span', 'meta-chip', plan.estimated_budget));
  const checked = element('span', 'meta-chip'); checked.append(icon('check'), document.createTextNode('已检查')); meta.append(checked);
  const mapUrl = safeUrl(plan.map_url, ['amap.com']);
  if (mapUrl) { const link = externalLink('在高德查看', mapUrl, 'map-link'); link.append(icon('link')); meta.append(link); }
  root.append(meta);
  const tabs = element('nav', 'day-tabs'); tabs.setAttribute('aria-label', '行程天数');
  plan.days.forEach((day, i) => { const b = element('button', i === state.day ? 'active' : '', `第 ${day.day} 天`); b.setAttribute('aria-pressed', i === state.day); b.onclick = () => {state.day = i; renderPlan(plan);}; tabs.append(b); });
  root.append(tabs);
  const day = plan.days[state.day];
  const header = element('div', 'day-header'); header.append(element('h2', '', day.title)); if (day.date) header.append(element('span', 'day-date', day.date)); root.append(header);
  const chain = element('div', 'route-chain');
  day.activities.filter(a => a.kind !== '休息' && a.kind !== '交通').forEach((a, i) => { if (i) chain.append(icon('chevron')); chain.append(element('span', '', a.name)); });
  if (chain.childNodes.length) root.append(chain);
  const timeline = element('div', 'timeline');
  day.activities.forEach((a, index) => {
    if (index && (a.travel_minutes || a.transport)) {
      const transit = element('div', 'transit'); transit.append(icon('route'), document.createTextNode([a.transport, a.travel_minutes ? `约 ${a.travel_minutes} 分钟` : ''].filter(Boolean).join(' · '))); timeline.append(transit);
    }
    const row = element('div', 'activity');
    const time = element('div', 'activity-time', a.time); time.append(element('small', '', a.end_time));
    const content = element('div', 'activity-content');
    const heading = element('div', 'activity-heading');
    heading.append(element('h3', '', a.name), element('span', `kind ${a.kind === '餐饮' ? 'food' : a.kind === '酒店' ? 'hotel' : ''}`, a.kind));
    content.append(heading, element('p', 'activity-description', a.description));
    const footer = element('div', 'activity-footer');
    if (a.cost) footer.append(element('span', '', a.cost));
    const url = markerUrl(a); if (url) footer.append(externalLink('查看位置 ↗', url));
    (a.source_ids || []).forEach(id => { const source = (plan.sources || []).find(s => s.note_id === id); const href = safeUrl(source?.url, ['xiaohongshu.com', 'xhslink.com']); if (href) footer.append(externalLink('攻略来源 ↗', href)); });
    if (footer.childNodes.length) content.append(footer);
    row.append(time, content); timeline.append(row);
  });
  root.append(timeline);
  if (plan.notes?.length) { const notes = element('div', 'plan-notes'); plan.notes.forEach(n => notes.append(element('p', '', n))); root.append(notes); }
  if (plan.sources?.length) {
    const sources = element('details', 'sources'); sources.append(element('summary', '', `参考攻略 · ${plan.sources.length} 篇`));
    plan.sources.forEach(s => {
      const href = safeUrl(s.url, ['xiaohongshu.com', 'xhslink.com']); if (!href) return;
      const row = element('div', 'source'); row.append(externalLink(s.title, href), element('time', '', s.published_at ? `发布于 ${new Date(s.published_at).toLocaleDateString('zh-CN')}` : '发布时间未知')); sources.append(row);
    }); root.append(sources);
  }
}
function updateHistory() {
  const select = $('#history'); select.replaceChildren();
  state.conversations.forEach(c => {const option = element('option', '', c.title); option.value = c.id; select.append(option);});
  if (!state.conversations.length) {const option = element('option', '', '新行程'); option.value = ''; select.append(option);}
  select.value = state.conversation?.id || '';
}
function applyConversation(conversation) {
  state.conversation = conversation;
  fillTrip(conversation?.trip_input);
  $('#message-input').value = '';
  $('#plan-button').textContent = conversation?.messages?.length ? '更新并规划 ↗' : '开始规划 ↗';
  renderMessages(conversation?.messages || []);
  renderPlan(conversation?.plan);
  updateHistory();
  setDebugRun(conversation?.active_run || conversation?.last_run || null);
}
function loginUI(info) {
  $('#login-button').classList.toggle('logged', info.logged_in);
  $('#login-label').textContent = info.logged_in ? '小红书已登录' : info.opening ? '等待登录' : '小红书登录';
  if (info.error && info.error !== lastLoginError) {toast(info.error); lastLoginError = info.error;}
}
async function loadState(id) {
  const data = await api('/api/state/' + (id ? `?conversation=${encodeURIComponent(id)}` : ''));
  state.conversations = data.conversations; loginUI(data.xhs); setDebugUI(data.debug_enabled); applyConversation(data.conversation);
  refreshDebug().catch(error => toast(error.message));
  if (data.conversation?.active_run) {setBusy(true); state.run = data.conversation.active_run; pollRun();}
}
function updateProgress(events, finished = false) {
  $('#progress').hidden = false;
  $('#progress').classList.toggle('finished', finished);
  $('#progress-label').textContent = finished ? '本轮处理完成' : events.at(-1)?.text || '正在规划';
  const list = $('#progress-events'); list.replaceChildren();
  events.forEach(event => list.append(element('li', '', event.text)));
  list.scrollTop = list.scrollHeight;
}
async function pollRun() {
  clearTimeout(state.pollTimer);
  if (!state.run) return;
  try {
    const data = await api(`/api/runs/${state.run}/`);
    updateProgress(data.events, data.status !== 'running');
    setDebugRun(data.id);
    await refreshDebug();
    if (data.status === 'running') {state.pollTimer = setTimeout(pollRun, 1500); return;}
    state.run = null; setBusy(false);
    applyConversation(data.conversation);
    const item = state.conversations.find(c => c.id === data.conversation.id);
    if (item) item.title = data.conversation.title;
    updateHistory();
    if (data.status === 'done' && data.conversation.plan?.days?.length) {state.day = 0; renderPlan(data.conversation.plan); setView('plan');}
    $('#message-input').focus();
  } catch (error) {
    toast(error.message + ' 正在重新连接。');
    state.pollTimer = setTimeout(pollRun, 5000);
  }
}
async function sendRequest(payload) {
  if (state.busy) return;
  setBusy(true);
  try {
    if (!state.conversation) {
      const conversation = await api('/api/conversations/', {});
      state.conversations.unshift({id: conversation.id, title: conversation.title});
      state.conversation = conversation; updateHistory();
    }
    const result = await api('/api/chat/', {conversation_id: state.conversation.id, ...payload});
    state.conversation.trip_input = result.trip_input;
    state.conversation.messages.push({role: 'user', content: result.message}); renderMessages(state.conversation.messages);
    const input = $('#message-input');
    input.value = ''; input.style.height = '';
    state.run = result.run_id; setDebugRun(result.run_id); updateProgress([]); pollRun();
  } catch (error) {setBusy(false); toast(error.message);}
}
$('#trip-form').addEventListener('submit', event => {
  event.preventDefault();
  sendRequest({trip: Object.fromEntries(new FormData(event.currentTarget))});
});
$('#chat-form').addEventListener('submit', event => {
  event.preventDefault();
  const message = $('#message-input').value.trim();
  if (message) sendRequest({message});
});
$('#message-input').addEventListener('keydown', event => {if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); $('#chat-form').requestSubmit();}});
$('#message-input').addEventListener('input', event => {event.target.style.height = 'auto'; event.target.style.height = Math.min(event.target.scrollHeight, 160) + 'px';});
$('#new-button').onclick = async () => {
  if (state.busy) return;
  try { const c = await api('/api/conversations/', {}); state.conversations.unshift({id:c.id,title:c.title}); state.day = 0; applyConversation(c); $('#progress').hidden = true; setView('chat'); $('#start-location').focus(); }
  catch (error) {toast(error.message);}
};
$('#history').onchange = async event => {state.day = 0; $('#progress').hidden = true; try {await loadState(event.target.value);} catch (error) {toast(error.message);}};
$('#login-button').onclick = async () => {try {const result = await api('/api/xhs/login/', {}); toast(result.message); $('#login-label').textContent = '等待登录';} catch (error) {toast(error.message);}};
document.querySelectorAll('[data-prompt]').forEach(b => {b.onclick = () => {if (state.busy) return; $('#requirements-input').value = b.dataset.prompt; setView('chat'); $('#requirements-input').focus();};});
document.querySelectorAll('[data-view]').forEach(b => {b.onclick = () => setView(b.dataset.view);});

function setDebugUI(enabled) {
  debugState.enabled = !!enabled;
  $('#debug-toggle').setAttribute('aria-checked', debugState.enabled);
  $('#debug-panel').hidden = !debugState.enabled;
}
function setDebugRun(run) {
  if (debugState.run === run) return;
  debugState.run = run; debugState.cursor = 0; debugState.count = 0; debugState.generation++;
  $('#debug-count').textContent = '0 条';
  $('#debug-log').replaceChildren(element('p', 'debug-empty', '开启后记录后续步骤；发送需求后可查看详细日志。'));
  $('#debug-download').hidden = true;
}
const debugLabels = {'run.start':'任务开始','run.finish':'任务结束','run.error':'任务异常','stage':'执行阶段',
  'llm.request':'模型请求','llm.response':'模型返回','llm.parsed':'结构化结果','llm.validation_retry':'格式修复','llm.error':'模型异常',
  'agent.iteration':'Agent 迭代','agent.waiting_user':'等待补充','tool.request':'调用工具','tool.response':'工具返回','tool.error':'工具异常','tool.waiting_login':'等待登录',
  'mcp.tools':'高德工具清单','mcp.request':'高德请求','mcp.response':'高德返回','mcp.error':'高德异常','mcp.cache_hit':'高德缓存命中',
  'rag.request':'RAG 查询','rag.window':'检索时间窗口','rag.judgment':'资料相关性与充分性','rag.result':'检索结果','rag.empty':'本地无资料','rag.supplement':'触发补爬',
  'crawl.start':'采集开始','crawl.search_opened':'打开搜索','crawl.candidates':'候选笔记','crawl.note':'读取笔记','crawl.note_error':'笔记读取失败','crawl.sort_fallback':'排序降级','crawl.finish':'采集结束','crawl.login_required':'需要登录','crawl.cooldown':'采集冷却',
  'embedding.complete':'向量计算','index.saved':'资料入库','index.unchanged':'笔记未变化','review.result':'行程检查结果','debug.enabled':'开启调试'};
async function refreshDebug() {
  if (!debugState.enabled || !debugState.run || debugState.loading) return;
  debugState.loading = true;
  const run = debugState.run, generation = debugState.generation;
  try {
    const data = await api(`/api/runs/${run}/debug/?after=${debugState.cursor}`);
    if (generation !== debugState.generation || !debugState.enabled) return;
    const root = $('#debug-log');
    const nearBottom = root.scrollTop + root.clientHeight >= root.scrollHeight - 40;
    if (data.entries.length && !debugState.count) root.replaceChildren();
    data.entries.forEach(entry => {
      const details = element('details', 'debug-entry');
      const purpose = entry.data?.purpose || entry.data?.tool || '';
      details.append(element('summary', '', `${new Date(entry.at).toLocaleTimeString('zh-CN')} · ${debugLabels[entry.kind] || entry.kind}${purpose ? ' · ' + purpose : ''}`));
      details.append(element('pre', '', JSON.stringify(entry.data, null, 2))); root.append(details);
    });
    debugState.cursor = data.cursor; debugState.count += data.entries.length;
    while (root.children.length > 300) root.firstElementChild.remove();
    $('#debug-count').textContent = `${debugState.count} 条`;
    if (debugState.count) {$('#debug-download').hidden = false; $('#debug-download').href = `/api/runs/${run}/debug/?download=1`;}
    if (nearBottom) root.scrollTop = root.scrollHeight;
    if (data.has_more) setTimeout(() => refreshDebug().catch(error => toast(error.message)), 50);
  } finally {debugState.loading = false;}
}
$('#debug-toggle').onclick = async () => {
  const button = $('#debug-toggle'); button.disabled = true;
  try {const data = await api('/api/debug/', {enabled: !debugState.enabled}); setDebugUI(data.debug_enabled); await refreshDebug();}
  catch (error) {toast(error.message);} finally {button.disabled = false;}
};
loadState().catch(error => toast(error.message));
setInterval(async () => {try {const data = await api('/api/state/'); loginUI(data.xhs);} catch (_) {}}, 15000);
