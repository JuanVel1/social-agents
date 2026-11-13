// Panel del agente: lista de ejecuciones, grafo de pasos animado con GSAP y detalle en vivo (SSE).
(() => {
  'use strict';

  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const D = (d) => (REDUCED ? 0 : d);
  const RING = 2 * Math.PI * 20;
  const NODE_W = 204, NODE_H = 136, GAP_X = 52, GAP_Y = 64;
  const ACTIVE = new Set(['starting', 'running']);
  const LIVE = new Set(['running', 'waiting']);
  const FINISHED = new Set(['done', 'error', 'skipped']);

  const STATUS = {
    pending: 'En espera', running: 'En curso', waiting: 'Esperando', done: 'Listo', error: 'Error',
    skipped: 'Omitido', starting: 'Iniciando', cancelled: 'Cancelada', interrupted: 'Interrumpida',
  };
  const CMD_INFO = {
    plan: ['Parrilla del mes', 'Temas, textos, diseño y envío a aprobación'],
    'rediseñar': ['Rediseñar parrilla', 'Nuevas imágenes para una parrilla ya escrita'],
    publish: ['Publicar aprobados', 'Programa en Postiz lo aprobado en Notion'],
    fotos: ['Banco de fotos', 'Recorta las fotos nuevas de Instagram'],
    moodboard: ['Moodboard', 'Dirección visual y plantillas de Pinterest'],
    'onboarding run': ['Onboarding', 'Procesa el formulario de clientes'],
  };
  const ARGS = {
    client: ['Cliente'], month: ['Mes'],
    dry_run: ['Simulación', 'No sube nada a Notion ni a Postiz'],
    reemplazar: ['Reemplazar pendientes', 'Archiva en Notion las filas del mes que siguen en Pendiente'],
    now: ['Programar en 10 minutos', 'Prueba: ignora la fecha del calendario'],
    importar: ['Importar de la carpeta', 'Fotos reales sueltas en clients/<cliente>/fotos/, en vez de leer Instagram'],
    forzar: ['Forzar análisis', 'Relee la dirección visual aunque el tablero no cambie'],
    rehacer_referencias: ['Rehacer referencias', 'Vuelve a diseñar las plantillas que ya existían'],
    solo_referencias: ['Solo referencias', 'No relee la dirección visual'],
    revalidar: ['Revalidar', 'Repite las pruebas automáticas de las plantillas aprobadas'],
    reintentar_descartadas: ['Reintentar descartadas', 'Rediseña solo las que la prueba descartó'],
  };

  const state = {
    meta: null, runs: new Map(), selected: null, run: null, stepKey: null, follow: true, tab: 'step',
    filter: 'all', graphFor: null, nodes: new Map(), edges: [], detailFor: null, logTimer: null,
    listEls: new Map(), launchCmd: 'plan',
  };

  // ------------------------------------------------------------------ utilidades
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* sin almacenamiento */ } },
  };
  const ms = (iso) => (iso ? new Date(iso).getTime() : NaN);
  function fmtDur(t) {
    if (!isFinite(t) || t < 0) return '—';
    const s = t / 1000;
    if (s < 10) return `${s.toFixed(1)} s`;
    if (s < 60) return `${Math.round(s)} s`;
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    if (m < 60) return `${m} min ${String(r).padStart(2, '0')} s`;
    return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
  }
  function clock(t) {
    if (!isFinite(t) || t < 0) return '00:00';
    const s = Math.floor(t / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return (h ? `${h}:` : '') + `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  }
  const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' }) : '');
  function ago(iso) {
    const t = Date.now() - ms(iso);
    if (!isFinite(t)) return '';
    if (t < 45e3) return 'hace un momento';
    if (t < 3600e3) return `hace ${Math.round(t / 60e3)} min`;
    if (t < 86400e3) return `hace ${Math.round(t / 3600e3)} h`;
    return new Date(iso).toLocaleDateString('es', { day: 'numeric', month: 'short' });
  }
  function dayLabel(iso) {
    const d = new Date(iso), today = new Date();
    const key = (x) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
    if (key(d) === key(today)) return 'Hoy';
    const y = new Date(today); y.setDate(today.getDate() - 1);
    if (key(d) === key(y)) return 'Ayer';
    return d.toLocaleDateString('es', { weekday: 'long', day: 'numeric', month: 'long' });
  }
  const fileUrl = (p) => `/files?path=${encodeURIComponent(p)}`;
  const isImage = (p) => /\.(png|jpe?g|webp|gif)$/i.test(p);
  const baseName = (p) => p.split(/[\\/]/).pop();

  function summarize(run) {
    const steps = run.steps || [];
    const cur = steps.find((s) => LIVE.has(s.status));
    return {
      id: run.id, command: run.command, title: run.title, client: run.client, group: run.group,
      status: run.status, started: run.started, finished: run.finished, pct: run.pct, error: run.error,
      args: run.args, current: cur ? cur.label : null, cancellable: run.cancellable,
    };
  }
  function cmdLine(run) {
    const parts = ['social-agent', ...String(run.command || '').split(' ')];
    for (const [k, v] of Object.entries(run.args || {})) {
      if (k === 'llm') continue;
      const flag = `--${k.replace(/_/g, '-')}`;
      if (v === true) parts.push(flag);
      else if (Array.isArray(v)) v.forEach((x) => parts.push(flag, x));
      else parts.push(flag, v);
    }
    return (run.args && run.args.llm === 'fake' ? 'LLM_PROVIDER=fake ' : '') + parts.join(' ');
  }
  function stepPct(s) {
    if (s.status === 'done') return 100;
    if (s.pct !== null && s.pct !== undefined) return s.pct;
    return null;
  }
  function stepDuration(s) {
    if (!s.started) return NaN;
    return (s.finished ? ms(s.finished) : Date.now()) - ms(s.started);
  }
  function waitLeft(s) {
    if (!s.wait || !s.wait.until) return 0;
    return Math.max(0, (ms(s.wait.until) - Date.now()) / 1000);
  }
  function subText(s) {
    switch (s.status) {
      case 'running':
        if (s.activity) return s.activity;
        if (s.detail) return s.detail;
        return s.total ? `${s.done} de ${s.total}` : 'En curso…';
      case 'waiting': return `${s.wait ? s.wait.reason : 'Esperando'} · ${Math.ceil(waitLeft(s))} s`;
      case 'done': return `Listo · ${fmtDur(stepDuration(s))}` + (s.total ? ` · ${s.done}/${s.total}` : '');
      case 'error': return s.error ? s.error.replace(/^\w+Error: /, '') : 'Error';
      case 'skipped': return s.detail || 'Omitido';
      default: return 'En espera';
    }
  }

  function toast(msg, err = false) {
    const el = document.createElement('div');
    el.className = `toast${err ? ' err' : ''}`;
    el.textContent = msg;
    $('#toasts').appendChild(el);
    gsap.from(el, { y: 14, opacity: 0, duration: D(0.35), ease: 'power3.out' });
    setTimeout(() => gsap.to(el, { opacity: 0, y: 8, duration: D(0.3), onComplete: () => el.remove() }), err ? 6000 : 3200);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const body = res.headers.get('content-type')?.includes('json') ? await res.json() : await res.text();
    if (!res.ok) throw new Error((body && body.error) || res.statusText);
    return body;
  }

  // ------------------------------------------------------------------ lista de ejecuciones
  function filteredRuns() {
    const all = [...state.runs.values()].sort((a, b) => (b.started || '').localeCompare(a.started || ''));
    if (state.filter === 'active') return all.filter((r) => ACTIVE.has(r.status));
    if (state.filter === 'error') return all.filter((r) => ['error', 'interrupted', 'cancelled'].includes(r.status));
    return all;
  }

  function listItem(r) {
    let el = state.listEls.get(r.id);
    const fresh = !el;
    if (fresh) {
      el = document.createElement('div');
      el.className = 'run-item';
      el.innerHTML = '<span class="dot"></span><div class="r-title"></div><span class="r-pct"></span><div class="r-meta"></div><div class="r-bar"><i></i></div>';
      el.addEventListener('click', () => selectRun(r.id, true));
      state.listEls.set(r.id, el);
    }
    const prevPct = Number(el.dataset.pct || 0);
    el.dataset.status = r.status;
    el.classList.toggle('selected', r.id === state.selected);
    const title = $('.r-title', el);
    title.innerHTML = esc(r.title || r.command) + (r.group ? '<span class="r-group">auto</span>' : '');
    $('.r-pct', el).textContent = ACTIVE.has(r.status) ? `${Math.round(r.pct || 0)}%` : (STATUS[r.status] || r.status);
    const bits = [r.client, r.args && r.args.month].filter(Boolean);
    if (ACTIVE.has(r.status)) bits.push(r.current || 'Iniciando…');
    else bits.push(ago(r.finished || r.started));
    $('.r-meta', el).textContent = bits.join(' · ');
    const pct = r.status === 'done' ? 100 : (r.pct || 0);
    const bar = $('.r-bar i', el);
    if (fresh) gsap.set(bar, { width: `${pct}%` });
    else if (pct !== prevPct) gsap.to(bar, { width: `${pct}%`, duration: D(0.6), ease: 'power2.out' });
    el.dataset.pct = pct;
    if (ACTIVE.has(r.status) && !el._pulse && !REDUCED) {
      el._pulse = gsap.to($('.dot', el), { scale: 1.35, repeat: -1, yoyo: true, duration: 0.7, ease: 'sine.inOut' });
    } else if (!ACTIVE.has(r.status) && el._pulse) {
      el._pulse.kill(); el._pulse = null; gsap.set($('.dot', el), { scale: 1 });
    }
    return { el, fresh };
  }

  function renderList() {
    const box = $('#run-list');
    const runs = filteredRuns();
    const frag = document.createDocumentFragment();
    const freshEls = [];
    let day = null;
    for (const r of runs) {
      const label = dayLabel(r.started);
      if (label !== day) {
        day = label;
        const h = document.createElement('div');
        h.className = 'run-day';
        h.textContent = label;
        frag.appendChild(h);
      }
      const { el, fresh } = listItem(r);
      if (fresh) freshEls.push(el);
      frag.appendChild(el);
    }
    box.replaceChildren(frag);
    if (!runs.length) {
      box.innerHTML = `<div class="d-empty">${state.filter === 'all' ? 'Aún no hay ejecuciones.' : 'Nada por aquí.'}</div>`;
    }
    if (freshEls.length && freshEls.length < 6) {
      gsap.from(freshEls, { x: -18, opacity: 0, duration: D(0.45), stagger: 0.05, ease: 'power3.out' });
    }
  }

  function flashListItem(id) {
    const el = state.listEls.get(id);
    if (!el || REDUCED) return;
    gsap.fromTo(el, { backgroundColor: 'rgba(124,108,255,.18)' }, { backgroundColor: 'rgba(124,108,255,0)', duration: 0.9, clearProps: 'backgroundColor' });
  }

  // ------------------------------------------------------------------ cabecera
  function buildHead(run) {
    const head = $('#run-head');
    const segs = run.steps.map((s) => `<div class="seg" data-key="${esc(s.key)}" style="flex-grow:${s.weight || 1}" title="${esc(s.label)}"><i></i></div>`).join('');
    head.innerHTML = `
      <div class="head-row">
        <div class="head-main">
          <div class="head-kicker">${ICONS.cmd(run.command).replace('<svg', '<svg width="14" height="14" style="fill:none;stroke:currentColor;stroke-width:2"')}<code class="h-cmd"></code></div>
          <h1 class="head-title"><span class="h-title"></span><span class="h-tags"></span><span class="status-badge h-status"></span></h1>
        </div>
        <div class="head-stats">
          <div class="head-side">
            <span class="clock h-clock">00:00</span>
            <span class="h-started"></span>
            <span class="h-actions"></span>
          </div>
          <div class="big-pct"><span class="h-pct">0</span><small>%</small></div>
        </div>
      </div>
      <div class="segbar">${segs}</div>
      <div class="head-error h-error" hidden></div>
      <div class="head-summary h-summary" hidden></div>`;
    $$('.seg', head).forEach((seg) => seg.addEventListener('click', () => pickStep(seg.dataset.key)));
    head._pct = { v: 0 };
    gsap.from($$('.head-main > *, .head-stats', head), { y: 10, opacity: 0, duration: D(0.45), stagger: 0.06, ease: 'power3.out' });
    gsap.from($$('.seg', head), { scaleX: 0, transformOrigin: 'left center', duration: D(0.5), stagger: 0.025, ease: 'power2.out' });
  }

  function updateHead(run) {
    const head = $('#run-head');
    $('.h-cmd', head).textContent = cmdLine(run);
    $('.h-title', head).textContent = run.title || run.command;
    const tags = [run.client, run.args && run.args.month, run.args && run.args.dry_run ? 'simulación' : null,
      run.args && run.args.llm === 'fake' ? 'LLM falso' : null, run.group ? 'serve' : null].filter(Boolean);
    $('.h-tags', head).innerHTML = tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(' ');
    const badge = $('.h-status', head);
    badge.dataset.status = run.status;
    badge.textContent = STATUS[run.status] || run.status;
    $('.h-started', head).textContent = `Inició ${hhmm(run.started)}` + (run.finished ? ` · terminó ${hhmm(run.finished)}` : '');
    const actions = $('.h-actions', head);
    const canCancel = run.cancellable && ACTIVE.has(run.status);
    if (canCancel && !actions.firstChild) {
      actions.innerHTML = '<button class="btn small danger">Cancelar</button>';
      actions.firstChild.addEventListener('click', () => cancelRun(run.id));
    } else if (!canCancel) actions.replaceChildren();

    const pct = run.status === 'done' ? 100 : (run.pct || 0);
    gsap.to(head._pct, {
      v: pct, duration: D(0.8), ease: 'power2.out',
      onUpdate: () => { $('.h-pct', head).textContent = Math.round(head._pct.v); },
    });
    for (const s of run.steps) {
      const seg = $(`.seg[data-key="${CSS.escape(s.key)}"]`, head);
      if (!seg) continue;
      seg.dataset.status = s.status;
      const p = s.status === 'done' || s.status === 'error' ? 100 : (s.status === 'skipped' ? 0 : (stepPct(s) ?? (LIVE.has(s.status) ? 12 : 0)));
      seg.title = `${s.label} · ${STATUS[s.status] || s.status}`;
      gsap.to($('i', seg), { width: `${p}%`, duration: D(0.6), ease: 'power2.out' });
    }
    const err = $('.h-error', head);
    err.hidden = !run.error;
    err.textContent = run.error || '';
    const sum = $('.h-summary', head);
    const lines = (run.summary || []).filter(Boolean);
    sum.hidden = !lines.length || ACTIVE.has(run.status);
    sum.textContent = lines.slice(-6).join('\n');
  }

  // ------------------------------------------------------------------ grafo
  function nodeEl(s, i) {
    const el = document.createElement('div');
    el.className = 'node';
    el.dataset.key = s.key;
    el.dataset.status = 'pending';
    el.innerHTML = `
      <div class="node-glow"></div>
      <div class="node-top">
        <div class="ring">
          <svg class="ring-svg" viewBox="0 0 48 48">
            <circle class="ring-track" cx="24" cy="24" r="20"/>
            <circle class="ring-bar" cx="24" cy="24" r="20" stroke-dasharray="${RING}" stroke-dashoffset="${RING}"/>
            <circle class="ring-spin" cx="24" cy="24" r="20" stroke-dasharray="${RING * 0.22} ${RING}"/>
          </svg>
          <div class="ring-icon">${ICONS.step(s.key)}</div>
          <div class="ring-check">${ICONS.svg('check')}</div>
        </div>
        <div class="node-pct"><span class="n">—</span><small></small></div>
      </div>
      <div class="node-label"></div>
      <div class="node-sub"></div>
      <div class="node-index">${String(i + 1).padStart(2, '0')}</div>`;
    $('.node-label', el).textContent = s.label;
    el.addEventListener('click', () => pickStep(s.key));
    return el;
  }

  function buildGraph(run) {
    const graph = $('#graph');
    for (const n of state.nodes.values()) killNodeAnims(n);
    state.edges.forEach((e) => e.flowTween && e.flowTween.kill());
    graph.replaceChildren();
    const edges = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    edges.setAttribute('class', 'edges');
    graph.appendChild(edges);
    state.nodes.clear();
    state.edges = [];
    run.steps.forEach((s, i) => {
      const el = nodeEl(s, i);
      graph.appendChild(el);
      state.nodes.set(s.key, { el, status: 'pending', pct: { v: 0 }, index: i });
    });
    for (let i = 0; i < run.steps.length - 1; i++) {
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.innerHTML = '<path class="edge-base"/><path class="edge-fill"/><path class="edge-flow"/>';
      edges.appendChild(g);
      const [base, fill, flow] = g.children;
      const flowTween = REDUCED ? null : gsap.to(flow, { strokeDashoffset: -28, duration: 0.7, ease: 'none', repeat: -1, paused: true });
      state.edges.push({ from: run.steps[i].key, to: run.steps[i + 1].key, base, fill, flow, flowTween, len: 0, filled: false, on: false });
    }
    state.graphFor = run.id + '|' + run.steps.map((s) => s.key).join(',');
    layout();
    run.steps.forEach((s) => applyStep(s, true));
    updateEdges(run, true);
    const els = [...state.nodes.values()].map((n) => n.el);
    gsap.from(els, { y: 22, opacity: 0, scale: 0.94, duration: D(0.55), stagger: D(0.045), ease: 'back.out(1.4)' });
    for (const e of state.edges) {
      gsap.fromTo(e.base, { strokeDasharray: e.len, strokeDashoffset: e.len },
        { strokeDashoffset: 0, duration: D(0.5), delay: D(0.15 + 0.045 * state.edges.indexOf(e)), ease: 'power2.inOut',
          onComplete: () => e.base.removeAttribute('style') });
    }
  }

  function layout() {
    if (!state.run) return;
    const wrap = $('.graph-wrap');
    const graph = $('#graph');
    const n = state.run.steps.length;
    const style = getComputedStyle(wrap);
    const avail = wrap.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const gapX = avail < 520 ? 24 : GAP_X;
    const cols = Math.max(1, Math.min(n, Math.floor((avail + gapX) / (NODE_W + gapX))));
    const rows = Math.ceil(n / cols);
    const width = cols * NODE_W + (cols - 1) * gapX;
    graph.style.width = `${width}px`;
    graph.style.height = `${rows * NODE_H + (rows - 1) * GAP_Y}px`;
    const pos = [];
    state.run.steps.forEach((s, i) => {
      const row = Math.floor(i / cols);
      let c = i % cols;
      if (row % 2) c = cols - 1 - c; // serpentina: las filas impares van de derecha a izquierda
      const x = c * (NODE_W + gapX), y = row * (NODE_H + GAP_Y);
      pos.push({ x, y, row });
      const node = state.nodes.get(s.key);
      if (node) { node.el.style.left = `${x}px`; node.el.style.top = `${y}px`; }
    });
    state.edges.forEach((e, i) => {
      const a = pos[i], b = pos[i + 1];
      let d;
      if (a.row === b.row) {
        const ltr = b.x > a.x;
        const y = a.y + NODE_H / 2;
        d = ltr ? `M${a.x + NODE_W} ${y} L${b.x} ${y}` : `M${a.x} ${y} L${b.x + NODE_W} ${y}`;
      } else {
        const x = a.x + NODE_W / 2;
        d = `M${x} ${a.y + NODE_H} L${x} ${b.y}`;
      }
      for (const p of [e.base, e.fill, e.flow]) p.setAttribute('d', d);
      e.len = e.base.getTotalLength();
      e.fill.setAttribute('stroke-dasharray', e.len);
      gsap.set(e.fill, { strokeDashoffset: e.filled ? 0 : e.len });
    });
  }

  function killNodeAnims(n) {
    if (n.pulse) { n.pulse.kill(); n.pulse = null; }
    if (n.spin) { n.spin.kill(); n.spin = null; }
  }

  function applyStep(s, instant = false) {
    const n = state.nodes.get(s.key);
    if (!n) return;
    const el = n.el;
    const prev = n.status;
    const changed = prev !== s.status;
    n.status = s.status;
    el.dataset.status = s.status;
    $('.node-sub', el).textContent = subText(s);
    el.title = `${s.label} — ${subText(s)}`;

    // Anillo y porcentaje
    const pct = stepPct(s);
    const bar = $('.ring-bar', el), spin = $('.ring-spin', el);
    const indeterminate = LIVE.has(s.status) && (pct === null || !s.total);
    const target = s.status === 'error' ? 0 : RING * (1 - (pct ?? 0) / 100);
    const dur = instant ? 0 : D(0.7);
    gsap.to(bar, { strokeDashoffset: target, duration: dur, ease: 'power2.out' });
    const num = $('.node-pct .n', el), unit = $('.node-pct small', el);
    if (s.status === 'error') { num.textContent = '!'; unit.textContent = ''; }
    else if (pct === null) { num.textContent = s.status === 'skipped' || s.status === 'pending' ? '—' : '…'; unit.textContent = ''; }
    else {
      unit.textContent = '%';
      gsap.to(n.pct, { v: pct, duration: dur, ease: 'power2.out', onUpdate: () => { num.textContent = Math.round(n.pct.v); } });
    }
    if (indeterminate && !n.spin) {
      gsap.to(spin, { opacity: 1, duration: D(0.3) });
      n.spin = REDUCED ? null : gsap.to(spin, { rotation: 360, svgOrigin: '24 24', duration: 1.1, ease: 'none', repeat: -1 });
    } else if (!indeterminate && (n.spin || Number(gsap.getProperty(spin, 'opacity')) > 0)) {
      if (n.spin) { n.spin.kill(); n.spin = null; }
      gsap.to(spin, { opacity: 0, duration: D(0.3) });
    }

    if (!changed && !instant) return;
    const glow = $('.node-glow', el), icon = $('.ring-icon', el), check = $('.ring-check', el);

    // Brillo de los pasos vivos
    if (LIVE.has(s.status)) {
      if (n.pulse) n.pulse.kill();
      gsap.set(glow, { opacity: 0.35 });
      n.pulse = REDUCED ? null : gsap.to(glow, {
        opacity: 1, duration: s.status === 'waiting' ? 1.5 : 0.85, ease: 'sine.inOut', repeat: -1, yoyo: true,
      });
      if (REDUCED) gsap.set(glow, { opacity: 1 });
    } else {
      if (n.pulse) { n.pulse.kill(); n.pulse = null; }
      gsap.to(glow, { opacity: 0, duration: dur });
    }

    // Icono o marca de terminado / error
    if (s.status === 'done' || s.status === 'error') {
      check.innerHTML = ICONS.svg(s.status === 'done' ? 'check' : 'x');
      if (instant) {
        gsap.set(icon, { opacity: 0, scale: 0.6 });
        gsap.set(check, { opacity: 1, scale: 1, rotation: 0 });
      } else {
        gsap.to(icon, { opacity: 0, scale: 0.5, duration: D(0.2) });
        gsap.fromTo(check, { opacity: 0, scale: 0.2, rotation: -40 },
          { opacity: 1, scale: 1, rotation: 0, duration: D(0.6), ease: 'back.out(2.6)', delay: D(0.1) });
      }
    } else {
      gsap.to(check, { opacity: 0, scale: 0.6, duration: dur });
      gsap.to(icon, { opacity: 1, scale: 1, duration: dur });
    }

    gsap.to(el, { opacity: s.status === 'skipped' ? 0.55 : 1, duration: dur });

    if (instant) return;
    if (s.status === 'done') {
      gsap.fromTo(el, { scale: 1 }, { scale: 1.045, duration: D(0.16), yoyo: true, repeat: 1, ease: 'power2.out' });
      burst(el, 'var(--done)');
    } else if (s.status === 'error') {
      gsap.fromTo(el, { x: -9 }, { x: 0, duration: D(0.8), ease: 'elastic.out(1.2, 0.22)' });
      burst(el, 'var(--error)');
    } else if (s.status === 'running' && prev === 'pending') {
      gsap.fromTo(el, { y: 0 }, { y: -6, duration: D(0.18), yoyo: true, repeat: 1, ease: 'power2.out' });
    }
  }

  function burst(el, color) {
    if (REDUCED) return;
    const b = document.createElement('div');
    Object.assign(b.style, { position: 'absolute', inset: '-2px', borderRadius: '16px', border: `2px solid ${color}`, pointerEvents: 'none' });
    el.appendChild(b);
    gsap.fromTo(b, { opacity: 0.9, scale: 1 }, { opacity: 0, scale: 1.12, duration: 0.7, ease: 'power2.out', onComplete: () => b.remove() });
  }

  function updateEdges(run, instant = false) {
    const by = Object.fromEntries(run.steps.map((s) => [s.key, s]));
    for (const e of state.edges) {
      const a = by[e.from], b = by[e.to];
      if (!a || !b) continue;
      const filled = FINISHED.has(a.status);
      e.fill.classList.toggle('skipped', a.status === 'skipped');
      e.fill.classList.toggle('error', a.status === 'error');
      if (filled !== e.filled) {
        e.filled = filled;
        gsap.to(e.fill, { strokeDashoffset: filled ? 0 : e.len, duration: instant ? 0 : D(0.6), ease: 'power2.inOut' });
      }
      const on = LIVE.has(b.status);
      e.flow.classList.toggle('waiting', b.status === 'waiting');
      if (on !== e.on) {
        e.on = on;
        gsap.to(e.flow, { opacity: on ? 1 : 0, duration: D(0.3) });
        if (e.flowTween) on ? e.flowTween.play() : e.flowTween.pause();
      }
    }
  }

  // ------------------------------------------------------------------ selección
  function liveStep(run) {
    return run.steps.find((s) => LIVE.has(s.status))
      || (run.status === 'error' || run.status === 'interrupted' || run.status === 'cancelled'
        ? run.steps.find((s) => s.status === 'error') : null);
  }

  function pickStep(key) {
    state.stepKey = key;
    state.follow = false;
    $('#follow').checked = false;
    setTab('step');
    markSelectedNode();
    renderDetail(true);
  }

  function markSelectedNode() {
    for (const [k, n] of state.nodes) n.el.classList.toggle('selected', k === state.stepKey);
  }

  async function selectRun(id, user = false) {
    if (!id) return;
    state.selected = id;
    if (user) { state.follow = true; $('#follow').checked = true; }
    try { history.replaceState(null, '', `#${encodeURIComponent(id)}`); } catch { /* file:// */ }
    for (const [rid, el] of state.listEls) el.classList.toggle('selected', rid === id);
    try {
      const run = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (state.selected === id) applyRun(run, true);
    } catch (e) {
      toast(`No se pudo abrir la ejecución: ${e.message}`, true);
    }
  }

  function applyRun(run, fromSelect = false) {
    const prev = state.runs.get(run.id);
    state.runs.set(run.id, summarize(run));
    if (prev && prev.status !== run.status) flashListItem(run.id);
    listItem(state.runs.get(run.id));
    if (!prev) renderList();
    if (run.id !== state.selected) return;

    const sig = run.id + '|' + run.steps.map((s) => s.key).join(',');
    const rebuilt = state.graphFor !== sig;
    state.run = run;
    if (rebuilt) {
      buildHead(run);
      buildGraph(run);
      state.detailFor = null;
      if (!run.steps.some((s) => s.key === state.stepKey)) state.stepKey = null;
    } else {
      run.steps.forEach((s) => applyStep(s));
      updateEdges(run);
    }
    updateHead(run);
    if (state.follow || !state.stepKey) {
      const live = liveStep(run);
      if (live) state.stepKey = live.key;
      else if (!state.stepKey) {
        const ran = run.steps.filter((s) => s.status !== 'pending' && s.status !== 'skipped');
        state.stepKey = (ran[ran.length - 1] || run.steps[0] || {}).key;
      }
    }
    markSelectedNode();
    renderDetail(rebuilt || fromSelect);
    if (state.tab === 'log') refreshLog();
  }

  // ------------------------------------------------------------------ detalle
  function setTab(tab) {
    state.tab = tab;
    $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
    state.detailFor = null;
    renderDetail(true);
    if (tab === 'log') refreshLog();
  }

  function renderDetail(force = false) {
    const body = $('#detail-body');
    const run = state.run;
    if (!run) { body.innerHTML = '<div class="d-empty">Elige una ejecución.</div>'; return; }
    if (state.tab === 'run') return renderRunTab(body, run, force);
    if (state.tab === 'log') return renderLogTab(body, run, force);
    const s = run.steps.find((x) => x.key === state.stepKey);
    if (!s) { body.innerHTML = '<div class="d-empty">Elige un paso del grafo.</div>'; return; }
    const key = `step|${run.id}|${s.key}`;
    if (force || state.detailFor !== key) {
      state.detailFor = key;
      body._counts = { items: 0, logs: 0, outputs: 0 };
      const idx = run.steps.indexOf(s) + 1;
      body.innerHTML = `
        <div class="d-head">
          <div class="d-icon">${ICONS.step(s.key)}</div>
          <div>
            <div class="d-kicker">Paso ${idx} de ${run.steps.length}</div>
            <h2 class="d-title"></h2>
          </div>
          <span class="status-badge d-status" style="margin-left:auto"></span>
        </div>
        <div class="d-progress"><span class="num d-num"></span><span class="of d-of"></span></div>
        <div class="d-bar"><i></i></div>
        <div class="d-section d-live"></div>
        <div class="d-section"><h3>Tiempos</h3><dl class="kv d-times"></dl></div>
        <div class="d-section d-metrics-wrap" hidden><h3>Métricas</h3><div class="metrics d-metrics"></div></div>
        <div class="d-section d-items-wrap" hidden><h3><span>Elementos</span><span class="d-items-count"></span></h3><ul class="items d-items"></ul></div>
        <div class="d-section d-out-wrap" hidden><h3><span>Salidas</span><span class="d-out-count"></span></h3><div class="thumbs d-thumbs"></div><div class="d-files"></div></div>
        <div class="d-section d-logs-wrap" hidden><h3>Registro</h3><div class="logs d-logs"></div></div>`;
      $('.d-title', body).textContent = s.label;
      gsap.from($$(':scope > *', body), { y: 10, opacity: 0, duration: D(0.4), stagger: 0.035, ease: 'power3.out' });
    }
    updateStepDetail(body, s);
  }

  function updateStepDetail(body, s) {
    const badge = $('.d-status', body);
    badge.dataset.status = s.status;
    badge.textContent = STATUS[s.status] || s.status;
    const pct = stepPct(s);
    $('.d-num', body).textContent = s.status === 'error' ? 'Error' : (pct === null ? (s.status === 'pending' ? '—' : (s.status === 'skipped' ? 'Omitido' : '…')) : `${Math.round(pct)}%`);
    $('.d-of', body).textContent = s.total ? `${s.done} de ${s.total}` : (s.done ? `${s.done} hechos` : '');
    const bar = $('.d-bar', body);
    bar.className = `d-bar${s.status === 'done' ? ' done' : ''}${s.status === 'error' ? ' error' : ''}`;
    const indeterminate = LIVE.has(s.status) && pct === null;
    bar.classList.toggle('indeterminate', indeterminate);
    if (indeterminate && !bar._sweep && !REDUCED) {
      bar._sweep = gsap.fromTo($('i', bar), { x: '-110%' }, { x: '290%', duration: 1.3, ease: 'power1.inOut', repeat: -1 });
    } else if (!indeterminate) {
      if (bar._sweep) { bar._sweep.kill(); bar._sweep = null; gsap.set($('i', bar), { x: 0 }); }
      gsap.to($('i', bar), { width: `${s.status === 'error' ? 100 : (pct ?? 0)}%`, duration: D(0.6), ease: 'power2.out' });
    }

    // En vivo: detalle, actividad, espera, error
    const live = $('.d-live', body);
    const parts = [];
    if (s.detail) parts.push(`<div class="callout">${esc(s.detail)}</div>`);
    if (s.activity && LIVE.has(s.status)) parts.push(`<div class="callout activity"><span class="spinner"></span><span>${esc(s.activity)}</span></div>`);
    if (s.status === 'waiting' && s.wait) parts.push(`<div class="callout wait"><span>${esc(s.wait.reason)}</span><span class="count" data-until="${esc(s.wait.until)}">${Math.ceil(waitLeft(s))} s</span></div>`);
    if (s.error) parts.push(`<div class="callout err">${esc(s.error)}</div>`);
    const html = parts.join('<div style="height:8px"></div>');
    if (live._html !== html) {
      live._html = html;
      live.innerHTML = html;
      live.hidden = !html;
      $$('.spinner', live).forEach((sp) => !REDUCED && gsap.to(sp, { rotation: 360, duration: 0.8, ease: 'none', repeat: -1 }));
    }

    const times = [['Inicio', s.started ? new Date(s.started).toLocaleTimeString('es') : '—'],
      ['Fin', s.finished ? new Date(s.finished).toLocaleTimeString('es') : (LIVE.has(s.status) ? 'en curso' : '—')],
      ['Duración', `<span class="d-dur">${s.started ? fmtDur(stepDuration(s)) : '—'}</span>`]];
    $('.d-times', body).innerHTML = times.map(([k, v]) => `<dt>${k}</dt><dd>${k === 'Duración' ? v : esc(v)}</dd>`).join('');

    const metrics = Object.entries(s.metrics || {});
    $('.d-metrics-wrap', body).hidden = !metrics.length;
    const mHtml = metrics.map(([k, v]) => `<div class="metric"><div class="m-val" title="${esc(v)}">${esc(v)}</div><div class="m-key" title="${esc(k)}">${esc(k)}</div></div>`).join('');
    const mBox = $('.d-metrics', body);
    if (mBox._html !== mHtml) {
      const grew = (mBox._n || 0) < metrics.length;
      mBox._html = mHtml; mBox.innerHTML = mHtml; mBox._n = metrics.length;
      if (grew && body._counts.metricsShown) gsap.from(mBox.lastElementChild, { scale: 0.9, opacity: 0, duration: D(0.35), ease: 'back.out(2)' });
      body._counts.metricsShown = true;
    }

    // Elementos (solo se añaden los nuevos)
    const items = s.items || [];
    $('.d-items-wrap', body).hidden = !items.length;
    $('.d-items-count', body).textContent = items.length ? `${items.length}` : '';
    const list = $('.d-items', body);
    if (items.length < body._counts.items) { list.replaceChildren(); body._counts.items = 0; }
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
    const added = [];
    for (const it of items.slice(body._counts.items)) {
      const li = document.createElement('li');
      li.dataset.s = it.status;
      const ico = it.status === 'done' ? 'check' : it.status === 'error' ? 'x' : 'minus';
      li.innerHTML = `<span class="i-ico">${ICONS.svg(ico)}</span><span class="i-label"></span>${it.note ? '<span class="i-note"></span>' : ''}`;
      $('.i-label', li).textContent = it.label;
      if (it.note) $('.i-note', li).textContent = it.note;
      list.appendChild(li);
      added.push(li);
    }
    if (added.length && body._counts.items) gsap.from(added, { x: 12, opacity: 0, duration: D(0.35), stagger: 0.04, ease: 'power2.out' });
    body._counts.items = items.length;
    if (atBottom && added.length) list.scrollTop = list.scrollHeight;

    // Salidas: miniaturas de imágenes y enlaces a archivos
    const outs = s.outputs || [];
    $('.d-out-wrap', body).hidden = !outs.length;
    $('.d-out-count', body).textContent = outs.length ? `${outs.length}` : '';
    if (outs.length < body._counts.outputs) { $('.d-thumbs', body).replaceChildren(); $('.d-files', body).replaceChildren(); body._counts.outputs = 0; }
    const newThumbs = [];
    for (const p of outs.slice(body._counts.outputs)) {
      if (isImage(p)) {
        const b = document.createElement('button');
        b.className = 'thumb';
        b.title = baseName(p);
        b.innerHTML = `<img loading="lazy" alt="">`;
        $('img', b).src = fileUrl(p);
        b.addEventListener('click', () => openLightbox(p));
        $('.d-thumbs', body).appendChild(b);
        newThumbs.push(b);
      } else {
        const a = document.createElement('a');
        a.className = 'file-link';
        a.href = fileUrl(p);
        a.target = '_blank';
        a.rel = 'noopener';
        a.innerHTML = ICONS.svg('form').replace('<svg', '<svg width="16" height="16" style="fill:none;stroke:currentColor;stroke-width:2"');
        a.append(baseName(p));
        $('.d-files', body).appendChild(a);
      }
    }
    if (newThumbs.length && body._counts.outputs) gsap.from(newThumbs, { scale: 0.8, opacity: 0, duration: D(0.4), stagger: 0.05, ease: 'back.out(1.8)' });
    body._counts.outputs = outs.length;

    // Registro del paso
    const logs = s.logs || [];
    $('.d-logs-wrap', body).hidden = !logs.length;
    const box = $('.d-logs', body);
    const html2 = logs.map((l) => `<div class="l ${esc(l.level)}"><time>${esc(hhmm(l.t))}</time>${esc(l.msg)}</div>`).join('');
    if (box._html !== html2) {
      const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
      box._html = html2; box.innerHTML = html2;
      if (stick || !box._seen) box.scrollTop = box.scrollHeight;
      box._seen = true;
    }
  }

  function renderRunTab(body, run, force) {
    const key = `run|${run.id}`;
    const first = force || state.detailFor !== key;
    state.detailFor = key;
    const total = (run.finished ? ms(run.finished) : Date.now()) - ms(run.started);
    const rows = [
      ['Comando', `<code>${esc(cmdLine(run))}</code>`],
      ['Estado', esc(STATUS[run.status] || run.status)],
      ['Inicio', esc(new Date(run.started).toLocaleString('es'))],
      ['Fin', esc(run.finished ? new Date(run.finished).toLocaleString('es') : '—')],
      ['Duración', `<span class="d-run-dur">${esc(fmtDur(total))}</span>`],
      ['Proceso', esc(run.pid || '—')],
      ['Id', `<code>${esc(run.id)}</code>`],
    ];
    if (run.group) rows.push(['Grupo', esc(run.group)]);
    const done = run.steps.filter((s) => s.status === 'done').length;
    const skipped = run.steps.filter((s) => s.status === 'skipped').length;
    const errors = run.steps.filter((s) => s.status === 'error').length;
    const llm = run.steps.reduce((a, s) => a + (Number((s.metrics || {})['Llamadas al modelo']) || 0), 0);
    const slowest = run.steps.filter((s) => s.started).map((s) => [s, stepDuration(s)]).sort((a, b) => b[1] - a[1]).slice(0, 4);
    body.innerHTML = `
      <div class="d-head"><div class="d-icon">${ICONS.cmd(run.command)}</div>
        <div><div class="d-kicker">Ejecución</div><h2 class="d-title">${esc(run.title || run.command)}</h2></div>
        <span class="status-badge" data-status="${esc(run.status)}" style="margin-left:auto">${esc(STATUS[run.status] || run.status)}</span></div>
      <div class="d-section"><div class="metrics">
        <div class="metric"><div class="m-val">${done}/${run.steps.length}</div><div class="m-key">Pasos listos</div></div>
        <div class="metric"><div class="m-val">${llm}</div><div class="m-key">Llamadas al modelo</div></div>
        <div class="metric"><div class="m-val">${skipped}</div><div class="m-key">Omitidos</div></div>
        <div class="metric"><div class="m-val" style="color:${errors ? 'var(--error)' : 'inherit'}">${errors}</div><div class="m-key">Con error</div></div>
      </div></div>
      <div class="d-section"><h3>Datos</h3><dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl></div>
      ${slowest.length ? `<div class="d-section"><h3>Pasos más lentos</h3><dl class="kv">${slowest.map(([s, t]) => `<dt>${esc(s.label)}</dt><dd>${esc(fmtDur(t))}</dd>`).join('')}</dl></div>` : ''}
      ${run.error ? `<div class="d-section"><h3>Error</h3><div class="callout err">${esc(run.error)}</div></div>` : ''}
      ${(run.summary || []).length ? `<div class="d-section"><h3>Resultado</h3><div class="logs">${run.summary.map((l) => `<div class="l">${esc(l)}</div>`).join('')}</div></div>` : ''}
      ${(run.logs || []).length ? `<div class="d-section"><h3>Registro general</h3><div class="logs">${run.logs.map((l) => `<div class="l ${esc(l.level)}"><time>${esc(hhmm(l.t))}</time>${esc(l.msg)}</div>`).join('')}</div></div>` : ''}`;
    if (first) gsap.from($$(':scope > *', body), { y: 10, opacity: 0, duration: D(0.4), stagger: 0.04, ease: 'power3.out' });
  }

  function renderLogTab(body, run, force) {
    const key = `log|${run.id}`;
    if (!force && state.detailFor === key) return;
    state.detailFor = key;
    body.innerHTML = `<div class="d-section" style="margin-top:0"><h3><span>Salida del proceso</span><span class="log-status"></span></h3><pre class="proc-log">Cargando…</pre></div>`;
  }

  async function refreshLog() {
    clearTimeout(state.logTimer);
    if (state.tab !== 'log' || !state.run) return;
    const run = state.run;
    try {
      const text = await api(`/api/runs/${encodeURIComponent(run.id)}/log`);
      const pre = $('#detail-body .proc-log');
      if (pre && state.run && state.run.id === run.id) {
        const stick = pre.parentElement.scrollTop + 40 >= pre.scrollHeight - pre.parentElement.clientHeight;
        pre.textContent = text || 'Esta ejecución no se lanzó desde el panel: su salida está en la terminal (o en el log de serve) donde corre.';
        const body = $('#detail-body');
        if (stick) body.scrollTop = body.scrollHeight;
      }
    } catch { /* se reintenta */ }
    if (ACTIVE.has(run.status)) state.logTimer = setTimeout(refreshLog, 1500);
  }

  // Reloj, cuentas regresivas y duraciones en curso
  function tick() {
    const run = state.run;
    if (!run) return;
    const head = $('#run-head');
    const c = $('.h-clock', head);
    if (c) c.textContent = clock((run.finished ? ms(run.finished) : Date.now()) - ms(run.started));
    for (const s of run.steps) {
      if (s.status === 'waiting' || s.status === 'running') {
        const n = state.nodes.get(s.key);
        if (n) $('.node-sub', n.el).textContent = subText(s);
      }
    }
    const body = $('#detail-body');
    $$('.count[data-until]', body).forEach((el) => { el.textContent = `${Math.ceil(Math.max(0, (ms(el.dataset.until) - Date.now()) / 1000))} s`; });
    const s = run.steps.find((x) => x.key === state.stepKey);
    const dur = $('.d-dur', body);
    if (s && dur && LIVE.has(s.status)) dur.textContent = fmtDur(stepDuration(s));
    const rd = $('.d-run-dur', body);
    if (rd && !run.finished) rd.textContent = fmtDur(Date.now() - ms(run.started));
  }

  // ------------------------------------------------------------------ lightbox
  function openLightbox(p) {
    const lb = $('#lightbox');
    $('#lightbox-img').src = fileUrl(p);
    $('#lightbox-cap').textContent = baseName(p);
    lb.hidden = false;
    gsap.fromTo(lb, { opacity: 0 }, { opacity: 1, duration: D(0.2) });
    gsap.fromTo('#lightbox-img', { scale: 0.92, y: 10 }, { scale: 1, y: 0, duration: D(0.35), ease: 'power3.out' });
  }
  $('#lightbox').addEventListener('click', () => {
    gsap.to('#lightbox', { opacity: 0, duration: D(0.15), onComplete: () => { $('#lightbox').hidden = true; } });
  });

  // ------------------------------------------------------------------ lanzar
  function nextMonth() {
    const d = new Date(); d.setMonth(d.getMonth() + 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }

  function buildLauncher() {
    const grid = $('#cmd-grid');
    grid.innerHTML = Object.keys(state.meta.launch).map((c) => {
      const [label, desc] = CMD_INFO[c] || [c, ''];
      return `<button type="button" class="cmd" data-cmd="${esc(c)}">${ICONS.cmd(c)}<b>${esc(label)}</b><span>${esc(desc)}</span></button>`;
    }).join('');
    $$('.cmd', grid).forEach((b) => b.addEventListener('click', () => { state.launchCmd = b.dataset.cmd; renderFields(true); }));
    const saved = store.get('launch', {});
    $('#fake-llm').checked = !!saved.fake;
    $('#fake-delay').value = saved.delay ?? 1.2;
    $('#delay-out').textContent = `${Number($('#fake-delay').value).toFixed(1)} s`;
    $('#fake-delay').addEventListener('input', () => { $('#delay-out').textContent = `${Number($('#fake-delay').value).toFixed(1)} s`; updatePreview(); });
    $('#fake-llm').addEventListener('change', updatePreview);
  }

  function renderFields(animate) {
    $$('.cmd').forEach((b) => b.classList.toggle('active', b.dataset.cmd === state.launchCmd));
    const spec = state.meta.launch[state.launchCmd];
    const saved = store.get('launch', {});
    const box = $('#fields');
    const html = [];
    for (const [name, kind] of Object.entries(spec)) {
      const [label, hint] = ARGS[name] || [name];
      if (kind === 'client') {
        const opts = state.meta.clients.map((c) => `<option ${c === saved.client ? 'selected' : ''}>${esc(c)}</option>`).join('');
        html.push(`<label class="field"><span>${esc(label)}</span><select name="${name}">${opts}</select></label>`);
      } else if (kind === 'month') {
        html.push(`<label class="field"><span>${esc(label)}</span><input type="month" name="${name}" value="${esc(saved.month || nextMonth())}" required></label>`);
      } else if (kind === 'int') {
        html.push(`<label class="field"><span>${esc(label)}</span><input type="number" name="${name}" min="1" max="100" placeholder="Opcional"></label>`);
      } else if (kind === 'file') {
        html.push(`<label class="field wide"><span>${esc(label)}</span><input type="text" name="${name}" placeholder="C:\\ruta\\a\\foto.jpg" required></label>`);
      } else if (kind === 'flag') {
        const on = name === 'dry_run' ? saved.dry_run !== false : false;
        html.push(`<label class="check"><input type="checkbox" name="${name}" ${on ? 'checked' : ''}><span>${esc(label)}${hint ? `<small>${esc(hint)}</small>` : ''}</span></label>`);
      }
    }
    if (!html.length) html.push('<p class="d-kicker" style="grid-column:1/-1;margin:0">Este comando no tiene opciones.</p>');
    box.innerHTML = html.join('');
    $$('input, select', box).forEach((i) => i.addEventListener('input', updatePreview));
    $$('input, select', box).forEach((i) => i.addEventListener('change', updatePreview));
    $('#launch-error').hidden = true;
    updatePreview();
    if (animate) gsap.from(box.children, { y: 8, opacity: 0, duration: D(0.3), stagger: 0.03, ease: 'power2.out' });
  }

  function formArgs() {
    const args = {};
    for (const el of $$('#fields [name]')) {
      if (el.type === 'checkbox') { if (el.checked) args[el.name] = true; }
      else if (el.value.trim()) args[el.name] = el.type === 'number' ? Number(el.value) : el.value.trim();
    }
    return args;
  }

  function updatePreview() {
    const args = formArgs();
    const fake = $('#fake-llm').checked;
    $('#delay-wrap').style.opacity = fake ? 1 : 0.4;
    $('#cmd-preview').textContent = cmdLine({ command: state.launchCmd, args: { ...args, ...(fake ? { llm: 'fake' } : {}) } });
    const warn = $('#launch-warn');
    let msg = '';
    if (['plan', 'rediseñar'].includes(state.launchCmd) && !args.dry_run) msg = 'Sin simulación: sube las imágenes a Postiz y crea filas en Notion.';
    else if (state.launchCmd === 'publish') msg = 'Programa de verdad en Postiz lo que esté aprobado en Notion.';
    if (!fake && state.meta.llm !== 'fake' && state.launchCmd !== 'publish') msg += (msg ? ' ' : '') + 'Usa el modelo real (gasta cuota).';
    warn.hidden = !msg;
    warn.textContent = msg;
  }

  function openModal() {
    const modal = $('#modal');
    modal.hidden = false;
    renderFields(false);
    gsap.fromTo('.modal-backdrop', { opacity: 0 }, { opacity: 1, duration: D(0.25) });
    gsap.fromTo('.modal-card', { y: 24, scale: 0.97, opacity: 0 }, { y: 0, scale: 1, opacity: 1, duration: D(0.4), ease: 'power3.out' });
    gsap.from('.cmd', { y: 10, opacity: 0, duration: D(0.35), stagger: 0.03, delay: D(0.08), ease: 'power2.out' });
  }
  function closeModal() {
    gsap.to('.modal-card', { y: 16, opacity: 0, duration: D(0.2) });
    gsap.to('.modal-backdrop', { opacity: 0, duration: D(0.2), onComplete: () => { $('#modal').hidden = true; } });
  }

  async function launch(ev) {
    ev.preventDefault();
    const args = formArgs();
    const fake = $('#fake-llm').checked;
    const delay = Number($('#fake-delay').value);
    store.set('launch', { ...store.get('launch', {}), fake, delay, client: args.client, month: args.month,
      dry_run: 'dry_run' in state.meta.launch[state.launchCmd] ? !!args.dry_run : store.get('launch', {}).dry_run });
    const btn = $('#launch-btn');
    btn.disabled = true;
    try {
      const { id } = await api('/api/runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: state.launchCmd, args, fake_llm: fake, fake_delay: delay }),
      });
      closeModal();
      toast('Ejecución lanzada');
      state.filter = 'all';
      $$('.chip').forEach((c) => c.classList.toggle('active', c.dataset.filter === 'all'));
      await selectRun(id, true);
    } catch (e) {
      const err = $('#launch-error');
      err.hidden = false;
      err.textContent = e.message;
      gsap.fromTo(err, { x: -6 }, { x: 0, duration: D(0.5), ease: 'elastic.out(1, 0.3)' });
    } finally {
      btn.disabled = false;
    }
  }

  async function cancelRun(id) {
    if (!confirm('¿Cancelar esta ejecución? Lo que ya hizo se queda; el paso en curso se corta.')) return;
    try {
      await api(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      toast('Ejecución cancelada');
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------------ tiempo real
  function connect() {
    const conn = $('#conn');
    const es = new EventSource('/api/events');
    es.addEventListener('open', async () => {
      conn.className = 'conn ok';
      $('.conn-text', conn).textContent = 'En vivo';
      if (state.wasDown) { state.wasDown = false; await loadRuns(); if (state.selected) selectRun(state.selected); }
    });
    es.addEventListener('error', () => {
      conn.className = 'conn down';
      $('.conn-text', conn).textContent = 'Sin conexión con el panel: reintentando…';
      state.wasDown = true;
    });
    es.addEventListener('run', (ev) => {
      const run = JSON.parse(ev.data);
      const isNew = !state.runs.has(run.id);
      applyRun(run);
      if (isNew && !state.selected) selectRun(run.id);
    });
    es.addEventListener('remove', (ev) => {
      const { id } = JSON.parse(ev.data);
      state.runs.delete(id);
      state.listEls.delete(id);
      renderList();
    });
  }

  async function loadRuns() {
    const list = await api('/api/runs');
    state.runs = new Map(list.map((r) => [r.id, r]));
    renderList();
    return list;
  }

  // ------------------------------------------------------------------ tema
  function initTheme() {
    const saved = store.get('theme', null);
    if (saved) document.documentElement.dataset.theme = saved;
    $('#theme-toggle').addEventListener('click', () => {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === 'dark'
        : matchMedia('(prefers-color-scheme: dark)').matches;
      const next = dark ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      store.set('theme', next);
    });
  }

  // ------------------------------------------------------------------ espacio en pantalla
  function initSidebar() {
    const app = $('.app');
    const set = (hidden) => {
      app.classList.toggle('side-hidden', hidden);
      store.set('sideHidden', hidden);
      requestAnimationFrame(layout);
    };
    set(!!store.get('sideHidden', false));
    $('#side-hide').addEventListener('click', () => set(true));
    $('#side-show').addEventListener('click', () => set(false));
  }

  // Si la ventana del navegador es más grande que la pantalla, los bordes del panel quedan fuera de la
  // vista aunque el diseño quepa: se avisa en vez de dejar el panel cortado sin explicación.
  function checkScreen() {
    const s = screen;
    const left = s.availLeft ?? 0, top = s.availTop ?? 0;
    const off = window.screenX + window.outerWidth > left + s.availWidth + 16
      || window.screenY + window.outerHeight > top + s.availHeight + 16
      || window.screenX < left - 16;
    let el = $('#screen-warn');
    if (off && !el) {
      el = document.createElement('div');
      el.id = 'screen-warn';
      el.className = 'screen-warn';
      el.textContent = 'La ventana del navegador se sale de la pantalla y corta el panel: maximízala (Win + ↑) o haz doble clic en su barra de título.';
      document.body.appendChild(el);
      gsap.from(el, { y: -12, opacity: 0, duration: D(0.35), ease: 'power3.out' });
    } else if (!off && el) el.remove();
  }

  function sendDiag() {
    const rect = (s) => { const el = document.querySelector(s); if (!el) return null; const b = el.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map(Math.round); };
    const vv = window.visualViewport || {};
    const data = {
      innerWidth, innerHeight, outerWidth, outerHeight, dpr: devicePixelRatio,
      screen: [screen.width, screen.height, screen.availWidth, screen.availHeight],
      vv: [vv.width, vv.height, vv.scale, vv.offsetLeft, vv.offsetTop, vv.pageLeft, vv.pageTop],
      docEl: [document.documentElement.clientWidth, document.documentElement.clientHeight, document.documentElement.scrollWidth, document.documentElement.scrollHeight],
      cols: getComputedStyle($('.app')).gridTemplateColumns, rows: getComputedStyle($('.app')).gridTemplateRows,
      app: rect('.app'), side: rect('.sidebar'), main: rect('.main'), detail: rect('.detail'), detailBody: rect('#detail-body'),
      graphWrap: rect('.graph-wrap'), graph: rect('#graph'), fontSize: getComputedStyle(document.body).fontSize,
      ua: navigator.userAgent,
    };
    fetch('/api/diag', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).catch(() => {});
  }

  // ------------------------------------------------------------------ arranque
  async function init() {
    setTimeout(sendDiag, 1500);
    window.addEventListener('resize', () => { clearTimeout(state.diagT); state.diagT = setTimeout(sendDiag, 800); });
    initTheme();
    initSidebar();
    checkScreen();
    window.addEventListener('resize', checkScreen);
    setInterval(checkScreen, 2000); // mover la ventana no dispara resize
    $('#new-run').addEventListener('click', openModal);
    $$('[data-close]').forEach((b) => b.addEventListener('click', closeModal));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if (!$('#lightbox').hidden) $('#lightbox').click();
        else if (!$('#modal').hidden) closeModal();
      }
    });
    $('#launch-form').addEventListener('submit', launch);
    $$('.chip').forEach((c) => c.addEventListener('click', () => {
      state.filter = c.dataset.filter;
      $$('.chip').forEach((x) => x.classList.toggle('active', x === c));
      renderList();
    }));
    $$('.tab').forEach((t) => t.addEventListener('click', () => setTab(t.dataset.tab)));
    $('#follow').addEventListener('change', (e) => {
      state.follow = e.target.checked;
      if (state.follow && state.run) applyRun(state.run);
    });
    new ResizeObserver(() => layout()).observe($('.graph-wrap'));
    setInterval(tick, 500);
    setInterval(() => { for (const r of state.runs.values()) if (!ACTIVE.has(r.status)) listItem(r); }, 30000);

    try {
      state.meta = await api('/api/meta');
      buildLauncher();
      const list = await loadRuns();
      const fromHash = decodeURIComponent(location.hash.slice(1));
      const pick = (fromHash && state.runs.has(fromHash) && fromHash)
        || (list.find((r) => ACTIVE.has(r.status)) || list[0] || {}).id;
      if (pick) await selectRun(pick);
      else renderDetail();
    } catch (e) {
      toast(`No se pudo cargar el panel: ${e.message}`, true);
    }
    connect();
  }

  init();
})();
