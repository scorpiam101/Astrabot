'use strict';
/*
 * WhatsApp bridge (Baileys 7.x by default; 6.x still works).
 * - Listens on 127.0.0.1 only.
 * - Pairs with a phone-number pairing code (no QR code is ever stored or shown).
 * - Forwards incoming private text and voice messages to the Flask dashboard.
 * - Forwards # commands you type in your own chat ("Message yourself") so you can control Astra from WhatsApp.
 * - Sends messages only when the dashboard asks, to validated recipients.
 * It never deletes authentication data and never logs codes, keys or message text.
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..');
const ENV_FILE = process.env.WAI_ENV_FILE || path.join(ROOT, '.env');
const AUTH_DIR = process.env.WAI_AUTH_DIR || path.join(ROOT, 'data', 'wa_auth');

function loadEnv(file) {
  const out = {};
  let text = '';
  try { text = fs.readFileSync(file, 'utf8'); } catch (e) { return out; }
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*#/.test(line)) continue;
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!m) continue;
    let v = m[2];
    if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v[v.length - 1] === v[0]) v = v.slice(1, -1);
    out[m[1]] = v;
  }
  return out;
}

const ENV = loadEnv(ENV_FILE);
const BRIDGE_PORT = parseInt(ENV.BRIDGE_PORT || '5001', 10);
const FLASK_PORT = parseInt(ENV.FLASK_PORT || '5000', 10);
const BRIDGE_TOKEN = ENV.BRIDGE_TOKEN || '';
const JID_RE = /^[0-9]{5,20}@(s\.whatsapp\.net|lid)$/;
const PHONE_RE = /^[0-9]{8,15}$/;
const MAX_TEXT = 4000;
const MAX_STATUS_TEXT = 700;
const COLOR_RE = /^#[0-9a-fA-F]{6}$/;
const STATUS_JID = 'status@broadcast';
const MAX_AUDIENCE = 1000;
const BROWSER_CHOICE = (ENV.WA_BROWSER || 'ubuntu').toLowerCase();

function log(level, message) {
  const stamp = new Date().toISOString().replace('T', ' ').slice(0, 19);
  console.log('[' + stamp + '] ' + level.toUpperCase() + ' ' + message);
}

if (!BRIDGE_TOKEN || BRIDGE_TOKEN.length < 16) {
  log('error', 'BRIDGE_TOKEN is missing or too short in .env. Re-run the installer.');
  process.exit(1);
}

// Baileys 6.x is CommonJS and 7.x is ESM-only, so it is loaded with import(), which handles both.
let makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, fetchLatestWaWebVersion, Browsers, downloadMediaMessage;
let baileysVersion = 'unknown';

async function loadBaileys() {
  if (makeWASocket) return;
  const mod = await import('@whiskeysockets/baileys');
  const cjs = mod.default && typeof mod.default === 'object' ? mod.default : null;
  const pick = (name) => (mod[name] !== undefined ? mod[name] : cjs ? cjs[name] : undefined);
  const factory = typeof mod.default === 'function'
    ? mod.default
    : (cjs && typeof cjs.default === 'function' ? cjs.default : pick('makeWASocket'));
  const missing = [];
  if (typeof factory !== 'function') missing.push('makeWASocket');
  if (typeof pick('useMultiFileAuthState') !== 'function') missing.push('useMultiFileAuthState');
  if (!pick('DisconnectReason')) missing.push('DisconnectReason');
  if (!pick('Browsers')) missing.push('Browsers');
  if (missing.length) throw new Error('Baileys loaded but is missing: ' + missing.join(', '));
  makeWASocket = factory;
  useMultiFileAuthState = pick('useMultiFileAuthState');
  DisconnectReason = pick('DisconnectReason');
  Browsers = pick('Browsers');
  fetchLatestBaileysVersion = pick('fetchLatestBaileysVersion');
  fetchLatestWaWebVersion = pick('fetchLatestWaWebVersion');
  downloadMediaMessage = pick('downloadMediaMessage');
  try {
    const pkgPath = path.join(__dirname, 'node_modules', '@whiskeysockets', 'baileys', 'package.json');
    baileysVersion = JSON.parse(fs.readFileSync(pkgPath, 'utf8')).version;
  } catch (e) { /* version only used for the log line */ }
  log('info', 'Baileys ' + baileysVersion + ' loaded');
}
const pino = require('pino');
const logger = pino({ level: 'silent' });

function pickBrowser() {
  // How this linked device presents itself to WhatsApp. Change with ./set_browser.sh
  switch (BROWSER_CHOICE) {
    case 'macos': return Browsers.macOS('Chrome');
    case 'safari': return Browsers.macOS('Safari');
    case 'windows': return Browsers.windows('Chrome');
    default: return Browsers.ubuntu('Chrome');
  }
}

// ---------------------------------------------------------------- state
const S = {
  state: 'starting', // starting | connecting | awaiting_pairing | connected | disconnected | logged_out
  lastError: null,
  connectedSince: null,
  registered: false,
  pairedPhone: null,
};
let sock = null;
let generation = 0;
let reconnectTimer = null;
let backoffMs = 2000;
let starting = false;
let pairingInFlight = false;
let pairingTouched = false;
let lastPairAt = 0;
const sendTimes = [];
const syncedContacts = new Map();
const sentIds = new Set(); // ids of messages this bridge sent, so echoes are never treated as commands

function rememberSent(id) {
  sentIds.add(id);
  if (sentIds.size > 400) sentIds.delete(sentIds.values().next().value);
}

// A session is really linked only after WhatsApp sent the signed account data (pair-success).
// Note: requesting a pairing code already stores a provisional "me", so "me" alone proves nothing.
function isLinked(creds) {
  return !!(creds && creds.account && creds.me && creds.me.id);
}

function setState(next, error) {
  if (S.state !== next) log('info', 'WhatsApp state: ' + S.state + ' -> ' + next);
  S.state = next;
  if (error !== undefined) S.lastError = error;
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// ---------------------------------------------------------------- own identity
function bareDigits(jid) {
  return String(jid || '').split('@')[0].split(':')[0].replace(/[^0-9]/g, '');
}

function ownDigits() {
  const u = sock && sock.user ? sock.user : null;
  const out = new Set();
  if (u) {
    for (const k of [u.id, u.lid, u.phoneNumber, u.jid]) { const d = bareDigits(k); if (d) out.add(d); }
  }
  if (S.pairedPhone) out.add(S.pairedPhone);
  return out;
}

// Baileys 7 can identify an account by phone number (PN) or by LID; accept both forms.
function isOwnChat(...jids) {
  const mine = ownDigits();
  for (const j of jids) {
    if (!j || !(String(j).endsWith('@s.whatsapp.net') || String(j).endsWith('@lid'))) continue;
    if (mine.has(bareDigits(j))) return true;
  }
  return false;
}

function phoneFromUser(u) {
  if (!u) return null;
  for (const k of [u.id, u.phoneNumber, u.jid]) {
    const v = String(k || '').replace(/:\d+@/, '@');
    if (v.endsWith('@s.whatsapp.net')) return v.split('@')[0].replace(/\D/g, '') || null;
  }
  return null;
}

// ---------------------------------------------------------------- forwarding
async function forward(payload) {
  const url = 'http://127.0.0.1:' + FLASK_PORT + '/internal/incoming';
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-bridge-token': BRIDGE_TOKEN },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(10000),
      });
      if (res.ok) return true;
      if (res.status >= 400 && res.status < 500) {
        log('warn', 'Dashboard rejected a message (HTTP ' + res.status + ').');
        return false;
      }
    } catch (e) { /* dashboard not reachable yet; retry */ }
    await sleep(2000 * attempt);
  }
  log('error', 'Could not deliver an incoming message to the dashboard after 4 tries.');
  return false;
}

function extractText(message) {
  if (!message) return '';
  const inner = message.ephemeralMessage?.message || message.viewOnceMessage?.message || message;
  return (
    inner.conversation ||
    inner.extendedTextMessage?.text ||
    inner.imageMessage?.caption ||
    inner.videoMessage?.caption ||
    ''
  );
}

function toSeconds(ts) {
  if (ts === undefined || ts === null) return Math.floor(Date.now() / 1000);
  if (typeof ts === 'object' && typeof ts.toNumber === 'function') return ts.toNumber();
  return Number(ts) || Math.floor(Date.now() / 1000);
}

const COMMAND_MAX_AGE_SECONDS = 120; // older "# commands" (history replay after reconnecting) are never run

async function onMessages(upsert) {
  if (upsert.type !== 'notify' && upsert.type !== 'append') return;
  for (const m of upsert.messages || []) {
    try {
      if (!m || !m.key || !m.message) continue;
      if (m.key.id && sentIds.has(m.key.id)) continue;
      const remote = m.key.remoteJid || '';
      const alt = m.key.remoteJidAlt || '';
      const text = extractText(m.message).trim();
      const ts = toSeconds(m.messageTimestamp);

      // 1) Messages you wrote yourself: only "# commands" in your own chat count.
      if (m.key.fromMe) {
        if (!text.startsWith('#')) continue;
        if (!isOwnChat(remote, alt)) continue;
        if (Math.floor(Date.now() / 1000) - ts > COMMAND_MAX_AGE_SECONDS) continue;
        const replyJid = JID_RE.test(remote) ? remote : (JID_RE.test(alt) ? alt : ownJid());
        if (!replyJid || !JID_RE.test(replyJid)) continue;
        await forward({
          wa_message_id: m.key.id,
          jid: replyJid,
          name: '',
          text: text.slice(0, MAX_TEXT),
          timestamp: ts,
          self_chat: true,
        });
        continue;
      }

      // 2) Messages from other people. History sync ("append") is never answered.
      if (upsert.type !== 'notify') continue;
      let jid = remote;
      if (jid.endsWith('@lid') && alt.endsWith('@s.whatsapp.net') && JID_RE.test(alt)) jid = alt;
      if (!JID_RE.test(jid)) continue; // ignores groups, broadcasts, status, channels
      if (isOwnChat(jid)) continue;

      let audioBase64 = '';
      let mimeType = 'audio/ogg';
      let finalText = text;
      const inner = m.message.ephemeralMessage?.message || m.message.viewOnceMessage?.message || m.message;
      const audio = inner.audioMessage || (inner.documentMessage && /^audio\//i.test(inner.documentMessage.mimetype || '') ? inner.documentMessage : null);
      if (audio) {
        if (typeof downloadMediaMessage !== 'function') {
          log('warn', 'Audio message skipped: Baileys media downloader is unavailable.');
          continue;
        }
        try {
          const buffer = await downloadMediaMessage(m, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          if (!buffer || !buffer.length || buffer.length > 13 * 1024 * 1024) {
            log('warn', 'Audio message skipped: empty or larger than 13 MB.');
            continue;
          }
          audioBase64 = buffer.toString('base64');
          mimeType = String(audio.mimetype || 'audio/ogg').split(';')[0].toLowerCase();
          finalText = '🎤 WhatsApp voice message (transcription pending)';
        } catch (e) {
          log('error', 'Could not download voice message: ' + (e && e.message));
          continue;
        }
      }
      if (!finalText && !audioBase64) continue;
      await forward({
        wa_message_id: m.key.id,
        jid,
        name: m.pushName || '',
        text: finalText.slice(0, MAX_TEXT),
        audio_base64: audioBase64,
        mime_type: mimeType,
        timestamp: ts,
      });
    } catch (e) {
      log('error', 'Failed to handle an incoming message: ' + (e && e.message));
    }
  }
}

// ---------------------------------------------------------------- socket
function archiveUnlinkedAuth() {
  try {
    const backups = path.join(path.dirname(AUTH_DIR), 'backups');
    fs.mkdirSync(backups, { recursive: true, mode: 0o700 });
    const target = path.join(backups, 'wa_auth-unlinked-' + Date.now());
    fs.renameSync(AUTH_DIR, target);
    fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 });
    log('info', 'Unfinished pairing files moved to ' + path.basename(target));
  } catch (e) {
    log('error', 'Could not archive unfinished pairing files: ' + (e && e.message));
  }
  pairingTouched = false;
}

function scheduleReconnect(minDelay) {
  if (reconnectTimer) return;
  const delay = Math.max(backoffMs, minDelay || 0);
  log('info', 'Reconnecting in ' + Math.round(delay / 1000) + 's');
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    startSocket().catch((e) => {
      setState('disconnected', 'Startup failed: ' + (e && e.message));
      log('error', 'Reconnect failed: ' + (e && e.message));
      scheduleReconnect(0);
    });
  }, delay);
  backoffMs = Math.min(backoffMs * 2, 60000);
}

async function startSocket() {
  if (starting) return;
  starting = true;
  try {
    if (S.state === 'logged_out') return;
    await loadBaileys();
    const myGen = ++generation;
    if (sock) { try { sock.end(undefined); } catch (e) { /* already closed */ } }
    fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 });
    try { fs.chmodSync(AUTH_DIR, 0o700); } catch (e) { /* ignore */ }
    let { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    if (!isLinked(state.creds) && state.creds.me) {
      // Leftovers of an earlier unfinished pairing attempt: WhatsApp rejects these on reconnect.
      archiveUnlinkedAuth();
      ({ state, saveCreds } = await useMultiFileAuthState(AUTH_DIR));
    }
    S.registered = isLinked(state.creds);

    const config = {
      auth: state,
      logger,
      browser: pickBrowser(),
      markOnlineOnConnect: false,
      syncFullHistory: false,
      generateHighQualityLinkPreview: false,
    };
    let versionSource = 'bundled with Baileys';
    if (typeof fetchLatestWaWebVersion === 'function') {
      try {
        const live = await withTimeout(fetchLatestWaWebVersion({}), 10000, 'version lookup timed out');
        if (live && Array.isArray(live.version) && !live.error) {
          config.version = live.version;
          versionSource = 'live from WhatsApp Web';
        }
      } catch (e) { /* try the next source */ }
    }
    if (!config.version && typeof fetchLatestBaileysVersion === 'function') {
      try {
        const latest = await withTimeout(fetchLatestBaileysVersion(), 8000, 'version lookup timed out');
        if (latest && Array.isArray(latest.version) && !latest.error) {
          config.version = latest.version;
          versionSource = 'Baileys version list';
        }
      } catch (e) { /* use the version bundled with Baileys */ }
    }
    log('info', 'WhatsApp protocol version: ' + (config.version ? config.version.join('.') : 'default') + ' (' + versionSource + ')');

    setState('connecting');
    const s = makeWASocket(config);
    sock = s;

    s.ev.on('creds.update', async () => {
      if (myGen !== generation) return;
      try {
        await saveCreds();
        try { fs.chmodSync(AUTH_DIR, 0o700); } catch (e) { /* ignore */ }
      } catch (e) { log('error', 'Could not save credentials: ' + (e && e.message)); }
    });

    s.ev.on('connection.update', (update) => {
      if (myGen !== generation) return;
      const { connection, lastDisconnect, qr } = update;
      if (qr && !isLinked(s.authState.creds)) {
        // The QR payload is deliberately discarded. Its arrival only tells us the
        // socket is ready, so a phone-number pairing code may now be requested.
        setState('awaiting_pairing', null);
      }
      if (connection === 'connecting' && S.state !== 'awaiting_pairing') setState('connecting');
      if (connection === 'open') {
        S.registered = true;
        S.pairedPhone = phoneFromUser(s.user);
        S.connectedSince = Math.floor(Date.now() / 1000);
        backoffMs = 2000;
        setState('connected', null);
      }
      if (connection === 'close') {
        S.connectedSince = null;
        const code = lastDisconnect?.error?.output?.statusCode;
        if (!isLinked(s.authState.creds) && (code === DisconnectReason.loggedOut || pairingTouched)) {
          // Never linked: only leftovers of an unfinished pairing attempt. Nothing valuable is lost;
          // the leftovers are moved to a backup folder and pairing starts clean.
          archiveUnlinkedAuth();
          backoffMs = 2000;
          setState('disconnected', 'The pairing attempt expired. Starting a fresh pairing session - request a new code when it says Ready.');
          scheduleReconnect(0);
          return;
        }
        if (code === DisconnectReason.loggedOut) {
          setState('logged_out',
            'WhatsApp logged this device out. Run ./reset_whatsapp_session.sh and pair again.');
          return;
        }
        const reason = String((lastDisconnect && lastDisconnect.error && lastDisconnect.error.message) || 'no reason given').slice(0, 120);
        log('warn', 'Connection closed by WhatsApp or network: code=' + (code === undefined ? 'unknown' : code) + ' reason="' + reason + '"');
        let note = 'Connection closed (code ' + (code === undefined ? 'unknown' : code) + ').';
        let minDelay = 0;
        if (code === DisconnectReason.restartRequired) {
          backoffMs = 1000; // normal right after pairing
          note = 'Restarting connection after pairing.';
        } else if (code === DisconnectReason.connectionReplaced) {
          note = 'Another session replaced this one. Reconnecting slowly.';
          minDelay = 30000;
        } else if (code === 405) {
          note = 'WhatsApp rejected the connection (405). The WhatsApp protocol version is probably out of date - run the installer again to update Baileys.';
          minDelay = 15000;
        } else if (code === DisconnectReason.timedOut && !isLinked(s.authState.creds)) {
          backoffMs = 2000; // idle pairing screen timed out; just open a new one
          note = 'Pairing screen timed out. Opening a new one.';
        } else if (code === DisconnectReason.badSession) {
          note = 'WhatsApp reports a bad session. If this repeats, run ./reset_whatsapp_session.sh.';
        }
        setState('disconnected', note);
        scheduleReconnect(minDelay);
      }
    });

    s.ev.on('contacts.upsert', (items) => {
      if (myGen !== generation || !Array.isArray(items)) return;
      for (const c of items) {
        const jid = String(c.id || c.jid || '');
        if (/^[0-9]{5,20}@(s\.whatsapp\.net|lid)$/.test(jid)) {
          syncedContacts.set(jid, { jid, name: String(c.name || c.notify || c.verifiedName || '').slice(0, 120), notify: String(c.notify || '').slice(0, 120) });
        }
      }
    });
    s.ev.on('contacts.update', (items) => {
      if (myGen !== generation || !Array.isArray(items)) return;
      for (const c of items) {
        const jid = String(c.id || c.jid || '');
        if (/^[0-9]{5,20}@(s\.whatsapp\.net|lid)$/.test(jid)) {
          const old = syncedContacts.get(jid) || { jid, name: '', notify: '' };
          syncedContacts.set(jid, { jid, name: String(c.name || old.name || c.notify || '').slice(0, 120), notify: String(c.notify || old.notify || '').slice(0, 120) });
        }
      }
    });

    s.ev.on('messages.upsert', (upsert) => {
      if (myGen !== generation) return;
      onMessages(upsert).catch((e) => log('error', 'messages handler: ' + (e && e.message)));
    });
  } finally {
    starting = false;
  }
}

// ---------------------------------------------------------------- actions
class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function requestPairing(phone) {
  if (!PHONE_RE.test(phone)) throw new HttpError(400, 'Phone number must be 8 to 15 digits with country code.');
  if (S.state === 'logged_out') throw new HttpError(409, 'This session was logged out. Run ./reset_whatsapp_session.sh first.');
  if (S.state === 'connected') throw new HttpError(409, 'WhatsApp is already connected.');
  if (!sock || isLinked(sock.authState.creds)) throw new HttpError(409, 'This session is already linked. Wait for it to connect.');
  if (S.state !== 'awaiting_pairing') throw new HttpError(409, 'WhatsApp is not ready for pairing yet (' + S.state + '). Wait a few seconds and try again.');
  if (pairingInFlight) throw new HttpError(409, 'A pairing request is already in progress.');
  const wait = 20000 - (Date.now() - lastPairAt);
  if (wait > 0) throw new HttpError(429, 'Please wait ' + Math.ceil(wait / 1000) + ' seconds before requesting a new code.');
  pairingInFlight = true;
  try {
    pairingTouched = true;
    log('info', 'Pairing code requested (browser identity: ' + BROWSER_CHOICE + ', number length ' + phone.length + ')');
    const code = await withTimeout(sock.requestPairingCode(phone), 30000, 'WhatsApp did not answer the pairing request in time.');
    lastPairAt = Date.now();
    log('info', 'WhatsApp issued a pairing code. Enter it in WhatsApp now.');
    return code;
  } catch (e) {
    lastPairAt = Date.now();
    log('error', 'Pairing request failed: ' + (e && e.message ? e.message : 'unknown error'));
    throw new HttpError(502, 'Pairing failed: ' + (e && e.message ? e.message : 'unknown error'));
  } finally {
    pairingInFlight = false;
  }
}

async function sendText(jid, text) {
  if (!JID_RE.test(jid)) throw new HttpError(400, 'Invalid recipient.');
  if (typeof text !== 'string' || !text.trim() || text.length > MAX_TEXT) throw new HttpError(400, 'Invalid message text.');
  if (S.state !== 'connected' || !sock) throw new HttpError(503, 'WhatsApp is not connected (' + S.state + ').');
  const nowMs = Date.now();
  while (sendTimes.length && nowMs - sendTimes[0] > 60000) sendTimes.shift();
  if (sendTimes.length >= 20) throw new HttpError(429, 'Sending too fast. Wait a minute.');
  sendTimes.push(nowMs);
  let result;
  try {
    result = await withTimeout(sock.sendMessage(jid, { text }), 30000, 'WhatsApp did not confirm the message in time.');
  } catch (e) {
    throw new HttpError(502, 'WhatsApp could not send the message: ' + (e && e.message ? e.message : 'unknown error'));
  }
  const id = result && result.key && result.key.id;
  if (!id) throw new HttpError(502, 'WhatsApp did not return a message id, so sending is not confirmed.');
  rememberSent(id);
  return id;
}


// ---- statuses -------------------------------------------------------------
// A WhatsApp status is sent to an explicit audience list. The linked account's own number is always
// added so the post also shows up on the owner's own phone.
function ownJid() {
  const id = sock && sock.user && sock.user.id;
  if (!id) return null;
  return String(id).replace(/:\d+@/, '@');
}

function audienceList(jids) {
  if (!Array.isArray(jids) || jids.length > MAX_AUDIENCE) throw new HttpError(400, 'Invalid audience list.');
  const out = new Set();
  for (const j of jids) {
    if (typeof j !== 'string' || !JID_RE.test(j)) throw new HttpError(400, 'Invalid audience list.');
    out.add(j);
  }
  const me = ownJid();
  if (!me) throw new HttpError(503, 'WhatsApp is not connected (' + S.state + ').');
  out.add(me);
  return Array.from(out);
}

function takeSendSlot() {
  const nowMs = Date.now();
  while (sendTimes.length && nowMs - sendTimes[0] > 60000) sendTimes.shift();
  if (sendTimes.length >= 20) throw new HttpError(429, 'Sending too fast. Wait a minute.');
  sendTimes.push(nowMs);
}

async function postStatus(text, background, jids) {
  if (typeof text !== 'string' || !text.trim() || text.length > MAX_STATUS_TEXT) throw new HttpError(400, 'Status text must be 1 to ' + MAX_STATUS_TEXT + ' characters.');
  if (typeof background !== 'string' || !COLOR_RE.test(background)) throw new HttpError(400, 'Invalid status colour.');
  if (S.state !== 'connected' || !sock) throw new HttpError(503, 'WhatsApp is not connected (' + S.state + ').');
  const list = audienceList(jids);
  takeSendSlot();
  let result;
  try {
    result = await withTimeout(
      sock.sendMessage(STATUS_JID, { text, backgroundColor: background, font: 2 }, { statusJidList: list }),
      45000, 'WhatsApp did not confirm the status in time.');
  } catch (e) {
    throw new HttpError(502, 'WhatsApp could not post the status: ' + (e && e.message ? e.message : 'unknown error'));
  }
  const id = result && result.key && result.key.id;
  if (!id) throw new HttpError(502, 'WhatsApp did not return a status id, so posting is not confirmed.');
  log('info', 'Status posted for ' + list.length + ' recipients.');
  return { id, audience: list };
}

async function deleteStatus(messageId, jids) {
  if (typeof messageId !== 'string' || !/^[A-Za-z0-9_-]{6,64}$/.test(messageId)) throw new HttpError(400, 'Invalid status id.');
  if (S.state !== 'connected' || !sock) throw new HttpError(503, 'WhatsApp is not connected (' + S.state + ').');
  const list = audienceList(jids);
  takeSendSlot();
  const key = { remoteJid: STATUS_JID, fromMe: true, id: messageId };
  try {
    await withTimeout(sock.sendMessage(STATUS_JID, { delete: key }, { statusJidList: list }),
      45000, 'WhatsApp did not confirm the deletion in time.');
  } catch (e) {
    throw new HttpError(502, 'WhatsApp could not delete the status: ' + (e && e.message ? e.message : 'unknown error'));
  }
  log('info', 'Status deletion sent.');
}

// ---------------------------------------------------------------- http api
function tokenOk(req) {
  const supplied = Buffer.from(String(req.headers['x-bridge-token'] || ''));
  const expected = Buffer.from(BRIDGE_TOKEN);
  return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > 120000) { reject(new HttpError(413, 'Request too large.')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {}); }
      catch (e) { reject(new HttpError(400, 'Invalid JSON.')); }
    });
    req.on('error', reject);
  });
}

function reply(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data), 'Cache-Control': 'no-store' });
  res.end(data);
}

const server = http.createServer(async (req, res) => {
  try {
    const url = (req.url || '').split('?')[0];
    if (req.method === 'GET' && url === '/health') return reply(res, 200, { ok: true, service: 'bridge', uptime: Math.floor(process.uptime()) });
    if (!tokenOk(req)) return reply(res, 403, { ok: false, error: 'Forbidden.' });
    if (req.method === 'GET' && url === '/status') {
      return reply(res, 200, {
        ok: true,
        state: S.state,
        registered: S.registered,
        pairing_ready: S.state === 'awaiting_pairing' && !pairingInFlight,
        last_error: S.lastError,
        connected_since: S.connectedSince,
        paired_phone: S.pairedPhone,
        baileys: baileysVersion,
      });
    }
    if (req.method === 'GET' && url === '/contacts') {
      return reply(res, 200, { ok: true, contacts: Array.from(syncedContacts.values()).slice(0, 1000) });
    }
    if (req.method === 'POST' && url === '/pair') {
      const body = await readJson(req);
      const code = await requestPairing(String(body.phone || ''));
      return reply(res, 200, { ok: true, code });
    }
    if (req.method === 'POST' && url === '/send') {
      const body = await readJson(req);
      const id = await sendText(String(body.jid || ''), body.text);
      return reply(res, 200, { ok: true, message_id: id });
    }
    if (req.method === 'POST' && url === '/status/post') {
      const body = await readJson(req);
      const out = await postStatus(body.text, body.background, body.jids);
      return reply(res, 200, { ok: true, message_id: out.id, audience: out.audience });
    }
    if (req.method === 'POST' && url === '/status/delete') {
      const body = await readJson(req);
      await deleteStatus(body.message_id, body.jids);
      return reply(res, 200, { ok: true });
    }
    return reply(res, 404, { ok: false, error: 'Not found.' });
  } catch (e) {
    if (e instanceof HttpError) return reply(res, e.status, { ok: false, error: e.message });
    log('error', 'Request failed: ' + (e && e.message));
    return reply(res, 500, { ok: false, error: 'Internal bridge error.' });
  }
});

server.on('error', (e) => {
  log('error', 'Cannot listen on 127.0.0.1:' + BRIDGE_PORT + ' (' + e.code + '). Is another program using that port?');
  process.exit(1);
});

process.on('unhandledRejection', (e) => log('error', 'Unhandled rejection: ' + (e && e.message ? e.message : e)));
process.on('uncaughtException', (e) => log('error', 'Uncaught exception: ' + (e && e.message ? e.message : e)));

function shutdown() {
  log('info', 'Shutting down.');
  try { if (sock) sock.end(undefined); } catch (e) { /* ignore */ }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

server.listen(BRIDGE_PORT, '127.0.0.1', () => {
  log('info', 'Bridge listening on 127.0.0.1:' + BRIDGE_PORT);
  startSocket().catch((e) => {
    setState('disconnected', 'Startup failed: ' + (e && e.message));
    log('error', 'Startup failed: ' + (e && e.message));
    scheduleReconnect(0);
  });
});
