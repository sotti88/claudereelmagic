/* Shared live terminal integration for both cutter cockpits. */
(function () {
  'use strict';
  var TermCtor = window.Terminal && window.Terminal.Terminal ? window.Terminal.Terminal : window.Terminal;
  var FitCtor = window.FitAddon && window.FitAddon.FitAddon ? window.FitAddon.FitAddon : window.FitAddon;
  var host = document.getElementById('claudeTermHost');
  var pane = document.getElementById('claudePane');
  var inspector = document.getElementById('inspectorPane');
  var tab = document.getElementById('tabClaude');
  var inspectorTab = document.getElementById('tabInspector');
  var exitOverlay = document.getElementById('claudeExit');
  var status = document.getElementById('providerStatus');
  var selector = document.getElementById('terminalProvider');
  if (!TermCtor || !host) return;
  var provider = 'claude';
  var ready = false;
  var stream = null;
  var inputQueue = Promise.resolve();
  var term = new TermCtor({fontSize: 12, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    theme: {background: '#0a0a0d', foreground: '#e5e7eb', cursor: '#f97316'}, cursorBlink: true, scrollback: 5000});
  var fit = FitCtor ? new FitCtor() : null;
  if (fit) term.loadAddon(fit);
  term.open(host);
  window.cutterTerminalProvider = function () { return provider; };
  window.cutterProviderLabel = function () { return provider === 'codex' ? 'Codex' : 'Claude'; };
  function post(path, body) {
    return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})})
      .then(function (response) { return response.json().then(function (data) {
        if (!response.ok || data.error) throw new Error(data.error || ('HTTP ' + response.status));
        return data;
      }); });
  }
  function fail(error) { status.textContent = error.message || String(error); }
  function label() {
    var name = window.cutterProviderLabel();
    tab.textContent = name;
    status.textContent = name + ' · Verbindet…';
    selector.value = provider;
    var send = document.getElementById('btnTSend');
    if (send && !send.disabled) send.textContent = 'An ' + name + ' senden';
    var exitText = exitOverlay.querySelector('div');
    if (exitText) exitText.textContent = name + '-Session beendet.';
    var model = document.getElementById('providerModel');
    model.textContent = provider === 'codex' ? 'Modell / Denken' : 'Modell';
  }
  function size() {
    if (!ready || !fit || pane.style.display === 'none') return;
    try { fit.fit(); } catch (_) { return; }
    post('/api/term/resize', {provider: provider, cols: term.cols, rows: term.rows}).catch(fail);
  }
  new ResizeObserver(size).observe(host);
  term.onData(function (data) {
    if (!ready) return;
    var target = provider;
    inputQueue = inputQueue.catch(function () {}).then(function () {
      return post('/api/term/input', {provider: target, data: btoa(unescape(encodeURIComponent(data)))});
    }).catch(fail);
  });
  function connect() {
    if (stream) stream.close();
    var target = provider;
    var connection = new EventSource('/api/term/stream?provider=' + encodeURIComponent(target));
    stream = connection;
    exitOverlay.style.display = 'none';
    connection.onopen = function () {
      if (stream !== connection) return;
      term.reset(); ready = true; size();
    };
    connection.onmessage = function (event) {
      if (stream !== connection) return;
      var decoded = atob(event.data);
      var bytes = new Uint8Array(decoded.length);
      for (var i = 0; i < decoded.length; i++) bytes[i] = decoded.charCodeAt(i);
      term.write(bytes);
    };
    connection.addEventListener('exit', function () {
      if (stream !== connection) return;
      ready = false; exitOverlay.style.display = 'flex'; connection.close();
    });
    connection.onerror = function () {
      if (stream === connection) status.textContent = 'Verbindung wird wiederhergestellt…';
    };
  }
  selector.addEventListener('change', function () {
    var next = selector.value;
    selector.disabled = true;
    post('/api/term/provider', {provider: next}).then(function () {
      provider = next; ready = false; label(); connect();
    }).catch(function (error) { selector.value = provider; fail(error); })
      .finally(function () { selector.disabled = false; });
  });
  document.getElementById('providerModel').addEventListener('click', function () {
    if (!ready) return;
    // Native CLI owns model/reasoning choices. Never submit a pre-existing draft.
    term.focus(); term.paste('/model');
    status.textContent = '/model eingefügt. Mit Enter öffnen.';
  });
  document.getElementById('providerPermissions').addEventListener('click', function () {
    if (!ready) return;
    term.focus(); term.paste('/permissions');
    status.textContent = '/permissions eingefügt. Mit Enter öffnen.';
  });
  document.getElementById('btnTermRestart').addEventListener('click', function () {
    post('/api/term/restart', {provider: provider}).then(connect).catch(fail);
  });
  var end = document.getElementById('btnKillSession');
  if (end) end.addEventListener('click', function () {
    if (!confirm(window.cutterProviderLabel() + '-Session dieses Projekts beenden und neu starten? Ein laufender Auftrag in dieser Session wird abgebrochen.')) return;
    post('/api/term/kill_session', {provider: provider}).then(connect).catch(fail);
  });
  function hideBroll() {
    var bp=document.getElementById('brollPane'), bt=document.getElementById('tabBroll');
    if(bp)bp.style.display='none'; if(bt)bt.classList.remove('active');
    if(end)end.style.display='';
  }
  tab.addEventListener('click', function () {
    hideBroll();
    tab.classList.add('active'); inspectorTab.classList.remove('active');
    pane.style.display = ''; inspector.style.display = 'none'; setTimeout(size, 30); term.focus();
  });
  inspectorTab.addEventListener('click', function () {
    hideBroll();
    inspectorTab.classList.add('active'); tab.classList.remove('active');
    inspector.style.display = ''; pane.style.display = 'none';
    var dot = document.getElementById('inspDot'); if (dot) dot.style.display = 'none';
  });
  var observer = new MutationObserver(function () {
    var dot = document.getElementById('inspDot');
    if (dot && inspector.style.display === 'none' && !document.querySelector('#inspBody .insp-empty')) dot.style.display = '';
  });
  var body = document.getElementById('inspBody');
  if (body) observer.observe(body, {childList: true, subtree: true});
  function refresh() {
    return fetch('/api/term/state').then(function (response) { return response.json(); }).then(function (state) {
      var current = state.providers && state.providers[provider];
      if (current) {
        var names = {starting: 'Startet…', ready: 'Bereit', idle: 'Bereit', working: 'Arbeitet', exited: 'Beendet', error: 'Fehler'};
        status.textContent = window.cutterProviderLabel() + ' · ' + (names[current.status] || current.status || 'Bereit');
      }
      return state;
    });
  }
  selector.disabled = true;
  refresh().then(function (state) {
    provider = state.provider; label(); selector.disabled = false; connect();
  }).catch(fail);
  var timer = setInterval(function () { refresh().catch(fail); }, 3000);
  window.addEventListener('beforeunload', function () {
    clearInterval(timer); observer.disconnect(); if (stream) stream.close();
  });
})();
