'use strict';
/* Dashboard logic. All user-supplied text is inserted with textContent (never innerHTML). */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var busy = {};            // ids with a request in flight (proposals: number, schedule items: 's' + number)
  var openJid = null;       // contact whose history is displayed
  var currentCode = '';
  var status = null;
  var failures = 0;
  var pairing = false;
  var cache = {};           // last rendered signature per list, so lists only rebuild when something changed

  // Status backgrounds. The class names (sw-0..sw-5) are defined in app.css (inline styles are not allowed).
  var PALETTE = ['#2563eb', '#7c3aed', '#0b1026', '#4f46e5', '#9333ea', '#0ea5e9'];
  var PALETTE_NAMES = ['Blue', 'Violet', 'Midnight', 'Indigo', 'Purple', 'Sky'];
  var kind = 'message';
  var colorIndex = 0;
  var formBusy = false;
  var OTHER = '__other__';

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function api(method, path, body) {
    var opts = { method: method, headers: { 'X-Dashboard': '1' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok || data.ok === false) throw new Error(data.error || ('Request failed (HTTP ' + res.status + ')'));
        return data;
      });
    });
  }

  // Rebuilding a list every few seconds can swallow a tap that is in progress,
  // so a list is only rebuilt when its data (or the current minute, for countdowns) changed.
  function renderIfChanged(key, data, extra, fn) {
    var sig = JSON.stringify(data) + '|' + (extra || '');
    if (cache[key] === sig) return;
    cache[key] = sig;
    fn(data);
  }
  function minuteBucket() { return String(Math.floor(Date.now() / 60000)); }

  function fmtTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function relTime(ts) {
    var s = ts - Math.floor(Date.now() / 1000);
    if (s <= 60) return 'due now';
    var m = Math.round(s / 60);
    if (m < 60) return 'in ' + m + ' min';
    var h = Math.floor(m / 60);
    if (h < 48) return 'in ' + h + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
    return 'in ' + Math.round(h / 24) + ' days';
  }
  function leftTime(ts) {
    var m = Math.max(0, Math.round((ts - Math.floor(Date.now() / 1000)) / 60));
    if (m < 60) return m + ' min';
    return Math.floor(m / 60) + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
  }

  function niceJid(jid) { return String(jid || '').split('@')[0]; }
  function setText(id, text) { var n = $(id); if (n) n.textContent = text; }
  function show(id, on) { var n = $(id); if (n) n.classList.toggle('hidden', !on); }
  function short(text, n) { text = String(text || ''); return text.length > n ? text.slice(0, n) + '...' : text; }
  function colorClass(hex) {
    var i = PALETTE.indexOf(String(hex || '').toLowerCase());
    return 'sw-' + (i < 0 ? 0 : i);
  }

  // ------------------------------------------------------------ status
  var WA_TEXT = {
    bridge_down: ['bad', 'WhatsApp bridge is not running', 'In Termux run ./status.sh to see what is wrong, then ./restart.sh.'],
    bridge_error: ['bad', 'Bridge not accepting requests', 'The bridge token does not match. Run ./restart.sh. If it persists run ./diagnose.sh.'],
    starting: ['wait', 'Starting...', 'The bridge is starting. This takes a few seconds.'],
    connecting: ['wait', 'Connecting to WhatsApp...', 'Please wait. This page updates by itself.'],
    awaiting_pairing: ['wait', 'Ready to pair', 'Enter your number below to get a pairing code.'],
    connected: ['ok', 'WhatsApp connected', 'Messages to your account will now be answered according to your settings.'],
    disconnected: ['wait', 'Disconnected - reconnecting', 'The bridge keeps trying to reconnect automatically.'],
    logged_out: ['bad', 'Logged out from WhatsApp', 'WhatsApp removed this linked device. In Termux run ./reset_whatsapp_session.sh (it backs up the old session first), then pair again here.']
  };

  function chip(label, kindClass) { return el('span', 'chip ' + kindClass, label); }

  function renderStatus(s) {
    status = s;
    var wa = s.whatsapp || {};
    var info = WA_TEXT[wa.state] || ['wait', String(wa.state || 'unknown'), ''];
    var chips = $('chips');
    chips.textContent = '';
    chips.appendChild(chip('Dashboard running', 'ok'));
    chips.appendChild(chip(s.bridge_running ? 'Bridge running' : 'Bridge stopped', s.bridge_running ? 'ok' : 'bad'));
    chips.appendChild(chip(info[1].indexOf('WhatsApp') === 0 ? info[1] : 'WhatsApp: ' + info[1], info[0]));
    chips.appendChild(chip(s.gemini_configured ? 'Gemini key set' : 'Gemini key missing', s.gemini_configured ? 'ok' : 'bad'));
    chips.appendChild(chip(s.manual_approval ? 'Manual approval' : 'AUTO replies', s.manual_approval ? 'ok' : 'wait'));
    if (s.account && s.account !== 'main') chips.appendChild(chip('Account: ' + s.account, 'wait'));
    if (s.away) chips.appendChild(chip('Away mode', 'wait'));
    if (s.quiet_now) chips.appendChild(chip('Quiet hours', 'wait'));
    if (!s.bot_enabled) chips.appendChild(chip('AI paused', 'bad'));
    if (s.scheduled_count) chips.appendChild(chip(s.scheduled_count + ' scheduled', 'wait'));
    renderAway(s);

    $('waDot').className = 'dot ' + info[0];
    setText('waState', info[1]);
    var help = info[2];
    if (wa.last_error && wa.state !== 'connected') help = (help ? help + ' ' : '') + wa.last_error;
    setText('waHelp', help);

    var canPair = wa.state === 'awaiting_pairing';
    var linked = wa.state === 'connected';
    show('pairBox', !linked && wa.state !== 'logged_out');
    $('pairBtn').disabled = !canPair || pairing;
    if (linked) { currentCode = ''; show('codeBox', false); }

    $('keyDot').className = 'dot ' + (s.gemini_configured ? 'ok' : 'bad');
    setText('keyState', s.gemini_configured ? 'Gemini key: configured' : 'Gemini key: not configured');
    $('autoWarn').classList.toggle('hidden', !!s.manual_approval);
    setText('pendingBadge', String(s.pending_count || 0));
    setText('scheduleBadge', String(s.scheduled_count || 0));

    var msg = s.last_ai_error ? ('Last AI problem: ' + s.last_ai_error) : '';
    if (msg) setText('keyMsg', msg);
  }

  // ------------------------------------------------------------ pairing
  function setPairError(text) { setText('pairError', text || ''); show('pairError', !!text); }

  function formatCode(code) {
    var c = String(code || '');
    return c.length === 8 ? c.slice(0, 4) + '-' + c.slice(4) : c;
  }

  $('pairBtn').addEventListener('click', function () {
    if (pairing) return;
    setPairError('');
    pairing = true;
    $('pairBtn').disabled = true;
    $('pairBtn').textContent = 'Requesting...';
    api('POST', '/api/whatsapp/pair', { phone: $('phone').value }).then(function (data) {
      currentCode = data.code;
      setText('pairCode', formatCode(data.code));
      show('codeBox', true);
    }).catch(function (err) {
      setPairError(err.message);
    }).then(function () {
      pairing = false;
      $('pairBtn').textContent = 'Get pairing code';
      if (status) renderStatus(status);
    });
  });

  $('copyBtn').addEventListener('click', function () {
    var btn = $('copyBtn');
    var done = function (ok) {
      btn.textContent = ok ? 'Copied!' : 'Press and hold the code to copy';
      setTimeout(function () { btn.textContent = 'Copy code'; }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(currentCode).then(function () { done(true); }, function () { done(false); });
      return;
    }
    var area = document.createElement('textarea');
    area.value = currentCode;
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(area);
    done(ok);
  });

  // ------------------------------------------------------------ settings
  function fillSettings(s) {
    $('botEnabled').checked = !!s.bot_enabled;
    $('manualApproval').checked = !!s.manual_approval;
    var modelSel = $('model');
    var savedModel = s.gemini_model || 'auto';
    var found = false;
    for (var mi = 0; mi < modelSel.options.length; mi++) if (modelSel.options[mi].value === savedModel) found = true;
    if (!found) { var custom = document.createElement('option'); custom.value = savedModel; custom.textContent = savedModel + ' (currently saved)'; modelSel.appendChild(custom); }
    modelSel.value = savedModel;
    if ($('tone')) $('tone').value = s.tone || '';
    if ($('quietHours')) $('quietHours').value = s.quiet_hours || '';
    if ($('voiceNotes')) $('voiceNotes').checked = s.voice_notes !== false;
    $('ctx').value = s.context_messages;
    $('maxAge').value = s.max_reply_age_minutes;
    $('autoCap').value = s.auto_reply_per_hour;
    $('prompt').value = s.system_prompt;
  }

  function loadModels() {
    setText('modelsMsg', 'Loading models available to this API key...');
    return api('GET', '/api/gemini/models').then(function (d) {
      var sel = $('model'); var saved = sel.value || 'auto';
      sel.textContent = '';
      var auto = document.createElement('option'); auto.value = 'auto'; auto.textContent = 'Auto (choose a usable Flash model)'; sel.appendChild(auto);
      d.models.forEach(function (name) { var o = document.createElement('option'); o.value = name; o.textContent = name; sel.appendChild(o); });
      if (!d.models.some(function (m) { return m === saved; }) && saved !== 'auto') { var old = document.createElement('option'); old.value = saved; old.textContent = saved + ' (saved, not in latest list)'; sel.appendChild(old); }
      sel.value = saved;
      setText('modelsMsg', 'Loaded ' + d.models.length + ' models from Google for this API key.');
    }).catch(function (err) { setText('modelsMsg', 'Could not load models: ' + err.message + '. Check the key, internet and Google AI Studio quota. Auto mode can still try fallback models.'); });
  }

  function loadSettings() {
    // Status can discover and save the linked WhatsApp number as the initial owner.
    return api('GET', '/api/status').catch(function () { return {}; }).then(function () {
      return api('GET', '/api/settings');
    }).then(function (d) {
      fillSettings(d.settings);
      if ($('ownerPhone') && d.owner_phone !== undefined) $('ownerPhone').value = d.owner_phone || '';
    });
  }

  function saveSettings() {
    var body = {
      owner_phone: $('ownerPhone').value.trim(),
      bot_enabled: $('botEnabled').checked,
      manual_approval: $('manualApproval').checked,
      gemini_model: $('model').value,
      tone: $('tone').value,
      quiet_hours: $('quietHours').value.trim(),
      voice_notes: $('voiceNotes').checked,
      context_messages: $('ctx').value,
      max_reply_age_minutes: $('maxAge').value,
      auto_reply_per_hour: $('autoCap').value,
      system_prompt: $('prompt').value
    };
    if (!body.manual_approval) {
      var ok = window.confirm('Turn OFF manual approval?\n\nThe AI will send replies to your contacts automatically, without asking you first. Only continue if you are sure.');
      if (!ok) { $('manualApproval').checked = true; return Promise.resolve(); }
      body.confirm_auto = true;
    }
    return api('POST', '/api/settings', body).then(function (d) {
      fillSettings(d.settings);
      if ($('ownerPhone') && d.owner_phone !== undefined) $('ownerPhone').value = d.owner_phone || '';
      setText('settingsMsg', 'Saved.');
      refresh();
    }).catch(function (err) {
      setText('settingsMsg', 'Not saved: ' + err.message);
      loadSettings();
    });
  }

  $('saveSettings').addEventListener('click', function () { saveSettings(); });
  $('botEnabled').addEventListener('change', function () { saveSettings(); });
  $('manualApproval').addEventListener('change', function () { saveSettings(); });
  $('voiceNotes').addEventListener('change', function () { saveSettings(); });
  $('refreshModels').addEventListener('click', function () { loadModels(); });

  $('saveKey').addEventListener('click', function () {
    var key = $('apiKey').value.trim();
    if (!key) { setText('keyMsg', 'Paste your Gemini API key first.'); return; }
    api('POST', '/api/gemini/key', { api_key: key }).then(function () {
      $('apiKey').value = '';
      setText('keyMsg', 'Key saved. Tap "Test Gemini" to check it works.');
      loadModels();
      refresh();
    }).catch(function (err) { setText('keyMsg', 'Not saved: ' + err.message); });
  });

  $('testKey').addEventListener('click', function () {
    setText('keyMsg', 'Testing...');
    api('POST', '/api/gemini/test', {}).then(function (d) {
      setText('keyMsg', 'Gemini works (model: ' + d.model + ').');
    }).catch(function (err) { setText('keyMsg', 'Gemini test failed: ' + err.message); });
  });

  $('clearKey').addEventListener('click', function () {
    if (!window.confirm('Remove the saved Gemini API key?')) return;
    api('POST', '/api/gemini/key', { clear: true }).then(function () {
      setText('keyMsg', 'Key removed.');
      setText('modelsMsg', 'Save a Gemini API key first, then load models.');
      refresh();
    }).catch(function (err) { setText('keyMsg', err.message); });
  });

  // ------------------------------------------------------------ AI provider manager
  function loadProviders() {
    api('GET','/api/providers').then(function(data) {
      var box=$('providerList'); box.textContent='';
      if(!data.providers.length){box.appendChild(el('p','empty','No dashboard-managed providers yet. The legacy Gemini key still works.'));return;}
      data.providers.forEach(function(p,idx){
        var item=el('div','item');
        item.appendChild(el('div','who',(idx+1)+'. '+p.name+' '+(p.has_key?'🔑':'⚠️ no key')));
        item.appendChild(el('div','meta',p.provider+' · '+p.model+' · '+(p.enabled?'enabled':'disabled')));
        var row=el('div','row');
        function btn(label,fn){var b=el('button','',label);b.type='button';b.addEventListener('click',fn);row.appendChild(b);}
        btn('Edit',function(){ $('providerId').value=p.id;$('providerName').value=p.name;$('providerType').value=p.provider;$('providerModel').value=p.model;$('providerBase').value=p.base_url||'';$('providerKey').value='';setText('providerMsg','Editing '+p.name+'. Leave API key blank to keep the saved key.'); });
        btn('Test',function(){setText('providerMsg','Testing '+p.name+'...');api('POST','/api/providers/test',{id:p.id}).then(function(r){setText('providerMsg',p.name+' works. Model: '+r.model);}).catch(function(e){setText('providerMsg','Test failed: '+e.message);});});
        btn(p.enabled?'Disable':'Enable',function(){api('POST','/api/providers',{id:p.id,name:p.name,provider:p.provider,model:p.model,base_url:p.base_url||'',api_key:'',enabled:!p.enabled}).then(loadProviders).catch(function(e){setText('providerMsg',e.message);});});
        if(idx>0)btn('Move up',function(){var order=data.order.slice();var i=order.indexOf(p.id);if(i>0){var t=order[i-1];order[i-1]=order[i];order[i]=t;api('POST','/api/providers/order',{order:order}).then(loadProviders);}});
        if(idx<data.providers.length-1)btn('Move down',function(){var order=data.order.slice();var i=order.indexOf(p.id);if(i>=0&&i<order.length-1){var t=order[i+1];order[i+1]=order[i];order[i]=t;api('POST','/api/providers/order',{order:order}).then(loadProviders);}});
        btn('Remove',function(){if(window.confirm('Remove provider '+p.name+'?'))api('POST','/api/providers/remove',{id:p.id}).then(loadProviders).catch(function(e){setText('providerMsg',e.message);});});
        item.appendChild(row);box.appendChild(item);
      });
    }).catch(function(e){setText('providerMsg','Could not load providers: '+e.message);});
  }
  $('providerType').addEventListener('change',function(){if($('providerType').value==='gemini'){$('providerModel').placeholder='auto or gemini model';$('providerBase').disabled=true;}else{$('providerModel').placeholder='model name from your API provider';$('providerBase').disabled=false;}});
  $('providerBase').disabled=true;
  $('saveProvider').addEventListener('click',function(){
    var payload={id:$('providerId').value||undefined,name:$('providerName').value,provider:$('providerType').value,api_key:$('providerKey').value,model:$('providerModel').value,base_url:$('providerBase').value,enabled:true};
    setText('providerMsg','Saving provider...');api('POST','/api/providers',payload).then(function(){ $('providerKey').value='';$('providerId').value='';setText('providerMsg','Provider saved. Check its status with Test.');loadProviders();refresh();}).catch(function(e){setText('providerMsg','Not saved: '+e.message);});
  });
  $('cancelProviderEdit').addEventListener('click',function(){['providerId','providerName','providerKey','providerModel','providerBase'].forEach(function(id){$(id).value='';});$('providerType').value='gemini';$('providerBase').disabled=true;setText('providerMsg','Form cleared.');});
  loadProviders();

  // ------------------------------------------------------------ proposals
  function renderProposals(list) {
    var box = $('pendingList');
    box.textContent = '';
    if (!list.length) { box.appendChild(el('p', 'empty', 'No replies waiting for approval.')); return; }
    list.forEach(function (p) {
      var item = el('div', 'item');
      item.appendChild(el('div', 'who', p.contact_name || niceJid(p.jid)));
      item.appendChild(el('div', 'meta', niceJid(p.jid) + ' - ' + fmtTime(p.incoming_ts)));
      item.appendChild(el('div', 'tag', 'They wrote'));
      item.appendChild(el('div', 'quote', p.incoming_body));
      item.appendChild(el('div', 'tag', 'Suggested reply'));
      item.appendChild(el('div', 'quote ai', p.suggestion));
      var sending = p.status === 'sending' || !!busy[p.id];
      if (sending) item.appendChild(el('p', 'muted', 'Sending...'));
      if (p.error) item.appendChild(el('p', 'error', p.error));
      var ok = el('button', 'primary', p.error ? 'Retry send' : 'Approve & Send');
      var no = el('button', 'danger', 'Reject');
      ok.disabled = no.disabled = sending;
      ok.addEventListener('click', function () { act(p.id, 'approve'); });
      no.addEventListener('click', function () { act(p.id, 'reject'); });
      item.appendChild(ok);
      item.appendChild(no);
      box.appendChild(item);
    });
  }

  function act(id, what) {
    busy[id] = true;
    refreshProposals();
    api('POST', '/api/proposals/' + id + '/' + what, {}).catch(function (err) {
      window.alert(err.message);
    }).then(function () {
      delete busy[id];
      refresh();
    });
  }

  function refreshProposals() {
    return api('GET', '/api/proposals').then(function (d) {
      renderIfChanged('proposals', d.proposals, JSON.stringify(busy), renderProposals);
    });
  }

  // ------------------------------------------------------------ scheduling form
  function toLocalInput(ms) {
    var d = new Date(ms);
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function setWhen(ms) { $('when').value = toLocalInput(ms); }
  function atHour(hour, forceTomorrow) {
    var d = new Date();
    d.setHours(hour, 0, 0, 0);
    if (forceTomorrow || d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
    return d.getTime();
  }

  function syncRecipient() { show('phoneBox', $('recipient').value === OTHER); }

  function fillRecipients(list) {
    var sel = $('recipient');
    var prev = sel.value;
    sel.textContent = '';
    list.forEach(function (c) {
      var o = document.createElement('option');
      o.value = c.jid;
      o.textContent = (c.name ? c.name + ' - ' : '') + niceJid(c.jid);
      sel.appendChild(o);
    });
    var other = document.createElement('option');
    other.value = OTHER;
    other.textContent = 'Another number...';
    sel.appendChild(other);
    var exists = false;
    for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === prev) exists = true;
    sel.value = exists ? prev : (list.length ? list[0].jid : OTHER);
    syncRecipient();
  }

  function updatePreview() {
    var text = $('schedText').value;
    setText('charCount', String(text.length));
    setText('previewText', text.trim() ? short(text, 160) : 'Your status');
  }

  function setKind(next) {
    kind = next;
    $('kindMessage').classList.toggle('on', next === 'message');
    $('kindStatus').classList.toggle('on', next === 'status');
    show('msgFields', next === 'message');
    show('statusFields', next === 'status');
    var max = next === 'status' ? 700 : 4000;
    $('schedText').maxLength = max;
    setText('charMax', String(max));
    setText('schedTextLabel', next === 'status' ? 'Status text' : 'Message');
    $('schedText').placeholder = next === 'status' ? 'What do you want to post?' : 'Write the message to send...';
    if ($('schedText').value.length > max) $('schedText').value = $('schedText').value.slice(0, max);
    updatePreview();
  }

  function setColor(i) {
    colorIndex = i;
    var swatches = $('swatches').children;
    for (var k = 0; k < swatches.length; k++) {
      var on = k === i;
      swatches[k].classList.toggle('on', on);
      swatches[k].setAttribute('aria-checked', on ? 'true' : 'false');
    }
    $('statusPreview').className = 'phone-preview sw-' + i;
  }

  function buildSwatches() {
    var box = $('swatches');
    PALETTE.forEach(function (hex, i) {
      var b = el('button', 'swatch sw-' + i);
      b.type = 'button';
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-label', PALETTE_NAMES[i]);
      b.addEventListener('click', function () { setColor(i); });
      box.appendChild(b);
    });
    setColor(0);
  }

  function buildPayload(sendNow) {
    var text = $('schedText').value.trim();
    if (!text) throw new Error('Write the text first.');
    var body = { kind: kind, text: text };
    if (kind === 'message') {
      var who = $('recipient').value;
      if (!who || who === OTHER) {
        body.phone = $('schedPhone').value;
        if (!body.phone.trim()) throw new Error('Type the number to send to.');
      } else {
        body.jid = who;
      }
    } else {
      body.bg = PALETTE[colorIndex];
      body.audience = $('audience').value;
    }
    if (!sendNow) {
      var ms = $('when').value ? new Date($('when').value).getTime() : NaN;
      if (isNaN(ms)) throw new Error('Pick a date and time.');
      body.when = Math.floor(ms / 1000);
    }
    return body;
  }

  function submitSchedule(sendNow) {
    if (formBusy) return;
    var body;
    try { body = buildPayload(sendNow); } catch (err) { setText('schedMsg', err.message); return; }
    if (sendNow) {
      var what = kind === 'status'
        ? (body.audience === 'me' ? 'Post this status now (only you will see it)?' : 'Post this status now to everyone who has messaged this assistant?')
        : 'Send this message now?';
      if (!window.confirm(what)) return;
    }
    formBusy = true;
    $('scheduleBtn').disabled = $('sendNowBtn').disabled = true;
    setText('schedMsg', sendNow ? 'Sending...' : 'Scheduling...');
    api('POST', '/api/scheduled', body).then(function () {
      $('schedText').value = '';
      updatePreview();
      setText('schedMsg', sendNow
        ? 'Queued - it goes out within a few seconds. Watch the status below.'
        : 'Scheduled for ' + fmtTime(body.when) + '.');
      return refresh();
    }).catch(function (err) {
      setText('schedMsg', 'Not scheduled: ' + err.message);
    }).then(function () {
      formBusy = false;
      $('scheduleBtn').disabled = $('sendNowBtn').disabled = false;
    });
  }

  $('kindMessage').addEventListener('click', function () { setKind('message'); });
  $('kindStatus').addEventListener('click', function () { setKind('status'); });
  $('recipient').addEventListener('change', syncRecipient);
  $('schedText').addEventListener('input', updatePreview);
  $('qHour').addEventListener('click', function () { setWhen(Date.now() + 3600 * 1000); });
  $('qTonight').addEventListener('click', function () { setWhen(atHour(20, false)); });
  $('qTomorrow').addEventListener('click', function () { setWhen(atHour(8, true)); });
  $('scheduleBtn').addEventListener('click', function () { submitSchedule(false); });
  $('sendNowBtn').addEventListener('click', function () { submitSchedule(true); });

  // ------------------------------------------------------------ scheduled items
  function itemTarget(i) {
    if (i.kind === 'message') return i.contact_name || niceJid(i.jid);
    return i.audience === 'me' ? 'Status - only you' : 'Status - people who messaged you';
  }

  function itemBody(parent, i) {
    if (i.kind === 'status') {
      parent.appendChild(el('span', 'status-swatch ' + colorClass(i.bg), short(i.body, 240)));
    } else {
      parent.appendChild(el('div', 'quote', short(i.body, 240)));
    }
  }

  function schedAct(id, what, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    busy['s' + id] = true;
    cache.upcoming = cache.history = cache.live = null;
    api('POST', '/api/scheduled/' + id + '/' + what, {}).catch(function (err) {
      window.alert(err.message);
    }).then(function () {
      delete busy['s' + id];
      refresh();
    });
  }

  function renderUpcoming(list) {
    var box = $('upcomingList');
    box.textContent = '';
    if (!list.length) { box.appendChild(el('p', 'empty', 'Nothing scheduled.')); return; }
    list.forEach(function (i) {
      var item = el('div', 'item');
      var head = el('div', 'who');
      head.appendChild(el('span', 'pill ' + i.status, i.kind));
      head.appendChild(document.createTextNode(itemTarget(i)));
      item.appendChild(head);
      item.appendChild(el('div', 'meta', fmtTime(i.run_at) + ' - ' + relTime(i.run_at)));
      itemBody(item, i);
      var sending = i.status === 'sending' || !!busy['s' + i.id];
      if (sending) item.appendChild(el('p', 'muted', 'Sending...'));
      else if (i.error) item.appendChild(el('p', 'error', 'Will retry shortly: ' + i.error));
      var cancel = el('button', 'danger', 'Cancel');
      cancel.disabled = sending;
      cancel.addEventListener('click', function () { schedAct(i.id, 'cancel', 'Cancel this scheduled ' + i.kind + '?'); });
      item.appendChild(cancel);
      box.appendChild(item);
    });
  }

  function renderHistory(list) {
    var box = $('schedHistoryList');
    box.textContent = '';
    if (!list.length) { box.appendChild(el('p', 'empty', 'No scheduled activity yet.')); return; }
    list.forEach(function (i) {
      var item = el('div', 'item');
      var head = el('div', 'who');
      head.appendChild(el('span', 'pill ' + i.status, i.status));
      head.appendChild(document.createTextNode(itemTarget(i)));
      item.appendChild(head);
      item.appendChild(el('div', 'meta', i.kind + ' - ' + fmtTime(i.sent_at || i.run_at)));
      itemBody(item, i);
      if (i.error) item.appendChild(el('p', 'error', i.error));
      if (i.status === 'failed' || i.status === 'missed') {
        var go = el('button', 'primary', 'Send now');
        go.disabled = !!busy['s' + i.id];
        go.addEventListener('click', function () {
          schedAct(i.id, 'retry', i.status === 'failed' ? 'Send this again? If WhatsApp already delivered it, the person gets it twice.' : null);
        });
        item.appendChild(go);
      }
      box.appendChild(item);
    });
  }

  function renderLive(list) {
    var box = $('liveStatusList');
    box.textContent = '';
    if (!list.length) {
      box.appendChild(el('p', 'empty', 'No live statuses posted from here. Statuses you post in the WhatsApp app itself cannot be deleted from this dashboard.'));
      return;
    }
    list.forEach(function (s) {
      var item = el('div', 'item');
      item.appendChild(el('div', 'who', s.audience === 'me' ? 'Visible to: only you' : 'Visible to: people who messaged you'));
      item.appendChild(el('div', 'meta', 'Posted ' + fmtTime(s.sent_at) + ' - disappears in ' + leftTime(s.sent_at + 86400)));
      item.appendChild(el('span', 'status-swatch ' + colorClass(s.bg), short(s.body, 240)));
      var del = el('button', 'danger', 'Delete status');
      del.disabled = !!busy['s' + s.id];
      del.addEventListener('click', function () {
        schedAct(s.id, 'delete_status', 'Delete this status from WhatsApp? People who could see it will no longer see it.');
      });
      item.appendChild(del);
      box.appendChild(item);
    });
  }

  function renderScheduled(d) {
    var extra = minuteBucket() + '|' + JSON.stringify(busy);
    renderIfChanged('upcoming', d.upcoming, extra, renderUpcoming);
    renderIfChanged('history', d.history, extra, renderHistory);
    renderIfChanged('live', d.live_statuses, extra, renderLive);
  }

  // ------------------------------------------------------------ contacts
  function renderContacts(list) {
    var box = $('contactList');
    box.textContent = '';
    if (!list.length) { box.appendChild(el('p', 'empty', 'No one has messaged you yet.')); return; }
    list.forEach(function (c) {
      var item = el('div', 'item');
      item.appendChild(el('div', 'who', c.name || niceJid(c.jid)));
      item.appendChild(el('div', 'meta', c.jid + ' - ' + fmtTime(c.last_message_at)));
      var last = (c.last_direction === 'out' ? 'You: ' : '') + (c.last_body || '');
      item.appendChild(el('div', 'quote', short(last, 140)));
      item.appendChild(el('p', 'muted', c.replies_enabled ? 'AI replies: ON' : 'AI replies: OFF'));
      var toggle = el('button', c.replies_enabled ? 'danger' : 'primary', c.replies_enabled ? 'Disable replies' : 'Enable replies');
      toggle.addEventListener('click', function () {
        api('POST', '/api/contacts/toggle', { jid: c.jid, enabled: !c.replies_enabled }).then(refresh).catch(function (e) { window.alert(e.message); });
      });
      var hist = el('button', '', 'History');
      hist.addEventListener('click', function () {
        openJid = c.jid;
        loadConversation(true);
      });
      var prof = el('button', '', (c.style || c.notes || c.memory) ? 'Style & memory \u2022' : 'Style & memory');
      prof.addEventListener('click', function () { openProfile(c); });
      item.appendChild(toggle);
      item.appendChild(hist);
      item.appendChild(prof);
      box.appendChild(item);
    });
  }

  function loadConversation(scroll) {
    if (!openJid) return Promise.resolve();
    return api('GET', '/api/conversation?jid=' + encodeURIComponent(openJid)).then(function (d) {
      var sig = JSON.stringify(d.messages) + '|' + openJid;
      if (!scroll && cache.conversation === sig) return;
      cache.conversation = sig;
      setText('historyTitle', 'Conversation with ' + (d.contact.name || niceJid(d.contact.jid)));
      var chat = $('chat');
      var atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
      chat.textContent = '';
      if (!d.messages.length) chat.appendChild(el('p', 'empty', 'No messages stored.'));
      d.messages.forEach(function (m) {
        var b = el('div', 'bubble ' + (m.direction === 'out' ? 'out' : 'in'));
        b.appendChild(document.createTextNode(m.body));
        b.appendChild(el('time', '', (m.direction === 'out' ? 'You' : 'Them') + ' - ' + fmtTime(m.ts)));
        chat.appendChild(b);
      });
      if (scroll) {
        $('history').scrollIntoView();
        chat.scrollTop = chat.scrollHeight;
      } else if (atBottom) {
        chat.scrollTop = chat.scrollHeight;
      }
    }).catch(function (err) { setText('historyTitle', err.message); });
  }

  // ------------------------------------------------------------ health
  function kv(label, value) {
    var li = el('li');
    li.appendChild(el('span', '', label));
    li.appendChild(el('span', '', value));
    return li;
  }

  function renderHealth(d) {
    var list = $('healthList');
    list.textContent = '';
    list.appendChild(kv('Dashboard (Flask)', 'running'));
    list.appendChild(kv('WhatsApp bridge', d.bridge_running ? 'running' : 'NOT running'));
    list.appendChild(kv('WhatsApp state', String(d.whatsapp_state || 'unknown')));
    list.appendChild(kv('Gemini key', d.gemini_configured ? 'configured' : 'missing'));
    list.appendChild(kv('Gemini model setting', String(d.gemini_model_setting)));
    list.appendChild(kv('Last WhatsApp error', d.whatsapp_last_error || 'none'));
    list.appendChild(kv('Last AI error', d.last_ai_error || 'none'));
    list.appendChild(kv('Stored contacts / messages', d.database.contacts + ' / ' + d.database.messages));
    list.appendChild(kv('Scheduled items waiting', String(d.database.scheduled || 0)));
    list.appendChild(kv('Python', d.python));
    var ev = $('eventList');
    ev.textContent = '';
    if (!d.events.length) ev.appendChild(el('li', 'empty', 'Nothing yet.'));
    d.events.forEach(function (e) {
      ev.appendChild(el('li', 'lvl-' + e.level, fmtTime(e.ts) + ' [' + e.source + '] ' + e.message));
    });
  }

  // ------------------------------------------------------------ refresh loop
  function banner(text) {
    setText('banner', text || '');
    show('banner', !!text);
  }

  function refresh() {
    return Promise.all([
      api('GET', '/api/status').then(renderStatus),
      refreshProposals(),
      api('GET', '/api/scheduled').then(renderScheduled),
      api('GET', '/api/contacts').then(function (d) {
        renderIfChanged('contacts', d.contacts, '', function (list) { renderContacts(list); fillRecipients(list); });
      }),
      api('GET', '/api/diagnostics').then(function (d) { renderIfChanged('health', d, '', renderHealth); }),
      loadConversation(false)
    ]).then(function () {
      failures = 0;
      banner('');
    }).catch(function (err) {
      failures += 1;
      if (failures >= 2) banner('Cannot reach the dashboard: ' + err.message + '. In Termux run ./status.sh');
    });
  }

  // Separate dashboard tabs make the interface comfortable on a phone.
  function setupTabs() {
    var links = document.querySelectorAll('#nav a[data-tab]');
    var panels = document.querySelectorAll('.tab-panel');
    function activate(name, updateHash) {
      for (var i = 0; i < panels.length; i++) panels[i].classList.toggle('hidden', panels[i].id !== 'tab-' + name);
      for (var j = 0; j < links.length; j++) {
        var active = links[j].getAttribute('data-tab') === name;
        links[j].classList.toggle('on', active);
        if (active) links[j].setAttribute('aria-current', 'page'); else links[j].removeAttribute('aria-current');
      }
      if (updateHash) history.replaceState(null, '', '#tab-' + name);
      window.scrollTo(0, 0);
    }
    for (var k = 0; k < links.length; k++) links[k].addEventListener('click', function (event) {
      event.preventDefault(); activate(this.getAttribute('data-tab'), true);
    });
    var initial = (location.hash || '').replace('#tab-', '');
    var allowed = ['whatsapp', 'ai', 'automation', 'contacts', 'activity'];
    activate(allowed.indexOf(initial) >= 0 ? initial : 'whatsapp', false);
  }

  // Kept for compatibility; tab navigation controls visibility now.
  function watchSections() {
    if (!('IntersectionObserver' in window)) return;
    var links = {};
    var anchors = document.querySelectorAll('#nav a');
    for (var k = 0; k < anchors.length; k++) links[anchors[k].getAttribute('href').slice(1)] = anchors[k];
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting || !links[e.target.id]) return;
        Object.keys(links).forEach(function (id) { links[id].classList.remove('on'); });
        links[e.target.id].classList.add('on');
      });
    }, { rootMargin: '-25% 0px -65% 0px' });
    Object.keys(links).forEach(function (id) { if ($(id)) io.observe($(id)); });
  }

  // ------------------------------------------------------------ away mode
  function renderAway(s) {
    if (!$('awayState')) return;
    $('awayDot').className = 'dot ' + (s.away ? 'wait' : 'ok');
    var text = s.away ? 'Away mode is ON' : 'Away mode is off';
    if (s.away && s.away_until) text += ' (until ' + new Date(s.away_until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ')';
    if (s.away && s.away_note) text += ' - ' + s.away_note;
    setText('awayState', text);
  }
  $('awayOn').addEventListener('click', function () {
    var minutes = parseInt($('awayMinutes').value, 10);
    var body = { enabled: true, note: $('awayNote').value.trim() };
    if (minutes > 0) body.minutes = minutes;
    api('POST', '/api/away', body).then(function (d) { setText('awayMsg', 'Away mode started. Astra will now reply automatically.'); refresh(); loadSettings(); })
      .catch(function (err) { setText('awayMsg', 'Could not start: ' + err.message); });
  });
  $('awayOff').addEventListener('click', function () {
    api('POST', '/api/away', { enabled: false }).then(function (d) { setText('awayMsg', (d.message || 'Welcome back.').replace(/[*]/g, '')); refresh(); loadSettings(); })
      .catch(function (err) { setText('awayMsg', 'Could not stop: ' + err.message); });
  });

  // ------------------------------------------------------------ # commands reference
  function loadCommands() {
    api('GET', '/api/commands').then(function (d) {
      var box = $('commandList'); box.textContent = '';
      d.sections.forEach(function (sec) {
        var det = document.createElement('details');
        var sum = document.createElement('summary'); sum.textContent = sec.icon + ' ' + sec.title + ' (' + sec.commands.length + ')';
        det.appendChild(sum);
        sec.commands.forEach(function (c) {
          var row = el('div', 'item');
          row.appendChild(el('div', 'who cmd-usage', c.usage));
          row.appendChild(el('div', 'meta', c.summary));
          det.appendChild(row);
        });
        box.appendChild(det);
      });
    }).catch(function (err) { setText('commandList', 'Could not load commands: ' + err.message); });
  }

  // ------------------------------------------------------------ logs
  function refreshLogs() {
    setText('logsMsg', 'Loading logs...');
    return api('GET', '/api/logs').then(function (d) {
      var out = [];
      out.push('=== DASHBOARD LOG ==='); out = out.concat(d.dashboard || []);
      out.push('', '=== WHATSAPP BRIDGE LOG ==='); out = out.concat(d.bridge || []);
      out.push('', '=== RECENT EVENTS ===');
      (d.events || []).forEach(function (e) { out.push(fmtTime(e.ts) + ' [' + e.level + '] [' + e.source + '] ' + e.message); });
      setText('logsOutput', out.join('\n'));
      setText('logsMsg', 'Logs refreshed.');
    }).catch(function (err) { setText('logsMsg', 'Could not load logs: ' + err.message); });
  }
  $('refreshLogs').addEventListener('click', refreshLogs);
  $('copyLogs').addEventListener('click', function () {
    var text = $('logsOutput').textContent;
    var fallback = function () { var area = document.createElement('textarea'); area.value = text; document.body.appendChild(area); area.select(); var ok = false; try { ok = document.execCommand('copy'); } catch (e) {} document.body.removeChild(area); setText('logsMsg', ok ? 'Logs copied.' : 'Select and copy the log text manually.'); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { setText('logsMsg', 'Logs copied.'); }, fallback); else fallback();
  });
  $('clearLogs').addEventListener('click', function () {
    if (!window.confirm('Clear dashboard logs, bridge logs, and recent event history? This cannot be undone.')) return;
    api('POST', '/api/logs/clear', { target: 'all' }).then(function () { setText('logsOutput', 'Logs cleared.'); setText('logsMsg', 'Logs cleared.'); refreshLogs(); }).catch(function (err) { setText('logsMsg', 'Could not clear logs: ' + err.message); });
  });


  // ------------------------------------------------------------ contact profile (style, notes, learned memory)
  var profileJid = '';
  function openProfile(c) {
    profileJid = c.jid;
    setText('profileWho', c.name || niceJid(c.jid));
    setText('profMsg', 'Loading...');
    show('profileCard', true);
    api('GET', '/api/contacts/profile?jid=' + encodeURIComponent(c.jid)).then(function (d) {
      $('profStyle').value = d.profile.style || '';
      $('profNotes').value = d.profile.notes || '';
      $('profMemory').value = d.profile.memory || '';
      setText('profMsg', '');
      $('profileCard').scrollIntoView();
    }).catch(function (e) { setText('profMsg', e.message); });
  }
  $('profSave').addEventListener('click', function () {
    if (!profileJid) return;
    setText('profMsg', 'Saving...');
    api('POST', '/api/contacts/profile', { jid: profileJid, style: $('profStyle').value, notes: $('profNotes').value, memory: $('profMemory').value })
      .then(function () { setText('profMsg', 'Saved. Astra will use this the next time this person writes.'); cache.contacts = ''; refresh(); })
      .catch(function (e) { setText('profMsg', e.message); });
  });
  $('profClearMemory').addEventListener('click', function () {
    if (!profileJid) return;
    api('POST', '/api/contacts/profile', { jid: profileJid, memory: '' })
      .then(function () { $('profMemory').value = ''; setText('profMsg', 'Learned memory cleared.'); cache.contacts = ''; refresh(); })
      .catch(function (e) { setText('profMsg', e.message); });
  });
  $('profClose').addEventListener('click', function () { profileJid = ''; show('profileCard', false); });

  // ------------------------------------------------------------ accounts (several WhatsApp numbers)
  function renderAccounts(list) {
    var box = $('accountList');
    box.textContent = '';
    list.forEach(function (a) {
      var item = el('div', 'item');
      item.appendChild(el('div', 'who', a.name + (a.current ? '  (this dashboard)' : '')));
      var state = a.running ? ('running - WhatsApp: ' + (a.current ? 'see the connection card above' : a.whatsapp)) : 'stopped';
      item.appendChild(el('div', 'meta', state + (a.owner_phone ? ' - owner ' + a.owner_phone : '') + (a.has_ai_key ? '' : ' - no AI key yet')));
      if (a.url) {
        var link = el('a', '', a.current ? a.url : 'Open ' + a.url);
        link.href = a.url; link.target = '_blank'; link.rel = 'noopener';
        var line = el('div', 'meta'); line.appendChild(link); item.appendChild(line);
      }
      if (!a.current) {
        var b = el('button', a.running ? 'danger' : 'primary', a.running ? 'Stop' : 'Start');
        b.addEventListener('click', function () {
          b.disabled = true; setText('accMsg', (a.running ? 'Stopping ' : 'Starting ') + a.name + '...');
          api('POST', a.running ? '/api/accounts/stop' : '/api/accounts/start', { name: a.name })
            .then(function () { setText('accMsg', 'Done.'); loadAccounts(true); })
            .catch(function (e) { setText('accMsg', e.message); b.disabled = false; });
        });
        item.appendChild(b);
      }
      box.appendChild(item);
    });
  }
  function loadAccounts(force) {
    return api('GET', '/api/accounts').then(function (d) {
      if (force) cache.accounts = '';
      renderIfChanged('accounts', d.accounts, '', renderAccounts);
    }).catch(function (e) { setText('accMsg', e.message); });
  }
  $('accAdd').addEventListener('click', function () {
    var name = $('accName').value.trim().toLowerCase();
    if (!name) { setText('accMsg', 'Type a name for the new account first.'); return; }
    $('accAdd').disabled = true; setText('accMsg', 'Creating ' + name + ' and starting it. This can take up to a minute...');
    api('POST', '/api/accounts/add', { name: name }).then(function (d) {
      $('accName').value = '';
      setText('accMsg', 'Account ' + name + ' is running. Open ' + (d.account && d.account.url ? d.account.url : 'its dashboard') + ', add its AI key and pair its number.');
      $('accAdd').disabled = false; loadAccounts(true);
    }).catch(function (e) { setText('accMsg', e.message); $('accAdd').disabled = false; });
  });

  // ------------------------------------------------------------ start
  buildSwatches();
  setKind('message');
  setWhen(Date.now() + 3600 * 1000);
  try { setText('tzName', Intl.DateTimeFormat().resolvedOptions().timeZone || 'your time zone'); } catch (e) { /* keep default */ }
  setupTabs();
  loadCommands();
  loadAccounts(false);
  setInterval(function () { if (!document.hidden) loadAccounts(false); }, 15000);
  loadSettings().then(function () { return api('GET', '/api/settings'); }).then(function (d) { if (d.gemini_configured) loadModels(); }).catch(function (err) { setText('settingsMsg', err.message); });
  refresh();
  setInterval(function () { if (!document.hidden) refresh(); }, 4000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();
