/* A job-owned pickup terminal. Never changes the cutter's selected provider. */
(function () {
  'use strict';
  var tab = document.getElementById('tabBroll'), pane = document.getElementById('brollPane');
  if (!tab || !pane) return;
  var host = document.getElementById('brollTermHost'), log = document.getElementById('brollSessionLog');
  var status = document.getElementById('brollSessionStatus'), draft = document.getElementById('brollDraft');
  var insert = document.getElementById('brollInsertDraft'), reconnect = document.getElementById('brollReconnect');
  var jobId = null, provider = null, stream = null, term = null, fit = null, ready = false;
  var state = null, timer = null, polling = false, inputQueue = Promise.resolve(), generation = 0;
  function visible() { return pane.style.display !== 'none'; }
  function key() { return 'cutter-broll-draft-' + jobId; }
  function fail(error) { status.textContent = error.message || String(error); }
  async function json(url, body) {
    var response = await fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : undefined);
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function route(action) { return '/api/term/' + action + '?broll_job=' + encodeURIComponent(jobId); }
  function resize() {
    if (!ready || !fit || !visible()) return;
    try { fit.fit(); } catch (_) { return; }
    json(route('resize'), {provider:provider, cols:term.cols, rows:term.rows}).catch(fail);
  }
  new ResizeObserver(resize).observe(host);
  function show() {
    document.getElementById('claudePane').style.display = 'none';
    document.getElementById('inspectorPane').style.display = 'none';
    document.getElementById('tabClaude').classList.remove('active');
    document.getElementById('tabInspector').classList.remove('active');
    document.getElementById('btnKillSession').style.display = 'none';
    pane.style.display = ''; tab.classList.add('active');
    poll().then(function () { if (state && state.can_resume && !stream) connect(); resize(); });
  }
  function adopt(id) {
    if (id === jobId) return;
    generation++; if (stream) stream.close(); stream = null; ready = false;
    if (term) term.dispose(); term = null; fit = null;
    jobId = id; state = null; host.style.display = 'none'; log.style.display = '';
    insert.disabled = true;
    try { draft.value = localStorage.getItem(key()) || ''; } catch (_) { draft.value = ''; }
  }
  function connect() {
    if (!state || !state.can_resume || !jobId) return;
    if (stream) stream.close(); ready = false;
    var Term = window.Terminal && window.Terminal.Terminal || window.Terminal;
    var Fit = window.FitAddon && window.FitAddon.FitAddon || window.FitAddon;
    if (!Term) return fail(new Error('Terminal konnte nicht geladen werden. Bitte Cockpit neu öffnen.'));
    if (!term) {
      term = new Term({fontSize:12, fontFamily:'ui-monospace, SFMono-Regular, Menlo, monospace', theme:{background:'#0a0a0d',foreground:'#e5e7eb',cursor:'#f97316'}, scrollback:5000,cursorBlink:true});
      if (Fit) { fit = new Fit(); term.loadAddon(fit); }
      term.open(host);
      term.onData(function (value) {
        if (!ready) return;
        var url = route('input'), target = provider;
        inputQueue = inputQueue.catch(function () {}).then(function () {
          return json(url,{provider:target,data:btoa(unescape(encodeURIComponent(value)))});
        }).catch(fail);
      });
    }
    host.style.display = ''; log.style.display = 'none';
    var connection = new EventSource(route('stream') + '&provider=' + encodeURIComponent(provider));
    stream = connection;
    connection.onopen = function () {
      if (stream !== connection) return;
      term.reset(); ready = true; insert.disabled = !draft.value.trim(); resize();
    };
    connection.onmessage = function (event) {
      if (stream !== connection) return;
      var raw = atob(event.data), bytes = new Uint8Array(raw.length);
      for(var i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);
      term.write(bytes);
    };
    connection.addEventListener('exit',function(){
      if(stream!==connection)return;
      ready=false;insert.disabled=true;connection.close();stream=null;
      status.textContent='B-Roll-Session beendet. Über „Session verbinden“ erneut öffnen.';
    });
    connection.onerror=function(){if(stream===connection)status.textContent='B-Roll-Verbindung wird wiederhergestellt…';};
  }
  async function poll() {
    if(polling)return; polling=true;
    var version=generation;
    try {
      var data=await json('/api/broll_session'+(jobId?'?job_id='+encodeURIComponent(jobId):''));
      if(version!==generation)return;
      if(!data.job_id)return;
      adopt(data.job_id);state=data;provider=data.provider;tab.style.display='';
      var names={queued:'Wartet auf Start',running:'Erstellt B-Roll',done:'Bereit zum Weiterarbeiten',imported:'In der Timeline',failed:'Produktion fehlgeschlagen',conflict:'Schnittstand geändert'};
      status.textContent=(provider==='codex'?'Codex':'Claude')+' · B-Roll · '+(names[data.status]||data.status||'');
      reconnect.disabled=!data.can_resume;
      if(!stream){
        var atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-40;
        if(log.textContent!==(data.log||''))log.textContent=data.log||'Der B-Roll-Agent bereitet den Auftrag vor…';
        if(atEnd)log.scrollTop=log.scrollHeight;
      }
      if(data.error)status.textContent=data.error;
    } catch(error){if(visible())fail(error);} finally{polling=false;}
  }
  tab.addEventListener('click',show);
  reconnect.addEventListener('click',function(){poll().then(connect);});
  draft.addEventListener('input',function(){
    if(jobId)try{localStorage.setItem(key(),draft.value);}catch(_){}
    insert.disabled=!ready||!draft.value.trim();
  });
  insert.addEventListener('click',function(){
    if(!ready||!draft.value.trim())return;
    term.paste(draft.value);term.focus();
    status.textContent='Ergänzung eingefügt. Prüfen und mit Enter senden.';
  });
  window.cutterBrollSession=function(job,open){if(job&&job.id){adopt(job.id);tab.style.display='';}if(open)show();else poll();};
  poll().then(function(){if(jobId && new URLSearchParams(location.search).get("panel")==="broll")show();});timer=setInterval(poll,4000);
  window.addEventListener('beforeunload',function(){clearInterval(timer);if(stream)stream.close();});
})();
