// OMNAI Dashboard SPA v9.5
// 5 paginas: Home, Inbox, Tarefas, Emails, Faturas

const POLL_MS = 8000;
const URG_LABEL = {P0:"Urgente", P1:"Esta semana", P2:"Acompanhar", P3:"Outros"};
const EMOJI_URG = {P0:"🔴", P1:"🟡", P2:"🟢", P3:"⚪"};

const ICONS = {
  check:'<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>',
  x:'<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  clock:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  trash:'<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>',
  open:'<svg viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
  plus:'<svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  send:'<svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
  inbox:'<svg viewBox="0 0 24 24"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
  mail:'<svg viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22 6 12 13 2 6"/></svg>',
  bot:'<svg viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>',
  alert:'<svg viewBox="0 0 24 24"><path d="M10.29 3.86l-8.06 14a2 2 0 0 0 1.71 3h16.12a2 2 0 0 0 1.71-3l-8.06-14a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12" y2="17"/></svg>',
  file:'<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  list:'<svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
};

const escapeHTML = s => (s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtAge = iso => {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime())/1000;
  if (diff < 60) return "agora";
  if (diff < 3600) return Math.floor(diff/60)+"min";
  if (diff < 86400) return Math.floor(diff/3600)+"h";
  if (diff < 604800) return Math.floor(diff/86400)+"d";
  return new Date(iso).toLocaleDateString("pt-PT",{day:"numeric",month:"short"});
};
const fmtHora = iso => iso ? new Date(iso).toLocaleTimeString("pt-PT",{hour:"2-digit",minute:"2-digit"}) : "";

function isoWeek(){
  const d = new Date(); d.setHours(0,0,0,0);
  d.setDate(d.getDate() + 4 - (d.getDay()||7));
  const ys = new Date(d.getFullYear(),0,1);
  return "S" + Math.ceil((((d-ys)/86400000)+1)/7) + "/" + d.getFullYear();
}

function toast(msg, type="info"){
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.remove("hide");
  t.classList.add("show");
  setTimeout(()=>{t.classList.remove("show");t.classList.add("hide");}, 2400);
}

const PAGES = ["home","inbox","todos","emails","invoices"];
const PAGE_TITLES = {home:"Home", inbox:"Inbox", todos:"Tarefas", emails:"Emails", invoices:"Faturas"};

let state = {
  active: "home",
  inbox_items: [],
  inbox_stats: {P0:0,P1:0,P2:0,P3:0},
  inbox_resolved: [],
  todos: [],
  todos_done: [],
  email_summary: {},
  emails: [],
  email_selected_account: null,
  invoices_today: [],
  invoices_manual: [],
  invoices_recent: [],
};
let pollTimer = null;

async function fetchJson(url, opts={}){
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

function switchTab(tab){
  state.active = tab;
  document.getElementById("page-title").textContent = PAGE_TITLES[tab];
  document.querySelectorAll("nav.tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  PAGES.forEach(p => document.getElementById("page-"+p).classList.toggle("active", p === tab));
  loadCurrent();
}

async function loadCurrent(){
  const tab = state.active;
  // v9.7.1: skipa refresh se utilizador esta a escrever num input/textarea
  const active = document.activeElement;
  if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
    refreshBadges();
    return;
  }
  try {
    if (tab === "home") await loadHome();
    else if (tab === "inbox") await loadInbox();
    else if (tab === "todos") await loadTodos();
    else if (tab === "emails") await loadEmails();
    else if (tab === "invoices") await loadInvoices();
  } catch(e){
    console.error(e);
  }
  refreshBadges();
}

// ============ HOME ============

async function loadHome(){
  const data = await fetchJson("/api/dashboard/summary");
  state.inbox_stats = data.inbox_stats || {};
  state.todos_stats = data.todos_stats || {};
  state.emails_total = data.emails_pending || 0;
  state.invoices_manual_count = data.invoices_manual_count || 0;

  const inbox_total = (data.inbox_stats?.P0||0)+(data.inbox_stats?.P1||0)+(data.inbox_stats?.P2||0)+(data.inbox_stats?.P3||0);

  const urgent = (data.urgentes || []);

  let html = `
  <div class="metric-grid">
    <div class="metric" onclick="switchTab('inbox')">
      <div class="label">${ICONS.inbox}Inbox</div>
      <div class="value">${inbox_total}</div>
      <div class="sub">${data.inbox_stats?.P0||0} P0 &middot; ${data.inbox_stats?.P1||0} P1</div>
    </div>
    <div class="metric" onclick="switchTab('todos')">
      <div class="label">${ICONS.check}Tarefas</div>
      <div class="value">${data.todos_stats?.total||0}</div>
      <div class="sub">${data.todos_stats?.P0||0} P0 &middot; ${data.todos_stats?.P1||0} P1</div>
    </div>
    <div class="metric" onclick="switchTab('emails')">
      <div class="label">${ICONS.mail}Emails</div>
      <div class="value">${data.emails_pending||0}</div>
      <div class="sub">a tratar em ${Object.keys(data.email_summary||{}).length} caixa(s)</div>
    </div>
    <div class="metric" onclick="switchTab('invoices')">
      <div class="label">${ICONS.file}Faturas</div>
      <div class="value">${data.invoices_today_count||0}</div>
      <div class="sub">hoje &middot; ${data.invoices_manual_count||0} manual</div>
    </div>
  </div>`;

  if (urgent.length){
    html += `<div class="section-title">${ICONS.alert} Urgente <span class="count">${urgent.length}</span></div><div class="cards">`;
    urgent.forEach(it => html += renderCard(it, "inbox", true));
    html += `</div>`;
  } else {
    html += `<div class="empty">${ICONS.inbox}<div class="empty-title">Nada urgente</div><div class="empty-sub">Sem itens P0 ou P1 abertos.</div></div>`;
  }

  document.getElementById("page-home").innerHTML = html;
}

// ============ INBOX ============

async function loadInbox(){
  const [a, b] = await Promise.all([
    fetchJson("/api/inbox/items"),
    fetchJson("/api/inbox/resolved"),
  ]);
  state.inbox_items = a.items || [];
  state.inbox_stats = a.stats || {};
  state.inbox_resolved = b.items || [];
  renderInbox();
}

function renderInbox(){
  const items = state.inbox_items;
  const stats = state.inbox_stats;
  const total = (stats.P0||0)+(stats.P1||0)+(stats.P2||0)+(stats.P3||0);

  let html = `
  <div class="metric-grid">
    <div class="metric"><div class="label">P0 Urgente</div><div class="value" style="color:#d94040">${stats.P0||0}</div></div>
    <div class="metric"><div class="label">P1 Esta semana</div><div class="value" style="color:#b97515">${stats.P1||0}</div></div>
    <div class="metric"><div class="label">P2 Acompanhar</div><div class="value" style="color:#5d9420">${stats.P2||0}</div></div>
    <div class="metric"><div class="label">Total aberto</div><div class="value">${total}</div></div>
  </div>`;

  if (items.length === 0){
    html += `<div class="empty">${ICONS.inbox}<div class="empty-title">Inbox limpo</div><div class="empty-sub">Sem itens abertos.</div></div>`;
  } else {
    const groups = {P0:[],P1:[],P2:[],P3:[]};
    items.forEach(it => (groups[it.urgencia]||groups.P2).push(it));
    for (const u of ["P0","P1","P2","P3"]){
      if (!groups[u].length) continue;
      html += `<div class="section-title">${EMOJI_URG[u]} ${URG_LABEL[u]} <span class="count">${groups[u].length}</span></div><div class="cards">`;
      for (const it of groups[u]) html += renderCard(it, "inbox", true);
      html += `</div>`;
    }
  }

  if (state.inbox_resolved.length){
    html += `<div class="section-title">${ICONS.check} Resolvido nas ultimas 24h <span class="count">${state.inbox_resolved.length}</span></div>`;
    state.inbox_resolved.forEach(r => {
      html += `<div style="font-size:13px;color:var(--hint);padding:4px 0;display:flex;gap:8px;align-items:center">
        <span style="text-decoration:line-through">${escapeHTML(r.titulo)}</span>
        <span style="margin-left:auto;font-size:11px">${escapeHTML(r.empresa||"")} &middot; ${fmtHora(r.resolvido_em)}</span>
      </div>`;
    });
  }

  document.getElementById("page-inbox").innerHTML = html;
}

function renderCard(it, type, withLeft){
  const u = it.urgencia || "P2";
  const link = it.link_origem
    ? `<button class="btn open" onclick="openLink('${(it.link_origem||"").replace(/'/g,"\\'")}')">${ICONS.open}Abrir</button>`
    : "";
  return `
    <div class="card ${u.toLowerCase()} ${withLeft?'lefted':''}" data-id="${it.id}" data-urg="${u}">
      <div class="card-head">
        <div class="tags">
          <span class="tag urg-${u.toLowerCase()}">${u} ${URG_LABEL[u]||""}</span>
          ${it.empresa ? '<span class="tag neutral">'+escapeHTML(it.empresa)+'</span>' : ''}
        </div>
        <span class="age">${fmtAge(it.criado_em)}</span>
      </div>
      <div class="title">${escapeHTML(it.titulo)}</div>
      ${it.detalhe ? '<div class="detail">'+escapeHTML(it.detalhe)+'</div>' : ''}
      <div class="actions">
        ${link}
        <button class="btn resolve" onclick="inboxAction('${it.id}','done')">${ICONS.check}Resolver</button>
        <button class="btn snooze" onclick="inboxAction('${it.id}','snooze')">${ICONS.clock}Snooze 7d</button>
        <button class="btn dismiss" onclick="inboxAction('${it.id}','dismiss')">${ICONS.trash}Dispensar</button>
      </div>
    </div>`;
}

async function inboxAction(id, action){
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if (card) card.classList.add("removing");
  try {
    await fetch(`/api/inbox/action?id=${id}&action=${action}`, {method:"POST"});
    toast(action === "done" ? "Resolvido" : action === "snooze" ? "Snoozed 7d" : "Dispensado");
    setTimeout(loadCurrent, 250);
  } catch(e){
    toast("Erro: " + e.message);
    loadCurrent();
  }
}

function openLink(url){ window.open(url, "_blank"); }

// ============ TODOS ============

async function loadTodos(){
  const a = await fetchJson("/api/todos");
  state.todos = a.open || [];
  state.todos_done = a.done || [];
  state.todos_stats = a.stats || {};
  renderTodos();
}

function renderTodos(){
  let html = `
  <input type="text" class="todo-input" id="todo-new" placeholder="+ Nova tarefa (Enter para adicionar)" autocomplete="off">
  `;

  if (state.todos.length === 0){
    html += `<div class="empty" style="padding:32px 16px">${ICONS.check}<div class="empty-title">Sem tarefas abertas</div><div class="empty-sub">Adiciona uma acima.</div></div>`;
  } else {
    html += `<div class="section-title">${ICONS.list} Abertas <span class="count">${state.todos.length}</span></div>`;
    state.todos.forEach(t => {
      html += `
        <div class="todo-row" data-id="${t.id}">
          <div class="chk" onclick="todoToggle('${t.id}', true)">${ICONS.check}</div>
          <div class="todo-body">
            <div class="todo-text">${escapeHTML(t.titulo)}</div>
            <div class="todo-meta">
              <span>${t.prioridade}</span>
              ${t.empresa ? '<span>'+escapeHTML(t.empresa)+'</span>' : ''}
              <span>${fmtAge(t.criado_em)}</span>
            </div>
          </div>
          <button class="del-btn" onclick="todoDelete('${t.id}')" title="Apagar">${ICONS.trash}</button>
        </div>`;
    });
  }

  if (state.todos_done.length){
    html += `<div class="section-title">${ICONS.check} Concluidas <span class="count">${state.todos_done.length}</span></div>`;
    state.todos_done.forEach(t => {
      html += `
        <div class="todo-row done" data-id="${t.id}">
          <div class="chk" onclick="todoToggle('${t.id}', false)">${ICONS.check}</div>
          <div class="todo-body">
            <div class="todo-text">${escapeHTML(t.titulo)}</div>
            <div class="todo-meta">
              <span>${t.prioridade}</span>
              <span>concluida ${fmtAge(t.completado_em)}</span>
            </div>
          </div>
          <button class="del-btn" onclick="todoDelete('${t.id}')" title="Apagar">${ICONS.trash}</button>
        </div>`;
    });
  }

  document.getElementById("page-todos").innerHTML = html;

  const input = document.getElementById("todo-new");
  if (input) {
    input.addEventListener("keydown", async e => {
      if (e.key === "Enter" && input.value.trim()){
        const titulo = input.value.trim();
        input.value = "";
        await fetch("/api/todos", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({titulo, prioridade:"P2"})});
        toast("Adicionada");
        loadTodos();
      }
    });
  }
}

async function todoToggle(id, complete){
  try {
    await fetch(`/api/todos/${id}/${complete?'complete':'reopen'}`, {method:"POST"});
    loadTodos();
  } catch(e){ toast("Erro"); }
}

async function todoDelete(id){
  if (!confirm("Apagar esta tarefa?")) return;
  try {
    await fetch(`/api/todos/${id}`, {method:"DELETE"});
    toast("Apagada");
    loadTodos();
  } catch(e){ toast("Erro"); }
}

// ============ EMAILS ============

async function loadEmails(){
  const a = await fetchJson("/api/emails/summary");
  state.email_summary = a.summary || {};
  state.emails_total = a.total_pending || 0;

  if (state.email_selected_account){
    const b = await fetchJson(`/api/emails/pending?account=${encodeURIComponent(state.email_selected_account)}`);
    state.emails = b.items || [];
  } else {
    state.emails = [];
  }
  renderEmails();
}

function renderEmails(){
  const summary = state.email_summary;
  const accounts = Object.keys(summary);

  let html = `<div class="section-title">${ICONS.mail} Resumo caixas <span class="count">${accounts.length}</span></div>`;
  html += `<div class="mailbox-grid">`;

  if (accounts.length === 0){
    html += `<div class="empty" style="grid-column:1/-1;padding:24px 16px">${ICONS.mail}<div class="empty-title">Sem actividade ainda</div><div class="empty-sub">Workers ainda nao detectaram emails actionable.</div></div>`;
  }

  accounts.forEach(acc => {
    const s = summary[acc];
    const isActive = state.email_selected_account === acc;
    html += `
      <div class="mailbox-card ${isActive?'active':''}" onclick="selectMailbox('${acc.replace(/'/g,"\\'")}')">
        <div class="name">${escapeHTML(acc)}</div>
        <div class="counts">
          <span><strong>${s.pending||0}</strong> pendentes</span>
          ${s.drafted ? '<span><strong>'+s.drafted+'</strong> com draft</span>' : ''}
          ${s.resolved_24h ? '<span>'+s.resolved_24h+' resolvidos 24h</span>' : ''}
        </div>
      </div>`;
  });
  html += `</div>`;

  if (state.email_selected_account){
    html += `<div class="section-title">${ICONS.mail} ${escapeHTML(state.email_selected_account)} <span class="count">${state.emails.length}</span></div>`;
    if (state.emails.length === 0){
      html += `<div class="empty">${ICONS.check}<div class="empty-title">Caixa tratada</div><div class="empty-sub">Sem emails actionable nesta conta.</div></div>`;
    } else {
      state.emails.forEach(e => html += renderEmailCard(e));
    }
  } else if (accounts.length){
    html += `<div class="empty"><div class="empty-sub" style="margin-top:0">Selecciona uma caixa acima para ver detalhes.</div></div>`;
  }

  document.getElementById("page-emails").innerHTML = html;
}

function renderEmailCard(e){
  const hasDraft = !!e.draft_text;
  const gmailUrl = e.gmail_thread_url || (e.thread_id ? `https://mail.google.com/mail/u/0/#inbox/${e.thread_id}` : "");
  return `
    <div class="email-card" data-id="${e.id}">
      <div class="email-from">${escapeHTML(e.from_addr||"(sem remetente)")}<span style="float:right;color:var(--hint);font-size:11px">${fmtAge(e.criado_em)}</span></div>
      <div class="email-subject">${escapeHTML(e.subject||"(sem assunto)")}</div>
      <div class="email-snippet">${escapeHTML(e.snippet||"")}</div>
      ${hasDraft ? '<div class="email-draft">'+escapeHTML(e.draft_text)+'</div>' : ''}
      <div class="actions">
        ${gmailUrl ? '<button class="btn open" onclick="openLink(\''+gmailUrl+'\')">'+ICONS.open+'Abrir Gmail</button>' : ''}
        ${hasDraft
          ? '<button class="btn primary" onclick="emailReplied(\''+e.id+'\')">'+ICONS.send+'Marcar respondido</button>'
          : '<button class="btn draft" onclick="emailGenDraft(\''+e.id+'\', this)">'+ICONS.bot+'Gerar resposta</button>'
        }
        <button class="btn resolve" onclick="emailDone('${e.id}')">${ICONS.check}Tratado</button>
        <button class="btn dismiss" onclick="emailDismiss('${e.id}')">${ICONS.trash}Dispensar</button>
      </div>
    </div>`;
}

async function selectMailbox(account){
  state.email_selected_account = state.email_selected_account === account ? null : account;
  await loadEmails();
}

async function emailGenDraft(id, btn){
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div>A gerar...';
  try {
    const r = await fetchJson(`/api/emails/${id}/draft`, {method:"POST"});
    if (r.ok){
      toast(r.cached ? "Draft em cache" : "Draft gerado");
      loadEmails();
    } else {
      toast("Erro: " + r.error);
      btn.disabled = false;
      btn.innerHTML = ICONS.bot + "Gerar resposta";
    }
  } catch(e){ toast("Erro: " + e.message); }
}

async function emailDone(id){
  const card = document.querySelector(`.email-card[data-id="${id}"]`);
  if (card) card.classList.add("removing");
  try {
    await fetch(`/api/emails/${id}/done`, {method:"POST"});
    toast("Marcado tratado");
    setTimeout(loadEmails, 250);
  } catch(e){ toast("Erro"); }
}

async function emailReplied(id){
  const card = document.querySelector(`.email-card[data-id="${id}"]`);
  if (card) card.classList.add("removing");
  try {
    await fetch(`/api/emails/${id}/replied`, {method:"POST"});
    toast("Respondido");
    setTimeout(loadEmails, 250);
  } catch(e){ toast("Erro"); }
}

async function emailDismiss(id){
  const card = document.querySelector(`.email-card[data-id="${id}"]`);
  if (card) card.classList.add("removing");
  try {
    await fetch(`/api/emails/${id}/dismiss`, {method:"POST"});
    toast("Dispensado");
    setTimeout(loadEmails, 250);
  } catch(e){ toast("Erro"); }
}

// ============ INVOICES ============

async function loadInvoices(){
  const a = await fetchJson("/api/invoices/overview");
  state.invoices_today = a.archived_today || [];
  state.invoices_manual = a.manual_queue || [];
  state.invoices_recent = a.recent_fs || [];
  renderInvoices();
}

function renderInvoices(){
  let html = `<div class="metric-grid">
    <div class="metric"><div class="label">${ICONS.check} Arquivadas hoje</div><div class="value">${state.invoices_today.length}</div></div>
    <div class="metric"><div class="label">${ICONS.alert} Manual queue</div><div class="value" style="color:${state.invoices_manual.length?'#d94040':''}">${state.invoices_manual.length}</div></div>
  </div>`;

  if (state.invoices_manual.length){
    html += `<div class="section-title">${ICONS.alert} Para extracao manual <span class="count">${state.invoices_manual.length}</span></div>`;
    state.invoices_manual.forEach((m, idx) => {
      const obj = (typeof m === "string") ? JSON.parse(m) : m;
      const subj = obj.subject || obj.raw || "(sem subject)";
      const from = obj.from || obj.from_addr || "";
      const key = "fp_" + idx;
      window.__manualQ = window.__manualQ || {};
      window.__manualQ[key] = {from, subj};
      html += `<div class="card" style="margin-bottom:6px">
        <div class="title">${escapeHTML(subj.substring(0,140))}</div>
        ${from?'<div class="detail">'+escapeHTML(from)+'</div>':''}
        <div class="actions" style="margin-top:8px">
          <button class="btn dismiss" onclick="promptLearnByKey('${key}')">${ICONS.bot} Não é fatura, aprender</button>
        </div>
      </div>`;
    });
  }

  html += `<div class="section-title">${ICONS.check} Arquivadas hoje <span class="count">${state.invoices_today.length}</span></div>`;
  if (state.invoices_today.length === 0){
    html += `<div class="empty" style="padding:24px 16px"><div class="empty-sub">Sem faturas arquivadas hoje.</div></div>`;
  } else {
    state.invoices_today.forEach(m => {
      const obj = (typeof m === "string") ? JSON.parse(m) : m;
      const company = obj.company || obj.empresa || "";
      const supplier = obj.supplier || "";
      const amount = obj.amount ? `${obj.amount} ${obj.currency||"EUR"}` : "";
      const date = obj.date || "";
      html += `<div class="card" style="margin-bottom:6px">
        <div class="card-head">
          <div class="tags">
            ${company?'<span class="tag neutral">'+escapeHTML(company)+'</span>':''}
            ${amount?'<span class="tag info">'+escapeHTML(amount)+'</span>':''}
          </div>
          <span class="age">${date}</span>
        </div>
        <div class="title">${escapeHTML(supplier||obj.subject||"(fatura)")}</div>
      </div>`;
    });
  }

  document.getElementById("page-invoices").innerHTML = html;
}

// ============ BADGES ============

async function refreshBadges(){
  try {
    const r = await fetchJson("/api/dashboard/badges");
    setBadge("inbox", r.inbox_total||0);
    setBadge("todos", r.todos_open||0);
    setBadge("emails", r.emails_pending||0);
  } catch(e){}
}

function setBadge(name, n){
  const b = document.getElementById("badge-"+name);
  if (!b) return;
  if (n > 0){
    b.textContent = n > 99 ? "99+" : n;
    b.style.display = "inline-flex";
  } else {
    b.style.display = "none";
  }
}

// ============ LEARN RULES ============

function promptLearnByKey(key) {
  const data = (window.__manualQ || {})[key];
  if (!data) { toast("Item nao encontrado"); return; }
  promptLearn(data.from || "", data.subj || "");
}

function promptLearn(fromAddr, subject) {
  const m = (fromAddr || "").match(/<([^>]+)>/);
  const email = (m ? m[1] : fromAddr || "").trim();
  const domain = (email.split("@")[1] || "").toLowerCase();
  const subjClean = (subject || "").replace(/[^a-zA-Z0-9 áéíóúãõçÁÉÍÓÚÂÊÔÀÈÌÒÙ]/g, " ");
  const words = subjClean.split(/\s+/).filter(w => w.length >= 3);
  const suggestedKw = words.slice(0, 4).join(" ");

  closeLearnModal();
  const m1 = document.createElement("div");
  m1.id = "learnModal";
  m1.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:200;display:flex;align-items:center;justify-content:center;padding:16px";

  const safeSubj = escapeHTML(subject || "(sem assunto)");
  const safeKw = escapeHTML(suggestedKw);
  const safeDomain = escapeHTML(domain);

  m1.innerHTML =
    '<div style="background:var(--surface);border:0.5px solid var(--border);border-radius:12px;padding:18px;max-width:480px;width:100%;box-shadow:0 8px 32px rgba(0,0,0,0.2)">' +
    '<div style="font-size:16px;font-weight:600;margin-bottom:4px">Aprender a classificar</div>' +
    '<div style="font-size:13px;color:var(--muted);margin-bottom:16px">Cria uma regra para que emails parecidos sejam classificados automaticamente.</div>' +
    '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">Assunto recebido:</div>' +
    '<div style="background:var(--bg);padding:8px 10px;border-radius:6px;font-size:13px;margin-bottom:14px;border:0.5px solid var(--border)">' + safeSubj + '</div>' +
    '<div style="font-size:12px;font-weight:500;margin-bottom:6px">Tipo de regra</div>' +
    '<select id="learnType" style="width:100%;padding:8px 10px;border-radius:6px;border:0.5px solid var(--border);background:var(--bg);color:var(--text);margin-bottom:12px;font-family:inherit;font-size:13px">' +
    '<option value="subject_keyword">Palavra-chave do assunto (recomendado)</option>' +
    '<option value="sender">Remetente exacto</option>' +
    '<option value="domain">Domínio inteiro @' + safeDomain + '</option>' +
    '</select>' +
    '<div style="font-size:12px;font-weight:500;margin-bottom:6px">Pattern</div>' +
    '<input id="learnPattern" type="text" style="width:100%;padding:8px 10px;border-radius:6px;border:0.5px solid var(--border);background:var(--bg);color:var(--text);margin-bottom:6px;font-family:inherit;font-size:13px" value="' + safeKw + '">' +
    '<div id="learnPreview" style="font-size:11px;color:var(--hint);margin-bottom:14px"></div>' +
    '<div style="font-size:12px;font-weight:500;margin-bottom:6px">Classificar como</div>' +
    '<div style="display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap">' +
    '<button id="cls-archive" class="cls-btn" onclick="markActive(\'archive\')" style="flex:1;min-width:90px;padding:7px;border-radius:6px;border:0.5px solid var(--border);background:var(--accent);color:white;cursor:pointer;font-family:inherit;font-size:13px">Archive</button>' +
    '<button id="cls-delete" class="cls-btn" onclick="markActive(\'delete\')" style="flex:1;min-width:90px;padding:7px;border-radius:6px;border:0.5px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer;font-family:inherit;font-size:13px">Delete</button>' +
    '<button id="cls-keep" class="cls-btn" onclick="markActive(\'keep\')" style="flex:1;min-width:90px;padding:7px;border-radius:6px;border:0.5px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer;font-family:inherit;font-size:13px">Keep</button>' +
    '</div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
    '<button onclick="closeLearnModal()" style="padding:8px 14px;border-radius:6px;border:0.5px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-family:inherit;font-size:13px">Cancelar</button>' +
    '<button onclick="submitLearn()" style="padding:8px 14px;border-radius:6px;border:0;background:var(--accent);color:white;cursor:pointer;font-family:inherit;font-size:13px;font-weight:500">Aprender regra</button>' +
    '</div>' +
    '</div>';

  document.body.appendChild(m1);
  window.__learnState = {email, domain, suggestedKw, cls: "archive", from: fromAddr, subject: subject};

  const type = document.getElementById("learnType");
  const pattern = document.getElementById("learnPattern");
  type.addEventListener("change", () => {
    if (type.value === "sender") pattern.value = window.__learnState.email;
    else if (type.value === "domain") pattern.value = window.__learnState.domain;
    else pattern.value = window.__learnState.suggestedKw;
    updateLearnPreview();
  });
  pattern.addEventListener("input", updateLearnPreview);
  updateLearnPreview();
  pattern.focus();
  pattern.select();
}

function updateLearnPreview() {
  const t = document.getElementById("learnType");
  const p = document.getElementById("learnPattern");
  const pv = document.getElementById("learnPreview");
  if (!t || !p || !pv) return;
  const cls = (window.__learnState && window.__learnState.cls) || "archive";
  const v = p.value;
  let label = "";
  if (t.value === "subject_keyword") label = "emails com '" + v + "' no assunto";
  else if (t.value === "sender") label = "emails do remetente " + v;
  else label = "emails de @" + v.replace(/^@/, "");
  pv.textContent = "Apanha " + label + " → " + cls;
}

function markActive(cls) {
  window.__learnState = window.__learnState || {};
  window.__learnState.cls = cls;
  ["archive", "delete", "keep"].forEach(c => {
    const b = document.getElementById("cls-" + c);
    if (!b) return;
    if (c === cls) {
      b.style.background = "var(--accent)";
      b.style.color = "white";
    } else {
      b.style.background = "var(--bg)";
      b.style.color = "var(--text)";
    }
  });
  updateLearnPreview();
}

function closeLearnModal() {
  const m = document.getElementById("learnModal");
  if (m) m.remove();
}

async function submitLearn() {
  const type = document.getElementById("learnType").value;
  const pattern = document.getElementById("learnPattern").value.trim();
  const cls = (window.__learnState && window.__learnState.cls) || "archive";
  if (!pattern) { toast("Pattern vazio"); return; }
  closeLearnModal();
  try {
    const r = await fetch("/api/learn", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({type, pattern, cls, notes: "manual via dashboard", remove_from: {from: (window.__learnState||{}).from || "", subject: (window.__learnState||{}).subject || ""}})
    });
    const j = await r.json();
    if (j.ok) {
      toast("Regra: " + pattern + " → " + cls);
      loadCurrent();
    } else {
      toast("Erro: " + (j.error || ""));
    }
  } catch (e) {
    toast("Erro: " + e.message);
  }
}

// ============ INIT ============

document.querySelectorAll("nav.tabs button").forEach(b => {
  b.addEventListener("click", () => switchTab(b.dataset.tab));
});

document.getElementById("weekinfo").textContent = isoWeek() + " · " + new Date().toLocaleDateString("pt-PT",{weekday:"long", day:"numeric", month:"long"});

switchTab("home");

setInterval(() => { if (!document.hidden) loadCurrent(); }, POLL_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadCurrent(); });

if ("serviceWorker" in navigator){
  navigator.serviceWorker.register("/sw.js").catch(()=>{});
}
