(() => {
  'use strict';

  const POLL_MS = 1500;
  const BUILTIN_TABS = ['All Downloads', 'Unfinished', 'Finished'];

  const state = {
    category: 'All Downloads',
    search: '',
    downloads: [],
    extraCategories: [],
  };

  const el = {
    engineDot: document.getElementById('engineDot'),
    engineLabel: document.getElementById('engineLabel'),
    addForm: document.getElementById('addForm'),
    urlInput: document.getElementById('urlInput'),
    addNowBtn: document.getElementById('addNowBtn'),
    addQueueBtn: document.getElementById('addQueueBtn'),
    scheduleBtn: document.getElementById('scheduleBtn'),
    moreOptionsToggle: document.getElementById('moreOptionsToggle'),
    moreOptions: document.getElementById('moreOptions'),
    pathInput: document.getElementById('pathInput'),
    scheduleInput: document.getElementById('scheduleInput'),
    addError: document.getElementById('addError'),
    categoryTabs: document.getElementById('categoryTabs'),
    searchInput: document.getElementById('searchInput'),
    pauseAllBtn: document.getElementById('pauseAllBtn'),
    resumeAllBtn: document.getElementById('resumeAllBtn'),
    clearCompletedBtn: document.getElementById('clearCompletedBtn'),
    downloadList: document.getElementById('downloadList'),
    emptyState: document.getElementById('emptyState'),
    toast: document.getElementById('toast'),
  };

  async function api(path, options = {}) {
    const response = await fetch(new URL(path, location.origin), {
      method: options.method || 'GET',
      headers: {
        'X-UDM-Web': '1',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      credentials: 'same-origin',
      cache: 'no-store',
    });

    let data;
    try {
      data = await response.json();
    } catch {
      data = { ok: false, error: `Unexpected response (${response.status})` };
    }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  }

  const ICONS = {
    pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
    resume: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7V5z"/></svg>',
    remove: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2m2 0-1 13a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L6 7"/></svg>',
  };

  let toastTimer = null;
  function showToast(message, isError = false) {
    el.toast.textContent = message;
    el.toast.classList.toggle('error', isError);
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 3200);
  }

  function renderTabs() {
    const extras = state.extraCategories.filter((name) => !BUILTIN_TABS.includes(name));
    const names = [...BUILTIN_TABS, ...extras];
    el.categoryTabs.innerHTML = '';

    for (const name of names) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'tab' + (name === state.category ? ' active' : '');
      tab.textContent = name;
      tab.addEventListener('click', () => {
        state.category = name;
        renderTabs();
        refresh();
      });
      el.categoryTabs.appendChild(tab);
    }
  }

  function statusLabel(d) {
    if (d.status_label === 'downloading') {
      return d.speed_human ? `Downloading · ${d.speed_human}` : 'Downloading';
    }
    if (d.status_label === 'queued') return 'Queued';
    if (d.status_label === 'scheduled') {
      return `Scheduled${d.start_time ? ' · ' + String(d.start_time).replace('T', ' ') : ''}`;
    }
    if (d.status_label === 'paused') return 'Paused';
    if (d.status_label === 'complete') return 'Complete';
    if (d.status_label === 'error') return 'Error';
    return d.status_label || 'Unknown';
  }

  function appendMeta(meta, text, className = '') {
    if (!text) return;
    const span = document.createElement('span');
    if (className) span.className = className;
    span.textContent = text;
    meta.appendChild(span);
  }

  function renderList() {
    el.downloadList.innerHTML = '';
    el.emptyState.hidden = state.downloads.length !== 0;

    for (const d of state.downloads) {
      const li = document.createElement('li');
      li.className = 'download-row';

      const main = document.createElement('div');
      main.className = 'dl-main';

      const name = document.createElement('div');
      name.className = 'dl-name';
      name.title = d.file_name || '';
      name.textContent = d.file_name || '';
      main.appendChild(name);

      const meta = document.createElement('div');
      meta.className = 'dl-meta';
      appendMeta(meta, d.category || 'General', 'category');
      const sizePart = d.size_human
        ? `${d.completed_human} / ${d.size_human}`
        : d.completed_human;
      appendMeta(meta, sizePart);
      appendMeta(meta, d.eta_human ? `ETA ${d.eta_human}` : '');
      appendMeta(meta, d.error_message || '', 'error');
      main.appendChild(meta);

      const track = document.createElement('div');
      track.className = 'progress-track';
      const fill = document.createElement('div');
      fill.className = 'progress-fill' + (d.status === 'complete' ? ' complete' : '');
      fill.style.width = `${Number(d.progress_percent) || 0}%`;
      track.appendChild(fill);
      main.appendChild(track);

      const badge = document.createElement('span');
      badge.className = `status-badge ${d.status_label || ''}`;
      badge.textContent = statusLabel(d);

      const actions = document.createElement('div');
      actions.className = 'dl-actions';

      if (!d.gid && (d.status === 'waiting' || d.status === 'scheduled')) {
        actions.appendChild(actionButton(
          ICONS.resume,
          'Start now',
          () => runAction(() => api(`/api/downloads/${d.id}/start`, { method: 'POST' })),
        ));
      } else if (d.status === 'paused') {
        actions.appendChild(actionButton(
          ICONS.resume,
          'Resume',
          () => runAction(() => api(`/api/downloads/${d.id}/resume`, { method: 'POST' })),
        ));
      } else if (d.gid && (d.status === 'active' || d.status === 'waiting')) {
        actions.appendChild(actionButton(
          ICONS.pause,
          'Pause',
          () => runAction(() => api(`/api/downloads/${d.id}/pause`, { method: 'POST' })),
        ));
      }

      const removeBtn = actionButton(
        ICONS.remove,
        'Remove',
        () => runAction(() => api(`/api/downloads/${d.id}/remove`, { method: 'POST' })),
      );
      removeBtn.classList.add('remove');
      actions.appendChild(removeBtn);

      li.appendChild(main);
      const right = document.createElement('div');
      right.style.display = 'flex';
      right.style.flexDirection = 'column';
      right.style.alignItems = 'flex-end';
      right.style.gap = '8px';
      right.appendChild(badge);
      right.appendChild(actions);
      li.appendChild(right);
      el.downloadList.appendChild(li);
    }
  }

  function actionButton(svg, label, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'icon-btn';
    btn.title = label;
    btn.setAttribute('aria-label', label);
    btn.innerHTML = svg;
    btn.addEventListener('click', onClick);
    return btn;
  }

  async function runAction(fn) {
    try {
      await fn();
      await refresh();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function pollEngine() {
    try {
      const data = await api('/api/status');
      el.engineDot.className = 'dot ' + (data.aria2_running ? 'on' : 'off');
      el.engineLabel.textContent = data.aria2_running ? 'Engine online' : 'Engine offline';
    } catch {
      el.engineDot.className = 'dot off';
      el.engineLabel.textContent = 'Unreachable';
    }
  }

  async function refresh() {
    try {
      const query = new URLSearchParams({
        category: state.category,
        search: state.search,
      });
      const data = await api(`/api/downloads?${query.toString()}`);
      state.downloads = Array.isArray(data.downloads) ? data.downloads : [];
      state.extraCategories = Array.isArray(data.categories)
        ? data.categories.map((c) => c.name)
        : [];
      renderTabs();
      renderList();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function setAddError(message) {
    el.addError.hidden = !message;
    el.addError.textContent = message || '';
  }

  async function addDownload(mode) {
    const url = el.urlInput.value.trim();
    setAddError('');
    if (!url) {
      setAddError('Paste a URL first.');
      return;
    }

    const payload = {
      url,
      path: el.pathInput.value.trim(),
      start: mode,
    };
    if (mode === 'schedule') {
      if (!el.scheduleInput.value) {
        setAddError('Pick a date and time to schedule this download.');
        return;
      }
      payload.at = el.scheduleInput.value;
    }

    const buttons = [el.addNowBtn, el.addQueueBtn, el.scheduleBtn];
    buttons.forEach((button) => { button.disabled = true; });
    try {
      await api('/api/downloads', { method: 'POST', body: payload });
      el.urlInput.value = '';
      el.scheduleInput.value = '';
      showToast(
        mode === 'now'
          ? 'Download started on the UDM machine'
          : mode === 'schedule'
            ? 'Download scheduled'
            : 'Added to the UDM queue',
      );
      await refresh();
    } catch (err) {
      setAddError(err.message);
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  el.addForm.addEventListener('submit', (event) => {
    event.preventDefault();
    addDownload('now');
  });
  el.addQueueBtn.addEventListener('click', () => addDownload('queue'));
  el.scheduleBtn.addEventListener('click', () => addDownload('schedule'));

  el.moreOptionsToggle.addEventListener('click', () => {
    el.moreOptions.hidden = !el.moreOptions.hidden;
    el.moreOptionsToggle.textContent = el.moreOptions.hidden
      ? 'More options'
      : 'Fewer options';
  });

  let searchTimer = null;
  el.searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = el.searchInput.value.trim();
      refresh();
    }, 300);
  });

  el.pauseAllBtn.addEventListener('click', () => runAction(
    () => api('/api/pause-all', { method: 'POST' }),
  ));
  el.resumeAllBtn.addEventListener('click', () => runAction(
    () => api('/api/resume-all', { method: 'POST' }),
  ));
  el.clearCompletedBtn.addEventListener('click', () => runAction(
    () => api('/api/clear-completed', { method: 'POST' }),
  ));

  renderTabs();
  refresh();
  pollEngine();
  setInterval(refresh, POLL_MS);
  setInterval(pollEngine, POLL_MS * 3);
})();
