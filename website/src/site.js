(() => {
  'use strict';
  const text = JSON.parse(document.getElementById('site-copy').textContent);
  const t = key => text[key];
  const friends = document.querySelector('[data-friends-src]');
  async function loadFriends() {
    if (!friends) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const result = await fetch(friends.dataset.friendsSrc, { signal: controller.signal, cache: 'no-cache', credentials: 'omit' });
      if (!result.ok) throw new Error('Fragment unavailable');
      const fragment = new DOMParser().parseFromString(await result.text(), 'text/html');
      const links = document.createElement('ul');
      links.className = 'footer-links';
      const seen = new Set();
      for (const source of fragment.querySelectorAll('.footer-friends .footer-links a[href]')) {
        let url;
        try { url = new URL(source.getAttribute('href')); } catch { continue; }
        const name = source.textContent.trim();
        if (!name || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || seen.has(url.href)) continue;
        seen.add(url.href);
        const item = document.createElement('li');
        const link = document.createElement('a');
        link.href = url.href; link.textContent = name; link.target = '_blank';
        link.rel = [...new Set(['noopener', 'noreferrer', ...source.rel.split(/\s+/).filter(token => ['nofollow', 'sponsored', 'ugc'].includes(token))])].join(' ');
        item.append(link); links.append(item);
      }
      if (!links.children.length) return;
      const title = document.createElement('h3'); title.textContent = t('footer.friends');
      friends.replaceChildren(title, links); friends.hidden = false;
    } catch {
      // A missing shared fragment must not affect the rest of the website.
      friends.hidden = true;
    } finally { clearTimeout(timeout); }
  }
  loadFriends();
  const menu = document.querySelector('.menu-toggle');
  const navigation = document.getElementById('navigation');
  function closeMenu() {
    navigation.classList.remove('open');
    menu.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-label', t('nav.menu'));
  }
  menu.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    navigation.classList.toggle('open', open);
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', t(open ? 'nav.close' : 'nav.menu'));
  });
  navigation.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') {
      closeMenu(); menu.focus();
    }
  });
  document.querySelectorAll('.language-link').forEach(link => {
    const destination = link.getAttribute('href');
    link.addEventListener('click', () => { link.href = destination + window.location.hash; });
  });

  let noticeTimer;
  document.querySelectorAll('.copy-button').forEach(button => {
    button.addEventListener('click', async () => {
      const value = document.getElementById(button.dataset.copy).textContent;
      const notice = document.getElementById('copy-notice');
      let copied = false;
      try { await navigator.clipboard.writeText(value); copied = true; } catch {
        const input = document.createElement('textarea');
        input.value = value; input.style.position = 'fixed'; input.style.opacity = '0';
        input.setAttribute('aria-hidden', 'true'); document.body.append(input);
        input.select();
        try { copied = document.execCommand('copy'); } catch { /* Keep manual fallback. */ }
        input.remove(); button.focus({ preventScroll: true });
      }
      notice.textContent = t(copied ? 'copy.success' : 'copy.failure');
      notice.hidden = false; clearTimeout(noticeTimer);
      noticeTimer = setTimeout(() => { notice.hidden = true; }, copied ? 2200 : 5500);
    });
  });

  const defaults = [
    { provider: 'openrouter', name: 'OpenRouter', model: 'model-a' },
    { provider: 'nvidia', name: 'NVIDIA', model: 'model-b' },
    { provider: 'ollama', name: 'Ollama Cloud', model: 'model-c' },
  ];
  let ranking = defaults.map(route => ({ ...route }));
  const list = document.getElementById('ranking-list');
  const message = document.getElementById('route-message');
  const response = document.getElementById('route-response');
  function resetResponse() {
    message.textContent = t('routing.idle');
    response.textContent = '{ "model": "auto", "demo": true }';
  }
  function renderRanking(focus) {
    list.replaceChildren();
    ranking.forEach((route, index) => {
      const item = document.createElement('li');
      const number = document.createElement('span');
      number.className = 'rank-number'; number.textContent = String(index + 1).padStart(2, '0');
      const identity = document.createElement('div');
      const name = document.createElement('strong'); name.textContent = route.name;
      const model = document.createElement('code'); model.textContent = route.model;
      identity.append(name, model);
      const actions = document.createElement('div'); actions.className = 'rank-actions';
      for (const [direction, offset] of [['up', -1], ['down', 1]]) {
        const button = document.createElement('button');
        button.className = 'icon-button'; button.dataset.direction = direction;
        button.setAttribute('aria-label', `${t('routing.' + direction)} ${route.name}`);
        button.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#${direction}"/></svg>`;
        button.disabled = index + offset < 0 || index + offset >= ranking.length;
        button.addEventListener('click', () => {
          const target = index + offset;
          [ranking[index], ranking[target]] = [ranking[target], ranking[index]];
          resetResponse(); renderRanking({ index: target, direction });
        });
        actions.append(button);
      }
      item.append(number, identity, actions); list.append(item);
    });
    if (!ranking.length) {
      const item = document.createElement('li'); item.className = 'empty-ranking';
      item.textContent = t('routing.empty'); list.append(item);
    }
    document.getElementById('ranking-code').textContent = ranking.length ?
      'auto_routing:\n  custom_ranking:\n' + ranking.map(route => `    - provider: ${route.provider}\n      model: ${route.model}`).join('\n') :
      'auto_routing:\n  custom_ranking: []';
    if (focus) {
      const button = list.children[focus.index]?.querySelector(`[data-direction="${focus.direction}"]`);
      const other = list.children[focus.index]?.querySelector('button:not(:disabled)');
      (button?.disabled ? other : button)?.focus({ preventScroll: true });
    }
  }
  document.getElementById('reset-ranking').addEventListener('click', () => {
    ranking = ranking.length ? [] : defaults.map(route => ({ ...route }));
    renderRanking(); resetResponse();
    document.getElementById('reset-ranking').textContent = t(ranking.length ? 'routing.reset' : 'routing.example') + ' ↺';
    if (!ranking.length) {
      message.textContent = t('routing.empty'); response.textContent = t('routing.default');
    }
  });
  document.getElementById('run-demo').addEventListener('click', () => {
    const candidates = ranking.length ? ranking : defaults;
    const failed = document.getElementById('simulate-failure').checked;
    const selected = candidates[failed ? 1 : 0];
    message.textContent = `${t(failed ? 'routing.skipped' : 'routing.selected')} ${selected.name} / ${selected.model}`;
    response.textContent = JSON.stringify({ provider: selected.provider, model: selected.model, demo: true }, null, 2);
  });
  renderRanking();

  const tabs = [...document.querySelectorAll('[role="tab"]')];
  function selectTab(name, focus = false) {
    tabs.forEach(tab => {
      const selected = tab.dataset.tab === name;
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !selected;
      if (selected && focus) tab.focus();
    });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    tab.addEventListener('keydown', event => {
      const keys = { ArrowRight: (index + 1) % tabs.length, ArrowDown: (index + 1) % tabs.length,
        ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowUp: (index + tabs.length - 1) % tabs.length,
        Home: 0, End: tabs.length - 1 };
      if (event.key in keys) { event.preventDefault(); selectTab(tabs[keys[event.key]].dataset.tab, true); }
    });
  });
  document.querySelectorAll('[data-start-tab]').forEach(link => link.addEventListener('click', () => selectTab(link.dataset.startTab)));

  const start = document.getElementById('desktop-start');
  const stop = document.getElementById('desktop-stop');
  const restart = document.getElementById('desktop-restart');
  const state = document.getElementById('desktop-state');
  const dot = document.getElementById('desktop-dot');
  let desktopTimer;
  function desktopState(value) {
    state.textContent = t('desktop.' + value);
    dot.className = 'status-dot ' + (value === 'ready' ? 'ready' : value === 'stopped' ? '' : 'busy');
    start.disabled = value !== 'stopped';
    stop.disabled = restart.disabled = value !== 'ready';
  }
  function beginDesktop(value) {
    clearTimeout(desktopTimer); desktopState(value);
    desktopTimer = setTimeout(() => desktopState('ready'), 700);
  }
  state.setAttribute('aria-live', 'polite');
  start.addEventListener('click', () => beginDesktop('starting'));
  restart.addEventListener('click', () => beginDesktop('restarting'));
  stop.addEventListener('click', () => { clearTimeout(desktopTimer); desktopState('stopped'); });
  desktopState('stopped');
})();
