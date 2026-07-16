/* SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla */
/* SPDX-License-Identifier: MIT */
/* OmicsANG SPA — vanilla JS, no build step. Globals Terminal + FitAddon from local vendor assets. */
'use strict';

const EditorCore = window.OmicsEditorCore;

let csrfToken = '';

async function responseError(response) {
  const body = await response.text();
  let parsed = null;
  try {
    parsed = JSON.parse(body);
  } catch (e) {}
  const detail = parsed && (parsed.detail || parsed.error);
  const message = typeof detail === 'string' ? detail
    : detail && typeof detail.message === 'string' ? detail.message
      : body || `HTTP ${response.status}`;
  const error = new Error(message);
  error.status = response.status;
  error.detail = detail;
  error.payload = parsed;
  return error;
}

async function apiFetch(url, options = {}) {
  const target = new URL(url, window.location.href);
  if (target.origin !== window.location.origin || target.username || target.password)
    throw new Error('Cross-origin or credentialed API requests are not allowed');
  const method = String(options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  const apiRequest = target.pathname === '/api' || target.pathname.startsWith('/api/');
  if (apiRequest && options.csrf !== false) {
    if (!csrfToken) throw new Error('This page is not authenticated for changes. Relaunch OmicsANG from its private launch URL.');
    headers.set('X-Benchtop-CSRF', csrfToken);
  }
  const init = { ...options, method, headers, credentials: 'same-origin' };
  delete init.csrf;
  return fetch(target.href, init);
}

async function jsonResponse(response) {
  if (!response.ok) throw await responseError(response);
  return response.json();
}

const api = {
  response(url, options = {}) { return apiFetch(url, options); },
  async get(url) { return jsonResponse(await apiFetch(url)); },
  async post(url, body, options = {}) {
    return jsonResponse(await apiFetch(url, {
      ...options,
      method: 'POST',
      headers: { ...(options.headers || {}), 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }));
  },
  async text(url) {
    const response = await apiFetch(url);
    if (!response.ok) throw await responseError(response);
    return response.text();
  },
};

const UI_PREFS_KEY = 'omicsang.uiPrefs.v1';
const LEGACY_UI_PREFS_KEY = 'benchtop.uiPrefs.v1';
const LEGACY_CODE_NOTES_PREFIX = 'benchtop.codeNotes.v1';
const LEGACY_WORKSPACE_CODE_NOTES_PREFIX = 'benchtop.codeNotes.v2';
const CODE_NOTES_PREFIX = 'omicsang.codeNotes.v2';
const SUBMISSION_INTENTS_KEY = 'omicsang.submissionIntents.v1';
const LEGACY_SUBMISSION_INTENTS_KEY = 'benchtop.submissionIntents.v1';
const UI_FONTS = [
  ['system', 'System', "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"],
  ['segoe', 'Segoe UI', "'Segoe UI',Roboto,Arial,sans-serif"],
  ['inter', 'Inter', "Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif"],
  ['roboto', 'Roboto', "Roboto,'Helvetica Neue',Arial,sans-serif"],
  ['plex', 'IBM Plex Sans', "'IBM Plex Sans','Segoe UI',Arial,sans-serif"],
  ['source', 'Source Sans 3', "'Source Sans 3','Segoe UI',Arial,sans-serif"],
];
const CODE_FONTS = [
  ['jetbrains', 'JetBrains Mono', "'JetBrains Mono','SF Mono',ui-monospace,Menlo,Consolas,monospace"],
  ['cascadia', 'Cascadia Code', "'Cascadia Code','Cascadia Mono','SF Mono',Consolas,monospace"],
  ['fira', 'Fira Code', "'Fira Code','SF Mono',Consolas,monospace"],
  ['plexmono', 'IBM Plex Mono', "'IBM Plex Mono','SF Mono',Consolas,monospace"],
  ['sourcecode', 'Source Code Pro', "'Source Code Pro','SF Mono',Consolas,monospace"],
  ['sfmono', 'SF Mono', "'SF Mono',Menlo,Consolas,monospace"],
];

const clampNum = (v, min, max) => Math.min(max, Math.max(min, Number(v) || min));

function storageJsonObject(key) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch (e) { return null; }
}

function writeStorageObject(keys, value) {
  const serialized = JSON.stringify(value);
  for (const key of keys) {
    try { localStorage.setItem(key, serialized); }
    catch (e) {}
  }
}

function defaultUiPrefs() {
  return {
    uiFont: 'system',
    codeFont: 'jetbrains',
    uiFontSize: 14,
    codeFontSize: 13,
    terminalFontSize: 13,
    sidebarOpen: true,
    monitorOpen: true,
    codeSidebarOpen: true,
    codeAssistantOpen: true,
    terminalOpen: true,
    codeInlineCompletions: true,
    codeHoverHelp: true,
    sidebarWidth: 248,
    monitorWidth: 320,
    codeSidebarWidth: 320,
    codeAssistantWidth: 340,
    codeIndentGuides: true,
    terminalHeight: clampNum((window.innerHeight || 820) - 220, 360, 900),
  };
}

function loadUiPrefs() {
  const defaults = defaultUiPrefs();
  const current = storageJsonObject(UI_PREFS_KEY);
  const legacy = current === null ? storageJsonObject(LEGACY_UI_PREFS_KEY) : null;
  const preferences = sanitizeUiPrefs({ ...defaults, ...(current || legacy || {}) });
  if (current === null && legacy !== null) writeStorageObject([UI_PREFS_KEY], preferences);
  return preferences;
}

function sanitizeUiPrefs(p) {
  const defaults = defaultUiPrefs();
  const knownUi = new Set(UI_FONTS.map(f => f[0]));
  const knownCode = new Set(CODE_FONTS.map(f => f[0]));
  return {
    uiFont: knownUi.has(p.uiFont) ? p.uiFont : defaults.uiFont,
    codeFont: knownCode.has(p.codeFont) ? p.codeFont : defaults.codeFont,
    uiFontSize: clampNum(p.uiFontSize, 11, 18),
    codeFontSize: clampNum(p.codeFontSize, 11, 20),
    terminalFontSize: clampNum(p.terminalFontSize, 10, 22),
    sidebarOpen: p.sidebarOpen !== false,
    monitorOpen: p.monitorOpen !== false,
    codeSidebarOpen: p.codeSidebarOpen !== false,
    codeAssistantOpen: p.codeAssistantOpen !== false,
    terminalOpen: p.terminalOpen !== false,
    codeInlineCompletions: p.codeInlineCompletions !== false,
    codeHoverHelp: p.codeHoverHelp !== false,
    sidebarWidth: clampNum(p.sidebarWidth, 180, 460),
    monitorWidth: clampNum(p.monitorWidth, 260, 660),
    codeSidebarWidth: clampNum(p.codeSidebarWidth, 220, 560),
    codeAssistantWidth: clampNum(p.codeAssistantWidth, 260, 620),
    codeIndentGuides: p.codeIndentGuides !== false,
    terminalHeight: clampNum(p.terminalHeight, 260, 1000),
  };
}

function fontStack(list, key) {
  return (list.find(f => f[0] === key) || list[0])[2];
}

const state = {
  meta: null, pipelines: [], selected: null, detail: null,
  tab: 'overview', sessions: [], focusedSession: null, runForm: {},
  runQueue: null, runTemplates: {}, slurm: null, slurmForm: {}, health: null,
  dagForm: {}, dagResult: {}, view: 'pipeline', fleetActive: null, fleetForm: null,
  code: {}, commandOpen: false, commandQuery: '',
  fleetMode: 'health', pipelineHealth: {}, resultsFilter: {}, github: {},
  sraGeo: {}, study: {}, capsules: {}, browse: {}, viewSettingsOpen: false, uiPrefs: loadUiPrefs(),
  pipelineQuery: '', recentNavigation: [],
  navigation: { index: 0, maxIndex: 0, restoring: false, restoreToken: 0 },
  help: { open: false, data: null, loading: false, attempted: false, error: '', returnFocus: null },
};

const $ = (sel) => document.querySelector(sel);
const dock = $('#terminal-dock');
const monitorPanel = $('#monitor-panel');
const commandPalette = $('#command-palette');
const lightbox = $('#lightbox');
const viewSettings = $('#view-settings');
const helpDrawer = $('#help-drawer');
const helpBackdrop = $('#help-backdrop');
const terminals = new Map();   // sid -> { term, fit, ws, host, session, exited }

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'html') throw new Error('Unsafe HTML attributes are not supported');
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return e;
}
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString() : '—';
const shortPath = (p, n = 3) => (p || '').split('/').filter(Boolean).slice(-n).join('/') || p || '—';
const shortPlanDigest = (digest) => digest ? `RunPlan ${String(digest).replace(/^sha256:/, '').slice(0, 12)}` : 'RunPlan unavailable';
const isLiveStatus = (s) => ['created', 'queued', 'running'].includes(s || '');
const severityRank = (s) => ({ error: 3, failed: 3, warn: 2, warning: 2, queued: 1, running: 1, ok: 0, exited: 0 }[s] || 0);
const truncateText = (text, max = 12000) => {
  text = String(text || '');
  return text.length <= max ? text : `${text.slice(0, max)}\n\n[truncated ${text.length - max} characters]`;
};

function sameOriginUrl(raw) {
  try {
    const url = new URL(String(raw || ''), window.location.href);
    return url.origin === window.location.origin && !url.username && !url.password ? url.href : '';
  } catch (e) { return ''; }
}

function httpsUrl(raw) {
  try {
    const url = new URL(String(raw || ''));
    if (url.protocol !== 'https:' || url.username || url.password) return '';
    return url.href;
  } catch (e) { return ''; }
}

const OFFICIAL_DOC_HOSTS = new Set([
  'anndata.readthedocs.io', 'bedtools.readthedocs.io', 'bio-bwa.sourceforge.net', 'bioconductor.org',
  'biopython.org', 'bowtie-bio.sourceforge.net', 'cutadapt.readthedocs.io', 'd3js.org',
  'docs.conda.io', 'docs.pydantic.dev', 'docs.scipy.org', 'docs.seqera.io', 'dplyr.tidyverse.org',
  'expressjs.com', 'fastapi.tiangolo.com', 'ggplot2.tidyverse.org', 'github.com', 'lh3.github.io',
  'mamba.readthedocs.io', 'matplotlib.org', 'numpy.org', 'pachterlab.github.io', 'pandas.pydata.org',
  'pysam.readthedocs.io', 'pyyaml.org', 'rdatatable.gitlab.io', 'react.dev', 'salmon.readthedocs.io',
  'samtools.github.io', 'satijalab.org', 'scanpy.readthedocs.io', 'scikit-learn.org',
  'seaborn.pydata.org', 'snakemake.readthedocs.io', 'subread.sourceforge.net',
  'www.bioinformatics.babraham.ac.uk', 'www.htslib.org', 'xtermjs.org',
]);
const CONTEXTUAL_COMMANDS = Object.freeze(['fastqc']);

function officialDocsUrl(raw) {
  const safe = httpsUrl(raw);
  if (!safe) return null;
  const url = new URL(safe);
  const host = url.hostname.toLowerCase();
  if (!OFFICIAL_DOC_HOSTS.has(host)) return null;
  return { href: url.href, host };
}

async function authenticatedObjectUrl(raw) {
  const target = sameOriginUrl(raw);
  if (!target) throw new Error('Invalid same-origin file URL');
  const response = await apiFetch(target);
  if (!response.ok) throw await responseError(response);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  let active = true;
  const revoke = () => {
    if (!active) return;
    active = false;
    URL.revokeObjectURL(url);
  };
  setTimeout(revoke, 60000);
  return {
    url,
    revoke,
    disposition: response.headers.get('content-disposition') || '',
  };
}

async function loadAuthenticatedImage(image, raw) {
  try {
    const resource = await authenticatedObjectUrl(raw);
    if (!image.isConnected) {
      resource.revoke();
      return;
    }
    image.addEventListener('load', resource.revoke, { once: true });
    image.addEventListener('error', resource.revoke, { once: true });
    image.src = resource.url;
  } catch (e) {
    image.alt = `Could not load image: ${e.message}`;
    image.title = e.message;
  }
}

async function openAuthenticatedFile(raw, filename = '') {
  const pending = window.open('about:blank', '_blank');
  if (pending) pending.opener = null;
  try {
    const resource = await authenticatedObjectUrl(raw);
    const download = /^\s*attachment(?:;|$)/i.test(resource.disposition);
    if (!download && pending) {
      pending.location.replace(resource.url);
      return true;
    }
    if (pending) pending.close();
    const link = el('a', {
      href: resource.url,
      rel: 'noopener noreferrer',
      target: download ? null : '_blank',
      download: download ? (filename || 'download') : null,
    });
    document.body.append(link);
    link.click();
    link.remove();
    return true;
  } catch (e) {
    if (pending) pending.close();
    window.alert(`Could not open file:\n${e.message}`);
    return false;
  }
}

async function openAuthenticatedPostFile(raw, body, filename = '') {
  const pending = window.open('about:blank', '_blank');
  if (pending) pending.opener = null;
  try {
    const response = await apiFetch(raw, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) throw await responseError(response);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const disposition = response.headers.get('content-disposition') || '';
    const download = /^\s*attachment(?:;|$)/i.test(disposition);
    let active = true;
    const revoke = () => {
      if (!active) return;
      active = false;
      URL.revokeObjectURL(url);
    };
    setTimeout(revoke, 60000);
    if (!download && pending) {
      pending.location.replace(url);
      return true;
    }
    if (pending) pending.close();
    const link = el('a', { href: url, download: filename || 'download', rel: 'noopener noreferrer' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(revoke, 1000);
    return true;
  } catch (e) {
    if (pending) pending.close();
    window.alert(`Could not open file:\n${e.message}`);
    return false;
  }
}

function openExternalHttps(raw) {
  const url = httpsUrl(raw);
  if (!url) return false;
  const opened = window.open(url, '_blank', 'noopener,noreferrer');
  if (opened) opened.opener = null;
  return true;
}

function confirmAgentLaunch(tool, context = '') {
  const name = tool === 'claude' ? 'Claude Code' : tool === 'codex' ? 'Codex' : 'Shell';
  const permissions = tool === 'shell'
    ? 'The shell runs as your OS account and can read or change anything that account can access.'
    : `${name} is contained to this repository and its own provider configuration; your home directory and other pipelines are not visible to it.`;
  const transmission = tool === 'shell'
    ? 'OmicsANG does not send shell input to an agent provider; commands or shell startup configuration may use the network.'
    : 'The prepared prompt and repository content the CLI reads may be transmitted to its external provider under that tool\'s configuration and terms.';
  const environment = tool === 'shell'
    ? 'The shell receives the ordinary local environment.'
    : 'The agent receives a provider-specific environment allowlist, not unrelated server tokens or SSH/cloud credentials. This does not restrict filesystem access.';
  return window.confirm(
    `Review before launch\n\nOmicsANG has not launched ${name} or supplied it with context. ` +
    `Approving this confirmation will launch it.\n\nPermissions: ${permissions}\n\nEnvironment: ${environment}\n\nData: ${transmission}` +
    `${context ? `\n\nLaunch context: ${context}` : ''}\n\nLaunch now?`,
  );
}

function agentDisclosure(context = '') {
  return el('details', { class: 'agent-disclosure' },
    el('summary', {}, 'Agent access & privacy · approval required'),
    el('div', { class: 'agent-disclosure-body' },
      el('p', {}, 'Agents are optional. OmicsANG does not launch a tool or supply context until you choose an agent action and approve its confirmation.'),
      el('p', {}, 'After approval, OmicsANG contains the selected CLI to the repository it was launched against; your home directory and other pipelines are not visible to it. Containment does not limit the network: Claude Code and Codex may send the prepared prompt and repository content they read to their providers. OmicsANG does not send shell input to an agent provider; commands or shell startup configuration may use the network.'),
      el('p', {}, 'Agent CLIs receive a provider-specific environment allowlist rather than unrelated server tokens, SSH-agent sockets, or generic cloud credentials. Neither containment nor environment filtering limits what an agent may disclose from the repository it can read.'),
      context ? el('p', { class: 'muted' }, context) : null));
}

function submissionIntentTimestamp(intent) {
  const created = Number(intent && intent.created);
  return Number.isFinite(created) && created > 0 ? created : Number.MAX_SAFE_INTEGER;
}

function mergeSubmissionIntents(current, legacy) {
  const merged = {};
  const slots = new Set([...Object.keys(legacy || {}), ...Object.keys(current || {})]);
  for (const slot of slots) {
    const currentIntent = current && current[slot];
    const legacyIntent = legacy && legacy[slot];
    const currentValid = currentIntent && typeof currentIntent === 'object' && currentIntent.key;
    const legacyValid = legacyIntent && typeof legacyIntent === 'object' && legacyIntent.key;
    if (!currentValid && !legacyValid) continue;
    if (!currentValid) merged[slot] = legacyIntent;
    else if (!legacyValid) merged[slot] = currentIntent;
    else if (currentIntent.key === legacyIntent.key) merged[slot] = { ...legacyIntent, ...currentIntent };
    else merged[slot] = submissionIntentTimestamp(currentIntent) < submissionIntentTimestamp(legacyIntent)
      ? currentIntent : legacyIntent;
  }
  return merged;
}

function boundedSubmissionIntents(intents) {
  return Object.fromEntries(Object.entries(intents)
    .sort(([, a], [, b]) => (b.created || 0) - (a.created || 0)).slice(0, 40));
}

function persistSubmissionIntents(intents) {
  const bounded = boundedSubmissionIntents(intents);
  // Mirror the map for cached legacy tabs; both generations must reuse one key.
  writeStorageObject([SUBMISSION_INTENTS_KEY, LEGACY_SUBMISSION_INTENTS_KEY], bounded);
  return bounded;
}

function submissionIntents() {
  const current = storageJsonObject(SUBMISSION_INTENTS_KEY) || {};
  const legacy = storageJsonObject(LEGACY_SUBMISSION_INTENTS_KEY) || {};
  return persistSubmissionIntents(mergeSubmissionIntents(current, legacy));
}

function pendingSubmissionIntent(scope, digest) {
  const intents = submissionIntents();
  const slot = `${scope}:${digest}`;
  if (intents[slot] && intents[slot].key) return { slot, ...intents[slot] };
  const key = window.crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : `bt-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  intents[slot] = { key, digest, scope, created: Date.now() };
  const bounded = persistSubmissionIntents(intents);
  return { slot, ...bounded[slot] };
}

function clearSubmissionIntent(slot) {
  const intents = submissionIntents();
  delete intents[slot];
  persistSubmissionIntents(intents);
}

async function lookupSubmissionIntent(intent) {
  const response = await api.response(`/api/jobs/idempotency/${encodeURIComponent(intent.key)}`);
  if (response.status === 404) return null;
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function durableSubmissionJob(response) {
  return response && (response.durable_job || response.job || response);
}

function submissionIntentMustPersist(response) {
  const job = durableSubmissionJob(response) || {};
  return ['created', 'submitting', 'submission_unknown', 'cancel_requested'].includes(job.state || '');
}

function runPlanResolution(response) {
  return response && response.plan && response.plan.resolution
    ? response.plan.resolution
    : { launch_safe: false, issues: ['RunPlan resolution status is unavailable'] };
}

function explainUnsafeRunPlan(response) {
  const resolution = runPlanResolution(response);
  const issues = (resolution.issues || []).slice(0, 8);
  const more = Math.max(0, (resolution.issues || []).length - issues.length);
  return `${issues.join('\n')}${more ? `\n… and ${more} more` : ''}`;
}

async function inspectPendingSubmissionIntent(intent) {
  const job = await lookupSubmissionIntent(intent);
  if (!job) return { intent, job: null, active: false };
  const terminal = ['succeeded', 'failed', 'cancelled', 'blocked'].includes(job.state || '');
  if (terminal) {
    clearSubmissionIntent(intent.slot);
    return { intent: pendingSubmissionIntent(intent.scope, intent.digest), job, active: false };
  }
  return { intent, job, active: true };
}

function workspaceCodeNotesPrefixFor(storagePrefix) {
  const workspaceId = state.meta && typeof state.meta.workspace_id === 'string' ? state.meta.workspace_id : '';
  return workspaceId ? `${storagePrefix}:${workspaceId}:` : '';
}

function workspaceCodeNotesPrefix() {
  return workspaceCodeNotesPrefixFor(CODE_NOTES_PREFIX);
}

function legacyWorkspaceCodeNotesPrefix() {
  return workspaceCodeNotesPrefixFor(LEGACY_WORKSPACE_CODE_NOTES_PREFIX);
}

function codeNoteStorageKeyFor(prefix, pipeline, path) {
  if (!prefix) return '';
  return `${prefix}${encodeURIComponent(pipeline)}:${encodeURIComponent(path || '__pipeline__')}`;
}

function codeNoteStorageKey(pipeline, path) {
  return codeNoteStorageKeyFor(workspaceCodeNotesPrefix(), pipeline, path);
}

function legacyWorkspaceCodeNoteStorageKey(pipeline, path) {
  return codeNoteStorageKeyFor(legacyWorkspaceCodeNotesPrefix(), pipeline, path);
}

function loadCodeNote(pipeline, path) {
  const key = codeNoteStorageKey(pipeline, path);
  const legacyKey = legacyWorkspaceCodeNoteStorageKey(pipeline, path);
  if (!key) return '';
  try {
    const current = localStorage.getItem(key);
    if (current !== null) return current;
    const legacy = legacyKey ? localStorage.getItem(legacyKey) : null;
    if (legacy !== null) {
      localStorage.setItem(key, legacy);
      return legacy;
    }
    return '';
  } catch (e) { return ''; }
}

function saveCodeNote(pipeline, path, text) {
  const key = codeNoteStorageKey(pipeline, path);
  const legacyKey = legacyWorkspaceCodeNoteStorageKey(pipeline, path);
  if (!key) return;
  try {
    for (const storageKey of [key, legacyKey].filter(Boolean)) {
      if (text) localStorage.setItem(storageKey, text);
      else localStorage.removeItem(storageKey);
    }
  }
  catch (e) {}
}

function clearWorkspaceCodeNotes() {
  const prefixes = [workspaceCodeNotesPrefix(), legacyWorkspaceCodeNotesPrefix()].filter(Boolean);
  let removed = 0;
  try {
    for (let i = localStorage.length - 1; i >= 0; i -= 1) {
      const key = localStorage.key(i) || '';
      // v1 was unscoped, so it is clearable but never auto-loaded into a workspace.
      if (prefixes.some(prefix => key.startsWith(prefix)) || key.startsWith(`${LEGACY_CODE_NOTES_PREFIX}:`)) {
        localStorage.removeItem(key);
        removed += 1;
      }
    }
  } catch (e) {}
  return removed;
}

function saveUiPrefs() {
  writeStorageObject([UI_PREFS_KEY, LEGACY_UI_PREFS_KEY], state.uiPrefs);
}

function applyUiPrefs() {
  const p = sanitizeUiPrefs(state.uiPrefs);
  state.uiPrefs = p;
  const root = document.documentElement;
  root.style.setProperty('--sans', fontStack(UI_FONTS, p.uiFont));
  root.style.setProperty('--mono', fontStack(CODE_FONTS, p.codeFont));
  root.style.setProperty('--ui-font-size', `${p.uiFontSize}px`);
  root.style.setProperty('--control-font-size', `${Math.max(11, p.uiFontSize - 1)}px`);
  root.style.setProperty('--small-font-size', `${Math.max(10, p.uiFontSize - 2)}px`);
  root.style.setProperty('--tiny-font-size', `${Math.max(9, p.uiFontSize - 3)}px`);
  root.style.setProperty('--code-font-size', `${p.codeFontSize}px`);
  root.style.setProperty('--terminal-font-size', `${p.terminalFontSize}px`);
  root.style.setProperty('--sidebar-w', `${p.sidebarWidth}px`);
  root.style.setProperty('--monitor-w', `${p.monitorWidth}px`);
  root.style.setProperty('--code-sidebar-w', `${p.codeSidebarWidth}px`);
  root.style.setProperty('--code-assistant-w', `${p.codeAssistantWidth}px`);
  root.style.setProperty('--terminal-h', `${p.terminalHeight}px`);
  root.classList.toggle('code-indent-guides-off', !p.codeIndentGuides);
  applyPanelVisibility(p);
  document.querySelectorAll('.code-editor').forEach(node =>
    node.dispatchEvent(new Event('omicsang:editor-metrics')));
  for (const rec of terminals.values()) {
    rec.term.options.fontFamily = fontStack(CODE_FONTS, p.codeFont);
    rec.term.options.fontSize = p.terminalFontSize;
  }
  scheduleTerminalFit();
}

let fitTimer = null;
function scheduleTerminalFit() {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => {
    for (const [sid] of terminals) fitTerminal(sid);
  }, 30);
}

function setUiPref(key, value, opts = {}) {
  state.uiPrefs[key] = value;
  state.uiPrefs = sanitizeUiPrefs(state.uiPrefs);
  applyUiPrefs();
  saveUiPrefs();
  if (key === 'monitorOpen') renderMonitor();
  if (opts.renderSettings !== false) renderViewSettings();
}

function startPreferenceResize(e, key, min, max, axis = 'x', invert = false) {
  e.preventDefault();
  const start = Number(state.uiPrefs[key]) || min;
  const startPos = axis === 'y' ? e.clientY : e.clientX;
  document.body.classList.add('resizing');
  document.body.classList.toggle('resizing-y', axis === 'y');
  const move = (ev) => {
    const pos = axis === 'y' ? ev.clientY : ev.clientX;
    const delta = (pos - startPos) * (invert ? -1 : 1);
    setUiPref(key, clampNum(Math.round(start + delta), min, max), { renderSettings: false });
  };
  const up = () => {
    document.body.classList.remove('resizing');
    document.body.classList.remove('resizing-y');
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    renderViewSettings();
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up, { once: true });
}

function initResizeHandles() {
  const sidebar = $('#sidebar');
  if (sidebar && !sidebar.querySelector('.sidebar-resizer')) {
    sidebar.append(el('div', {
      class: 'resize-handle sidebar-resizer',
      title: 'Resize sidebar',
      onpointerdown: (e) => startPreferenceResize(e, 'sidebarWidth', 180, 460),
    }));
  }
}

function syncPanelRestoreDock(preferences = state.uiPrefs) {
  const restoreDock = $('#panel-restore-dock');
  if (!restoreDock) return;
  const p = sanitizeUiPrefs(preferences);
  const codeContext = state.view === 'pipeline' && state.tab === 'code' && !!state.detail;
  const terminalContext = state.view === 'pipeline' && state.tab === 'terminal' &&
    !!state.focusedSession && terminals.has(state.focusedSession);
  const codeId = codeContext
    ? String(state.detail.name || 'pipeline').replace(/[^A-Za-z0-9_-]/g, '-')
    : '';
  const terminalId = terminalContext
    ? String(state.focusedSession).replace(/[^A-Za-z0-9_-]/g, '-')
    : '';
  const restoreState = {
    sidebarOpen: { visible: !p.sidebarOpen, controls: 'sidebar' },
    monitorOpen: { visible: !p.monitorOpen, controls: 'monitor-panel' },
    codeSidebarOpen: { visible: codeContext && !p.codeSidebarOpen, controls: codeId ? `code-file-tree-${codeId}` : '' },
    codeAssistantOpen: { visible: codeContext && !p.codeAssistantOpen, controls: codeId ? `code-assistant-${codeId}` : '' },
    terminalOpen: { visible: terminalContext && !p.terminalOpen, controls: terminalId ? `terminal-stage-${terminalId}` : '' },
  };
  let visibleCount = 0;
  restoreDock.querySelectorAll('[data-panel-restore]').forEach((button) => {
    const key = button.getAttribute('data-panel-restore');
    const item = restoreState[key] || { visible: false, controls: '' };
    const label = button.getAttribute('data-panel-label') || 'panel';
    button.classList.toggle('hidden', !item.visible);
    button.setAttribute('aria-hidden', item.visible ? 'false' : 'true');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-label', `Show ${label}`);
    button.setAttribute('title', `Show ${label}`);
    if (item.controls) button.setAttribute('aria-controls', item.controls);
    else button.removeAttribute('aria-controls');
    if (item.visible) visibleCount += 1;
  });
  restoreDock.classList.toggle('hidden', visibleCount === 0);
  restoreDock.setAttribute('aria-hidden', visibleCount === 0 ? 'true' : 'false');
  document.body.classList.toggle('panel-restore-visible', visibleCount > 0);
}

function focusRestoredPanel(key) {
  if (key === 'terminalOpen' && state.focusedSession) {
    fitTerminal(state.focusedSession);
    return;
  }
  const target = {
    sidebarOpen: () => $('#pipeline-filter'),
    monitorOpen: () => monitorPanel,
    codeSidebarOpen: () => document.querySelector('.code-sidebar:not([aria-hidden="true"]) .code-list'),
    codeAssistantOpen: () => document.querySelector('.code-assistant:not([aria-hidden="true"]) .assistant-notes'),
  }[key];
  const element = target ? target() : null;
  if (element) element.focus({ preventScroll: true });
}

function applyPanelVisibility(preferences = state.uiPrefs) {
  const p = sanitizeUiPrefs(preferences);
  const sidebar = $('#sidebar');
  if (sidebar) {
    sidebar.classList.toggle('hidden', !p.sidebarOpen);
    sidebar.setAttribute('aria-hidden', p.sidebarOpen ? 'false' : 'true');
  }
  document.body.classList.toggle('monitor-open', !!p.monitorOpen);
  if (monitorPanel) {
    monitorPanel.classList.toggle('hidden', !p.monitorOpen);
    monitorPanel.setAttribute('aria-hidden', p.monitorOpen ? 'false' : 'true');
  }
  document.querySelectorAll('.code-workbench').forEach((workbench) => {
    workbench.classList.toggle('code-sidebar-collapsed', !p.codeSidebarOpen);
    workbench.classList.toggle('code-assistant-collapsed', !p.codeAssistantOpen);
    const codeSidebar = workbench.querySelector('.code-sidebar');
    const codeAssistant = workbench.querySelector('.code-assistant');
    if (codeSidebar) codeSidebar.setAttribute('aria-hidden', p.codeSidebarOpen ? 'false' : 'true');
    if (codeAssistant) codeAssistant.setAttribute('aria-hidden', p.codeAssistantOpen ? 'false' : 'true');
  });
  document.querySelectorAll('.terminal-stage').forEach((stage) => {
    stage.classList.toggle('hidden', !p.terminalOpen);
    stage.setAttribute('aria-hidden', p.terminalOpen ? 'false' : 'true');
  });
  document.querySelectorAll('[data-panel-pref]').forEach((button) => {
    const key = button.getAttribute('data-panel-pref');
    if (!Object.prototype.hasOwnProperty.call(p, key)) return;
    const open = !!p[key];
    const label = button.getAttribute('data-panel-label') || 'panel';
    button.classList.toggle('active', open);
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.setAttribute('title', `${open ? 'Hide' : 'Show'} ${label}`);
    button.setAttribute('aria-label', `${open ? 'Hide' : 'Show'} ${label}`);
    if (button.hasAttribute('data-dynamic-label')) {
      button.textContent = `${open ? 'Hide' : 'Show'} ${label}`;
    }
  });
  syncPanelRestoreDock(p);
}

function renderViewSettings() {
  if (!viewSettings) return;
  const toggle = $('#view-settings-toggle');
  if (toggle) toggle.classList.toggle('active', !!state.viewSettingsOpen);
  if (!state.viewSettingsOpen) {
    viewSettings.classList.add('hidden');
    viewSettings.innerHTML = '';
    return;
  }
  const p = state.uiPrefs;
  const selectControl = (label, key, options) => {
    const sel = el('select', {}, ...options.map(([id, name]) =>
      el('option', { value: id, selected: p[key] === id ? '' : null }, name)));
    sel.onchange = () => setUiPref(key, sel.value);
    return el('label', {}, label, sel);
  };
  const rangeControl = (label, key, min, max, step = 1) => {
    const range = el('input', { type: 'range', min, max, step, value: p[key] });
    const num = el('input', { type: 'number', min, max, step, value: p[key] });
    const sync = (value) => {
      const clean = clampNum(value, min, max);
      range.value = clean;
      num.value = clean;
      setUiPref(key, clean, { renderSettings: false });
    };
    range.oninput = () => sync(range.value);
    num.oninput = () => sync(num.value);
    return el('div', { class: 'view-setting-row' },
      el('label', {}, label),
      el('div', { class: 'range-pair' }, range, num));
  };
  const toggleControl = (label, key, detail) => {
    const input = el('input', { type: 'checkbox', checked: p[key] ? '' : null });
    input.onchange = () => setUiPref(key, input.checked);
    return el('label', { class: 'view-setting-toggle' },
      el('span', {}, label, detail ? el('small', {}, detail) : null), input);
  };
  viewSettings.innerHTML = '';
  viewSettings.classList.remove('hidden');
  viewSettings.append(
    el('div', { class: 'view-settings-head' },
      el('strong', {}, 'View'),
      el('button', { class: 'ghost sm icon-only', onclick: () => { state.viewSettingsOpen = false; renderViewSettings(); } }, 'x')),
    el('div', { class: 'view-settings-grid' },
      selectControl('Interface font', 'uiFont', UI_FONTS),
      selectControl('Code font', 'codeFont', CODE_FONTS),
      rangeControl('Interface size', 'uiFontSize', 11, 18),
      rangeControl('Code size', 'codeFontSize', 11, 20),
      rangeControl('Terminal size', 'terminalFontSize', 10, 22),
      toggleControl('Pipelines panel', 'sidebarOpen', 'Keep the registered-pipeline sidebar visible'),
      toggleControl('Run Monitor', 'monitorOpen', 'Keep the persistent run monitor visible'),
      toggleControl('Code file tree', 'codeSidebarOpen', 'Show the file tree beside the editor'),
      toggleControl('Code Assistant', 'codeAssistantOpen', 'Show the Assistant beside the editor'),
      toggleControl('Terminal', 'terminalOpen', 'Keep the live terminal canvas visible'),
      toggleControl('Inline completions', 'codeInlineCompletions', 'Suggest local in-file and language completions'),
      toggleControl('Line hover help', 'codeHoverHelp', 'Explain the hovered line locally'),
      rangeControl('Sidebar width', 'sidebarWidth', 180, 460),
      rangeControl('Monitor width', 'monitorWidth', 260, 660),
      rangeControl('Code tree width', 'codeSidebarWidth', 220, 560),
      rangeControl('Assistant width', 'codeAssistantWidth', 260, 620),
      toggleControl('Indent guides', 'codeIndentGuides', 'Show VS Code-style vertical scope rails'),
      rangeControl('Terminal height', 'terminalHeight', 260, 1000)),
    el('div', { class: 'toolbar view-settings-actions' },
      el('button', { class: 'ghost sm', onclick: () => {
        state.uiPrefs = defaultUiPrefs();
        applyUiPrefs();
        saveUiPrefs();
        renderMonitor();
        renderViewSettings();
      } }, 'Reset')));
}

function pipelineSessions(name) {
  return (state.sessions || []).filter(s => (s.meta && s.meta.pipeline) === name);
}

function pipelineQueueRows(name) {
  const q = state.runQueue || {};
  return [...(q.running || []), ...(q.queued || [])].filter(s => (s.meta && s.meta.pipeline) === name);
}

function latestRun(d) {
  return ((d && d.history) || []).find(h => h.kind === 'run') || ((d && d.history) || [])[0] || null;
}

function pipelineHealthSnapshot(name) {
  if (state.pipelineHealth[name]) return state.pipelineHealth[name];
  const rows = state.health && state.health.pipelines;
  return rows ? rows.find(p => p.name === name) || null : null;
}

function healthLabel(d) {
  const h = pipelineHealthSnapshot(d.name);
  const q = pipelineQueueRows(d.name);
  const last = latestRun(d);
  const diag = h && h.diagnostics;
  if (q.some(s => s.status === 'running')) return { level: 'running', label: 'Running', note: `${q.length} active or queued` };
  if (last && last.status === 'failed') return { level: 'error', label: 'Failed', note: 'last run failed' };
  if (diag && (diag.error || 0) > 0) return { level: 'error', label: 'Needs fix', note: `${diag.error} diagnostics errors` };
  if (diag && (diag.warning || 0) > 0) return { level: 'warn', label: 'Warnings', note: `${diag.warning} diagnostics warnings` };
  if (last && last.status) return { level: last.status === 'exited' ? 'ok' : last.status, label: last.status, note: fmtTime(last.ended || last.started) };
  return { level: 'warn', label: 'Not run', note: 'no run history' };
}

function nextAction(d) {
  const h = pipelineHealthSnapshot(d.name);
  const last = latestRun(d);
  const diag = h && h.diagnostics;
  if (last && last.status === 'failed') {
    return { label: 'Debug failed run', tab: 'terminal', run: () => debugWithClaude(last.id), tone: 'danger' };
  }
  if (diag && (diag.error || 0) > 0) return { label: 'Open diagnostics', tab: 'code', run: null, tone: 'warn' };
  if (!last) return { label: 'Prepare dry-run', tab: 'run', run: null, tone: 'primary' };
  if (h && h.result_freshness && h.result_freshness.latest) return { label: 'Inspect results', tab: 'results', run: null, tone: 'primary' };
  return { label: 'Run dry-run', tab: 'run', run: null, tone: 'primary' };
}

/* ---------------- boot ---------------- */
function bootstrapTokenFromFragment() {
  const takeCapturedToken = window.__benchtopTakeBootstrapToken;
  if (typeof takeCapturedToken === 'function') return takeCapturedToken();
  const fragment = window.location.hash.slice(1);
  window.history.replaceState(null, document.title, `${window.location.pathname}${window.location.search}`);
  if (!fragment) return '';
  const params = new URLSearchParams(fragment);
  const parameterToken = params.get('bootstrap') || params.get('token');
  if (parameterToken) return parameterToken;
  if (!fragment.includes('=')) {
    try { return decodeURIComponent(fragment); }
    catch (e) { return ''; }
  }
  return '';
}

async function bootstrapAuthentication() {
  const token = bootstrapTokenFromFragment();
  const result = token
    ? await api.post('/api/auth/bootstrap', { token }, { csrf: false })
    : await jsonResponse(await apiFetch('/api/auth/session', { csrf: false }));
  csrfToken = typeof result.csrf === 'string' ? result.csrf : '';
  if (!csrfToken) throw new Error('Authentication did not return a CSRF token.');
}

async function boot() {
  if (!EditorCore) throw new Error('The local editor core did not load. Reinstall or rebuild OmicsANG.');
  applyUiPrefs();
  initResizeHandles();
  document.querySelectorAll('[data-panel-restore]').forEach((button) => {
    button.onclick = () => {
      const key = button.getAttribute('data-panel-restore');
      if (!['sidebarOpen', 'monitorOpen', 'codeSidebarOpen', 'codeAssistantOpen', 'terminalOpen'].includes(key)) return;
      setUiPref(key, true);
      requestAnimationFrame(() => focusRestoredPanel(key));
    };
  });
  document.addEventListener('click', (e) => {
    const menu = $('#tool-menu');
    if (menu && menu.open && !menu.contains(e.target)) closeToolMenu();
    const recent = $('.recent-menu[open]');
    if (recent && !recent.contains(e.target)) recent.open = false;
  });
  await bootstrapAuthentication();
  state.meta = await api.get('/api/meta');
  state.runQueue = state.meta.run_queue || null;
  $('#root-label').textContent = state.meta.root;
  renderToolBadges();
  $('#sidebar-toggle').onclick = () => setUiPref('sidebarOpen', !state.uiPrefs.sidebarOpen);
  $('#sidebar-collapse').onclick = () => {
    setUiPref('sidebarOpen', false);
    requestAnimationFrame(() => $('#sidebar-restore').focus());
  };
  $('#monitor-toggle').onclick = () => setUiPref('monitorOpen', !state.uiPrefs.monitorOpen);
  $('#view-settings-toggle').onclick = () => { state.viewSettingsOpen = !state.viewSettingsOpen; renderViewSettings(); };
  $('#cmdk-button').onclick = () => openCommandPalette();
  $('#help-toggle').onclick = (e) => state.help.open ? closeHelp() : openHelp(e.currentTarget);
  $('#help-backdrop').onclick = () => closeHelp();
  $('#help-drawer').addEventListener('keydown', trapHelpFocus);
  const pipelineFilter = $('#pipeline-filter');
  pipelineFilter.oninput = () => { state.pipelineQuery = pipelineFilter.value; renderSidebar(); };
  pipelineFilter.onkeydown = (e) => {
    if (e.key !== 'Escape' || !pipelineFilter.value) return;
    pipelineFilter.value = '';
    state.pipelineQuery = '';
    renderSidebar();
  };
  state.pipelines = await api.get('/api/pipelines');
  renderSidebar();
  if (state.pipelines.length) selectPipeline(state.pipelines[0].name);
  pollSessions();
  setInterval(pollSessions, 2500);
}

function renderToolBadges() {
  const wrap = $('#tool-badges'); wrap.innerHTML = '';
  for (const [id, ok] of Object.entries(state.meta.tools || {})) {
    wrap.append(el('span', { class: `badge ${ok ? 'on' : 'off'}` }, `${ok ? '●' : '○'} ${id}`));
  }
}

/* ---------------- sidebar ---------------- */
function renderSidebar() {
  const query = state.pipelineQuery.trim().toLowerCase();
  const pipelines = (state.pipelines || []).filter((p) => !query ||
    `${p.name || ''} ${p.path || p.root || ''}`.toLowerCase().includes(query));
  $('#pipe-count').textContent = query ? `${pipelines.length}/${state.pipelines.length}` : state.pipelines.length;
  const ul = $('#pipeline-list'); ul.innerHTML = '';
  ul.append(el('li', { class: 'fleet-nav' + (state.view === 'fleet' ? ' active' : '') },
    el('button', {
      class: 'pipeline-nav-button', type: 'button', 'aria-current': state.view === 'fleet' ? 'page' : null,
      onclick: () => { state.view = 'fleet'; renderSidebar(); render(); },
    }, el('span', { class: 'pl-name' }, '✦ Fleet'), el('span', { class: 'pl-kind' }, 'all'))));
  for (const p of pipelines) {
    ul.append(el('li', { class: state.view === 'pipeline' && state.selected === p.name ? 'active' : '' },
      el('button', {
        class: 'pipeline-nav-button', type: 'button',
        'aria-current': state.view === 'pipeline' && state.selected === p.name ? 'page' : null,
        onclick: () => selectPipeline(p.name),
      }, el('span', { class: 'pl-name' }, p.name), el('span', { class: 'pl-kind' }, p.kind))));
  }
  if (query && !pipelines.length) {
    ul.append(el('li', { class: 'pipeline-empty', 'aria-live': 'polite' },
      el('span', { class: 'muted' }, 'No matching pipelines')));
  }
}

async function selectPipeline(name) {
  state.selected = name;
  state.view = 'pipeline';
  renderSidebar();
  state.detail = await api.get(`/api/pipelines/${encodeURIComponent(name)}`);
  if (!['overview','browse','study','run','dag','results','capsules','config','sra_geo','code','resources','github','agents','terminal'].includes(state.tab)) state.tab = 'overview';
  render();
}

/* ---------------- tabs + view ---------------- */
const PRIMARY_TABS = [
  ['overview', 'Monitor'], ['browse', 'Browse'], ['study', 'Study'], ['run', 'Run'], ['results', 'Results'], ['capsules', 'Capsules'], ['code', 'Code'], ['agents', 'Agents'],
];
const TOOL_TABS = [
  ['dag', 'DAG', 'Inspect workflow structure'],
  ['config', 'Config', 'Edit pipeline parameters'],
  ['sra_geo', 'SRA/GEO', 'Find and download public data'],
  ['resources', 'Resources', 'Review compute requirements'],
  ['github', 'GitHub', 'Manage repository work'],
  ['terminal', 'Terminal', 'Attach to live sessions'],
];

const navDomId = (id) => `nav-tab-${id}`;
const moreNavDomId = (id) => `more-tab-${id}`;
const NAVIGATION_STATE_KEY = 'omicsang.navigation.v1';
const LEGACY_NAVIGATION_STATE_KEY = 'benchtopNavigationV1';
const OVERFLOW_WORKSPACE_TABS = new Set(['study', 'capsules', 'agents']);

function navigationSnapshot() {
  return state.view === 'fleet'
    ? { view: 'fleet', fleetMode: state.fleetMode === 'task' ? 'task' : 'health' }
    : { view: 'pipeline', pipelineIndex: Math.max(0, (state.pipelines || []).findIndex(item => item.name === state.selected)), tab: state.tab || 'overview' };
}

function navigationSnapshotKey(snapshot) {
  if (!snapshot) return '';
  return snapshot.view === 'fleet'
    ? `fleet:${snapshot.fleetMode || 'health'}`
    : `pipeline:${Number(snapshot.pipelineIndex) || 0}:${snapshot.tab || 'overview'}`;
}

function navigationSnapshotLabel(snapshot) {
  if (!snapshot) return 'Unknown view';
  if (snapshot.view === 'fleet') return snapshot.fleetMode === 'task' ? 'Fleet Task' : 'Fleet Health';
  const tab = [...PRIMARY_TABS, ...TOOL_TABS].find(([id]) => id === snapshot.tab);
  const pipeline = (state.pipelines || [])[Number(snapshot.pipelineIndex) || 0];
  return `${pipeline ? pipeline.name : 'Pipeline'} · ${tab ? tab[1] : 'Monitor'}`;
}

function rememberRecentNavigation(snapshot) {
  const key = navigationSnapshotKey(snapshot);
  if (!key) return;
  state.recentNavigation = [snapshot, ...(state.recentNavigation || []).filter(item => navigationSnapshotKey(item) !== key)].slice(0, 6);
}

function navigationStatePayload(snapshot, index) {
  return { [NAVIGATION_STATE_KEY]: { snapshot, index } };
}

function navigationFromHistoryState(historyState) {
  return historyState && (historyState[NAVIGATION_STATE_KEY] || historyState[LEGACY_NAVIGATION_STATE_KEY]);
}

function syncNavigationHistory() {
  if (state.navigation.restoring) return;
  const snapshot = navigationSnapshot();
  const key = navigationSnapshotKey(snapshot);
  const historyState = window.history.state;
  const current = navigationFromHistoryState(historyState);
  if (!current) {
    state.navigation.index = 0;
    state.navigation.maxIndex = 0;
    window.history.replaceState(navigationStatePayload(snapshot, 0), document.title, `${window.location.pathname}${window.location.search}`);
    return;
  }
  if (historyState && !historyState[NAVIGATION_STATE_KEY]) {
    window.history.replaceState(navigationStatePayload(current.snapshot, current.index), document.title, `${window.location.pathname}${window.location.search}`);
  }
  if (navigationSnapshotKey(current.snapshot) === key) {
    state.navigation.index = Number(current.index) || 0;
    state.navigation.maxIndex = Math.max(state.navigation.maxIndex, state.navigation.index);
    return;
  }
  rememberRecentNavigation(current.snapshot);
  const index = (Number(current.index) || 0) + 1;
  state.navigation.index = index;
  state.navigation.maxIndex = index;
  window.history.pushState(navigationStatePayload(snapshot, index), document.title, `${window.location.pathname}${window.location.search}`);
}

async function applyNavigationSnapshot(snapshot, restoring = false) {
  if (!snapshot || !['fleet', 'pipeline'].includes(snapshot.view)) return;
  const token = ++state.navigation.restoreToken;
  state.navigation.restoring = restoring;
  if (snapshot.view === 'fleet') {
    state.view = 'fleet';
    state.fleetMode = snapshot.fleetMode === 'task' ? 'task' : 'health';
    renderSidebar();
    render();
    state.navigation.restoring = false;
    return;
  }
  const allowedTabs = new Set([...PRIMARY_TABS, ...TOOL_TABS].map(([id]) => id));
  const pipeline = (state.pipelines || [])[Number(snapshot.pipelineIndex) || 0];
  if (!pipeline) {
    state.navigation.restoring = false;
    return;
  }
  try {
    const detail = state.selected === pipeline.name && state.detail
      ? state.detail
      : await api.get(`/api/pipelines/${encodeURIComponent(pipeline.name)}`);
    if (token !== state.navigation.restoreToken) return;
    state.selected = pipeline.name;
    state.detail = detail;
    state.view = 'pipeline';
    state.tab = allowedTabs.has(snapshot.tab) ? snapshot.tab : 'overview';
    renderSidebar();
    render();
  } catch (e) {
    if (!restoring) window.alert(`Could not restore that workspace view:\n${e.message}`);
  } finally {
    if (token === state.navigation.restoreToken) state.navigation.restoring = false;
  }
}

function cycleNavigation(delta) {
  const tabs = state.view === 'fleet'
    ? ['fleet-health', 'fleet-task']
    : [...PRIMARY_TABS, ...TOOL_TABS].map(([id]) => id);
  const current = state.view === 'fleet'
    ? (state.fleetMode === 'task' ? 'fleet-task' : 'fleet-health')
    : state.tab;
  const index = Math.max(0, tabs.indexOf(current));
  activateNavigation(tabs[(index + delta + tabs.length) % tabs.length], 'auto');
}

function activateNavigation(id, focusTarget = id) {
  if (id === 'fleet-health' || id === 'fleet-task') {
    state.view = 'fleet';
    state.fleetMode = id === 'fleet-health' ? 'health' : 'task';
  } else {
    state.view = 'pipeline';
    state.tab = id;
  }
  renderSidebar();
  render();
  requestAnimationFrame(() => {
    const secondary = TOOL_TABS.some(([tabId]) => tabId === id);
    const target = focusTarget === 'more' || (focusTarget === 'auto' && secondary)
      ? document.querySelector('#tool-menu > summary')
      : document.getElementById(navDomId(focusTarget === 'auto' ? id : focusTarget));
    if (!target) return;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}

function navigationContext(scope, kind, label, activeId) {
  const recent = el('details', { class: 'recent-menu' });
  const recentList = el('div', { class: 'recent-menu-popover', role: 'menu', 'aria-label': 'Recent views' });
  const recentItems = (state.recentNavigation || []).filter(item => navigationSnapshotKey(item) !== navigationSnapshotKey(navigationSnapshot()));
  if (!recentItems.length) recentList.append(el('div', { class: 'recent-empty' }, 'No recent views yet'));
  recentItems.forEach(item => recentList.append(el('button', {
    type: 'button', role: 'menuitem',
    onclick: () => { recent.open = false; applyNavigationSnapshot(item, false); },
  }, navigationSnapshotLabel(item))));
  recent.append(el('summary', { title: 'Reopen a recent tab' }, 'Recent'), recentList);
  return el('div', {
    class: 'nav-context',
    'aria-live': 'polite',
    'aria-atomic': 'true',
  },
    el('span', { class: 'nav-scope', title: scope }, scope),
    el('span', { class: 'nav-current' },
      el('span', { class: `nav-kind ${kind.toLowerCase()}` }, kind),
      el('strong', { id: 'active-view-title' }, label)),
    el('span', { class: 'nav-route-actions' },
      el('button', {
        class: 'nav-history-button', type: 'button', title: 'Back to the previous OmicsANG view',
        'aria-label': 'Back to previous view', disabled: state.navigation.index <= 0 ? '' : null,
        onclick: () => window.history.back(),
      }, '←'),
      el('button', {
        class: 'nav-history-button', type: 'button', title: 'Forward to the next OmicsANG view',
        'aria-label': 'Forward to next view', disabled: state.navigation.index >= state.navigation.maxIndex ? '' : null,
        onclick: () => window.history.forward(),
      }, '→'),
      recent,
      el('button', {
        class: 'context-help-button', type: 'button', 'aria-controls': 'help-drawer',
        onclick: (e) => openHelp(e.currentTarget),
      }, `? Help for ${label}`)));
}

function setViewSemantics(view, labelledBy) {
  view.setAttribute('role', 'tabpanel');
  view.setAttribute('aria-labelledby', labelledBy);
  view.setAttribute('tabindex', '0');
}

function render() {
  syncNavigationHistory();
  // park all terminal hosts back in the dock so we can re-mount cleanly
  for (const rec of terminals.values()) dock.appendChild(rec.host);
  const tabs = $('#tabs'); tabs.innerHTML = '';
  const view = $('#view'); view.innerHTML = '';
  syncPanelRestoreDock(state.uiPrefs);

  if (state.view === 'fleet') {
    const fleetTabs = [['fleet-health', 'Fleet Health'], ['fleet-task', 'Fleet Task']];
    const activeId = state.fleetMode === 'task' ? 'fleet-task' : 'fleet-health';
    const active = fleetTabs.find(([id]) => id === activeId);
    tabs.setAttribute('aria-label', 'Fleet workspace');
    tabs.append(
      navigationContext('All pipelines', 'Fleet', active[1], activeId),
      navGroup('primary', fleetTabs, 'Fleet views'));
    setViewSemantics(view, navDomId(activeId));
    document.title = `${active[1]} · OmicsANG`;
    viewFleet(view);
    renderMonitor();
    renderHelpDrawer();
    return;
  }

  const primaryActive = PRIMARY_TABS.find(([id]) => state.tab === id);
  const toolActive = TOOL_TABS.find(([id]) => state.tab === id);
  const active = primaryActive || toolActive || PRIMARY_TABS[0];
  const scope = state.detail ? state.detail.name : (state.selected || 'No pipeline selected');
  tabs.setAttribute('aria-label', `${scope} workspace`);
  tabs.append(
    navigationContext(scope, primaryActive ? 'Workspace' : 'Tool', active[1], active[0]),
    navGroup('primary', PRIMARY_TABS, 'Workspace views'),
    toolMenu(toolActive || (primaryActive && OVERFLOW_WORKSPACE_TABS.has(primaryActive[0]) ? primaryActive : null)));
  setViewSemantics(view, primaryActive ? navDomId(active[0]) : moreNavDomId(active[0]));
  document.title = `${active[1]} · ${scope} · OmicsANG`;
  if (!state.detail) {
    view.append(el('div', { class: 'empty' }, 'No pipeline selected'));
    renderHelpDrawer();
    return;
  }
  ({ overview: viewOverview, browse: viewBrowse, study: viewStudy, run: viewRun, dag: viewDag, results: viewResults, capsules: viewCapsules,
     config: viewConfig, sra_geo: viewSraGeo, code: viewCode, resources: viewResources, github: viewGithub,
     agents: viewAgents, terminal: viewTerminal }[state.tab])(view);
  renderMonitor();
  renderHelpDrawer();
}

function navGroup(kind, tabs, label) {
  const group = el('div', { class: `nav-group ${kind}`, role: 'tablist', 'aria-label': label, 'aria-orientation': 'horizontal' });
  const activeIndex = tabs.findIndex(([id]) => id === 'fleet-health' ? state.fleetMode === 'health' :
    id === 'fleet-task' ? state.fleetMode === 'task' : state.tab === id);
  const focusIndex = activeIndex >= 0 ? activeIndex : 0;
  for (const [index, [id, tabLabel]] of tabs.entries()) {
    const active = id === 'fleet-health' ? state.fleetMode === 'health' :
      id === 'fleet-task' ? state.fleetMode === 'task' : state.tab === id;
    group.append(el('button', {
      class: `tab nav-tab-${id} ${OVERFLOW_WORKSPACE_TABS.has(id) ? 'responsive-overflow-tab' : ''} ${active ? 'active' : ''}`,
      id: navDomId(id),
      type: 'button',
      role: 'tab',
      'aria-selected': active ? 'true' : 'false',
      'aria-controls': 'view',
      tabindex: index === focusIndex ? '0' : '-1',
      onclick: () => activateNavigation(id),
      onkeydown: (e) => {
        let next = null;
        if (e.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (e.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = tabs.length - 1;
        if (next === null) return;
        e.preventDefault();
        activateNavigation(tabs[next][0]);
      },
    }, tabLabel));
  }
  return el('div', { class: 'nav-primary-zone' },
    el('span', { class: 'nav-section-label', 'aria-hidden': 'true' }, label.replace(/\s+views$/i, '')),
    el('div', { class: 'nav-viewport' }, group));
}

function toolMenu(activeTool) {
  const details = el('details', { id: 'tool-menu', class: `tool-menu ${activeTool ? 'active' : ''}` });
  const summary = el('summary', {
    class: 'tool-trigger',
    'aria-haspopup': 'true',
    'aria-expanded': 'false',
    'aria-controls': 'tool-menu-popover',
    onkeydown: (e) => {
      if (!['ArrowDown', 'ArrowUp'].includes(e.key)) return;
      e.preventDefault();
      details.open = true;
      requestAnimationFrame(() => {
        const items = details.querySelectorAll('[role="tab"]:not(.responsive-more-hidden)');
        const target = e.key === 'ArrowUp' ? items[items.length - 1] : items[0];
        if (target) target.focus();
      });
    },
  },
    el('span', { class: 'tool-trigger-label' }, 'More'),
    el('span', { class: 'tool-trigger-current' }, activeTool ? activeTool[1] : `${TOOL_TABS.length} tools`),
    el('span', { class: 'tool-chevron', 'aria-hidden': 'true' }, '▾'));
  const popover = el('div', { id: 'tool-menu-popover', class: 'tool-menu-popover', role: 'tablist', 'aria-label': 'More OmicsANG views' });
  const overflowTabs = PRIMARY_TABS.filter(([id]) => OVERFLOW_WORKSPACE_TABS.has(id))
    .map(([id, label]) => [id, label, `Open the ${label} workspace`, true]);
  const allItems = [...overflowTabs, ...TOOL_TABS.map(item => [...item, false])];
  const menuFocusIndex = Math.max(0, allItems.findIndex(([id]) => state.tab === id));
  popover.append(el('div', { class: 'tool-menu-section-label workspace-overflow-label' }, 'Workspace views'));
  for (const [index, [id, label, description, workspaceOverflow]] of allItems.entries()) {
    if (index === overflowTabs.length) popover.append(el('div', { class: 'tool-menu-section-label' }, 'Tools'));
    const active = state.tab === id;
    popover.append(el('button', {
      id: moreNavDomId(id),
      class: `tool-menu-item ${workspaceOverflow ? `workspace-overflow-item overflow-${id}` : ''} ${active ? 'active' : ''}`,
      type: 'button', role: 'tab',
      'aria-selected': active ? 'true' : 'false', 'aria-controls': 'view',
      tabindex: index === menuFocusIndex ? '0' : '-1',
      onclick: () => activateNavigation(id, 'more'),
    }, el('span', { class: 'tool-menu-label' }, label),
      el('span', { class: 'tool-menu-description' }, description),
      active ? el('span', { class: 'tool-menu-check', 'aria-hidden': 'true' }, '●') : null));
  }
  popover.onkeydown = (e) => {
    const items = [...popover.querySelectorAll('[role="tab"]')].filter(item => getComputedStyle(item).display !== 'none');
    const index = items.indexOf(document.activeElement);
    let next = null;
    if (e.key === 'ArrowDown') next = (index + 1) % items.length;
    else if (e.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = items.length - 1;
    else if (e.key === 'Escape') {
      e.preventDefault();
      closeToolMenu(true);
      return;
    }
    if (next === null) return;
    e.preventDefault();
    items[next].focus();
  };
  details.addEventListener('toggle', () => summary.setAttribute('aria-expanded', details.open ? 'true' : 'false'));
  details.append(summary, popover);
  return details;
}

function closeToolMenu(restoreFocus = false) {
  const details = $('#tool-menu');
  if (!details || !details.open) return false;
  details.open = false;
  if (restoreFocus) requestAnimationFrame(() => details.querySelector('summary').focus());
  return true;
}

function closeRecentMenu(restoreFocus = false) {
  const details = $('.recent-menu[open]');
  if (!details) return false;
  details.open = false;
  if (restoreFocus) requestAnimationFrame(() => details.querySelector('summary')?.focus());
  return true;
}

const BUILTIN_TAB_GUIDES = {
  overview: { summary: 'See the pipeline’s operational state and choose the safest next action.', how_to: ['Review health and recent runs.', 'Use the suggested action or open a focused tab.'], prerequisites: ['A registered pipeline.'], reads: 'Pipeline metadata, run history, diagnostics, and repository status.', writes: 'Nothing until you choose an explicit action.', network: 'None for the overview itself.', persistence: 'No overview data is stored in the browser.', cautions: ['Status is a snapshot; refresh before acting on rapidly changing runs.'] },
  browse: { summary: 'Search approved pipeline directories and open files in the right OmicsANG workspace.', how_to: ['Choose a scope or directory.', 'Type a name, then use arrow keys and Enter to open a result.'], prerequisites: ['A registered pipeline and server-approved search roots.'], reads: 'Bounded file and directory metadata; file bytes only after you explicitly open one.', writes: 'Nothing.', network: 'Only same-origin requests to this OmicsANG server.', persistence: 'Queries and paths remain in memory and are not added to URLs or browser storage.', cautions: ['Partial-result notices mean the bounded search stopped before scanning everything.'] },
  study: { summary: 'Review cohort balance and experimental-design findings before a run.', how_to: ['Inspect detected factors and issues.', 'Resolve blocking findings in source files, then refresh.'], prerequisites: ['A readable sample sheet or supported study metadata.'], reads: 'Local study tables and pipeline configuration.', writes: 'Only explicit fixes or file edits in Code.', network: 'None.', persistence: 'Review state may be stored by the local OmicsANG backend.', cautions: ['Automated checks support, but do not replace, scientific review.'] },
  run: { summary: 'Resolve a reproducible run plan, dry-run it, then launch deliberately.', how_to: ['Set target, cores, and profile.', 'Resolve the plan and review warnings before launch.'], prerequisites: ['The workflow engine and declared environments on this machine or cluster.'], reads: 'Workflow, configuration, environment, and scheduler metadata.', writes: 'A confirmed launch can create outputs, logs, and durable job records.', network: 'Only tools invoked by the workflow may use the network.', persistence: 'Run plans, queue records, logs, and provenance are stored locally.', cautions: ['A workflow runs with your OS-account permissions; review its commands and paths.'] },
  results: { summary: 'Inspect local reports, figures, tables, QC, and attached result directories.', how_to: ['Filter by name or type.', 'Open a result only after reviewing its source.'], prerequisites: ['Completed or attached result directories.'], reads: 'Approved result metadata and files you explicitly open.', writes: 'Attaching or detaching changes OmicsANG metadata, not result files.', network: 'None unless a report itself loads remote content.', persistence: 'Result-directory attachments are stored by the local backend.', cautions: ['Missing or partial sources can make a result set incomplete.'] },
  capsules: { summary: 'Compare bounded run evidence and provenance across executions.', how_to: ['Choose baseline and candidate capsules.', 'Review verdicts and domain-specific differences.'], prerequisites: ['At least one captured run capsule.'], reads: 'Local run manifests, fingerprints, provenance, and study summaries.', writes: 'Nothing.', network: 'None.', persistence: 'Capsules are local backend records.', cautions: ['A matching fingerprint does not prove scientific equivalence.'] },
  code: { summary: 'Browse and edit supported pipeline files with diagnostics and package-aware help.', how_to: ['Select a file in the tree.', 'Use line numbers, active indentation guides, and the Ln/Col status bar to stay oriented.', 'Use Tool & package help, or F1 at the caret, for static parameter help, local detection, and official documentation.'], prerequisites: ['Write permission is required only to save changes.'], reads: 'Editable files, the caret context, and local package declarations.', writes: 'Save, rename, duplicate, mkdir, and trash actions change local files after explicit clicks.', network: 'Tool and package help is local; official docs open only after you click a vetted link. Agents may transmit supplied context only after confirmation.', persistence: 'Open tabs stay in memory; explicitly saved notes use workspace-scoped browser storage.', cautions: ['Unsaved tabs are not durable. Revision conflicts block silent overwrite. Agents and shells are not sandboxes.'], tips: ['Tab and Shift+Tab indent or outdent; Ctrl/Cmd+/ toggles supported line comments.', 'Place the caret on a supported command or parameter and press F1 for the matching version-labelled guide.', 'Press Escape, then Tab or Shift+Tab, to move keyboard focus out of the editor.'] },
  agents: { summary: 'Launch a deliberately reviewed coding or analysis agent.', how_to: ['Review the tool, prompt, and supplied context.', 'Nothing launches until you approve the confirmation.'], prerequisites: ['A configured agent command and provider credentials, where required.'], reads: 'The prepared context plus repository content the approved CLI reads.', writes: 'After approval, the agent CLI runs with your OS-account permissions.', network: 'Claude Code or Codex may send content to their providers only after confirmation. OmicsANG does not send shell input to an agent provider; commands or shell startup configuration may use the network.', persistence: 'Agent terminal output is not a durable OmicsANG audit log; provider retention follows its own terms.', cautions: ['OmicsANG does not add an OS sandbox. Git worktrees separate changes but are not security boundaries. Never include secrets or regulated data without authorization.'] },
  dag: { summary: 'Inspect the workflow graph and rule relationships.', how_to: ['Choose configuration and target.', 'Generate the DAG, then inspect failures or gaps.'], prerequisites: ['A supported workflow engine and parseable workflow.'], reads: 'Workflow and configuration files.', writes: 'Temporary local inspection artifacts only.', network: 'None unless the workflow engine itself performs network setup.', persistence: 'The rendered graph is held in memory.', cautions: ['A parseable DAG does not guarantee a successful run.'] },
  config: { summary: 'Review and edit pipeline parameters with schema-aware guidance.', how_to: ['Choose a configuration file.', 'Validate changes before saving.'], prerequisites: ['A supported configuration file.'], reads: 'Local configuration and schema metadata.', writes: 'Saving changes the selected local configuration file.', network: 'None.', persistence: 'Saved values remain in the pipeline file.', cautions: ['Configuration changes can alter scientific meaning and output locations.'] },
  sra_geo: { summary: 'Find public SRA/GEO studies and launch tracked local downloads.', how_to: ['Search or paste accessions.', 'Preview destination and commands before downloading.'], prerequisites: ['Network access and required NCBI tools for downloads.'], reads: 'Public metadata and the accessions you provide.', writes: 'Confirmed downloads create files in the selected approved destination.', network: 'Queries public NCBI services and downloads public datasets.', persistence: 'Downloaded files and tracked session logs remain local.', cautions: ['Verify consent, controlled-access status, storage, and downstream licensing yourself.'] },
  resources: { summary: 'Review local and scheduler capacity before launching work.', how_to: ['Compare requested and available resources.', 'Inspect active scheduler jobs.'], prerequisites: ['Scheduler tools for cluster-specific status.'], reads: 'Local system and scheduler metadata.', writes: 'Only explicit scheduler actions can change jobs.', network: 'Depends on the configured scheduler connection.', persistence: 'Status is transient; job records may be stored locally.', cautions: ['Capacity readings can become stale quickly.'] },
  github: { summary: 'Review repository and GitHub state from a status-first surface.', how_to: ['Inspect branch, remote, and working-tree state.', 'Use only explicitly available actions after review.'], prerequisites: ['A Git repository; GitHub CLI authentication for GitHub data.'], reads: 'Local Git metadata and explicitly requested GitHub status.', writes: 'Only enabled, explicit repository actions can change local or remote state.', network: 'GitHub operations contact GitHub after an explicit action.', persistence: 'Git and GitHub retain their normal repository and service records.', cautions: ['Review diffs and remote targets before any publish action.'] },
  terminal: { summary: 'Attach to tracked local processes without losing session context.', how_to: ['Choose a session.', 'Review its title and status before entering commands.'], prerequisites: ['A live or retained OmicsANG session.'], reads: 'Terminal output and anything entered commands can access.', writes: 'Commands can change anything your OS account can access.', network: 'Commands may use the network.', persistence: 'Session logs and process effects may persist locally.', cautions: ['The terminal is not sandboxed. Avoid pasting secrets.'] },
  'fleet-health': { summary: 'Compare health across all registered pipelines.', how_to: ['Scan warnings and failures.', 'Open a pipeline for detailed diagnosis.'], prerequisites: ['At least one registered pipeline.'], reads: 'Pipeline health, queue, run, and scheduler summaries.', writes: 'Nothing.', network: 'None for local fleet status.', persistence: 'No fleet filter is stored.', cautions: ['Cross-pipeline status is a snapshot.'] },
  'fleet-task': { summary: 'Prepare a reviewed task across selected pipelines.', how_to: ['Select pipelines and review the launch context.', 'Confirm the agent only after reviewing context.'], prerequisites: ['Configured agent tooling.'], reads: 'The task, selected repository metadata, and repository content each approved CLI reads.', writes: 'After confirmation, each agent runs with your OS-account permissions and may access more than the selected repositories.', network: 'Claude Code or Codex may send content they read to their external providers after confirmation.', persistence: 'Fleet metadata persists locally, but agent terminal output is not a durable OmicsANG audit log.', cautions: ['Fleet multiplies impact across targets. Worktrees separate Git changes but are not security boundaries.'] },
};

function activeHelpId() {
  return state.view === 'fleet' ? (state.fleetMode === 'task' ? 'fleet-task' : 'fleet-health') : state.tab;
}

function activeHelpLabel() {
  const id = activeHelpId();
  const found = [...PRIMARY_TABS, ...TOOL_TABS, ['fleet-health', 'Fleet Health'], ['fleet-task', 'Fleet Task']].find(([tabId]) => tabId === id);
  return found ? found[1] : 'OmicsANG';
}

function asHelpList(value) {
  if (Array.isArray(value)) return value.map(item => typeof item === 'string' ? item : (item.text || item.label || '')).filter(Boolean);
  return value == null || value === '' ? [] : [String(value)];
}

function helpTabRecord(id) {
  const tabs = state.help.data && state.help.data.tabs;
  if (Array.isArray(tabs)) return tabs.find(item => item && item.id === id) || null;
  return tabs && typeof tabs === 'object' ? tabs[id] || null : null;
}

async function loadHelpData() {
  if (state.help.loading || state.help.attempted) return;
  state.help.loading = true;
  state.help.attempted = true;
  state.help.error = '';
  renderHelpDrawer();
  try {
    state.help.data = await api.get('/api/help');
  } catch (e) {
    state.help.error = e.message;
  } finally {
    state.help.loading = false;
    renderHelpDrawer();
  }
}

function openHelp(returnFocus = document.activeElement) {
  state.help.returnFocus = returnFocus && typeof returnFocus.focus === 'function' ? returnFocus : document.activeElement;
  state.help.open = true;
  renderHelpDrawer();
  loadHelpData();
  requestAnimationFrame(() => helpDrawer.querySelector('.help-close')?.focus());
}

function closeHelp() {
  if (!state.help.open) return;
  const returnFocus = state.help.returnFocus;
  state.help.open = false;
  renderHelpDrawer();
  const target = returnFocus && returnFocus.isConnected ? returnFocus : $('#help-toggle');
  if (target) requestAnimationFrame(() => target.focus());
}

function trapHelpFocus(e) {
  if (!state.help.open || e.key !== 'Tab') return;
  const focusable = [...helpDrawer.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')];
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

function helpListSection(title, values) {
  const items = asHelpList(values);
  if (!items.length) return null;
  return el('section', { class: 'help-section' }, el('h3', {}, title),
    el('ul', {}, ...items.map(item => el('li', {}, item))));
}

function renderHelpDrawer(focus = false) {
  if (!helpDrawer || !helpBackdrop) return;
  const open = !!state.help.open;
  helpDrawer.classList.toggle('hidden', !open);
  helpBackdrop.classList.toggle('hidden', !open);
  document.body.classList.toggle('help-open', open);
  const topButton = $('#help-toggle');
  if (topButton) {
    topButton.classList.toggle('active', open);
    topButton.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  if (!open) return;
  const id = activeHelpId();
  const builtin = BUILTIN_TAB_GUIDES[id] || BUILTIN_TAB_GUIDES.overview;
  const remote = helpTabRecord(id) || {};
  const guide = { ...builtin, ...remote };
  const close = el('button', { class: 'ghost icon-only help-close', type: 'button', 'aria-label': 'Close help', onclick: closeHelp }, '×');
  const status = state.help.loading
    ? el('div', { class: 'help-status', role: 'status' }, 'Loading the server’s tab guide…')
    : state.help.error
      ? el('div', { class: 'help-status warn', role: 'status' }, 'Server help is unavailable. Showing the built-in guide; the active tab remains fully usable.')
      : null;
  const facts = [
    ['Reads locally', guide.reads], ['Can write or change', guide.writes], ['Network use', guide.network],
    ['Persistence', guide.persistence],
  ];
  const related = asHelpList(guide.related_tabs);
  const shortcutRows = state.help.data && Array.isArray(state.help.data.shortcuts) ? state.help.data.shortcuts : [
    { keys: 'Ctrl/Command + K', action: 'Open command palette' },
    { keys: 'Ctrl + PageUp/PageDown', action: 'Cycle tabs' },
    { keys: 'Alt + Left/Right', action: 'Move through OmicsANG view history' },
    { keys: 'Escape', action: 'Close the active overlay' },
  ];
  helpDrawer.innerHTML = '';
  const drawerContent = [
    el('div', { class: 'help-head' },
      el('div', {}, el('div', { class: 'eyebrow' }, 'Contextual guide'), el('h2', { id: 'help-title' }, `Help for ${remote.label || activeHelpLabel()}`)), close),
    status,
    el('p', { class: 'help-summary' }, guide.summary || builtin.summary),
    helpListSection('How to use this tab', guide.how_to),
    helpListSection('Prerequisites', guide.prerequisites),
    facts.length ? el('section', { class: 'help-section' }, el('h3', {}, 'What happens locally'),
      el('dl', { class: 'help-facts' }, ...facts.flatMap(([term, value]) => {
        const detail = asHelpList(value);
        return [el('dt', {}, term), el('dd', {}, detail.length ? detail.join(' ') : 'None for this tab.')];
      }))) : null,
    helpListSection('Cautions', guide.cautions),
    helpListSection('Tips', guide.tips),
    el('section', { class: 'help-section' }, el('h3', {}, 'Keyboard shortcuts'),
      el('div', { class: 'shortcut-list' }, ...shortcutRows.map(item => el('div', { class: 'shortcut-row' },
        el('kbd', {}, Array.isArray(item.keys) ? item.keys.join(' + ') : String(item.keys || '')), el('span', {}, item.action || item.label || ''))))),
    related.length ? el('section', { class: 'help-section' }, el('h3', {}, 'Related tabs'),
      el('div', { class: 'help-related' }, ...related.map(tabId => {
        const item = [...PRIMARY_TABS, ...TOOL_TABS, ['fleet-health', 'Fleet Health'], ['fleet-task', 'Fleet Task']].find(([candidate]) => candidate === tabId);
        return item ? el('button', { class: 'ghost sm', onclick: () => { closeHelp(); activateNavigation(item[0], 'auto'); } }, item[1]) : null;
      }))) : null,
    id === 'code' && state.help.data && state.help.data.package_help
      ? helpListSection('Package help', state.help.data.package_help.note || state.help.data.package_help.supported_ecosystems) : null,
    el('p', { class: 'help-privacy' }, 'This drawer renders text returned by your local OmicsANG server. It never sends tab content or code to an external service.'),
  ];
  helpDrawer.append(...drawerContent.filter(Boolean));
  if (focus) requestAnimationFrame(() => close.focus());
}

function renderMonitor() {
  if (!monitorPanel) return;
  const open = !!state.uiPrefs.monitorOpen;
  document.body.classList.toggle('monitor-open', open);
  monitorPanel.classList.toggle('hidden', !open);
  monitorPanel.setAttribute('aria-hidden', open ? 'false' : 'true');
  const monitorToggle = $('#monitor-toggle');
  monitorToggle.classList.toggle('active', open);
  monitorToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  monitorToggle.setAttribute('title', `${open ? 'Hide' : 'Show'} run monitor`);
  if (!open) {
    monitorPanel.innerHTML = '';
    return;
  }
  const q = state.runQueue || { running: [], queued: [], active_cores: 0, max_cores: 1 };
  const rows = [...(q.running || []), ...(q.queued || []), ...(q.lost || [])];
  const liveSessions = (state.sessions || []).filter(s => isLiveStatus(s.status));
  const failedRuns = (state.sessions || []).filter(s => s.kind === 'run' && s.status === 'failed').slice(0, 4);
  monitorPanel.innerHTML = '';
  monitorPanel.append(
    el('div', {
      class: 'resize-handle monitor-resizer',
      title: 'Resize monitor',
      onpointerdown: (e) => startPreferenceResize(e, 'monitorWidth', 260, 660, 'x', true),
    }),
    el('div', { class: 'monitor-head' },
      el('div', {}, el('strong', {}, 'Run Monitor'), el('div', { class: 'muted' }, `${q.active_cores || 0}/${q.max_cores || 1} local cores`)),
      el('button', {
        class: 'ghost sm icon-only', type: 'button', title: 'Hide monitor',
        'aria-label': 'Hide run monitor', onclick: () => {
          setUiPref('monitorOpen', false);
          requestAnimationFrame(() => $('#monitor-restore').focus());
        },
      }, '×')),
    monitorMeter(q),
    monitorSection('Active queue', rows, s => queueMonitorRow(s)),
    monitorSection('Live terminals', liveSessions, s => sessionMonitorRow(s)),
    monitorSection('Failed runs', failedRuns, s => sessionMonitorRow(s, true)),
  );
}

function monitorMeter(q) {
  const max = Math.max(1, Number(q.max_cores || 1));
  const pct = Math.min(100, Math.round(((q.active_cores || 0) / max) * 100));
  return el('div', { class: 'monitor-meter' },
    el('div', { class: 'meter-track' }, el('div', { class: 'meter-fill', style: `width:${pct}%` })),
    el('div', { class: 'monitor-stats' },
      el('span', {}, `${(q.running || []).length} running`),
      el('span', {}, `${(q.queued || []).length} queued`)));
}

function monitorSection(title, rows, renderRow) {
  const wrap = el('section', { class: 'monitor-section' }, el('h3', {}, title));
  if (!rows.length) wrap.append(el('div', { class: 'empty slim' }, 'None'));
  else rows.slice(0, 8).forEach(r => wrap.append(renderRow(r)));
  return wrap;
}

function queueMonitorRow(s) {
  return el('button', { class: 'monitor-row', onclick: () => focusOrOpen(s) },
    el('span', { class: `dot ${s.status}` }),
    el('span', { class: 'monitor-title' }, s.title || s.command || s.id),
    el('span', { class: 'tag' }, s.status));
}

function sessionMonitorRow(s, failed = false) {
  const row = el('div', { class: 'monitor-row' },
    el('span', { class: `dot ${s.status}` }),
    el('button', { class: 'monitor-link', onclick: () => focusOrOpen(s) }, s.title || s.id),
    el('span', { class: 'tag' }, s.status));
  if (failed) row.append(el('button', { class: 'ghost sm danger-outline', onclick: () => debugWithClaude(s.id) }, 'Debug'));
  return row;
}

function commandItems() {
  const items = [];
  const add = (title, detail, run, weight = 0) => items.push({ title, detail, run, weight });
  add('Go to Fleet Health', 'all pipelines', () => { state.view = 'fleet'; state.fleetMode = 'health'; renderSidebar(); render(); }, 20);
  add('Launch Fleet Task', 'agents across selected pipelines', () => { state.view = 'fleet'; state.fleetMode = 'task'; renderSidebar(); render(); }, 18);
  if (state.detail) {
    for (const [id, label] of [...PRIMARY_TABS, ...TOOL_TABS]) {
      add(`Open ${label}`, state.detail.name, () => { state.view = 'pipeline'; state.tab = id; renderSidebar(); render(); }, 15);
    }
    add('Next suggested action', state.detail.name, () => {
      const a = nextAction(state.detail);
      if (a.run) a.run();
      else { state.tab = a.tab; render(); }
    }, 25);
  }
  (state.pipelines || []).forEach(p => add(`Pipeline: ${p.name}`, p.kind, () => selectPipeline(p.name), 10));
  (state.sessions || []).slice(0, 16).forEach(s => add(`Attach: ${s.title || s.id}`, `${s.status} · ${s.kind}`, () => focusOrOpen(s), severityRank(s.status)));
  const failed = (state.sessions || []).find(s => s.kind === 'run' && s.status === 'failed');
  if (failed) add(`Debug failed run: ${failed.title || failed.id}`, 'Claude Code', () => debugWithClaude(failed.id), 30);
  return items;
}

function openCommandPalette() {
  state.commandOpen = true;
  state.commandQuery = '';
  renderCommandPalette();
}

function closeCommandPalette() {
  state.commandOpen = false;
  if (commandPalette) commandPalette.classList.add('hidden');
}

function renderCommandPalette() {
  if (!commandPalette) return;
  if (!state.commandOpen) { commandPalette.classList.add('hidden'); return; }
  commandPalette.classList.remove('hidden');
  const q = state.commandQuery.trim().toLowerCase();
  let items = commandItems().map((item) => {
    const text = `${item.title} ${item.detail}`.toLowerCase();
    const score = q ? fuzzyScore(text, q) : 0;
    return { ...item, score: score + item.weight };
  }).filter(item => !q || item.score > -Infinity)
    .sort((a, b) => b.score - a.score)
    .slice(0, 12);
  const input = el('input', { type: 'text', value: state.commandQuery, placeholder: 'Jump to pipeline, view, run, result, or terminal' });
  input.oninput = () => { state.commandQuery = input.value; renderCommandPalette(); };
  input.onkeydown = (e) => {
    if (e.key === 'Escape') { closeCommandPalette(); return; }
    if (e.key === 'Enter' && items[0]) { closeCommandPalette(); items[0].run(); }
  };
  const list = el('div', { class: 'cmdk-list' });
  if (!items.length) list.append(el('div', { class: 'cmdk-empty' }, 'No matching command'));
  items.forEach((item) => list.append(el('button', { class: 'cmdk-item', onclick: () => { closeCommandPalette(); item.run(); } },
    el('span', {}, item.title), el('small', {}, item.detail || ''))));
  commandPalette.innerHTML = '';
  commandPalette.append(el('div', { class: 'cmdk-backdrop', onclick: closeCommandPalette }),
    el('div', { class: 'cmdk-box' }, input, list));
  setTimeout(() => input.focus(), 0);
}

/* ---------------- overview ---------------- */
function viewOverview(view) {
  const d = state.detail, g = d.git || {};
  const status = healthLabel(d);
  const action = nextAction(d);
  const healthHost = el('div', { class: 'dashboard-grid' });
  const runRows = el('div', { class: 'timeline-list' });
  const queue = pipelineQueueRows(d.name);

  const go = (tab) => { state.tab = tab; render(); };
  const runAction = () => {
    if (action.run) action.run();
    else go(action.tab);
  };

  view.append(
    el('section', { class: `hero-panel status-${status.level}` },
      el('div', {},
        el('div', { class: 'eyebrow' }, d.kind),
        el('h2', {}, d.name),
        el('div', { class: 'muted hero-path' }, d.path)),
      el('div', { class: 'hero-status' },
        el('span', { class: `health-pill ${status.level === 'error' ? 'error' : status.level === 'warn' ? 'warn' : 'ok'}` }, status.label),
        el('span', { class: 'muted' }, status.note))),
    el('div', { class: 'action-strip' },
      el('button', { class: action.tone === 'danger' ? 'danger' : '', onclick: runAction }, action.label),
      el('button', { class: 'ghost', onclick: () => go('run') }, 'Dry-run'),
      el('button', { class: 'ghost', onclick: () => go('results') }, 'Results'),
      el('button', { class: 'ghost', onclick: () => go('code') }, 'Diagnostics'),
      el('button', { class: 'ghost', onclick: () => go('agents') }, 'Agent'),
      d.readme ? el('button', { class: 'ghost', onclick: () => openAuthenticatedFile(`/api/file?pipeline=${encodeURIComponent(d.name)}&path=${encodeURIComponent(d.readme)}`, d.readme) }, 'README') : null),
    healthHost,
    el('div', { class: 'overview-layout' },
      el('section', { class: 'panel' },
        el('h3', {}, 'Recent runs'),
        runRows),
      el('section', { class: 'panel' },
        el('h3', {}, 'Pipeline facts'),
        el('div', { class: 'kv compact' },
          el('div', { class: 'k' }, 'Snakefile'), el('div', {}, shortPath(d.snakefile)),
          el('div', { class: 'k' }, 'Conda env'), el('div', {}, d.conda_env || d.env_resolved || '—'),
          el('div', { class: 'k' }, 'Configs'), el('div', {}, (d.configs || []).map(x => shortPath(x, 2)).join(', ') || '—'),
          el('div', { class: 'k' }, 'Profiles'), el('div', {}, (d.profiles || []).map(x => shortPath(x, 2)).join(', ') || '—'),
          el('div', { class: 'k' }, 'Git'), el('div', {},
            g.is_repo ? `${g.branch || 'repo'} · ${g.dirty ? `${g.dirty} changed` : 'clean'}` : '—'),
          el('div', { class: 'k' }, 'Queue'), el('div', {}, queue.length ? `${queue.length} active or queued` : 'idle')))));

  renderRunTimeline(runRows, d);
  loadPipelineDashboard(d.name, healthHost);
}

function renderRunTimeline(host, d) {
  const hist = d.history || [];
  host.innerHTML = '';
  if (!hist.length) { host.append(el('div', { class: 'empty slim' }, 'No runs yet.')); return; }
  hist.slice(0, 10).forEach((h) => {
    const row = el('div', { class: 'timeline-row' },
      el('span', { class: `dot ${h.status}` }),
      el('span', { class: 'timeline-title' }, h.title || h.command),
      el('span', { class: 'muted' }, fmtTime(h.ended || h.started)));
    if (h.status === 'failed' && h.kind === 'run')
      row.append(el('button', { class: 'ghost sm danger-outline', onclick: () => debugWithClaude(h.id) }, 'Debug'));
    host.append(row);
  });
}

async function loadPipelineDashboard(name, host) {
  const d = state.detail;
  const cached = pipelineHealthSnapshot(name);
  const draw = (h) => {
    const diag = h && h.diagnostics || {};
    const run = h && h.run_state || {};
    const fresh = h && h.result_freshness || {};
    host.innerHTML = '';
    host.append(
      dashboardTile('Diagnostics', diag.error == null ? '—' : `${diag.error || 0} err · ${diag.warning || 0} warn`, diag.error ? 'error' : diag.warning ? 'warn' : 'ok'),
      dashboardTile('Runs', `${(run.running || []).length} running · ${(run.queued || []).length} queued`, (run.running || []).length ? 'running' : 'ok'),
      dashboardTile('Results', fresh.latest ? fmtTime(fresh.latest) : 'No outputs', fresh.latest ? 'ok' : 'warn'),
      dashboardTile('Environment', h && h.env_resolved ? shortPath(h.env_resolved, 2) : (d.env_resolved || d.conda_env || '—'), h && h.env_resolved ? 'ok' : 'warn'),
    );
  };
  if (cached) draw(cached);
  else host.append(dashboardTile('Health', 'loading...', 'running'));
  try {
    const h = await api.get(`/api/pipelines/${name}/health`);
    state.pipelineHealth[name] = h;
    if (state.detail && state.detail.name === name) draw(h);
  } catch (e) {
    host.innerHTML = '';
    host.append(dashboardTile('Health', e.message, 'error'));
  }
}

function dashboardTile(label, value, level = 'ok') {
  return el('button', { class: `metric-tile ${level}`, onclick: () => {
    if (label === 'Diagnostics') state.tab = 'code';
    else if (label === 'Runs') state.tab = 'terminal';
    else if (label === 'Results') state.tab = 'results';
    else state.tab = 'config';
    render();
  } },
    el('span', { class: 'metric-label' }, label),
    el('strong', {}, value));
}

/* ---------------- run ---------------- */
function defaultRunForm(d) {
  return {
    target: '', configfile: '', profile: '', cores: 8, dryrun: true, use_conda: true,
    extra: '', env: d.env_resolved || d.conda_env || '', study_sheet: '', study_roles: {}, study_override: false,
    plan_digest: '',
  };
}

function runFormFor(d) {
  return state.runForm[d.name] || (state.runForm[d.name] = defaultRunForm(d));
}


function studyState(d) {
  return state.study[d.name] || (state.study[d.name] = {
    report: null, configfile: '', sheet: '', rolesKey: '{}', loading: false, error: '', request: 0,
  });
}

function studyRolesKey(roles) {
  return JSON.stringify(Object.fromEntries(Object.entries(roles || {}).sort(([a], [b]) => a.localeCompare(b))));
}

async function loadStudyReport(d, options = {}) {
  const s = studyState(d);
  const request = ++s.request;
  s.loading = true;
  s.error = '';
  const configfile = options.configfile !== undefined ? options.configfile : runFormFor(d).configfile;
  const bindRun = options.bindRun !== false;
  try {
    let report;
    if (options.roles !== undefined) {
      report = await api.post(`/api/pipelines/${d.name}/study`, {
        configfile,
        sheet: options.sheet || '',
        roles: options.roles,
      });
    } else {
      const query = new URLSearchParams({ configfile, sheet: options.sheet || '' });
      report = await api.get(`/api/pipelines/${d.name}/study?${query}`);
    }
    if (request !== s.request) return s.report;
    s.report = report;
    s.configfile = configfile;
    s.sheet = report.selected ? report.selected.path : '';
    s.rolesKey = studyRolesKey(report.roles);
    if (bindRun) {
      const f = runFormFor(d);
      f.study_sheet = report.selected ? report.selected.path : '';
      f.study_roles = report.roles || {};
      f.plan_digest = '';
    }
    return report;
  } catch (e) {
    if (request === s.request) s.error = e.message;
    throw e;
  } finally {
    if (request === s.request) s.loading = false;
  }
}

function studyGatePanel(d, report, error = '') {
  const gate = report ? report.gate : 'unknown';
  const summary = report && report.summary ? report.summary : {};
  const unconfigured = Boolean(report && report.selected && !report.selected.authoritative);
  const label = unconfigured ? 'Not configured for Run' : gate === 'ready' ? 'Ready' : gate === 'review' ? 'Review' : gate === 'blocked' ? 'Blocked' : 'No study sheet';
  const tone = gate === 'blocked' || unconfigured ? 'error' : gate === 'review' || gate === 'unknown' ? 'warn' : 'ok';
  return el('section', { class: `panel study-gate ${tone}`, 'aria-live': 'polite' },
    el('div', { class: 'study-gate-head' },
      el('h3', {}, 'Study Guard'),
      el('span', { class: `health-pill ${tone}` }, label)),
    el('div', { class: 'study-gate-metrics' },
      el('span', {}, `${summary.biological_units || 0} biological units`),
      el('span', {}, `${summary.groups || 0} groups`),
      el('span', {}, `${summary.errors || 0} blockers · ${summary.warnings || 0} warnings`)),
    unconfigured ? el('div', { class: 'muted err' }, 'This discovered table is not referenced by the selected Run config. Update Config and re-audit before a real run.') : null,
    error ? el('div', { class: 'muted err' }, error) : null,
    el('button', { class: 'ghost sm', onclick: () => { state.tab = 'study'; render(); } }, 'Review study'));
}

function reconcilePlannedStudy(d, f, planned, realRun) {
  const report = planned && planned.study;
  if (!report) return true;
  if (realRun && report.selected && !report.selected.authoritative) {
    state.tab = 'study';
    render();
    window.alert('The discovered study is not referenced by this Run config. Update Config and re-audit before a real run.');
    return false;
  }
  const s = studyState(d);
  const hadStudy = Boolean(report.selected || report.fingerprint);
  const matches = !hadStudy || Boolean(
    s.report && s.configfile === f.configfile &&
    s.sheet === ((report.selected && report.selected.path) || '') &&
    s.rolesKey === studyRolesKey(report.roles) &&
    s.report.fingerprint === report.fingerprint
  );
  s.report = report;
  s.configfile = f.configfile;
  s.sheet = report.selected ? report.selected.path : '';
  s.rolesKey = studyRolesKey(report.roles);
  f.study_sheet = s.sheet;
  f.study_roles = report.roles || {};
  f.plan_digest = '';
  if (!matches) {
    render();
    window.alert('Study Guard changed since the displayed audit. Review the refreshed findings, then launch again.');
    return false;
  }
  return true;
}

async function refreshRunStudyGate(d, f, host, onBound = null) {
  host.innerHTML = '';
  host.append(studyGatePanel(d, null));
  const s = studyState(d);
  try {
    const matches = s.report && s.configfile === f.configfile && s.sheet === (f.study_sheet || '') &&
      s.rolesKey === studyRolesKey(f.study_roles);
    const loaded = !matches;
    let report;
    let request = s.request;
    if (matches) {
      report = s.report;
    } else {
      const options = { configfile: f.configfile, sheet: f.study_sheet || '' };
      if (f.study_sheet || Object.keys(f.study_roles || {}).length) options.roles = f.study_roles || {};
      const pending = loadStudyReport(d, options);
      request = s.request;
      report = await pending;
      if (request !== s.request) return;
    }
    if (!host.isConnected && state.tab !== 'run') return;
    host.innerHTML = '';
    host.append(studyGatePanel(d, report));
    if (loaded && onBound) onBound(report);
  } catch (e) {
    if (s.loading || (s.report && s.configfile === f.configfile && s.sheet === (f.study_sheet || ''))) return;
    host.innerHTML = '';
    host.append(studyGatePanel(d, null, e.message));
  }
}

function studyDistribution(label, items) {
  const host = el('div', { class: 'study-distribution' }, el('h3', {}, label));
  const max = Math.max(1, ...(items || []).map(item => Number(item.n || 0)));
  if (!(items || []).length) {
    host.append(el('div', { class: 'muted' }, 'No mapped levels'));
    return host;
  }
  (items || []).forEach((item) => host.append(el('div', { class: 'study-dist-row' },
    el('span', { class: 'mono' }, item.level),
    el('span', { class: 'study-bar-track' }, el('span', { class: 'study-bar-fill', style: `width:${Math.max(4, Math.round(item.n / max * 100))}%` })),
    el('strong', {}, String(item.n)))));
  return host;
}

function studyBalanceTable(balance) {
  const conditions = balance.conditions || [];
  const batches = balance.batches || [];
  if (!conditions.length || !batches.length) return el('div', { class: 'muted' }, 'Batch balance is unavailable.');
  const counts = new Map((balance.cells || []).map(cell => [`${cell.condition}\u0000${cell.batch}`, cell.n]));
  const table = el('table', { class: 'qc-table study-balance' });
  table.append(el('thead', {}, el('tr', {}, el('th', {}, 'Condition'), ...batches.map(batch => el('th', { class: 'num' }, batch)))));
  const body = el('tbody');
  conditions.forEach(condition => body.append(el('tr', {},
    el('td', { class: 'samp' }, condition),
    ...batches.map(batch => el('td', { class: 'num' }, String(counts.get(`${condition}\u0000${batch}`) || 0))))));
  table.append(body);
  return el('div', { class: 'study-table-wrap' }, table);
}

function studyPreviewTable(report) {
  const mapped = [...new Set(Object.values(report.roles || {}).filter(Boolean))];
  const columns = mapped.length ? mapped : (report.columns || []).slice(0, 8);
  if (!columns.length || !(report.rows || []).length) return el('div', { class: 'muted' }, 'No cohort rows to preview.');
  const table = el('table', { class: 'qc-table study-preview-table' });
  table.append(el('thead', {}, el('tr', {}, ...columns.map(column => el('th', {}, column)))));
  const body = el('tbody');
  report.rows.slice(0, 100).forEach(row => body.append(el('tr', {},
    ...columns.map((column, index) => el('td', { class: index ? '' : 'samp' }, row[column] || '')))));
  table.append(body);
  return el('div', { class: 'study-table-wrap' }, table);
}

function studyFindings(report) {
  const host = el('div', { class: 'study-findings' });
  if (!(report.issues || []).length) {
    host.append(el('div', { class: 'study-empty-ok' }, 'No cohort or design findings.'));
    return host;
  }
  report.issues.forEach(item => host.append(el('div', { class: `study-finding ${item.severity}` },
    el('span', { class: `diag-level ${item.severity}` }, item.severity),
    el('div', {}, el('strong', {}, item.title), el('div', { class: 'muted' }, item.message)),
    item.rows ? el('span', { class: 'tag' }, `${item.rows.length} rows`) : null)));
  return host;
}

async function openPipelineCodeFile(d, path) {
  if (!path || path.startsWith('/')) return;
  const c = codeState(d.name);
  const openTab = c.tabs.find(item => item.path === path || item.loadedPath === path);
  if (openTab) {
    Object.assign(c, {
      activeTab: openTab.key, selected: openTab.path, loadedPath: openTab.loadedPath,
      content: openTab.content, saved: openTab.saved, dirty: openTab.dirty,
      language: openTab.language, size: openTab.size, mtime: openTab.mtime,
    });
    state.tab = 'code';
    render();
    return;
  }
  try {
    const r = await api.post(`/api/pipelines/${encodeURIComponent(d.name)}/code/read`, { path });
    let tab = c.tabs.find(item => item.path === r.path || item.loadedPath === r.path);
    if (!tab) {
      tab = { key: r.path };
      c.tabs.push(tab);
    }
    Object.assign(tab, {
      key: r.path, path: r.path, loadedPath: r.path, content: r.content, saved: r.content,
      dirty: false, language: r.language, size: r.size, mtime: r.mtime,
    });
    Object.assign(c, {
      activeTab: r.path, selected: r.path, loadedPath: r.path, content: r.content, saved: r.content,
      dirty: false, language: r.language, size: r.size, mtime: r.mtime,
    });
    state.tab = 'code';
    render();
  } catch (e) {
    window.alert('Could not open study source:\n' + e.message);
  }
}

async function viewStudy(view) {
  const d = state.detail;
  const slot = el('div', { 'aria-live': 'polite' });
  const refresh = el('button', { class: 'ghost sm' }, 'Refresh');
  view.append(el('div', { class: 'view-head' },
    el('div', {}, el('h2', {}, `Study · ${d.name}`), el('div', { class: 'muted' }, 'Cohort structure, biological independence, input integrity, and model estimability.')),
    refresh), slot);

  const draw = (report) => {
    slot.innerHTML = '';
    const source = el('select', {}, ...(report.candidates || []).map(candidate => el('option', {
      value: candidate.path,
      selected: report.selected && candidate.path === report.selected.path ? '' : null,
    }, `${candidate.path} · ${candidate.rows || 0} rows${candidate.authoritative ? ' · configured for Run' : ''}${candidate.error ? ' · unreadable' : ''}`)));
    const sourceStatus = el('span', { class: 'muted' });
    const analyze = async (sheet, roles = null) => {
      sourceStatus.textContent = 'analyzing...';
      try {
        const next = await loadStudyReport(d, {
          configfile: runFormFor(d).configfile,
          sheet,
          bindRun: false,
          ...(roles ? { roles } : {}),
        });
        draw(next);
      } catch (e) {
        sourceStatus.textContent = 'error: ' + e.message;
      }
    };
    source.onchange = () => analyze(source.value);

    if (!report.selected) {
      slot.append(el('div', { class: 'panel' },
        el('div', { class: 'toolbar' }, el('label', {}, 'Candidate table', source), sourceStatus),
        el('div', { class: 'empty slim' }, 'No sample or metadata table was discovered for this pipeline.')));
      return;
    }
    const roleLabels = [
      ['sample_id', 'Sample ID'], ['condition', 'Condition'], ['batch', 'Batch'],
      ['subject', 'Biological unit'], ['replicate', 'Replicate'], ['read1', 'Read 1'],
      ['read2', 'Read 2'], ['data', 'Data file'],
    ];
    const roleControls = el('div', { class: 'study-role-grid' });
    const selects = {};
    roleLabels.forEach(([role, label]) => {
      const select = el('select', {}, el('option', { value: '' }, '(not mapped)'),
        ...(report.columns || []).map(column => el('option', {
          value: column,
          selected: report.roles && report.roles[role] === column ? '' : null,
        }, column)));
      selects[role] = select;
      roleControls.append(el('label', {}, label, select));
    });
    Object.values(selects).forEach(select => select.onchange = () => analyze(
      report.selected.path,
      Object.fromEntries(Object.entries(selects).map(([role, control]) => [role, control.value])),
    ));
    const gateTone = report.gate === 'blocked' ? 'error' : report.gate === 'review' ? 'warn' : 'ok';
    const authoritative = Boolean(report.selected.authoritative);
    const configured = (report.candidates || []).find(candidate => candidate.authoritative);
    const useStudy = el('button', {
      class: 'ghost sm',
      disabled: authoritative ? null : '',
      title: authoritative
        ? 'Bind this audited table and role mapping to Run'
        : `Exploratory table only. Run remains bound to ${configured ? configured.path : 'the sample sheet referenced by the selected config'}.`,
      onclick: async () => {
      if (!authoritative) return;
      const f = runFormFor(d);
      sourceStatus.textContent = 'verifying configured study...';
      useStudy.setAttribute('disabled', '');
      try {
        const confirmed = await loadStudyReport(d, {
          configfile: f.configfile,
          sheet: '',
          roles: report.roles || {},
          bindRun: false,
        });
        if (!confirmed.selected || !confirmed.selected.authoritative ||
            confirmed.selected.path !== report.selected.path ||
            confirmed.fingerprint !== report.fingerprint) {
          draw(confirmed);
          window.alert('The configured study changed. Review the fresh audit, then click Use audited study again.');
          return;
        }
        f.study_sheet = confirmed.selected.path;
        f.study_roles = confirmed.roles || {};
        f.study_override = false;
        f.plan_digest = '';
        sourceStatus.textContent = 'audited study bound to Run';
      } catch (e) {
        sourceStatus.textContent = 'error: ' + e.message;
      } finally {
        if (sourceStatus.isConnected) useStudy.removeAttribute('disabled');
      }
    } }, 'Use audited study');
    if (!authoritative) {
      sourceStatus.textContent = configured
        ? `Exploratory only · Run remains bound to ${configured.path}`
        : 'Exploratory only · select a sample sheet referenced by the Run config';
    }
    slot.append(
      el('div', { class: 'study-controls' },
        el('div', { class: 'study-source-control' }, el('label', {}, 'Sample sheet'), source),
        report.selected.external ? null : el('button', { class: 'ghost sm', onclick: () => openPipelineCodeFile(d, report.selected.path) }, 'Open in Code'),
        useStudy,
        el('span', { class: `health-pill ${gateTone}` }, report.gate), sourceStatus),
      el('div', { class: 'results-summary' },
        resultStat('Biological units', String(report.summary.biological_units || 0), report.summary.biological_units ? 'ok' : 'warn'),
        resultStat('Groups', String(report.summary.groups || 0), report.summary.groups > 1 ? 'ok' : 'warn'),
        resultStat('Design rank', `${report.design.rank}/${report.design.columns.length}`, report.design.full_rank ? 'ok' : 'error'),
        resultStat('Study score', `${report.score}/100`, gateTone)),
      el('div', { class: 'study-layout' },
        el('section', { class: 'study-main' },
          el('div', { class: 'panel' }, el('h3', {}, 'Column roles'), roleControls),
          el('div', { class: 'panel' }, el('h3', {}, 'Factor balance'),
            el('div', { class: 'study-distribution-grid' },
              studyDistribution('Condition', report.distributions.condition || []),
              studyDistribution('Batch', report.distributions.batch || [])),
            studyBalanceTable(report.balance || {})),
          el('div', { class: 'panel' }, el('h3', {}, 'Model'),
            el('div', { class: 'kv compact' },
              el('div', { class: 'k' }, 'Formula'), el('div', { class: 'mono' }, report.design.formula || '—'),
              el('div', { class: 'k' }, 'Factors'), el('div', {}, (report.design.factors || []).join(', ') || '—'),
              el('div', { class: 'k' }, 'Rank'), el('div', {}, `${report.design.rank} of ${report.design.columns.length}`))),
          el('div', { class: 'panel' }, el('h3', {}, 'Cohort preview'), studyPreviewTable(report))),
        el('aside', { class: 'study-review panel' },
          el('h3', {}, `Findings · ${report.summary.errors || 0} blockers · ${report.summary.warnings || 0} warnings`),
          studyFindings(report))));
  };

  const load = async (force = false) => {
    const s = studyState(d);
    slot.innerHTML = '';
    slot.append(el('div', { class: 'muted' }, 'Analyzing cohort and design...'));
    try {
      const f = runFormFor(d);
      const report = !force && s.report && s.configfile === f.configfile &&
        s.sheet === (f.study_sheet || '') && s.rolesKey === studyRolesKey(f.study_roles)
        ? s.report
        : await loadStudyReport(d, {
          configfile: f.configfile,
          sheet: f.study_sheet || '',
          bindRun: false,
          ...(f.study_sheet || Object.keys(f.study_roles || {}).length ? { roles: f.study_roles || {} } : {}),
        });
      if (state.detail && state.detail.name === d.name && state.tab === 'study') draw(report);
    } catch (e) {
      slot.innerHTML = '';
      slot.append(el('div', { class: 'empty' }, e.message));
    }
  };
  refresh.onclick = () => load(true);
  load();
}

async function refreshRunQueue(shouldRender = false) {
  try {
    state.runQueue = await api.get('/api/run_queue');
    if (shouldRender) render();
  } catch (e) {}
}

function runQueuePanel() {
  const q = state.runQueue || { max_cores: 1, active_cores: 0, running: [], queued: [], lost: [] };
  const maxIn = el('input', { type: 'number', min: 1, max: 1024, value: q.max_cores || 1, style: 'max-width:90px' });
  const status = el('span', { class: 'muted' },
    `${q.active_cores || 0}/${q.max_cores || 1} local cores active · ${(q.queued || []).length} queued · ${(q.lost || []).length} orphaned`);
  const save = async () => {
    status.textContent = 'saving budget...';
    try {
      state.runQueue = await api.post('/api/run_queue', { max_cores: +maxIn.value || 1 });
      render();
    } catch (e) {
      status.textContent = 'error: ' + e.message;
    }
  };
  const rows = [...(q.running || []), ...(q.queued || []), ...(q.lost || [])];
  const list = el('div', { class: 'queue-list' });
  if (!rows.length) list.append(el('div', { class: 'muted' }, 'No running, queued, or orphaned Snakemake runs.'));
  rows.forEach((s) => {
    const cores = s.meta && s.meta.cores ? s.meta.cores : 1;
    const durableId = (s.meta && s.meta.durable_job_id) || s.id;
    const durableState = s.durable_state || (s.meta && s.meta.durable_state) || s.status;
    const identity = s.meta && s.meta.process_identity_status;
    const reserved = s.meta && s.meta.cores_reserved;
    const deadLost = durableState === 'lost' && identity === 'not-running';
    const cancellable = !deadLost && !['succeeded', 'failed', 'cancelled', 'blocked'].includes(durableState);
    list.append(el('div', { class: 'queue-row', onclick: () => focusOrOpen(s) },
      el('span', { class: `dot ${s.status}` }),
      el('span', { class: 'queue-title' }, s.title || s.command),
      el('span', { class: 'tag' }, durableState),
      el('span', { class: 'muted' }, `${cores} cores${reserved ? ' reserved' : ''} · ${identity || String(durableId).slice(0, 12)}`),
      cancellable ? el('button', { class: 'ghost sm danger-outline', onclick: async (e) => {
        e.stopPropagation();
        if (!window.confirm(`Cancel ${s.title || durableId}?`)) return;
        e.currentTarget.disabled = true;
        e.currentTarget.textContent = 'Canceling…';
        try { await api.post(`/api/jobs/${durableId}/cancel`, {}); await refreshRunQueue(true); }
        catch (err) { window.alert('Cancel failed: ' + err.message); await refreshRunQueue(true); }
      } }, 'Cancel') : deadLost ? el('button', { class: 'ghost sm danger-outline', onclick: async (e) => {
        e.stopPropagation();
        const reason = window.prompt('Acknowledge this orphaned job as failed. Enter an audit reason:');
        if (!reason || !reason.trim()) return;
        e.currentTarget.disabled = true;
        try {
          await api.post(`/api/jobs/${durableId}/resolve`, {
            action: 'acknowledge_failed', reason: reason.trim(), expected_version: s.state_version,
          });
          await refreshRunQueue(true);
        } catch (err) {
          window.alert('Resolve failed: ' + err.message);
          await refreshRunQueue(true);
        }
      } }, 'Resolve failed') : null));
  });
  return el('div', { class: 'card queue-card' },
    el('div', { class: 'toolbar' },
      el('strong', {}, 'Run queue'),
      el('label', { class: 'inline' }, 'Max local cores ', maxIn),
      el('button', { class: 'ghost sm', onclick: save }, 'Save budget'),
      status),
    list);
}

async function loadRunTemplates(name, shouldRender = false) {
  try {
    const res = await api.get(`/api/pipelines/${name}/run_templates`);
    state.runTemplates[name] = res.templates || [];
    if (shouldRender) render();
  } catch (e) {
    state.runTemplates[name] = [];
  }
}

function runTemplatePanel(d, f) {
  const templates = state.runTemplates[d.name];
  if (!templates) {
    loadRunTemplates(d.name, true);
    return el('div', { class: 'card' }, el('div', { class: 'muted' }, 'Loading saved run profiles...'));
  }
  const sel = el('select', { style: 'max-width:280px' },
    el('option', { value: '' }, templates.length ? 'Select saved profile' : 'No saved profiles'),
    ...templates.map(t => el('option', { value: t.id }, t.name)));
  const status = el('span', { class: 'muted' });
  const apply = () => {
    const t = templates.find(x => x.id === sel.value);
    if (!t) return;
    Object.assign(f, defaultRunForm(d), t.form || {});
    if (!f.study_sheet && Object.keys(f.study_roles || {}).length) f.study_roles = {};
    f.study_override = false;
    f.plan_digest = '';
    render();
  };
  const save = async () => {
    const name = window.prompt('Save run profile as:', sel.value ? (templates.find(t => t.id === sel.value) || {}).name : 'Default dry-run');
    if (!name) return;
    status.textContent = 'saving profile...';
    try {
      const id = sel.value || '';
      const form = { ...f };
      delete form.plan_digest;
      await api.post(`/api/pipelines/${d.name}/run_templates`, { id, name, form });
      await loadRunTemplates(d.name, true);
    } catch (e) {
      status.textContent = 'error: ' + e.message;
    }
  };
  const del = async () => {
    if (!sel.value) return;
    const t = templates.find(x => x.id === sel.value);
    if (!window.confirm(`Delete run profile "${t ? t.name : sel.value}"?`)) return;
    status.textContent = 'deleting profile...';
    try {
      await api.post(`/api/pipelines/${d.name}/run_templates/${sel.value}/delete`, {});
      await loadRunTemplates(d.name, true);
    } catch (e) {
      status.textContent = 'error: ' + e.message;
    }
  };
  return el('div', { class: 'card template-card' },
    el('div', { class: 'toolbar' },
      el('strong', {}, 'Run profiles'),
      sel,
      el('button', { class: 'ghost sm', onclick: apply }, 'Apply'),
      el('button', { class: 'ghost sm', onclick: save }, sel.value ? 'Update' : 'Save current'),
      el('button', { class: 'ghost sm danger-outline', onclick: del, disabled: templates.length ? null : '' }, 'Delete'),
      status));
}

async function refreshSlurm(shouldRender = false) {
  try {
    state.slurm = await api.get('/api/slurm/status');
    if (shouldRender) render();
  } catch (e) {
    state.slurm = { available: false, tools: {}, jobs: [], partitions: [], error: e.message };
    if (shouldRender) render();
  }
}

function slurmCapabilities(slurm) {
  const data = slurm || {};
  const tools = data.tools || {};
  const declared = data.capabilities || {};
  const has = (name, fallback) => Object.prototype.hasOwnProperty.call(declared, name)
    ? Boolean(declared[name]) : Boolean(fallback);
  return {
    submit: has('submit', tools.sbatch),
    monitor: has('monitor', tools.squeue),
    accounting: has('accounting', tools.sacct),
    cancel: has('cancel', tools.scancel && tools.squeue),
    partitions: has('partitions', tools.sinfo),
  };
}

function slurmFormFor(d) {
  return state.slurmForm[d.name] || (state.slurmForm[d.name] = {
    job_name: `bt-${d.name}`.slice(0, 32), partition: '', account: '', qos: '',
    time_limit: '04:00:00', mem: '', gres: '',
  });
}

function slurmSubmitPanel(d, f) {
  if (!state.slurm) refreshSlurm(true);
  const slurm = state.slurm || { available: false, tools: {}, partitions: [], jobs: [] };
  const capabilities = slurmCapabilities(slurm);
  const sf = slurmFormFor(d);
  const partitionOptions = ['', ...new Set((slurm.partitions || []).map(p => p.partition).filter(Boolean))];
  const jobIn = el('input', { type: 'text', value: sf.job_name });
  const partSel = el('select', {}, ...partitionOptions.map(p => el('option', { value: p, selected: p === sf.partition ? '' : null }, p || '(partition default)')));
  const accountIn = el('input', { type: 'text', value: sf.account, placeholder: 'account' });
  const qosIn = el('input', { type: 'text', value: sf.qos, placeholder: 'qos' });
  const timeIn = el('input', { type: 'text', value: sf.time_limit, placeholder: '04:00:00' });
  const memIn = el('input', { type: 'text', value: sf.mem, placeholder: 'e.g. 32G' });
  const gresIn = el('input', { type: 'text', value: sf.gres, placeholder: 'e.g. gpu:1' });
  const statusText = slurm.error
    ? `Slurm status unavailable: ${truncateText(slurm.error, 180)}`
    : capabilities.submit
      ? (capabilities.monitor ? 'sbatch and squeue detected' : 'sbatch detected; squeue unavailable')
      : 'sbatch not detected; submission unavailable';
  const status = el('span', { class: capabilities.submit ? 'muted' : 'muted err' }, statusText);
  const sync = () => Object.assign(sf, {
    job_name: jobIn.value, partition: partSel.value, account: accountIn.value,
    qos: qosIn.value, time_limit: timeIn.value, mem: memIn.value, gres: gresIn.value,
  });
  [jobIn, partSel, accountIn, qosIn, timeIn, memIn, gresIn].forEach(e => e.addEventListener('input', sync));
  let submitting = false;
  const submit = async () => {
    if (submitting) return;
    submitting = true;
    let intent = null;
    sync();
    status.textContent = 'locking Slurm RunPlan...';
    try {
      const studyCache = state.study[d.name];
      const cached = studyCache && studyCache.configfile === f.configfile &&
        studyCache.sheet === (f.study_sheet || '') && studyCache.rolesKey === studyRolesKey(f.study_roles)
        ? studyCache.report : null;
      const body = {
        ...f,
        ...sf,
        study_sheet: cached && cached.selected ? cached.selected.path : (f.study_sheet || ''),
        study_roles: cached && cached.roles ? cached.roles : (f.study_roles || {}),
        study_fingerprint: cached ? (cached.fingerprint || '') : '',
        study_override: false,
        plan_digest: '',
      };
      let planned = await api.post(`/api/pipelines/${d.name}/slurm_preview`, body);
      if (!reconcilePlannedStudy(d, f, planned, !body.dryrun)) return;
      const blocking = planned.study && planned.study.summary ? Number(planned.study.summary.errors || 0) : 0;
      if (!body.dryrun && blocking) {
        const proceed = window.confirm(`Study Guard found ${blocking} blocking issue${blocking === 1 ? '' : 's'}. Prepare an explicit Slurm override for this exact audit?`);
        if (!proceed) return;
        body.study_override = true;
        body.study_fingerprint = planned.study_fingerprint || '';
        planned = await api.post(`/api/pipelines/${d.name}/slurm_preview`, body);
        if (!reconcilePlannedStudy(d, f, planned, true)) return;
      }
      body.plan_digest = planned.plan_digest || '';
      if (!body.dryrun && !runPlanResolution(planned).launch_safe) {
        status.textContent = 'real submission blocked: RunPlan resolution is incomplete';
        window.alert(`Real Slurm execution is blocked by unresolved RunPlan material:\n\n${explainUnsafeRunPlan(planned)}`);
        return;
      }
      const confirmed = window.confirm(
        `Submit locked Slurm RunPlan?\n\n${planned.command}\n\n${shortPlanDigest(body.plan_digest)}`,
      );
      if (!confirmed) return;
      intent = pendingSubmissionIntent(`slurm:${d.name}`, body.plan_digest);
      const pending = await inspectPendingSubmissionIntent(intent);
      intent = pending.intent;
      body.idempotency_key = intent.key;
      if (pending.active) {
        const existingId = pending.job.scheduler_job_id || pending.job.id || 'pending intent';
        status.textContent = `existing ${pending.job.state} job ${existingId}; no duplicate was submitted`;
        await refreshSlurm(true);
        return;
      }
      status.textContent = 'submitting locked RunPlan to Slurm...';
      const res = await api.post(`/api/pipelines/${d.name}/slurm_submit`, body);
      if (!submissionIntentMustPersist(res)) clearSubmissionIntent(intent.slot);
      const jobLabel = res.job.job_id || res.job.id || 'intent pending reconciliation';
      status.textContent = submissionIntentMustPersist(res)
        ? `submission outcome unresolved; safe intent retained · ${shortPlanDigest(res.plan_digest)}`
        : `${res.replayed ? 'recovered' : 'submitted'} job ${jobLabel} · ${shortPlanDigest(res.plan_digest)}`;
      await refreshSlurm(true);
    } catch (e) {
      status.textContent = 'error (retry uses the same safe intent): ' + e.message;
    } finally {
      submitting = false;
    }
  };
  return el('div', { class: 'card slurm-submit' },
    el('h3', {}, 'Slurm submission'),
    el('div', { class: 'grid3' },
      el('div', {}, el('label', {}, 'Job name'), jobIn),
      el('div', {}, el('label', {}, 'Partition'), partSel),
      el('div', {}, el('label', {}, 'Time'), timeIn)),
    el('div', { class: 'grid3' },
      el('div', {}, el('label', {}, 'Memory'), memIn),
      el('div', {}, el('label', {}, 'Account'), accountIn),
      el('div', {}, el('label', {}, 'QoS'), qosIn)),
    el('div', {}, el('label', {}, 'GRES'), gresIn),
    el('div', { class: 'toolbar', style: 'margin-top:12px' },
      el('button', { class: 'ghost', onclick: submit, disabled: capabilities.submit ? null : '' }, 'Submit via sbatch'),
      el('button', { class: 'ghost sm', onclick: () => refreshSlurm(true) }, 'Refresh Slurm'),
      status));
}

function viewRun(view) {
  const d = state.detail;
  if (d.kind !== 'snakemake') {
    view.append(el('div', { class: 'empty' }, `Run UI currently supports Snakemake pipelines. This one is "${d.kind}".`));
    return;
  }
  const f = runFormFor(d);
  if (!state.runQueue) refreshRunQueue(true);

  const cfgOpts = [...new Set(['', f.configfile, ...(d.configs || []), ...(d.project_local || [])])];
  const profOpts = [...new Set(['', f.profile, ...(d.profiles || [])])];
  const envOpts = [...new Set([f.env, d.env_resolved, ...(d.available_envs || [])].filter(Boolean))];

  const targetIn = el('input', { type: 'text', value: f.target, placeholder: 'rule or output (blank = default target)' });
  const cfgSel = el('select', {}, ...cfgOpts.map(c => el('option', { value: c, selected: c === f.configfile ? '' : null }, c || '(default config)')));
  const profSel = el('select', {}, ...profOpts.map(c => el('option', { value: c, selected: c === f.profile ? '' : null }, c || '(no profile)')));
  const envSel = el('select', {}, ...envOpts.map(c => el('option', { value: c, selected: c === f.env ? '' : null }, c)));
  const coresIn = el('input', { type: 'number', value: f.cores, min: 1, max: 128 });
  const dryIn = el('input', { type: 'checkbox' }); dryIn.checked = f.dryrun;
  const condaIn = el('input', { type: 'checkbox' }); condaIn.checked = f.use_conda;
  const extraIn = el('input', { type: 'text', value: f.extra, placeholder: 'e.g. --until align --rerun-incomplete' });
  const preview = el('div', { class: 'cmd' }, 'building…');
  const impactHost = el('div', {});
  const studyHost = el('div', { class: 'run-study-host' });
  let studyConfig = f.configfile;

  const sync = () => {
    const configChanged = studyConfig !== cfgSel.value;
    Object.assign(f, { target: targetIn.value, configfile: cfgSel.value, profile: profSel.value,
      cores: +coresIn.value, dryrun: dryIn.checked, use_conda: condaIn.checked,
      extra: extraIn.value, env: envSel.value });
    f.plan_digest = '';
    if (configChanged) {
      f.study_sheet = '';
      f.study_roles = {};
      f.study_override = false;
      const s = studyState(d);
      s.request += 1;
      s.report = null;
      s.configfile = '';
      s.sheet = '';
      s.rolesKey = '{}';
    }
    updatePreview(d.name, f, preview);
    impactHost.innerHTML = '';
    impactHost.append(runImpactPanel(f));
    if (studyConfig !== f.configfile) {
      studyConfig = f.configfile;
      refreshRunStudyGate(d, f, studyHost, () => updatePreview(d.name, f, preview));
    }
  };
  [targetIn, cfgSel, profSel, envSel, coresIn, dryIn, condaIn, extraIn].forEach(e => e.addEventListener('input', sync));

  view.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('h2', {}, `Run · ${d.name}`), el('div', { class: 'muted' }, 'Configure, preview, and launch Snakemake without losing sight of resource impact.')),
      el('button', { class: 'ghost sm', onclick: () => { state.tab = 'resources'; render(); } }, 'Resources')),
    el('div', { class: 'run-layout' },
      el('section', { class: 'panel run-setup' },
        el('h3', {}, 'Launch settings'),
        runTemplatePanel(d, f),
        el('div', { class: 'grid2' },
          el('div', {}, el('label', {}, 'Target'), targetIn),
          el('div', {}, el('label', {}, 'Config file'), cfgSel)),
        el('div', { class: 'grid3' },
          el('div', {}, el('label', {}, 'Conda env'), envSel),
          el('div', {}, el('label', {}, 'Profile'), profSel),
          el('div', {}, el('label', {}, 'Cores'), coresIn)),
        el('div', {}, el('label', {}, 'Extra args'), extraIn),
        el('div', { class: 'switch-row' },
          el('label', { class: 'inline switch' }, dryIn, ' dry-run (-n)'),
          el('label', { class: 'inline switch' }, condaIn, ' --use-conda'))),
      el('aside', { class: 'run-review' },
        studyHost,
        impactHost,
        el('section', { class: 'panel sticky-panel' },
          el('h3', {}, 'Command preview'),
          preview,
          el('div', { class: 'launch-buttons' },
            el('button', { onclick: () => launchRun(d.name, f, true) }, 'Dry-run'),
            el('button', { class: 'danger', onclick: () => launchRun(d.name, f, false) }, 'Real run'))),
        runQueuePanel(),
        slurmSubmitPanel(d, f))));
  sync();
  refreshRunStudyGate(d, f, studyHost, () => updatePreview(d.name, f, preview));
}

function runImpactPanel(f) {
  const q = state.runQueue || { max_cores: 1, active_cores: 0, queued: [], running: [] };
  const requested = Math.max(1, Number(f.cores || 1));
  const max = Math.max(1, Number(q.max_cores || 1));
  const after = Math.min(100, Math.round((((q.active_cores || 0) + requested) / max) * 100));
  const queued = requested + (q.active_cores || 0) > max;
  return el('section', { class: `panel run-impact ${queued ? 'warn' : 'ok'}` },
    el('h3', {}, 'Launch impact'),
    el('div', { class: 'impact-grid' },
      el('div', {}, el('span', { class: 'metric-label' }, 'Requested'), el('strong', {}, `${requested} cores`)),
      el('div', {}, el('span', { class: 'metric-label' }, 'Queue'), el('strong', {}, queued ? 'Will queue' : 'Can start')),
      el('div', {}, el('span', { class: 'metric-label' }, 'After launch'), el('strong', {}, `${after}% budget`))));
}

let previewTimer = null;
const previewRequests = new Map();
function updatePreview(name, f, target) {
  const request = (previewRequests.get(name) || 0) + 1;
  previewRequests.set(name, request);
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const body = { ...f, plan_digest: '' };
    try {
      const res = await api.post(`/api/pipelines/${name}/preview`, body);
      if (previewRequests.get(name) !== request || !target.isConnected) return;
      const d = state.detail && state.detail.name === name ? state.detail : null;
      const s = state.study[name];
      if (d && s && s.report && res.study && s.report.fingerprint !== res.study.fingerprint) {
        s.report = res.study;
        s.configfile = f.configfile;
        s.sheet = res.study.selected ? res.study.selected.path : '';
        s.rolesKey = studyRolesKey(res.study.roles);
        f.study_sheet = s.sheet;
        f.study_roles = res.study.roles || {};
        f.plan_digest = '';
        render();
        return;
      }
      f.plan_digest = res.plan_digest || '';
      target.textContent = `${res.command}\n\n${shortPlanDigest(res.plan_digest)} · schema v${res.plan_schema_version || 1}`;
    }
    catch (e) {
      if (previewRequests.get(name) === request && target.isConnected) target.textContent = '(' + e.message + ')';
    }
  }, 450);
}

async function launchRun(name, f, dryrun) {
  state.launching = state.launching || {};
  if (state.launching[name]) return;
  state.launching[name] = true;
  let intent = null;
  try {
    const d = state.detail;
    const studyCache = state.study[name];
    const cached = studyCache && studyCache.configfile === f.configfile &&
      studyCache.sheet === (f.study_sheet || '') && studyCache.rolesKey === studyRolesKey(f.study_roles)
      ? studyCache.report : null;
    const body = {
      ...f,
      dryrun,
      study_sheet: cached && cached.selected ? cached.selected.path : (f.study_sheet || ''),
      study_roles: cached && cached.roles ? cached.roles : (f.study_roles || {}),
      study_override: false,
      study_fingerprint: cached ? (cached.fingerprint || '') : '',
      plan_digest: '',
    };
    let planned = await api.post(`/api/pipelines/${name}/preview`, body);
    if (!reconcilePlannedStudy(d, f, planned, !dryrun)) return;
    const blocking = planned.study && planned.study.summary ? Number(planned.study.summary.errors || 0) : 0;
    if (!dryrun && blocking) {
      const proceed = window.confirm(`Study Guard found ${blocking} blocking issue${blocking === 1 ? '' : 's'}. Prepare an explicit override for this exact audit?`);
      if (!proceed) {
        state.tab = 'study';
        render();
        return;
      }
      body.study_override = true;
      body.study_fingerprint = planned.study_fingerprint || '';
      planned = await api.post(`/api/pipelines/${name}/preview`, body);
      if (!reconcilePlannedStudy(d, f, planned, true)) return;
    }
    body.plan_digest = planned.plan_digest || '';
    f.plan_digest = body.plan_digest;
    if (!dryrun && !runPlanResolution(planned).launch_safe) {
      window.alert(`Real execution is blocked by unresolved RunPlan material:\n\n${explainUnsafeRunPlan(planned)}`);
      return;
    }
    const mode = dryrun ? 'dry-run' : 'REAL RUN';
    const confirmed = window.confirm(
      `Launch locked ${mode}?\n\n${planned.command}\n\n${shortPlanDigest(body.plan_digest)}`,
    );
    if (!confirmed) return;
    intent = pendingSubmissionIntent(`local:${name}`, body.plan_digest);
    const pending = await inspectPendingSubmissionIntent(intent);
    intent = pending.intent;
    body.idempotency_key = intent.key;
    if (pending.active) {
      const existingId = pending.job.session_id || pending.job.id;
      window.alert(`This RunPlan already has a durable ${pending.job.state} job (${existingId}). No duplicate was launched.`);
      await refreshRunQueue();
      return;
    }
    const res = await api.post(`/api/pipelines/${name}/run`, body);
    if (!submissionIntentMustPersist(res)) clearSubmissionIntent(intent.slot);
    if (res.queue) state.runQueue = res.queue;
    openTerminal(res.session);
  } catch (e) {
    window.alert('Could not launch run (retry will reuse the same safe intent):\n' + e.message);
    await refreshRunQueue();
  } finally {
    state.launching[name] = false;
  }
}

/* ---------------- DAG ---------------- */
const DAG_MODES = [['rulegraph', 'Rule graph'], ['dag', 'Job DAG'], ['filegraph', 'File graph']];

function dagHint(msg) {
  if (/MissingInputException|Missing input/i.test(msg))
    return 'Snakemake builds the job graph from real inputs. Pick a .test config (bundled data) or a config whose input files exist.';
  if (/not found/i.test(msg))
    return 'The chosen conda env has no snakemake binary — pick a different env above.';
  if (/Workflow defines that rule|ambiguous/i.test(msg))
    return 'Workflow/config mismatch — try a matching config file.';
  return 'See the Snakemake error above; adjust the config or env and re-render.';
}

function viewDag(view) {
  const d = state.detail;
  if (d.kind !== 'snakemake') {
    view.append(el('div', { class: 'empty' }, `DAG view supports Snakemake pipelines. This one is "${d.kind}".`));
    return;
  }
  const f = state.dagForm[d.name] || (state.dagForm[d.name] =
    { mode: 'rulegraph', configfile: (d.test_configs || [])[0] || '', env: d.env_resolved || '' });

  const cfgOpts = ['', ...(d.configs || []), ...(d.project_local || []), ...(d.test_configs || [])];
  const envOpts = [...new Set([f.env, d.env_resolved, ...(d.snakemake_envs || [])].filter(Boolean))];

  const modeSel = el('select', {}, ...DAG_MODES.map(([v, l]) => el('option', { value: v, selected: v === f.mode ? '' : null }, l)));
  const cfgSel = el('select', { style: 'max-width:280px' }, ...cfgOpts.map(c =>
    el('option', { value: c, selected: c === f.configfile ? '' : null }, c || '(default config)')));
  const envSel = el('select', {}, ...envOpts.map(c => el('option', { value: c, selected: c === f.env ? '' : null }, c)));
  const status = el('span', { class: 'muted' });
  const cmdLine = el('div', { class: 'cmd', style: 'display:none' });

  const viewport = el('div', { class: 'dag-viewport' });
  const inner = el('div', { class: 'dag-inner' });
  viewport.append(inner);
  let pz = null;

  const sync = () => Object.assign(f, { mode: modeSel.value, configfile: cfgSel.value, env: envSel.value });

  const showResult = (r) => {
    inner.innerHTML = '';
    if (r.command) { cmdLine.style.display = 'block'; cmdLine.textContent = r.command; }
    const png = typeof r.png === 'string' && /^[A-Za-z0-9+/]+={0,2}$/.test(r.png) ? r.png : '';
    if (r.ok && png) {
      const image = el('img', {
        class: 'dag-image',
        src: `data:image/png;base64,${png}`,
        alt: `${String(r.mode || 'workflow')} graph`,
        draggable: 'false',
      });
      inner.append(image);
      const nodeCount = Number.isSafeInteger(Number(r.node_count)) && Number(r.node_count) >= 0
        ? Number(r.node_count)
        : 0;
      status.replaceChildren(
        el('span', { class: 'dag-status-ok' }, `✓ ${String(r.mode || 'graph')}`),
        document.createTextNode(` · ${nodeCount} nodes · drag to pan, scroll to zoom`),
      );
      pz = attachPanZoom(viewport, inner);
      if (image.complete) requestAnimationFrame(() => pz.fit());
      else image.addEventListener('load', () => pz && pz.fit(), { once: true });
    } else {
      const msg = r.render_error || r.error || (r.ok ? 'invalid graph image returned' : 'no graph produced');
      inner.append(el('div', { class: 'dag-error' },
        el('strong', {}, '⚠ Could not build the graph'),
        el('pre', {}, msg),
        el('div', { class: 'muted', style: 'margin-top:8px' }, dagHint(msg))));
      status.replaceChildren(el('span', { class: 'dag-status-error' }, 'failed'));
    }
  };

  const render = async () => {
    sync();
    status.textContent = 'rendering… (running snakemake)';
    inner.innerHTML = '';
    inner.append(el('div', { class: 'empty' }, '⟳ generating graph…'));
    const q = new URLSearchParams({ mode: f.mode, configfile: f.configfile, env: f.env });
    try {
      const r = await api.get(`/api/pipelines/${d.name}/dag?${q}`);
      state.dagResult[d.name] = r;
      showResult(r);
    } catch (e) { status.textContent = 'error: ' + e.message; inner.innerHTML = ''; }
  };

  view.append(
    el('h2', {}, `DAG · ${d.name}`),
    el('div', { class: 'toolbar' },
      el('label', { class: 'inline' }, 'Graph ', modeSel),
      el('label', { class: 'inline' }, 'Config ', cfgSel),
      el('label', { class: 'inline' }, 'Env ', envSel),
      el('button', { class: 'sm', onclick: render }, '⟳ Render'),
      el('button', { class: 'ghost sm', onclick: () => pz && pz.fit() }, 'Fit'),
      el('button', { class: 'ghost sm', onclick: () => pz && pz.reset() }, '1:1'),
      status),
    cmdLine, viewport);

  if (state.dagResult[d.name]) showResult(state.dagResult[d.name]);
  else inner.append(el('div', { class: 'empty' },
    'Pick a config with data (a .test config works without staging inputs), then ⟳ Render.'));
}

function attachPanZoom(viewport, inner) {
  let scale = 1, tx = 0, ty = 0, dragging = false, ox = 0, oy = 0;
  inner.style.transformOrigin = '0 0';
  const apply = () => { inner.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`; };
  viewport.onwheel = (e) => {
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const ns = Math.min(12, Math.max(0.04, scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    tx = mx - (mx - tx) * (ns / scale); ty = my - (my - ty) * (ns / scale); scale = ns; apply();
  };
  viewport.onpointerdown = (e) => { dragging = true; viewport.setPointerCapture(e.pointerId); ox = e.clientX - tx; oy = e.clientY - ty; viewport.style.cursor = 'grabbing'; };
  viewport.onpointermove = (e) => { if (!dragging) return; tx = e.clientX - ox; ty = e.clientY - oy; apply(); };
  viewport.onpointerup = () => { dragging = false; viewport.style.cursor = 'grab'; };
  const fit = () => {
    const image = inner.querySelector('img.dag-image'); if (!image) return;
    const sw = image.naturalWidth || image.width || inner.offsetWidth;
    const sh = image.naturalHeight || image.height || inner.offsetHeight;
    const vw = viewport.clientWidth, vh = viewport.clientHeight;
    scale = Math.min(vw / sw, vh / sh) * 0.94;
    if (!isFinite(scale) || scale <= 0) scale = 1;
    tx = (vw - sw * scale) / 2; ty = (vh - sh * scale) / 2; apply();
  };
  return { fit, reset: () => { scale = 1; tx = 0; ty = 0; apply(); } };
}

/* ---------------- results ---------------- */
const PLOT_RE = /umap|t-?sne|pca|volcano|heatmap|fragment|fraglen|tss|fingerprint|frip|corr|enrich|saturation|qc/i;

function qcPanel(panel) {
  const wrap = el('div', { class: 'card qc-card' });
  if (panel.columns[0] === 'error') {
    wrap.append(el('div', { class: 'qc-title' }, '⚠ ' + panel.title),
      el('pre', { class: 'qc-err' }, panel.rows[0][0]));
    return wrap;
  }
  wrap.append(el('div', { class: 'qc-title' }, panel.title,
    el('span', { class: 'muted', style: 'margin-left:8px;font-weight:400;font-size:12px' }, panel.note || '')));
  const tbl = el('table', { class: 'qc-table' });
  tbl.append(el('thead', {}, el('tr', {}, ...panel.columns.map((c, i) =>
    el('th', { class: i ? 'num' : '' }, c)))));
  const tb = el('tbody');
  for (const row of panel.rows) {
    const tr = el('tr');
    row.forEach((v, i) => {
      const td = el('td', { class: i ? 'num' : 'samp' }, String(v));
      if (i && /%$/.test(String(v))) {
        const pct = parseFloat(v);
        if (isFinite(pct)) {
          const c = Math.min(100, Math.max(0, pct));
          td.style.background = `linear-gradient(90deg, rgba(79,156,255,.22) ${c}%, transparent ${c}%)`;
        }
      }
      tr.append(td);
    });
    tb.append(tr);
  }
  tbl.append(tb);
  wrap.append(el('div', { class: 'qc-scroll' }, tbl));
  return wrap;
}


function capsuleState(d) {
  return state.capsules[d.name] || (state.capsules[d.name] = {
    rows: [], left: '', right: 'current', diff: null, domain: 'all', loading: false, error: '', request: 0,
  });
}

function capsuleOptionLabel(item) {
  if (item.id === 'current') return 'Current checkout and outputs';
  const stamp = item.started ? new Date(item.started * 1000).toLocaleString() : item.id;
  return `${stamp} · ${item.status || 'unknown'} · ${(item.fingerprint || '').slice(0, 10)}`;
}

function loadCapsuleSettings(d, capsule) {
  if (!capsule || !capsule.launch) return;
  const f = runFormFor(d);
  Object.assign(f, capsule.launch, { dryrun: true, study_override: false, plan_digest: '' });
  const s = studyState(d);
  s.request += 1;
  s.report = null;
  s.configfile = '';
  s.sheet = '';
  s.rolesKey = '{}';
  state.tab = 'run';
  render();
}

function capsuleRunTable(d, c) {
  const table = el('table', { class: 'qc-table capsule-table' });
  table.append(el('thead', {}, el('tr', {}, ...['Run', 'Status', 'Source', 'Study', 'Outputs', 'Actions'].map(label => el('th', {}, label)))));
  const body = el('tbody');
  c.rows.forEach(item => {
    const source = item.commit ? `${item.branch || 'git'} @ ${item.commit.slice(0, 8)}${item.dirty ? ` +${item.dirty}` : ''}` : 'unversioned';
    const cohort = item.cohort || {};
    const summary = cohort.summary || {};
    body.append(el('tr', {},
      el('td', { class: 'samp' }, fmtTime(item.started),
        el('div', { class: 'muted' }, (item.fingerprint || '').slice(0, 14)),
        item.plan_digest ? el('div', { class: 'muted' }, shortPlanDigest(item.plan_digest)) : null),
      el('td', {}, el('span', { class: `health-pill ${item.status === 'failed' ? 'error' : item.complete ? 'ok' : 'warn'}` }, item.status || 'unknown')),
      el('td', { class: 'mono capsule-source' }, source),
      el('td', {}, `${summary.biological_units || 0} units · ${summary.groups || 0} groups`,
        el('div', { class: 'muted' }, cohort.gate || 'unknown')),
      el('td', { class: 'num' }, String((item.outputs || {}).count || 0),
        el('div', { class: 'muted' }, `+${(item.outputs || {}).added || 0} ~${(item.outputs || {}).changed || 0}`)),
      el('td', { class: 'capsule-actions' },
        el('button', { class: 'ghost sm', title: 'Use as comparison A', 'aria-label': `Use ${fmtTime(item.started)} as comparison A`, onclick: () => { c.request += 1; c.left = item.id; c.diff = null; render(); } }, 'A'),
        el('button', { class: 'ghost sm', title: 'Use as comparison B', 'aria-label': `Use ${fmtTime(item.started)} as comparison B`, onclick: () => { c.request += 1; c.right = item.id; c.diff = null; render(); } }, 'B'),
        el('button', { class: 'ghost sm', onclick: () => loadCapsuleSettings(d, item) }, 'Load setup'))));
  });
  table.append(body);
  return el('div', { class: 'study-table-wrap capsule-table-wrap' }, table);
}

function renderCapsuleDiff(host, d, c) {
  host.innerHTML = '';
  const diff = c.diff;
  if (!diff) {
    host.append(el('div', { class: 'empty slim' }, 'Choose two states and compare.'));
    return;
  }
  const domains = [['all', 'All'], ...diff.sections.map(section => [section.id, section.label])];
  const rail = el('div', { class: 'capsule-domain-rail' });
  domains.forEach(([id, label]) => rail.append(el('button', {
    class: `ghost sm ${c.domain === id ? 'active' : ''}`,
    'aria-pressed': c.domain === id ? 'true' : 'false',
    onclick: () => { c.domain = id; renderCapsuleDiff(host, d, c); },
  }, label)));
  const sections = el('div', { class: 'capsule-sections' });
  diff.sections.filter(section => c.domain === 'all' || c.domain === section.id).forEach(section => {
    const changed = (section.entries || []).filter(entry => entry.changed);
    const block = el('section', { class: `capsule-section ${section.status}` },
      el('div', { class: 'capsule-section-head' },
        el('h3', {}, section.label),
        el('span', { class: `health-pill ${changed.length ? 'warn' : 'ok'}` }, changed.length ? `${changed.length} changed` : 'same')));
    if (!changed.length) {
      block.append(el('div', { class: 'muted' }, 'No differences in this domain.'));
    } else {
      const rows = el('div', { class: 'capsule-diff-list' });
      changed.forEach(entry => rows.append(el('div', { class: 'capsule-diff-row' },
        el('strong', { class: 'breakable' }, entry.field),
        el('code', { class: 'breakable' }, entry.left),
        el('code', { class: 'breakable' }, entry.right),
        el('span', { class: `tag ${entry.impact === 'scientific' ? 'scientific' : ''}` }, entry.impact || 'drift'))));
      block.append(rows);
    }
    sections.append(block);
  });
  host.append(
    el('div', { class: `capsule-verdict ${diff.changed ? 'warn' : 'ok'}` },
      el('strong', {}, diff.verdict),
      el('span', { class: 'muted' }, `${diff.changed || 0} changed values across ${(diff.changed_domains || []).length} domains`)),
    el('div', { class: 'capsule-layout' }, rail, sections));
}

async function viewCapsules(view) {
  const d = state.detail;
  const c = capsuleState(d);
  const refresh = el('button', { class: 'ghost sm' }, 'Refresh');
  const slot = el('div', { 'aria-live': 'polite' });
  view.append(el('div', { class: 'view-head' },
    el('div', {}, el('h2', {}, `Capsules · ${d.name}`), el('div', { class: 'muted' }, 'Execution evidence and scientific drift across code, cohorts, environments, outputs, and QC.')),
    refresh), slot);

  const loadRows = async () => {
    c.request += 1;
    c.loading = true;
    c.error = '';
    slot.innerHTML = '';
    slot.append(el('div', { class: 'muted' }, 'Loading run capsules...'));
    try {
      const data = await api.get(`/api/pipelines/${d.name}/capsules?limit=50`);
      c.rows = data.capsules || [];
      c.diff = null;
      if (!c.left || (!c.rows.some(item => item.id === c.left) && c.left !== 'current')) c.left = c.rows[0] ? c.rows[0].id : '';
      if (!c.right || (!c.rows.some(item => item.id === c.right) && c.right !== 'current')) c.right = 'current';
      draw();
    } catch (e) {
      c.error = e.message;
      slot.innerHTML = '';
      slot.append(el('div', { class: 'empty' }, e.message));
    } finally {
      c.loading = false;
    }
  };

  const compare = async (host, status) => {
    if (!c.left || !c.right || c.left === c.right) {
      status.textContent = 'Choose two different states.';
      return;
    }
    const leftId = c.left;
    const rightId = c.right;
    const request = ++c.request;
    status.textContent = 'Computing bounded scientific diff...';
    try {
      const query = new URLSearchParams({ left: leftId, right: rightId });
      const diff = await api.get(`/api/pipelines/${d.name}/capsules/compare?${query}`);
      if (request !== c.request || c.left !== leftId || c.right !== rightId) return;
      c.diff = diff;
      status.textContent = '';
      renderCapsuleDiff(host, d, c);
    } catch (e) {
      status.textContent = 'error: ' + e.message;
    }
  };

  const draw = () => {
    slot.innerHTML = '';
    if (!c.rows.length) {
      slot.append(el('div', { class: 'empty' },
        el('div', {}, 'No run capsules have been captured for this pipeline.'),
        el('button', { class: 'ghost', style: 'margin-top:12px', onclick: () => { state.tab = 'run'; render(); } }, 'Open Run')));
      return;
    }
    const options = [{ id: 'current', status: 'working-tree', fingerprint: '' }, ...c.rows];
    const left = el('select', {}, ...options.map(item => el('option', { value: item.id, selected: item.id === c.left ? '' : null }, capsuleOptionLabel(item))));
    const right = el('select', {}, ...options.map(item => el('option', { value: item.id, selected: item.id === c.right ? '' : null }, capsuleOptionLabel(item))));
    const diffHost = el('div', { class: 'capsule-diff-host' });
    const status = el('span', { class: 'muted' });
    left.onchange = () => { c.request += 1; c.left = left.value; c.diff = null; status.textContent = ''; renderCapsuleDiff(diffHost, d, c); };
    right.onchange = () => { c.request += 1; c.right = right.value; c.diff = null; status.textContent = ''; renderCapsuleDiff(diffHost, d, c); };
    const swap = () => {
      c.request += 1;
      [c.left, c.right] = [c.right, c.left];
      c.diff = null;
      draw();
    };
    slot.append(
      el('div', { class: 'results-summary' },
        resultStat('Captured runs', String(c.rows.length), 'ok'),
        resultStat('Complete', String(c.rows.filter(item => item.complete).length), c.rows.some(item => item.complete) ? 'ok' : 'warn'),
        resultStat('Study reviewed', String(c.rows.filter(item => item.cohort && item.cohort.gate !== 'unknown').length), 'ok'),
        resultStat('Latest fingerprint', (c.rows[0].fingerprint || '').slice(0, 14), 'ok')),
      el('section', { class: 'panel' }, el('h3', {}, 'Run evidence'), capsuleRunTable(d, c)),
      el('div', { class: 'capsule-picker' },
        el('label', {}, 'A · baseline', left),
        el('button', { class: 'ghost icon-only capsule-swap', title: 'Swap comparison sides', 'aria-label': 'Swap comparison sides', onclick: swap }, '⇄'),
        el('label', {}, 'B · candidate', right),
        el('button', { onclick: () => compare(diffHost, status) }, 'Compare'), status),
      diffHost);
    if (c.diff) renderCapsuleDiff(diffHost, d, c);
    else compare(diffHost, status);
  };
  refresh.onclick = loadRows;
  loadRows();
}

function browseState(name) {
  return state.browse[name] || (state.browse[name] = {
    query: '', scope: 'all', path: '', results: [], breadcrumbs: [], count: 0,
    truncated: false, truncatedReason: '', loading: false, error: '', request: 0,
  });
}

function viewBrowse(view) {
  const d = state.detail;
  const b = browseState(d.name);
  const scopes = [
    ['all', 'All'], ['code', 'Code'], ['config', 'Config'], ['results', 'Results'], ['directories', 'Folders'],
  ];
  const search = el('input', {
    type: 'search', value: b.query || '', placeholder: 'Search names in this pipeline',
    autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Search pipeline files and directories',
  });
  const refresh = el('button', { class: 'ghost sm', type: 'button' }, 'Refresh');
  const scopeBar = el('div', { class: 'browse-scopes', role: 'group', 'aria-label': 'Browse result filters' });
  const breadcrumbs = el('nav', { class: 'browse-breadcrumbs', 'aria-label': 'Current pipeline directory' });
  const status = el('div', { class: 'browse-status muted', role: 'status', 'aria-live': 'polite' });
  const resultHost = el('div', { class: 'browse-results', role: 'listbox', 'aria-label': 'Matching files and directories' });

  view.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('h2', {}, `Browse · ${d.name}`),
        el('div', { class: 'muted' }, 'Search bounded, server-approved directories without exposing queries or paths in the browser URL.')),
      refresh),
    el('section', { class: 'browse-shell' },
      el('div', { class: 'browse-searchbar' }, search),
      scopeBar,
      breadcrumbs,
      status,
      resultHost));

  const normalizeResult = (item) => {
    if (typeof item === 'string') item = { path: item };
    const path = String(item.path || item.relative_path || '');
    const name = String(item.name || path.split('/').filter(Boolean).pop() || path || 'Unnamed item');
    const kind = String(item.kind || item.type || (item.is_dir ? 'directory' : 'file')).toLowerCase();
    const action = Object.prototype.hasOwnProperty.call(item, 'open_action')
      ? item.open_action
      : (kind === 'directory' ? 'browse' : 'download');
    return {
      ...item, path, name, kind,
      parent: String(item.parent || path.split('/').slice(0, -1).join('/')),
      openAction: action == null ? 'unavailable' : String(action).toLowerCase(),
    };
  };

  const actionLabel = (item) => {
    if (item.openAction === 'code' || ['code', 'config'].includes(item.kind)) return 'Open in Code';
    if (item.openAction === 'browse' || item.kind === 'directory' || item.kind === 'dir') return 'Browse folder';
    if (item.openAction === 'results' || item.kind.includes('result')) return 'Open in Results';
    if (item.openAction === 'unavailable') return 'Metadata only';
    return 'Open file';
  };

  const openItem = async (item) => {
    if (!item.path) return;
    if (item.openAction === 'unavailable') {
      window.alert('This file is too large or not eligible for browser opening. Its metadata is shown, but OmicsANG will not fetch its contents here.');
      return;
    }
    if (item.openAction === 'browse' || item.kind === 'directory' || item.kind === 'dir') {
      b.path = item.path;
      b.query = '';
      search.value = '';
      await load(true);
      return;
    }
    if (item.openAction === 'code' || ['code', 'config'].includes(item.kind)) {
      await openPipelineCodeFile(d, item.path);
      return;
    }
    if (item.openAction === 'results' || item.kind.includes('result')) {
      const rf = state.resultsFilter[d.name] || (state.resultsFilter[d.name] = { q: '', type: 'all' });
      rf.q = item.name || item.path;
      activateNavigation('results', 'auto');
      return;
    }
    await openAuthenticatedPostFile(
      `/api/pipelines/${encodeURIComponent(d.name)}/browse/open`,
      { path: item.path }, item.name,
    );
  };

  const fallbackBreadcrumbs = () => {
    const crumbs = [{ name: d.name, path: '' }];
    const parts = String(b.path || '').split('/').filter(Boolean);
    parts.forEach((name, index) => crumbs.push({ name, path: parts.slice(0, index + 1).join('/') }));
    return crumbs;
  };

  const drawBreadcrumbs = () => {
    breadcrumbs.innerHTML = '';
    const crumbs = b.breadcrumbs.length ? b.breadcrumbs : fallbackBreadcrumbs();
    crumbs.forEach((crumb, index) => {
      const path = String(crumb.path || '');
      const label = String(crumb.name || crumb.label || (index ? shortPath(path, 1) : d.name));
      if (index) breadcrumbs.append(el('span', { class: 'browse-separator', 'aria-hidden': 'true' }, '/'));
      breadcrumbs.append(index === crumbs.length - 1
        ? el('span', { class: 'browse-current', 'aria-current': 'page' }, label)
        : el('button', { class: 'browse-crumb', type: 'button', onclick: () => { b.path = path; b.query = ''; search.value = ''; load(true); } }, label));
    });
  };

  const drawScopes = () => {
    scopeBar.innerHTML = '';
    scopes.forEach(([id, label]) => scopeBar.append(el('button', {
      class: `browse-scope-chip ${b.scope === id ? 'active' : ''}`, type: 'button',
      'aria-pressed': b.scope === id ? 'true' : 'false',
      onclick: () => { if (b.scope === id) return; b.scope = id; load(true); },
    }, label)));
  };

  const drawResults = (focusFirst = false) => {
    resultHost.innerHTML = '';
    if (b.loading) {
      resultHost.append(el('div', { class: 'browse-empty' }, 'Searching approved directories…'));
      status.textContent = 'Searching…';
      return;
    }
    if (b.error) {
      resultHost.append(el('div', { class: 'browse-empty error' }, b.error));
      status.textContent = 'Browse search failed. The rest of OmicsANG remains available.';
      return;
    }
    const rows = (b.results || []).map(normalizeResult).filter(item => item.path);
    if (!rows.length) resultHost.append(el('div', { class: 'browse-empty' }, b.query ? 'No matching files or directories.' : 'This directory has no matching entries.'));
    rows.forEach((item, index) => resultHost.append(el('button', {
      class: 'browse-result', type: 'button', role: 'option', 'aria-selected': 'false',
      title: item.path, onclick: () => openItem(item),
    },
      el('span', { class: `browse-kind kind-${item.kind}` }, item.kind === 'directory' || item.kind === 'dir' ? 'DIR' : item.kind.toUpperCase().slice(0, 8)),
      el('span', { class: 'browse-result-main' }, el('strong', {}, item.name),
        el('span', { class: 'mono muted browse-result-path' }, item.parent || '.')),
      item.match || item.reason || item.size != null ? el('span', { class: 'browse-match' }, item.match || item.reason || fmtBytes(Number(item.size) || 0)) : null,
      el('span', { class: 'browse-open-action' }, actionLabel(item)))));
    const partial = b.truncated
      ? ` Partial results${b.truncatedReason ? `: ${b.truncatedReason}` : '; the bounded scan reached its limit'}.`
      : '';
    status.textContent = `${b.count == null ? rows.length : b.count} result${(b.count == null ? rows.length : b.count) === 1 ? '' : 's'}${partial}`;
    status.classList.toggle('warn', !!b.truncated);
    if (focusFirst) requestAnimationFrame(() => resultHost.querySelector('.browse-result')?.focus());
  };

  const load = async (focusResults = false) => {
    const request = ++b.request;
    b.loading = true;
    b.error = '';
    drawResults();
    try {
      const payload = await api.post(`/api/pipelines/${encodeURIComponent(d.name)}/browse/search`, {
        path: b.path || '', query: b.query.trim(), scope: b.scope || 'all', limit: 80,
      });
      if (request !== b.request || !view.isConnected) return;
      b.path = typeof payload.path === 'string' ? payload.path : b.path;
      b.breadcrumbs = Array.isArray(payload.breadcrumbs) ? payload.breadcrumbs : [];
      b.results = payload.results || payload.items || payload.matches || [];
      b.count = payload.count == null ? b.results.length : Number(payload.count);
      b.truncated = Boolean(payload.truncated || payload.partial);
      b.truncatedReason = String(payload.truncated_reason || payload.partial_reason || payload.reason || '');
    } catch (e) {
      if (request !== b.request || !view.isConnected) return;
      b.error = e.message;
      b.results = [];
      b.count = 0;
    } finally {
      if (request !== b.request || !view.isConnected) return;
      b.loading = false;
      drawBreadcrumbs();
      drawResults(focusResults);
    }
  };

  let timer = null;
  search.oninput = () => {
    b.query = search.value;
    clearTimeout(timer);
    timer = setTimeout(() => load(false), 180);
  };
  search.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); b.query = search.value; load(true); }
    else if (e.key === 'ArrowDown') {
      const first = resultHost.querySelector('.browse-result');
      if (first) { e.preventDefault(); first.focus(); }
    }
  };
  resultHost.onkeydown = (e) => {
    const rows = [...resultHost.querySelectorAll('.browse-result')];
    const index = rows.indexOf(document.activeElement);
    let next = null;
    if (e.key === 'ArrowDown') next = Math.min(rows.length - 1, index + 1);
    else if (e.key === 'ArrowUp') next = Math.max(0, index - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = rows.length - 1;
    if (next == null || !rows[next]) return;
    e.preventDefault(); rows[next].focus();
  };
  refresh.onclick = () => load(false);
  drawScopes();
  drawBreadcrumbs();
  load(false);
}

async function viewResults(view) {
  const d = state.detail;
  const refresh = el('button', { class: 'ghost sm' }, 'Refresh');
  view.append(el('div', { class: 'view-head' },
    el('div', {}, el('h2', {}, `Results · ${d.name}`), el('div', { class: 'muted' }, 'Review QC, figures, reports, tables, and result freshness in one place.')),
    refresh));
  const slot = el('div', {}, el('div', { class: 'muted' }, 'scanning outputs & parsing QC…'));
  view.append(slot);

  const rf = state.resultsFilter[d.name] || (state.resultsFilter[d.name] = { q: '', type: 'all' });
  let r = null;
  let q = null;

  const draw = () => {
    slot.innerHTML = '';
    const counts = (r && r.counts) || {};
    const sources = resultSources(r || {});
    const availableSources = sources.filter(source => source.available).length;
    const queryIn = el('input', {
      type: 'text', value: rf.q || '',
      placeholder: 'Filter results, samples, reports, or figure names',
      'aria-label': 'Filter result files',
    });
    const typeSel = el('select', { 'aria-label': 'Filter result type' },
      ...[['all', 'All files'], ['figures', 'Figures'], ['reports', 'Reports'], ['tables', 'Tables'], ['notebooks', 'Notebooks']].map(([v, l]) =>
        el('option', { value: v, selected: rf.type === v ? '' : null }, l)));
    const content = el('div', {});
    const redraw = () => {
      rf.q = queryIn.value;
      rf.type = typeSel.value;
      renderResultsContent(content, d, r || {}, q || { panels: [] }, rf);
    };
    queryIn.oninput = redraw;
    typeSel.oninput = redraw;
    slot.append(
      resultDirectoryManager(d, r || {}, rf, load),
      el('div', { class: 'results-summary' },
        resultStat('Reports', String(counts.reports || 0), counts.reports ? 'ok' : 'warn'),
        resultStat('Figures', String(counts.images || 0), counts.images ? 'ok' : 'warn'),
        resultStat('Tables', String(counts.tables || 0), counts.tables ? 'ok' : 'warn'),
        resultStat(
          'Sources',
          availableSources === sources.length ? String(sources.length) : `${availableSources} / ${sources.length} available`,
          availableSources ? 'ok' : 'warn')),
      el('div', { class: 'result-filterbar' }, queryIn, typeSel),
      content);
    redraw();
  };

  async function load() {
    refresh.disabled = true;
    try {
      [r, q] = await Promise.all([
        api.get(`/api/pipelines/${encodeURIComponent(d.name)}/results`),
        api.get(`/api/pipelines/${encodeURIComponent(d.name)}/qc`).catch(() => ({ panels: [] })),
      ]);
      draw();
    } catch (e) {
      slot.innerHTML = '';
      slot.append(el('div', { class: 'empty' }, e.message));
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.onclick = load;
  await load();
}

function resultSources(r) {
  const attachmentItems = r.attachments || [];
  const attachments = new Set(attachmentItems.map(item =>
    typeof item === 'string' ? item : (item.path || item.root || '')));
  const raw = (r.sources && r.sources.length)
    ? r.sources
    : (r.roots || []).map((path, i) => ({ id: `legacy-${i}`, path, kind: 'discovered' }));
  const merged = [...raw];
  const activePaths = new Set(raw.map(item => typeof item === 'string' ? item : (item.path || item.root || '')));
  attachmentItems.forEach(item => {
    const path = typeof item === 'string' ? item : (item.path || item.root || '');
    if (path && !activePaths.has(path)) merged.push(item);
  });
  return merged.map((item, i) => {
    if (typeof item === 'string') item = { path: item };
    const path = item.path || item.root || '';
    const origins = Array.isArray(item.origins) ? item.origins : [];
    const label = item.label || item.name || shortPath(path, 2);
    return {
      ...item,
      id: item.id || item.source_id || `source-${i}`,
      path,
      label,
      kind: item.kind || item.type || origins.join(' + ') || (attachments.has(path) ? 'attachment' : 'result'),
      external: item.external == null ? String(label).startsWith('/') : Boolean(item.external),
      attached: Boolean(item.attached || attachments.has(path) || item.kind === 'attached'),
      available: item.available !== false && item.exists !== false,
    };
  });
}

function resultDirectoryManager(d, r, rf, onSourcesChanged) {
  const safeName = String(d.name || 'pipeline').replace(/[^a-zA-Z0-9_-]/g, '-');
  const headingId = `result-directories-${safeName}`;
  const section = el('section', { class: 'panel result-directory-panel', 'aria-labelledby': headingId });
  const apiVersion = Number(state.meta && state.meta.api && state.meta.api.results_directories || 0);
  if (apiVersion < 2) {
    section.append(
      el('div', { class: 'result-directory-head' },
        el('div', {}, el('h3', { id: headingId }, 'Result directories'),
          el('div', { class: 'muted' }, 'Attach an existing output directory without moving or copying its files.'))),
      el('div', { class: 'result-directory-compat', role: 'alert' },
        el('strong', {}, 'Backend restart required'),
        el('span', {}, 'This page has the new Results interface, but the running OmicsANG server has the older API. Restart OmicsANG, then hard-refresh this page.')));
    return section;
  }
  const ds = rf.directories || (rf.directories = {
    query: '', exactPath: '', rootId: '', candidates: [], allowedRoots: [], preview: null,
    busy: '', message: '', error: '', bounds: null, expanded: null,
    lastSourceCount: null, lastMissingCount: null,
  });
  const endpoint = `/api/pipelines/${encodeURIComponent(d.name)}/results`;
  const pathOf = item => typeof item === 'string' ? item : (item.path || item.root || item.absolute_path || '');
  ds.exactPath = ds.exactPath || '';
  ds.rootId = ds.rootId || '';
  const rootOptions = () => (ds.allowedRoots || []).map((item) => {
    if (typeof item === 'string') return null;
    const id = item.root_id || item.id || '';
    return id ? { id: String(id), label: item.label || item.name || 'Approved root' } : null;
  }).filter(Boolean);

  const boundsText = () => {
    const b = ds.bounds || {};
    const scanned = b.visited == null ? (b.scanned == null ? b.directories_scanned : b.scanned) : b.visited;
    const limit = b.limit == null ? b.max_results : b.limit;
    const depth = b.max_depth == null ? b.depth : b.max_depth;
    const parts = [];
    if (scanned != null) parts.push(`${scanned} directories checked`);
    if (depth != null) parts.push(`depth ≤ ${depth}`);
    if (limit != null) parts.push(`limit ${limit}`);
    return parts.join(' · ');
  };

  const previewPath = async (path) => {
    path = String(path || '').trim();
    if (!path) return;
    ds.busy = 'preview'; ds.error = ''; ds.message = `Previewing ${shortPath(path, 3)}…`; ds.preview = null; draw();
    try {
      const payload = await api.post(`${endpoint}/preview`, { path });
      ds.preview = payload.preview || payload;
      ds.message = `Preview ready for ${shortPath(path, 3)}.`;
    } catch (e) {
      ds.error = e.message; ds.message = '';
    } finally { ds.busy = ''; draw(); }
  };

  const search = async () => {
    ds.busy = 'search'; ds.error = ''; ds.message = 'Searching allowed result locations…'; ds.preview = null;
    ds.truncated = false; ds.truncatedReason = ''; draw();
    try {
      const payload = await api.post(`${endpoint}/directories/search`, {
        query: String(ds.query || '').trim(), root_id: ds.rootId || '', limit: 40,
      });
      ds.candidates = payload.candidates || payload.items || payload.directories || [];
      const allowed = payload.allowed_roots || payload.allowedRoots || ds.allowedRoots || [];
      ds.allowedRoots = allowed.map(item => typeof item === 'string' ? null : ({
        root_id: item.root_id || item.id || '', label: item.label || item.name || 'Approved root',
      })).filter(item => item && item.root_id);
      ds.bounds = payload.bounds || null;
      ds.truncated = Boolean(payload.truncated || payload.partial || (ds.bounds && ds.bounds.truncated));
      ds.truncatedReason = payload.truncated_reason || payload.partial_reason ||
        (ds.bounds && (ds.bounds.reason || (Array.isArray(ds.bounds.reasons) ? ds.bounds.reasons.join(', ') : ''))) || '';
      ds.message = ds.candidates.length
        ? `${ds.candidates.length} director${ds.candidates.length === 1 ? 'y' : 'ies'} found.`
        : 'No matching directories found in the allowed search roots.';
    } catch (e) {
      ds.error = e.message; ds.message = ''; ds.candidates = [];
    } finally { ds.busy = ''; draw(); }
  };

  const mutate = async (action, path) => {
    path = String(path || '').trim();
    if (!path) return;
    if (action === 'detach') ds.expanded = true;
    ds.busy = action; ds.error = ''; ds.message = `${action === 'attach' ? 'Attaching' : 'Detaching'} ${shortPath(path, 3)}…`; draw();
    try {
      await api.post(`${endpoint}/${action}`, { path });
      ds.busy = '';
      ds.preview = null;
      ds.expanded = action !== 'attach';
      ds.message = `${action === 'attach' ? 'Attached' : 'Detached'} ${path}.`;
      await onSourcesChanged();
    } catch (e) {
      ds.busy = ''; ds.error = e.message; ds.message = ''; draw();
    }
  };

  const previewCard = () => {
    if (!ds.preview) return null;
    const p = ds.preview;
    const source = p.directory || p.source || {};
    const counts = p.counts || source.counts || {};
    const path = p.path || source.path || source.root || '';
    const countEntries = Object.entries(counts).filter(([, value]) => typeof value === 'number');
    const sampleGroups = p.samples && !Array.isArray(p.samples) ? p.samples : {};
    const files = p.files || p.sample_files || p.examples || (Array.isArray(p.samples) ? p.samples :
      Object.entries(sampleGroups).flatMap(([kind, items]) => (items || []).map(item =>
        typeof item === 'string' ? { path: item, preview_kind: kind } : { ...item, preview_kind: kind })));
    return el('div', { class: 'result-directory-preview' },
      el('div', { class: 'result-directory-preview-head' },
        el('strong', {}, 'Directory preview'),
        path ? el('span', { class: 'mono breakable', title: path }, path) : null),
      countEntries.length ? el('div', { class: 'result-preview-counts' },
        ...countEntries.map(([key, value]) => el('span', { class: 'tag' }, `${key}: ${value}`)),
        p.already_attached ? el('span', { class: 'tag scientific' }, 'already attached') : null) : null,
      files.length ? el('div', { class: 'result-preview-files' },
        ...files.slice(0, 6).map(item => el('span', { class: 'mono' },
          typeof item === 'string' ? item : `${item.preview_kind ? `${item.preview_kind} · ` : ''}${item.display_path || item.path || item.name || 'file'}`))) : null);
  };

  function draw() {
    section.innerHTML = '';
    const sources = resultSources(r);
    const sourcePaths = new Set(sources.map(item => item.path));
    const availableCount = sources.filter(item => item.available).length;
    const missingCount = sources.length - availableCount;
    if (typeof ds.expanded !== 'boolean') {
      ds.expanded = sources.length === 0 || missingCount > 0;
    } else if (
      (missingCount > 0 && Number(ds.lastMissingCount || 0) === 0)
      || (sources.length === 0 && Number(ds.lastSourceCount || 0) > 0)
    ) {
      ds.expanded = true;
    }
    ds.lastSourceCount = sources.length;
    ds.lastMissingCount = missingCount;
    section.classList.toggle('collapsed', !ds.expanded);
    const busy = Boolean(ds.busy);
    const input = el('input', {
      id: `${headingId}-search`, type: 'text', value: ds.query || '',
      placeholder: 'Directory name, for example results or multiqc',
      autocomplete: 'off', spellcheck: 'false', disabled: busy ? '' : null,
    });
    input.oninput = () => { ds.query = input.value; };
    input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); search(); } };
    const exactPath = el('input', {
      id: `${headingId}-exact-path`, type: 'text', value: ds.exactPath || '',
      placeholder: 'Exact approved directory path', autocomplete: 'off', spellcheck: 'false', disabled: busy ? '' : null,
    });
    exactPath.oninput = () => { ds.exactPath = exactPath.value; };
    exactPath.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); previewPath(ds.exactPath); } };
    const roots = rootOptions();
    const baseSelect = el('select', { 'aria-label': 'Result directory search root', disabled: busy || !roots.length ? '' : null },
      el('option', { value: '', selected: !ds.rootId ? '' : null }, 'All allowed roots'),
      ...roots.map(root => el('option', { value: root.id, selected: ds.rootId === root.id ? '' : null }, root.label)));
    baseSelect.onchange = () => { ds.rootId = baseSelect.value; };

    const sourceList = el('div', { class: 'result-source-list' });
    if (!sources.length) sourceList.append(el('div', { class: 'empty slim' }, 'No result directories are active. Search or paste a path below to attach one.'));
    sources.forEach(source => sourceList.append(el('div', { class: `result-source-row ${source.available ? '' : 'unavailable'}` },
      el('div', { class: 'result-source-main' },
        el('div', { class: 'result-source-title' },
          el('strong', {}, source.label),
          el('span', { class: 'tag' }, source.kind),
          source.external ? el('span', { class: 'tag' }, 'external') : null,
          source.attached ? el('span', { class: 'tag scientific' }, 'attached') : null,
          el('span', { class: `result-source-status ${source.available ? 'available' : 'missing'}` }, source.available ? 'available' : 'missing')),
        el('div', { class: 'mono breakable result-source-path', title: source.path }, source.path || '—')),
      source.attached ? el('button', {
        class: 'ghost sm danger-outline', disabled: busy ? '' : null,
        title: 'Remove this Results-tab attachment; files are not deleted',
        onclick: () => mutate('detach', source.path),
      }, 'Detach') : null)));

    const controls = el('div', { class: 'result-directory-controls' },
      el('div', { class: 'result-directory-input' },
        el('label', { for: input.id }, 'Search directory names'), input),
      el('div', { class: 'result-directory-root' }, el('label', {}, 'Search within'), baseSelect),
      el('div', { class: 'result-directory-actions' },
        el('button', { class: 'ghost', disabled: busy ? '' : null, onclick: search }, ds.busy === 'search' ? 'Searching…' : 'Search'),
      ),
      el('div', { class: 'result-directory-exact' },
        el('label', { for: exactPath.id }, 'Preview or attach an exact path'), exactPath,
        el('div', { class: 'result-directory-actions' },
          el('button', {
            class: 'ghost', disabled: busy || !String(ds.exactPath || '').trim() ? '' : null,
            title: 'Preview the exact approved directory path', onclick: () => previewPath(ds.exactPath),
          }, 'Preview path'),
          el('button', {
            disabled: busy || !String(ds.exactPath || '').trim() ? '' : null,
            title: 'Attach the exact approved directory path', onclick: () => mutate('attach', ds.exactPath),
          }, 'Attach path'))));

    const candidates = el('div', { class: 'result-candidate-list' });
    (ds.candidates || []).forEach(candidate => {
      const path = pathOf(candidate);
      if (!path) return;
      const current = sources.find(source => source.path === path);
      const attached = Boolean(candidate.attached || (current && current.attached));
      const active = Boolean(candidate.active || attached || sourcePaths.has(path));
      candidates.append(el('div', { class: 'result-candidate-row' },
        el('div', { class: 'result-candidate-main' },
          el('strong', {}, candidate.name || shortPath(path, 1)),
          el('span', { class: 'mono breakable', title: path }, path),
          candidate.match || candidate.reason ? el('small', { class: 'muted' }, candidate.match || candidate.reason) : null),
        el('div', { class: 'result-candidate-actions' },
          active ? el('span', { class: 'tag' }, 'active') : null,
          el('button', { class: 'ghost sm', disabled: busy ? '' : null, onclick: () => previewPath(path) }, 'Preview'),
          el('button', { class: 'sm', disabled: busy || active ? '' : null, onclick: () => mutate('attach', path) }, attached ? 'Attached' : (active ? 'Active' : 'Attach')))));
    });

    const bodyId = `${headingId}-body`;
    const toggle = el('button', {
      class: 'ghost sm result-directory-toggle',
      'aria-expanded': ds.expanded ? 'true' : 'false',
      'aria-controls': bodyId,
      onclick: () => {
        ds.expanded = !ds.expanded;
        draw();
        const nextToggle = section.querySelector('.result-directory-toggle');
        if (nextToggle) nextToggle.focus();
      },
    }, ds.expanded ? 'Hide directories' : 'Manage directories');
    const body = el('div', {
      id: bodyId, class: 'result-directory-body', hidden: ds.expanded ? null : '',
    },
      el('div', { class: 'muted result-directory-help' }, 'Attach an existing output directory without moving or copying its files.'),
      sourceList,
      controls,
      el('div', { class: `result-directory-status ${ds.error ? 'error' : ''}`, role: 'status', 'aria-live': 'polite' },
        ds.error || ds.message || 'Search is limited to server-approved roots.',
        ds.truncated ? el('span', { class: 'warn' }, `Partial results${ds.truncatedReason ? `: ${ds.truncatedReason}` : ' — the bounded search reached its limit'}.`) : null,
        boundsText() ? el('span', {}, boundsText()) : null),
      candidates.childElementCount ? candidates : null,
      previewCard());
    section.append(
      el('div', { class: 'result-directory-head' },
        el('div', { class: 'result-directory-heading' },
          el('h3', { id: headingId }, 'Result directories'),
          el('span', { class: 'result-directory-summary', role: 'status', 'aria-live': 'polite' },
            `${sources.length} active · ${availableCount} available · ${missingCount} missing`)),
        toggle),
      body);
  }

  draw();
  return section;
}

function resultStat(label, value, level = 'ok') {
  return el('div', { class: `metric-tile passive ${level}` },
    el('span', { class: 'metric-label' }, label),
    el('strong', {}, value));
}

function renderResultsContent(slot, d, r, q, rf) {
  slot.innerHTML = '';
  const needle = (rf.q || '').toLowerCase();
  const keep = (item) => !needle || `${item.display_path || ''} ${item.path || ''} ${item.name || ''}`.toLowerCase().includes(needle);

  const fileUrl = (item) => {
    const path = item.relative_path || item.path || '';
    if (item.source_id && path) {
      return `/api/pipelines/${encodeURIComponent(d.name)}/results/file?source=${encodeURIComponent(item.source_id)}&path=${encodeURIComponent(path)}`;
    }
    return `/api/file?pipeline=${encodeURIComponent(d.name)}&path=${encodeURIComponent(path)}`;
  };

  // --- parsed QC summary ---
  if ((rf.type === 'all') && (q.panels || []).length) {
    slot.append(el('h3', {}, 'QC summary'));
    q.panels.forEach(p => slot.append(qcPanel(p)));
  }

  // --- key figures (named like known plots) ---
  const images = (r.images || []).filter(keep);
  const keyImgs = images.filter(im => PLOT_RE.test(im.name)).slice(0, 12);
  if ((rf.type === 'all' || rf.type === 'figures') && keyImgs.length) {
    slot.append(el('h3', {}, 'Key figures'));
    const g = el('div', { class: 'gallery' });
    keyImgs.forEach(im => {
      const image = el('img', { loading: 'lazy', alt: '' });
      g.append(el('button', { class: 'thumb', 'aria-label': `Open figure ${im.name}`, onclick: () => openLightbox(im, fileUrl(im)) },
        image, el('span', { class: 'cap' }, im.name)));
      loadAuthenticatedImage(image, fileUrl(im));
    });
    slot.append(g);
  }

  const reports = (r.reports || []).filter(keep);
  if ((rf.type === 'all' || rf.type === 'reports') && reports.length) {
    slot.append(el('h3', {}, 'Reports'));
    const tb = el('div', { class: 'toolbar' });
    reports.forEach(rep => tb.append(el('button', { class: 'ghost sm', onclick: () => openAuthenticatedFile(fileUrl(rep), rep.name) }, 'Open ' + rep.name)));
    slot.append(tb);
  }
  if ((rf.type === 'all' || rf.type === 'figures') && images.length) {
    slot.append(el('h3', {}, 'Figures'));
    const g = el('div', { class: 'gallery' });
    images.forEach(im => {
      const image = el('img', { loading: 'lazy', alt: '' });
      g.append(el('button', { class: 'thumb', 'aria-label': `Open figure ${im.name}`, onclick: () => openLightbox(im, fileUrl(im)) },
        image, el('span', { class: 'cap' }, im.name)));
      loadAuthenticatedImage(image, fileUrl(im));
    });
    slot.append(g);
  }
  for (const [key, label] of [['tables', 'Tables'], ['notebooks', 'Notebooks']]) {
    if (rf.type !== 'all' && rf.type !== key) continue;
    const rows = (r[key] || []).filter(keep);
    if (!rows.length) continue;
    slot.append(el('h3', {}, label));
    const ul = el('ul', { class: 'filelist' });
    rows.forEach(t => ul.append(el('li', {},
      el('button', { class: 'result-file-link', onclick: () => openAuthenticatedFile(fileUrl(t), t.name) }, t.display_path || t.path),
      el('span', { class: 'sz' }, fmtBytes(Number(t.size) || 0)))));
    slot.append(ul);
  }
  if (!slot.children.length) slot.append(el('div', { class: 'empty slim' }, 'No matching results.'));
}

function openLightbox(item, src) {
  if (!lightbox) { openAuthenticatedFile(src, item.name || 'figure'); return; }
  lightbox.innerHTML = '';
  lightbox.classList.remove('hidden');
  const image = el('img', { alt: item.name || item.path });
  lightbox.append(el('div', { class: 'lightbox-backdrop', onclick: closeLightbox }),
    el('figure', { class: 'lightbox-figure' },
      el('button', { class: 'ghost sm icon-only', onclick: closeLightbox, title: 'Close' }, '×'),
      image,
      el('figcaption', {}, el('strong', {}, item.name || 'figure'), el('span', { class: 'muted' }, item.display_path || item.path || ''))));
  loadAuthenticatedImage(image, src);
}

function closeLightbox() {
  if (lightbox) lightbox.classList.add('hidden');
}

/* ---------------- config editor (schema-aware form + raw) ---------------- */
function renderField(f) {
  const row = el('div', { class: 'cfg-field' });
  const lab = el('label', { class: 'cfg-label' }, f.label);
  if (f.pathlike) lab.append(el('span', { class: 'tag', style: 'margin-left:6px' }, 'path'));
  let input, get;
  if (f.type === 'boolean') {
    input = el('input', { type: 'checkbox' }); input.checked = !!f.value; get = () => input.checked;
  } else if (f.type === 'integer' || f.type === 'number') {
    input = el('input', { type: 'number', value: f.value, step: f.type === 'integer' ? '1' : 'any' });
    get = () => input.value;
  } else if (f.type === 'enum') {
    const opts = [...new Set([String(f.value), ...(f.enum || [])])];
    input = el('select', {}, ...opts.map(o => el('option', { value: o, selected: o === String(f.value) ? '' : null }, o)));
    get = () => input.value;
  } else if (f.type === 'array') {
    input = el('textarea', { class: 'cfg-array', spellcheck: 'false' }, (f.value || []).join('\n'));
    get = () => input.value.split('\n');
  } else if (f.type === 'yaml') {
    input = el('textarea', { class: 'cfg-yaml', spellcheck: 'false' }, f.value || '');
    get = () => input.value;
  } else {
    input = el('input', { type: 'text', value: f.value == null ? '' : f.value }); get = () => input.value;
  }
  if (f.pathlike && input && input.tagName === 'INPUT' && input.type === 'text') {
    const textInput = input;
    const opts = (f.path_options || []).filter(o => o && o.value);
    if (opts.length) {
      const listId = `path-options-${String(f.id || '').replace(/[^A-Za-z0-9_-]/g, '-')}`;
      textInput.setAttribute('list', listId);
      const datalist = el('datalist', { id: listId },
        ...opts.map(o => el('option', { value: o.value }, o.value)));
      const picker = el('select', { class: 'cfg-path-picker', title: 'Browse existing pipeline paths' },
        el('option', { value: '' }, `Browse ${f.path_kind === 'dir' ? 'folders' : 'paths'}...`),
        ...opts.map(o => el('option', { value: o.value }, `${o.kind || 'path'}: ${o.value}`)));
      picker.onchange = () => {
        if (picker.value) {
          textInput.value = picker.value;
          picker.value = '';
        }
      };
      input = el('div', { class: 'cfg-path-control' }, textInput, picker, datalist);
      get = () => textInput.value;
    }
  }
  const msg = el('div', { class: 'cfg-msg' });
  row.append(lab, input, f.help ? el('div', { class: 'cfg-help' }, f.help) : null, msg);
  return {
    el: row, id: f.id,
    get: () => ({ id: f.id, path: f.path, type: f.type, enum: f.enum, pathlike: f.pathlike, path_kind: f.path_kind, value: get() }),
    mark: (level, message) => { row.classList.add(level === 'error' ? 'has-error' : 'has-warn'); msg.textContent = message; msg.className = 'cfg-msg ' + (level === 'error' ? 'err' : 'warn'); },
    clear: () => { row.classList.remove('has-error', 'has-warn'); msg.textContent = ''; msg.className = 'cfg-msg'; },
  };
}

function viewConfig(view) {
  const d = state.detail;
  const seenFiles = new Set();
  const groupFiles = (label, paths) => {
    const files = (paths || []).filter(fp => {
      if (!fp || seenFiles.has(fp)) return false;
      seenFiles.add(fp);
      return true;
    });
    return files.length ? { label, files } : null;
  };
  const fileGroups = [
    groupFiles('Config', d.configs),
    groupFiles('Project local', d.project_local),
    groupFiles('Test configs', d.test_configs),
    groupFiles('Environment', d.env_file ? [d.env_file] : []),
  ].filter(Boolean);
  const files = fileGroups.flatMap(g => g.files);
  view.append(el('h2', {}, `Config · ${d.name}`));
  if (!files.length) { view.append(el('div', { class: 'empty' }, 'No config files detected.')); return; }

  state.configSel = state.configSel || {};
  if (!files.includes(state.configSel[d.name])) state.configSel[d.name] = files[0];
  state.configMode = state.configMode || 'form';

  const sel = el('select', { style: 'max-width:340px' },
    ...fileGroups.map(group => el('optgroup', { label: group.label },
      ...group.files.map(fp => el('option', { value: fp, selected: fp === state.configSel[d.name] ? '' : null }, fp)))));
  const modeForm = el('button', { class: 'sm' });
  const modeRaw = el('button', { class: 'sm' });
  const status = el('span', { class: 'muted' });
  const banner = el('div', {});
  const host = el('div', { class: 'cfg-host' });
  let formApi = null;

  const syncModes = () => {
    modeForm.className = 'sm' + (state.configMode === 'form' ? '' : ' ghost'); modeForm.textContent = 'Form';
    modeRaw.className = 'sm' + (state.configMode === 'raw' ? '' : ' ghost'); modeRaw.textContent = 'Raw';
  };

  const showErrors = (errors) => {
    const byId = {}; errors.forEach(e => { byId[e.where] = e; });
    (formApi.rows || []).forEach(r => { const e = byId[r.id]; if (e) r.mark(e.level, e.message); });
    const errs = errors.filter(e => e.level === 'error').length, warns = errors.length - errs;
    banner.innerHTML = '';
    if (!errors.length) banner.append(el('div', { class: 'cfg-banner ok' }, '✓ valid'));
    else banner.append(el('div', { class: 'cfg-banner ' + (errs ? 'bad' : 'warnb') },
      `${errs} error${errs !== 1 ? 's' : ''}, ${warns} warning${warns !== 1 ? 's' : ''}`));
  };

  const renderBody = async () => {
    host.innerHTML = ''; banner.innerHTML = ''; status.textContent = ''; formApi = null;
    const path = state.configSel[d.name];
    if (state.configMode === 'raw') {
      const ta = el('textarea', { spellcheck: 'false' }); host.append(ta);
      status.textContent = 'loading…';
      try { ta.value = await api.text(`/api/file?pipeline=${d.name}&path=${encodeURIComponent(path)}`); status.textContent = ''; }
      catch (e) { status.textContent = e.message; }
      formApi = { raw: true, collect: () => ta.value };
      return;
    }
    status.textContent = 'parsing…';
    let form;
    try { form = await api.get(`/api/pipelines/${d.name}/config_form?path=${encodeURIComponent(path)}`); }
    catch (e) { state.configMode = 'raw'; syncModes(); status.textContent = 'not form-friendly — showing raw'; return renderBody(); }
    status.textContent = '';
    const rows = [];
    for (const sec of form.sections) {
      const card = el('div', { class: 'card cfg-section' }, el('div', { class: 'cfg-sec-title' }, sec.name));
      for (const f of sec.fields) { const r = renderField(f); rows.push(r); card.append(r.el); }
      host.append(card);
    }
    formApi = { raw: false, rows, collect: () => rows.map(r => r.get()) };
  };

  const validate = async () => {
    if (!formApi || formApi.raw) { status.textContent = 'switch to Form mode to validate'; return; }
    formApi.rows.forEach(r => r.clear());
    try {
      const res = await api.post(`/api/pipelines/${d.name}/config_validate`, { path: state.configSel[d.name], fields: formApi.collect() });
      showErrors(res.errors || []);
    } catch (e) { status.textContent = 'error: ' + e.message; }
  };

  const save = async () => {
    if (!formApi) { status.textContent = 'still loading…'; return; }
    status.textContent = 'saving…';
    if (formApi && formApi.raw) {
      try { await api.post(`/api/file?pipeline=${d.name}`, { path: state.configSel[d.name], content: formApi.collect() }); status.textContent = 'saved ✓ (backup written)'; }
      catch (e) { status.textContent = 'error: ' + e.message; }
      return;
    }
    formApi.rows.forEach(r => r.clear());
    try {
      const res = await api.post(`/api/pipelines/${d.name}/config_save`, { path: state.configSel[d.name], fields: formApi.collect() });
      showErrors(res.errors || []);
      status.textContent = res.ok ? `saved ✓ (backup ${res.backup})` : 'not saved — fix the errors above';
    } catch (e) { status.textContent = 'error: ' + e.message; }
  };

  modeForm.onclick = () => { state.configMode = 'form'; syncModes(); renderBody(); };
  modeRaw.onclick = () => { state.configMode = 'raw'; syncModes(); renderBody(); };
  sel.onchange = () => { state.configSel[d.name] = sel.value; renderBody(); };
  syncModes();
  view.append(el('div', { class: 'toolbar' }, sel, modeForm, modeRaw,
    el('button', { class: 'sm', onclick: validate }, '✓ Validate'),
    el('button', { class: 'sm', onclick: save }, 'Save'),
    el('button', { class: 'ghost sm', onclick: renderBody }, 'Reload'), status), banner, host);
  renderBody();
}

/* ---------------- SRA/GEO search + SRA download ---------------- */
function sraGeoFormFor(d) {
  return state.sraGeo[d.name] || (state.sraGeo[d.name] = {
    db: 'sra',
    term: '',
    limit: 20,
    accessions: '',
    destination: 'raw_files/sra',
    threads: 6,
    use_prefetch: true,
    convert_fastq: true,
    gzip_fastq: true,
    split_files: true,
    skip_technical: true,
    info: null,
    results: null,
    preview: null,
    status: '',
  });
}

function sraAccessionsFromText(text) {
  const seen = new Set();
  const out = [];
  for (const m of String(text || '').matchAll(/\b(?:[SED]RR|[SED]RX|[SED]RS|[SED]RP)\d+\b/gi)) {
    const acc = m[0].toUpperCase();
    if (!seen.has(acc)) { seen.add(acc); out.push(acc); }
  }
  return out;
}

function appendSraAccessions(f, text) {
  const existing = new Set(sraAccessionsFromText(f.accessions));
  const add = sraAccessionsFromText(text).filter(acc => !existing.has(acc));
  if (!add.length) return 0;
  f.accessions = [f.accessions.trim(), add.join('\n')].filter(Boolean).join('\n');
  return add.length;
}

function sraDownloadBody(f) {
  return {
    accessions: f.accessions,
    destination: f.destination,
    threads: +f.threads || 6,
    use_prefetch: !!f.use_prefetch,
    convert_fastq: !!f.convert_fastq,
    gzip_fastq: !!f.gzip_fastq,
    split_files: !!f.split_files,
    skip_technical: !!f.skip_technical,
  };
}

async function loadSraGeoInfo(d, f, shouldRender = true) {
  try {
    f.info = await api.get(`/api/pipelines/${d.name}/sra_geo/info`);
    f.destination = f.destination || f.info.default_destination || 'raw_files/sra';
  } catch (e) {
    f.status = 'error: ' + e.message;
  }
  if (shouldRender && state.detail && state.detail.name === d.name && state.tab === 'sra_geo') render();
}

function viewSraGeo(view) {
  const d = state.detail;
  const f = sraGeoFormFor(d);
  if (!f.info) loadSraGeoInfo(d, f, true);

  const dbSel = el('select', {},
    el('option', { value: 'sra', selected: f.db === 'sra' ? '' : null }, 'SRA'),
    el('option', { value: 'geo', selected: f.db === 'geo' ? '' : null }, 'GEO DataSets'));
  const queryIn = el('input', { type: 'text', value: f.term, placeholder: 'e.g. Parkinson small RNA human plasma' });
  const limitIn = el('input', { type: 'number', min: 1, max: 100, value: f.limit, style: 'max-width:90px' });
  const resultHost = el('div', {});
  const status = el('span', { class: 'muted' }, f.status || '');

  const syncSearch = () => {
    f.db = dbSel.value;
    f.term = queryIn.value;
    f.limit = +limitIn.value || 20;
  };
  const runSearch = async () => {
    syncSearch();
    f.status = 'searching NCBI...';
    viewSraGeoRefresh(d);
    try {
      f.results = await api.post(`/api/pipelines/${d.name}/sra_geo/search`, {
        db: f.db,
        term: f.term,
        limit: f.limit,
      });
      f.status = `${(f.results.items || []).length} shown of ${f.results.count || 0} result(s)`;
    } catch (e) {
      f.status = 'error: ' + e.message;
    }
    viewSraGeoRefresh(d);
  };
  queryIn.oninput = syncSearch;
  queryIn.onkeydown = (e) => { if (e.key === 'Enter') runSearch(); };
  dbSel.onchange = syncSearch;
  limitIn.oninput = syncSearch;

  view.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('h2', {}, `SRA/GEO · ${d.name}`),
        el('div', { class: 'muted' }, 'Search public NCBI records, collect SRA accessions, and download FASTQ data into a controlled local folder.')),
      sraToolBadge(f.info)),
    el('section', { class: 'panel sra-search-panel' },
      el('h3', {}, 'Search'),
      el('div', { class: 'sra-searchbar' },
        dbSel, queryIn, limitIn,
        el('button', { class: 'sm', onclick: runSearch }, 'Search')),
      status,
      resultHost),
    sraDownloadPanel(d, f),
    sraSessionPanel(d));
  renderSraResults(resultHost, d, f);
}

function viewSraGeoRefresh(d) {
  if (state.detail && state.detail.name === d.name && state.tab === 'sra_geo') render();
}

function sraToolBadge(info) {
  const tools = (info && info.tools) || {};
  const ok = !!tools.prefetch && !!tools.fasterq_dump;
  const text = ok ? 'SRA Toolkit ready' : 'SRA Toolkit incomplete';
  return el('div', { class: `sra-toolbox ${ok ? 'ok' : 'warn'}` },
    el('span', { class: `health-pill ${ok ? 'ok' : 'warn'}` }, ok ? 'ready' : 'check'),
    el('span', {}, text),
    el('small', {}, `prefetch:${tools.prefetch ? 'yes' : 'no'} · fasterq:${tools.fasterq_dump ? 'yes' : 'no'}`));
}

function renderSraResults(host, d, f) {
  const rows = (f.results && f.results.items) || [];
  host.innerHTML = '';
  if (!f.results) {
    host.append(el('div', { class: 'empty slim' }, 'Search SRA or GEO to populate this table.'));
    return;
  }
  if (!rows.length) {
    host.append(el('div', { class: 'empty slim' }, 'No records returned.'));
    return;
  }
  const tbl = el('table', { class: 'qc-table sra-table' });
  tbl.append(el('thead', {}, el('tr', {}, ...['Accession', 'Title', 'Organism', 'Runs', 'Date', ''].map(h => el('th', {}, h)))));
  const tb = el('tbody');
  rows.forEach(row => {
    const runs = row.runs || [];
    const sourceUrl = httpsUrl(row.url);
    const addRuns = () => {
      const count = appendSraAccessions(f, runs.join('\n') || row.accession || '');
      f.status = count ? `added ${count} accession(s)` : 'no new SRA run accessions found on that record';
      viewSraGeoRefresh(d);
    };
    tb.append(el('tr', {},
      el('td', { class: 'samp' }, sourceUrl ? el('a', { href: sourceUrl, target: '_blank', rel: 'noopener noreferrer' }, row.accession || row.uid) : (row.accession || row.uid)),
      el('td', {}, row.title || '—'),
      el('td', {}, row.organism || '—'),
      el('td', { class: 'mono' }, runs.length ? runs.slice(0, 5).join(', ') + (runs.length > 5 ? ` +${runs.length - 5}` : '') : '—'),
      el('td', {}, row.date || '—'),
      el('td', {}, el('button', { class: 'ghost sm', onclick: addRuns, disabled: runs.length || row.accession ? null : '' }, runs.length ? 'Use runs' : 'Use accession'))));
  });
  tbl.append(tb);
  host.append(el('div', { class: 'qc-scroll' }, tbl));
}

function sraDownloadPanel(d, f) {
  const accIn = el('textarea', { class: 'sra-accessions', spellcheck: 'false', placeholder: 'SRR..., ERR..., DRR... one per line' }, f.accessions);
  const fileIn = el('input', { type: 'file', accept: '.txt,.tsv,.csv,.list,.acc,.text' });
  const destListId = `sra-dest-${d.name.replace(/[^A-Za-z0-9_-]/g, '-')}`;
  const destIn = el('input', { type: 'text', value: f.destination, list: destListId, placeholder: 'relative path, for example raw_files/sra' });
  const pathOptions = (f.info && f.info.path_options) || ['raw_files/sra', 'raw_files', 'data/sra', 'resources/sra'];
  const threadsIn = el('input', { type: 'number', min: 1, max: 64, value: f.threads, style: 'max-width:90px' });
  const previewHost = el('pre', { class: 'sra-preview' }, f.preview ? formatSraPreview(f.preview) : '');
  const msg = el('span', { class: 'muted' }, f.status || '');
  const sync = () => {
    f.accessions = accIn.value;
    f.destination = destIn.value;
    f.threads = +threadsIn.value || 6;
  };
  accIn.oninput = sync;
  destIn.oninput = sync;
  threadsIn.oninput = sync;

  const toggle = (key, label) => {
    const input = el('input', { type: 'checkbox' });
    input.checked = !!f[key];
    input.onchange = () => { f[key] = input.checked; };
    return el('label', { class: 'inline switch' }, input, ' ', label);
  };

  fileIn.onchange = () => {
    const file = fileIn.files && fileIn.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const count = appendSraAccessions(f, String(reader.result || ''));
      f.status = count ? `loaded ${count} accession(s) from ${file.name}` : `no SRA accessions found in ${file.name}`;
      viewSraGeoRefresh(d);
    };
    reader.onerror = () => { f.status = `could not read ${file.name}`; viewSraGeoRefresh(d); };
    reader.readAsText(file);
  };

  const preview = async () => {
    sync();
    f.status = 'validating download plan...';
    viewSraGeoRefresh(d);
    try {
      const res = await api.post(`/api/pipelines/${d.name}/sra_geo/preview`, sraDownloadBody(f));
      f.preview = res.plan;
      f.status = 'download plan ready';
    } catch (e) {
      f.preview = null;
      f.status = 'error: ' + e.message;
    }
    viewSraGeoRefresh(d);
  };

  const download = async () => {
    sync();
    const accs = sraAccessionsFromText(f.accessions);
    if (!accs.length) { f.status = 'add at least one SRA accession first'; viewSraGeoRefresh(d); return; }
    const note = `Start SRA download for ${accs.length} accession(s) into ${f.destination || 'raw_files/sra'}?`;
    if (!window.confirm(note)) return;
    f.status = 'starting download session...';
    viewSraGeoRefresh(d);
    try {
      const res = await api.post(`/api/pipelines/${d.name}/sra_geo/download`, sraDownloadBody(f));
      f.preview = res.plan;
      f.status = 'download started';
      openTerminal(res.session);
    } catch (e) {
      f.status = 'error: ' + e.message;
      viewSraGeoRefresh(d);
    }
  };

  return el('section', { class: 'panel sra-download-panel' },
    el('h3', {}, 'Download SRA accessions'),
    el('div', { class: 'sra-download-grid' },
      el('div', {},
        el('label', {}, 'Accessions'), accIn,
        el('div', { class: 'toolbar' },
          fileIn,
          el('button', { class: 'ghost sm', onclick: () => { f.accessions = ''; viewSraGeoRefresh(d); } }, 'Clear'))),
      el('div', {},
        el('label', {}, 'Destination'), destIn,
        el('datalist', { id: destListId }, ...pathOptions.map(p => el('option', { value: p }, p))),
        el('div', { class: 'grid2 compact-form' },
          el('label', {}, 'Threads', threadsIn),
          el('div', {}, el('label', {}, 'Mode'),
            el('div', { class: 'switch-row sra-switches' },
              toggle('use_prefetch', 'prefetch'),
              toggle('convert_fastq', 'FASTQ'),
              toggle('gzip_fastq', 'gzip'),
              toggle('split_files', 'split'),
              toggle('skip_technical', 'skip technical')))),
        el('div', { class: 'toolbar' },
          el('button', { class: 'ghost sm', onclick: preview }, 'Preview'),
          el('button', { class: 'sm', onclick: download }, 'Start download'),
          msg),
        previewHost)));
}

function formatSraPreview(plan) {
  if (!plan) return '';
  const lines = [
    `accessions: ${(plan.accessions || []).join(', ')}`,
    `destination: ${plan.destination}`,
    `prefetch: ${plan.use_prefetch ? plan.prefetch_dir : 'off'}`,
    `fastq: ${plan.convert_fastq ? plan.fastq_dir : 'off'}`,
    `threads: ${plan.threads}`,
  ];
  if ((plan.warnings || []).length) {
    lines.push('', 'warnings:');
    plan.warnings.forEach(w => lines.push(`- ${w}`));
  }
  return lines.join('\n');
}

function sraSessionPanel(d) {
  const sessions = (state.sessions || []).filter(s =>
    s.kind === 'download' && s.meta && s.meta.pipeline === d.name && s.meta.kind === 'sra_geo');
  const box = el('section', { class: 'panel' }, el('h3', {}, 'Download sessions'));
  if (!sessions.length) {
    box.append(el('div', { class: 'empty slim' }, 'No SRA download sessions for this pipeline yet.'));
    return box;
  }
  const list = el('ul', { class: 'filelist' });
  sessions.slice(0, 12).forEach(s => list.append(el('li', {},
    el('span', { class: `dot ${s.status}` }),
    el('span', { class: 'mono' }, s.title || s.id),
    el('span', { class: 'tag' }, s.status),
    el('span', { class: 'muted' }, shortPath((s.meta && s.meta.destination) || '', 4)),
    el('button', { class: 'ghost sm', style: 'margin-left:auto', onclick: () => focusOrOpen(s) }, 'Attach'))));
  box.append(list);
  return box;
}

/* ---------------- code workspace ---------------- */
const DIR_ORDER = new Map([
  ['workflow', 0], ['rules', 1], ['src', 2], ['scripts', 3], ['bin', 4],
  ['config', 5], ['project_local', 6], ['profiles', 7], ['envs', 8],
  ['tests', 9], ['docs', 10],
]);
const PINNED_FILES = new Map([
  ['Snakefile', 0], ['pyproject.toml', 1], ['environment.yml', 2],
  ['environment.yaml', 3], ['requirements.txt', 4], ['README.md', 5],
]);

function parentPath(path) {
  const i = path.lastIndexOf('/');
  return i === -1 ? '' : path.slice(0, i);
}

function ancestors(path) {
  const parts = path.split('/').filter(Boolean);
  const out = [''];
  let cur = '';
  for (let i = 0; i < parts.length - 1; i += 1) {
    cur = cur ? `${cur}/${parts[i]}` : parts[i];
    out.push(cur);
  }
  return out;
}

function fileRank(name) {
  return PINNED_FILES.has(name) ? PINNED_FILES.get(name) : 1000;
}

function dirRank(name) {
  return DIR_ORDER.has(name) ? DIR_ORDER.get(name) : 1000;
}

function codeSort(a, b) {
  if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
  const ar = a.type === 'dir' ? dirRank(a.name) : fileRank(a.name);
  const br = b.type === 'dir' ? dirRank(b.name) : fileRank(b.name);
  if (ar !== br) return ar - br;
  return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
}

function buildCodeTree(files) {
  const root = { type: 'dir', name: '', path: '', children: new Map(), count: 0, size: 0, mtime: 0 };
  for (const f of files || []) {
    const parts = f.path.split('/').filter(Boolean);
    let node = root;
    let cur = '';
    for (let i = 0; i < parts.length - 1; i += 1) {
      cur = cur ? `${cur}/${parts[i]}` : parts[i];
      if (!node.children.has(parts[i])) {
        node.children.set(parts[i], { type: 'dir', name: parts[i], path: cur, children: new Map(), count: 0, size: 0, mtime: 0 });
      }
      node = node.children.get(parts[i]);
    }
    const name = parts[parts.length - 1];
    node.children.set(name, { ...f, type: 'file', name });
  }
  const finish = (node) => {
    if (node.type === 'file') return { count: 1, size: node.size || 0, mtime: node.mtime || 0 };
    const children = [...node.children.values()].sort(codeSort);
    node.children = children;
    node.count = 0; node.size = 0; node.mtime = 0;
    for (const child of children) {
      const s = finish(child);
      node.count += s.count;
      node.size += s.size;
      node.mtime = Math.max(node.mtime, s.mtime);
    }
    return { count: node.count, size: node.size, mtime: node.mtime };
  };
  finish(root);
  return root;
}

function fuzzyScore(path, query) {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const text = path.toLowerCase();
  let score = 0;
  for (const raw of q.split(/\s+/).filter(Boolean)) {
    const idx = text.indexOf(raw);
    if (idx >= 0) {
      score += 250 - idx - Math.abs(path.length - raw.length) * 0.15;
      if (idx === 0 || text[idx - 1] === '/' || text[idx - 1] === '_' || text[idx - 1] === '-') score += 80;
      continue;
    }
    let pos = -1;
    let spread = 0;
    for (const ch of raw) {
      const next = text.indexOf(ch, pos + 1);
      if (next === -1) return -Infinity;
      if (pos >= 0) spread += next - pos;
      pos = next;
    }
    score += 70 - spread;
  }
  return score;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function splitComment(line, marker = '#') {
  let quote = '';
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i], prev = line[i - 1];
    if ((ch === '"' || ch === "'" || ch === '`') && prev !== '\\') {
      quote = quote === ch ? '' : (quote || ch);
    }
    if (!quote) {
      if (marker === '//' && ch === '/' && line[i + 1] === '/') {
        return [line.slice(0, i), line.slice(i)];
      }
      if (marker !== '//' && ch === marker && (i === 0 || /\s/.test(prev || ''))) {
        return [line.slice(0, i), line.slice(i)];
      }
    }
  }
  return [line, ''];
}

const HIGHLIGHT_RANGE_LIMIT = 12000;
const HIGHLIGHT_BUDGET_EXCEEDED = Symbol('highlight-budget-exceeded');
let highlightRangesRemaining = Infinity;
let lastHighlightWasCapped = false;

function collectRanges(text, patterns) {
  const ranges = [];
  const occupied = [];
  const overlaps = (s, e) => occupied.some(r => s < r.e && e > r.s);
  patterns.forEach((p) => {
    const re = new RegExp(p.re.source, p.re.flags.includes('g') ? p.re.flags : p.re.flags + 'g');
    let m;
    while ((m = re.exec(text)) !== null) {
      const raw = m[0];
      const offset = p.group ? raw.indexOf(m[p.group]) : 0;
      const value = p.group ? m[p.group] : raw;
      const start = m.index + offset;
      const end = start + value.length;
      if (start !== end && !overlaps(start, end)) {
        if (highlightRangesRemaining <= 0) throw HIGHLIGHT_BUDGET_EXCEEDED;
        highlightRangesRemaining -= 1;
        ranges.push({ start, end, cls: p.cls });
        occupied.push({ s: start, e: end });
      }
      if (raw.length === 0) re.lastIndex += 1;
    }
  });
  return ranges.sort((a, b) => a.start - b.start);
}

function renderRanges(text, ranges) {
  let out = '', pos = 0;
  for (const r of ranges) {
    out += escapeHtml(text.slice(pos, r.start));
    out += `<span class="${r.cls}">${escapeHtml(text.slice(r.start, r.end))}</span>`;
    pos = r.end;
  }
  return out + escapeHtml(text.slice(pos));
}

const COMMON_CODE_PATTERNS = [
  { re: /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`/g, cls: 'tok-string' },
  { re: /\b(?:true|false|null|none|na|nan)\b/gi, cls: 'tok-const' },
  { re: /\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b/gi, cls: 'tok-number' },
  { re: /\b(?:snakemake|conda|mamba|micromamba|singularity|apptainer|docker|multiqc|fastqc|fastp|star|bowtie2|bwa|samtools|bedtools|macs2|featurecounts|salmon)\b/gi, cls: 'tok-tool' },
  { re: /\b(?:threads|cores|resources|mem_mb|mem_gb|runtime|walltime|partition|queue|cluster|slurm)\b/gi, cls: 'tok-resource' },
  { re: /\b(?:input|output|params|log|benchmark|conda|container|script|shell|wildcards|config)\b/g, cls: 'tok-pipeline' },
  { re: /(?:^|[\s:=,[({])((?:\.{1,2}\/|\/|~\/)?[\w@.+-]+(?:\/[\w@.+-]+)+(?:\.\w+)?)/g, cls: 'tok-path', group: 1 },
];

function inlineHighlight(text, extra = []) {
  return renderRanges(text, collectRanges(text, [...extra, ...COMMON_CODE_PATTERNS]));
}

function keyClass(key) {
  if (/(path|file|dir|ref|genome|gtf|gff|fasta|fa|bed|bam|fastq|fq|csv|tsv|yaml|yml|json|manifest|index|sample|results?|output|log|report)/i.test(key)) {
    return 'tok-path-key';
  }
  if (/(threads|cores|resources|mem|runtime|walltime|partition|queue|cluster|slurm|conda|env|container)/i.test(key)) {
    return 'tok-resource';
  }
  return 'tok-key';
}

function highlightYaml(line) {
  const [body, comment] = splitComment(line, '#');
  const m = body.match(/^(\s*)(-\s+)?([\w.-]+)(\s*:)(.*)$/);
  if (m) {
    return escapeHtml(m[1] + (m[2] || '')) +
      `<span class="${keyClass(m[3])}">${escapeHtml(m[3])}</span>` +
      `<span class="tok-punct">${escapeHtml(m[4])}</span>` +
      inlineHighlight(m[5]) +
      (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
  }
  return inlineHighlight(body) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
}

function highlightToml(line) {
  const [body, comment] = splitComment(line, '#');
  const section = body.match(/^(\s*)(\[[^\]]+\])(.*)$/);
  if (section) {
    return escapeHtml(section[1]) + `<span class="tok-section">${escapeHtml(section[2])}</span>` +
      inlineHighlight(section[3]) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
  }
  const kv = body.match(/^(\s*)([\w.-]+)(\s*=)(.*)$/);
  if (kv) {
    return escapeHtml(kv[1]) + `<span class="${keyClass(kv[2])}">${escapeHtml(kv[2])}</span>` +
      `<span class="tok-punct">${escapeHtml(kv[3])}</span>` + inlineHighlight(kv[4]) +
      (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
  }
  return inlineHighlight(body) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
}

function highlightJson(line) {
  return inlineHighlight(line, [
    { re: /"([^"\\]*(?:\\.[^"\\]*)*)"\s*:/g, cls: 'tok-key', group: 1 },
  ]);
}

function highlightSnake(line) {
  const [body, comment] = splitComment(line, '#');
  const rule = body.match(/^(\s*)(rule|checkpoint|module|subworkflow)\s+([\w.-]+)(.*)$/);
  if (rule) {
    return escapeHtml(rule[1]) + `<span class="tok-keyword">${rule[2]}</span> ` +
      `<span class="tok-rule">${escapeHtml(rule[3])}</span>` + inlineHighlight(rule[4]) +
      (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
  }
  const directive = body.match(/^(\s*)(input|output|params|threads|resources|log|benchmark|conda|container|script|shell|run|wrapper|configfile|include|wildcard_constraints|priority|localrules|ruleorder)(\s*:?.*)$/);
  if (directive) {
    const cls = /(threads|resources)/.test(directive[2]) ? 'tok-resource' :
      /(log|benchmark|conda|container|script|shell)/.test(directive[2]) ? 'tok-pipeline' : 'tok-keyword';
    return escapeHtml(directive[1]) + `<span class="${cls}">${escapeHtml(directive[2])}</span>` +
      inlineHighlight(directive[3]) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
  }
  return inlineHighlight(body, PY_PATTERNS) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
}

const PY_PATTERNS = [
  { re: /\b(?:def|class|return|yield|import|from|as|if|elif|else|for|while|with|try|except|finally|raise|assert|lambda|pass|break|continue|in|is|not|and|or)\b/g, cls: 'tok-keyword' },
  { re: /\b(?:pd|np|Path|DataFrame|AnnData|Snakefile|config|snakemake|wildcards)\b/g, cls: 'tok-tool' },
  { re: /([A-Za-z_]\w*)(?=\s*\()/g, cls: 'tok-fn', group: 1 },
];

const R_PATTERNS = [
  { re: /\b(?:function|library|require|if|else|for|while|repeat|next|break|return|TRUE|FALSE|NULL|NA)\b/g, cls: 'tok-keyword' },
  { re: /\b(?:data\.table|DESeq2|edgeR|limma|ggplot|ggplot2|dplyr|Seurat|SingleCellExperiment|readr)\b/g, cls: 'tok-tool' },
  { re: /(%>%|<-|::)/g, cls: 'tok-punct' },
  { re: /([A-Za-z.]\w*)(?=\s*\()/g, cls: 'tok-fn', group: 1 },
];

const SH_PATTERNS = [
  { re: /\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|function|export|local|set|trap)\b/g, cls: 'tok-keyword' },
  { re: /\b(?:rm|rmdir|mv|chmod|chown|sudo|kill|pkill)\b/g, cls: 'tok-danger' },
  { re: /\$\{?[\w@#?$!.-]+\}?/g, cls: 'tok-var' },
  { re: /--?[\w-]+/g, cls: 'tok-flag' },
];

function highlightHashCommentLang(line, patterns) {
  const [body, comment] = splitComment(line, '#');
  return inlineHighlight(body, patterns) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
}

function highlightJsCss(line) {
  const [body, comment] = splitComment(line, '//');
  return inlineHighlight(body, [
    { re: /\b(?:const|let|var|function|return|if|else|for|while|class|new|await|async|try|catch|import|from|export|default|this)\b/g, cls: 'tok-keyword' },
    { re: /(--[\w-]+|#[0-9a-f]{3,8}\b|rgba?\([^)]*\))/gi, cls: 'tok-const' },
    { re: /([A-Za-z_$][\w$-]*)(?=\s*\()/g, cls: 'tok-fn', group: 1 },
  ]) + (comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : '');
}

function highlightMarkdown(line) {
  if (/^\s*```/.test(line)) return `<span class="tok-section">${escapeHtml(line)}</span>`;
  const heading = line.match(/^(#{1,6})(\s+.*)$/);
  if (heading) return `<span class="tok-keyword">${heading[1]}</span><span class="tok-section">${escapeHtml(heading[2])}</span>`;
  return inlineHighlight(line, [
    { re: /`[^`]+`/g, cls: 'tok-string' },
    { re: /\[[^\]]+\]\([^)]+\)/g, cls: 'tok-path' },
    { re: /\*\*[^*]+\*\*/g, cls: 'tok-keyword' },
  ]);
}

function editorLanguageForPath(path, fallback = 'text') {
  const file = String(path || '').toLowerCase();
  const name = file.split('/').pop() || '';
  if (name === 'snakefile' || file.endsWith('.smk')) return 'snakemake';
  if (/\.py$/.test(file)) return 'python';
  if (/\.r$/.test(file)) return 'r';
  if (/\.(?:sh|bash|zsh)$/.test(file)) return 'shell';
  if (/\.ya?ml$/.test(file)) return 'yaml';
  if (/\.toml$/.test(file)) return 'toml';
  if (/\.jsonl$/.test(file)) return 'jsonl';
  if (/\.json$/.test(file)) return 'json';
  if (/\.[cm]?js$/.test(file)) return 'javascript';
  if (/\.[cm]?ts$/.test(file)) return 'typescript';
  if (/\.css$/.test(file)) return 'css';
  if (/\.html?$/.test(file)) return 'html';
  if (/\.(?:md|markdown)$/.test(file)) return 'markdown';
  return String(fallback || 'text').toLowerCase();
}

function highlightCode(src, lang, path = '') {
  const file = path.toLowerCase();
  const mode = editorLanguageForPath(file, lang);
  highlightRangesRemaining = HIGHLIGHT_RANGE_LIMIT;
  lastHighlightWasCapped = false;
  try {
    return src.split('\n').map((line) => {
      // Token-range collection is intentionally skipped for pathological
      // generated lines so a single dense line cannot freeze the editor.
      if (line.length > 5000) return escapeHtml(line);
      if (mode === 'snakemake') return highlightSnake(line);
      if (mode === 'yaml') return highlightYaml(line);
      if (mode === 'toml') return highlightToml(line);
      if (mode === 'json' || mode === 'jsonl') return highlightJson(line);
      if (mode === 'python') return highlightHashCommentLang(line, PY_PATTERNS);
      if (mode === 'r') return highlightHashCommentLang(line, R_PATTERNS);
      if (mode === 'shell') return highlightHashCommentLang(line, SH_PATTERNS);
      if (mode === 'javascript' || mode === 'typescript' || mode === 'css' || mode === 'html') return highlightJsCss(line);
      if (mode === 'markdown' || file.endsWith('.md')) return highlightMarkdown(line);
      return highlightHashCommentLang(line, []);
    }).join('\n');
  } catch (error) {
    if (error !== HIGHLIGHT_BUDGET_EXCEEDED) throw error;
    lastHighlightWasCapped = true;
    return escapeHtml(src);
  } finally {
    highlightRangesRemaining = Infinity;
  }
}

function codeState(name) {
  const c = state.code[name] || (state.code[name] = {
    files: [], loaded: false, filter: '', selected: '', loadedPath: '',
    content: '', saved: '', dirty: false, language: '', size: 0, mtime: null, revision: '',
    indentMode: 'spaces', indentSize: 4,
    expanded: new Set(['']), focusPath: '', visibleTree: [],
    tabs: [], activeTab: '', diagnostics: null, saveConflict: null,
    packageHelpOpen: false, packageHelp: null, packageHelpPath: '', packageHelpLoading: false, packageHelpError: '', packageHelpRequest: 0,
    commandContext: null, commandHelp: null, commandHelpName: '',
    commandHelpLoading: false, commandHelpError: '', commandHelpRequest: 0,
  });
  if (!(c.expanded instanceof Set)) c.expanded = new Set(['']);
  c.visibleTree = c.visibleTree || [];
  c.tabs = c.tabs || [];
  if (!['spaces', 'tabs'].includes(c.indentMode)) c.indentMode = 'spaces';
  c.indentSize = EditorCore.normalizeTabSize(c.indentSize, 4);
  if (typeof c.revision !== 'string') c.revision = '';
  if (typeof c.packageHelpOpen !== 'boolean') c.packageHelpOpen = false;
  if (typeof c.packageHelpRequest !== 'number') c.packageHelpRequest = 0;
  if (typeof c.commandHelpRequest !== 'number') c.commandHelpRequest = 0;
  return c;
}

function codeTabLabel(path) {
  if (!path) return 'untitled';
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

function duplicatePath(path) {
  const slash = path.lastIndexOf('/');
  const dir = slash === -1 ? '' : path.slice(0, slash + 1);
  const name = slash === -1 ? path : path.slice(slash + 1);
  const dot = name.lastIndexOf('.');
  if (dot > 0) return `${dir}${name.slice(0, dot)}.copy${name.slice(dot)}`;
  return `${dir}${name}.copy`;
}

function lineOffset(text, line) {
  const target = Math.max(1, Number(line) || 1);
  let pos = 0;
  for (let i = 1; i < target; i += 1) {
    const next = text.indexOf('\n', pos);
    if (next === -1) return text.length;
    pos = next + 1;
  }
  return pos;
}

function viewCode(view) {
  const d = state.detail;
  const c = codeState(d.name);
  view.append(el('h2', {}, `Code · ${d.name}`),
    el('div', { class: 'muted' }, d.path));

  const codeId = String(d.name || 'pipeline').replace(/[^A-Za-z0-9_-]/g, '-');
  const codeSidebarId = `code-file-tree-${codeId}`;
  const codeAssistantId = `code-assistant-${codeId}`;
  const filterIn = el('input', { id: `code-filter-${codeId}`, type: 'search', value: c.filter, placeholder: 'Fuzzy search files', autocomplete: 'off' });
  const filterLabel = el('label', { class: 'code-filter', for: filterIn.id }, el('span', { class: 'sr-only' }, 'Filter editable files'), filterIn);
  const pathIn = el('input', { id: `code-path-${codeId}`, type: 'text', value: c.selected, placeholder: 'path/to/file.py', spellcheck: 'false' });
  const pathLabel = el('label', { class: 'code-path-field', for: pathIn.id }, el('span', { class: 'sr-only' }, 'Active file path'), pathIn);
  const status = el('span', { class: 'muted code-status' });
  const meta = el('div', { class: 'code-meta muted' });
  const fileList = el('div', { class: 'code-list', tabindex: '0', role: 'tree', 'aria-label': 'Project files' });
  const tabStrip = el('div', { class: 'code-tabs', role: 'tablist', 'aria-label': 'Open code files' });
  const diagnosticsPanel = el('div', { class: 'diagnostics-panel hidden' });
  const packageHelpPanel = el('section', { class: 'code-package-help hidden', 'aria-live': 'polite' });
  const saveConflictPanel = el('section', {
    class: 'code-save-conflict hidden', role: 'alert', 'aria-live': 'assertive',
  });
  const activeLine = el('div', { class: 'code-active-line', 'aria-hidden': 'true' });
  const indentLayer = el('div', { class: 'code-indent-layer', 'aria-hidden': 'true' });
  const highlight = el('pre', { class: 'code-highlight', 'aria-hidden': 'true' });
  const inlineCompletion = el('pre', { class: 'code-inline-completion hidden', 'aria-hidden': 'true' });
  const hoverHelpId = `code-line-hover-${codeId}`;
  const lineHoverHelp = el('div', {
    id: hoverHelpId, class: 'code-line-hover hidden', role: 'tooltip', 'aria-live': 'polite',
  });
  const lineNumbers = el('pre', { class: 'code-line-numbers' });
  const currentLineNumber = el('span', { class: 'code-current-line-number' });
  const gutter = el('div', { class: 'code-gutter', 'aria-hidden': 'true' }, lineNumbers, currentLineNumber);
  const editor = el('textarea', {
    class: 'code-editor',
    'aria-label': 'Code editor',
    'aria-describedby': hoverHelpId,
    'aria-keyshortcuts': 'Alt+F1 Control+Space Meta+Space',
    title: 'Tab accepts a visible completion or indents. Hover a line or press Alt+F1 for local help. Press Escape, then Tab to move focus out.',
    spellcheck: 'false',
    wrap: 'off',
    placeholder: 'Select a file or create a new one.',
  });
  const cursorStatus = el('span', { class: 'code-cursor-position' }, 'Ln 1, Col 1');
  const selectionStatus = el('span', { class: 'code-selection-status hidden' });
  const largeFileStatus = el('span', { class: 'code-large-file-status hidden' }, 'Large file · syntax colors paused');
  const languageStatus = el('span', { class: 'code-language-status' }, 'text');
  const indentSelect = el('select', { class: 'code-indent-control', 'aria-label': 'Editor indentation style' },
    ...[
      ['spaces:2', 'Spaces: 2'], ['spaces:4', 'Spaces: 4'], ['spaces:8', 'Spaces: 8'],
      ['tabs:2', 'Tabs: 2'], ['tabs:4', 'Tabs: 4'], ['tabs:8', 'Tabs: 8'],
    ].map(([value, label]) => el('option', {
      value, selected: value === `${c.indentMode}:${c.indentSize}` ? '' : null,
    }, label)));
  const guidesBtn = el('button', {
    class: 'code-status-action', type: 'button',
    'aria-pressed': state.uiPrefs.codeIndentGuides ? 'true' : 'false',
    title: 'Toggle indentation guides',
  }, 'Guides');
  const completionBtn = el('button', {
    class: 'code-status-action', type: 'button',
    'aria-pressed': state.uiPrefs.codeInlineCompletions ? 'true' : 'false',
    title: 'Toggle local inline completions (Ctrl/Command+Space requests one)',
  }, 'Complete');
  const hoverHelpBtn = el('button', {
    class: 'code-status-action', type: 'button',
    'aria-pressed': state.uiPrefs.codeHoverHelp ? 'true' : 'false',
    title: 'Toggle local line explanations on hover or Alt+F1',
  }, 'Hover help');
  const editorStatusbar = el('div', { class: 'code-editor-statusbar' },
    el('div', { class: 'code-status-left' }, cursorStatus, selectionStatus, largeFileStatus),
    el('div', { class: 'code-status-right' }, languageStatus,
      el('span', { class: 'code-encoding-status' }, 'UTF-8 · LF'), indentSelect,
      completionBtn, hoverHelpBtn, guidesBtn));
  const editorWrap = el('div', {
    id: `code-editor-panel-${codeId}`, class: 'code-editor-wrap', role: 'tabpanel',
    'aria-label': 'Active file editor',
  }, activeLine, indentLayer, highlight, inlineCompletion, editor, gutter, lineHoverHelp);
  const editorShell = el('div', { class: 'code-editor-shell' }, editorWrap, editorStatusbar);
  editor.value = c.content || '';

  const assistantPath = el('div', { class: 'assistant-path mono' }, 'No file selected');
  const assistantStatus = el('span', { class: 'muted assistant-status' });
  const assistantNotes = el('textarea', {
    class: 'assistant-notes',
    spellcheck: 'true',
    placeholder: 'Notes for this file, TODOs, assumptions, review questions...',
  });
  const assistantTool = el('select', {},
    el('option', { value: 'claude' }, 'Claude'),
    el('option', { value: 'codex' }, 'Codex'));
  const assistantPreview = el('div', { class: 'assistant-preview muted' }, 'Inline completions and hover explanations stay local. Approved AI prompts include the active file, selection, diagnostics, and these notes. Saved notes stay in this browser and are scoped to this workspace root.');

  const saveBtn = el('button', { class: 'sm' }, 'Save');
  const reloadBtn = el('button', { class: 'ghost sm' }, 'Reload');
  const refreshBtn = el('button', { class: 'ghost sm' }, 'Refresh files');
  const collapseBtn = el('button', { class: 'ghost sm' }, 'Collapse folders');
  const revealBtn = el('button', { class: 'ghost sm' }, 'Reveal active');
  const newBtn = el('button', { class: 'ghost sm' }, 'New file');
  const mkdirBtn = el('button', { class: 'ghost sm' }, 'New folder');
  const renameBtn = el('button', { class: 'ghost sm' }, 'Rename/move');
  const duplicateBtn = el('button', { class: 'ghost sm' }, 'Duplicate');
  const deleteBtn = el('button', { class: 'ghost sm danger-outline' }, 'Delete');
  const diagnosticsBtn = el('button', { class: 'ghost sm' }, 'Diagnostics');
  const fileTreeToggleBtn = el('button', {
    class: 'ghost sm', type: 'button', 'aria-controls': codeSidebarId,
    'data-panel-pref': 'codeSidebarOpen', 'data-panel-label': 'file tree', 'data-dynamic-label': '',
    onclick: () => setUiPref('codeSidebarOpen', !state.uiPrefs.codeSidebarOpen),
  }, 'Hide file tree');
  const assistantToggleBtn = el('button', {
    class: 'ghost sm', type: 'button', 'aria-controls': codeAssistantId,
    'data-panel-pref': 'codeAssistantOpen', 'data-panel-label': 'assistant', 'data-dynamic-label': '',
    onclick: () => setUiPref('codeAssistantOpen', !state.uiPrefs.codeAssistantOpen),
  }, 'Hide assistant');
  const hideFileTreeBtn = el('button', {
    class: 'ghost sm icon-only panel-collapse', type: 'button', 'aria-controls': codeSidebarId,
    'data-panel-pref': 'codeSidebarOpen', 'data-panel-label': 'file tree',
    onclick: () => {
      setUiPref('codeSidebarOpen', false);
      requestAnimationFrame(() => $('#code-sidebar-restore').focus());
    },
  }, '‹');
  const hideAssistantBtn = el('button', {
    class: 'ghost sm icon-only panel-collapse', type: 'button', 'aria-controls': codeAssistantId,
    'data-panel-pref': 'codeAssistantOpen', 'data-panel-label': 'assistant',
    onclick: () => {
      setUiPref('codeAssistantOpen', false);
      requestAnimationFrame(() => $('#code-assistant-restore').focus());
    },
  }, '›');
  const packageHelpBtn = el('button', {
    class: 'ghost sm package-help-toggle', type: 'button', 'aria-controls': `code-package-help-${codeId}`,
    'aria-expanded': c.packageHelpOpen ? 'true' : 'false', title: 'Open tool and package help (F1 in the editor)',
  }, 'Tool & package help');
  packageHelpPanel.id = `code-package-help-${codeId}`;

  const setStatus = (msg, kind = '') => {
    status.textContent = msg;
    status.className = `muted code-status ${kind}`;
  };

  const activeCodePath = () => c.selected || c.loadedPath || '__pipeline__';

  const commandOptionCard = (option, matched = false) => {
    const flags = asHelpList(option.flags);
    return el('article', { class: `command-help-option${matched ? ' matched' : ''}` },
      el('div', { class: 'command-help-option-head' },
        el('code', {}, `${flags.join(', ')}${option.value ? ` ${option.value}` : ''}`),
        option.category ? el('span', { class: 'tag' }, option.category) : null),
      option.summary ? el('p', {}, option.summary) : null,
      option.caution ? el('p', { class: 'command-help-caution' }, `Caution: ${option.caution}`) : null);
  };

  const renderCommandHelp = () => {
    const section = el('section', { class: 'command-help-section' },
      el('div', { class: 'package-help-subhead' },
        el('h4', {}, 'Command help at cursor'),
        el('span', { class: 'muted' }, 'Static, version-labelled catalog')));
    const context = c.commandContext;
    if (!context) {
      section.append(el('div', { class: 'package-help-empty compact' },
        'Place the caret on a FastQC command, option, or option value. Press F1 to reopen this panel.'));
      return section;
    }
    section.append(el('div', { class: 'command-help-context' },
      el('span', { class: 'tag' }, `Line ${context.line}`),
      el('code', {}, context.option ? `${context.command} ${context.option}` : context.command),
      context.commandLine !== context.line
        ? el('span', { class: 'muted' }, `command starts on line ${context.commandLine}`) : null));
    if (c.commandHelpLoading && c.commandHelpName !== context.command) {
      section.append(el('div', { class: 'package-help-empty compact' }, `Loading ${context.command} help…`));
      return section;
    }
    if (c.commandHelpError && c.commandHelpName === context.command) {
      section.append(el('div', { class: 'package-help-empty compact error' },
        `Command help is unavailable: ${c.commandHelpError}`));
      return section;
    }
    const guide = c.commandHelpName === context.command ? c.commandHelp : null;
    if (!guide) {
      section.append(el('div', { class: 'package-help-empty compact' }, 'Loading the local command catalog…'));
      return section;
    }
    const options = Array.isArray(guide.options) ? guide.options : [];
    const matched = context.option
      ? options.find(option => asHelpList(option.flags).includes(context.option)) : null;
    const links = el('div', { class: 'command-help-links' });
    [
      ['Official guide', guide.docs_url],
      ['Catalog source', guide.source_url],
    ].forEach(([label, raw]) => {
      const docs = officialDocsUrl(raw || '');
      if (docs) links.append(el('a', {
        class: 'official-doc-link', href: docs.href, target: '_blank', rel: 'noopener noreferrer',
        referrerpolicy: 'no-referrer', title: `Open ${label.toLowerCase()} on ${docs.host}`,
      }, `${label} · ${docs.host} ↗`));
    });
    section.append(el('div', { class: 'command-help-overview' },
      el('div', {},
        el('div', { class: 'package-help-title' }, el('strong', {}, guide.name || guide.command),
          el('span', { class: 'tag' }, guide.version_scope || guide.catalog_version || 'versioned catalog')),
        guide.summary ? el('p', {}, guide.summary) : null,
        guide.synopsis ? el('code', { class: 'command-help-synopsis' }, guide.synopsis) : null),
      links));
    if (matched) {
      section.append(el('div', { class: 'command-help-match' },
        el('div', { class: 'muted' }, 'At cursor'), commandOptionCard(matched, true)));
    } else if (context.option) {
      section.append(el('div', { class: 'command-help-miss' },
        el('strong', {}, `${context.option} is not in this catalog snapshot.`),
        el('span', { class: 'muted' }, 'Check the active environment version and use the searchable full list below.')));
    }
    const details = el('details', { class: 'command-help-all', open: matched ? '' : null },
      el('summary', {}, `All ${options.length} ${guide.name || guide.command} options`));
    const searchId = `command-help-search-${codeId}`;
    const search = el('input', {
      id: searchId, type: 'search', autocomplete: 'off',
      placeholder: 'Filter flags, categories, or descriptions',
    });
    const optionList = el('div', { class: 'command-help-options' });
    const optionCards = options.map(option => {
      const card = commandOptionCard(option, option === matched);
      optionList.append(card);
      return card;
    });
    const noMatches = el('div', { class: 'package-help-empty compact hidden' }, 'No options match this filter.');
    search.oninput = () => {
      const query = search.value.trim().toLowerCase();
      let visible = 0;
      optionCards.forEach((card) => {
        const show = !query || (card.textContent || '').toLowerCase().includes(query);
        card.classList.toggle('hidden', !show);
        if (show) visible += 1;
      });
      noMatches.classList.toggle('hidden', visible !== 0);
    };
    details.append(el('label', { class: 'command-help-search', for: searchId },
      el('span', { class: 'sr-only' }, 'Filter command options'), search), optionList, noMatches);
    section.append(details,
      el('p', { class: 'package-help-privacy' },
        'This guide is a bundled snapshot; OmicsANG did not run the repository command. Confirm the installed FastQC version before relying on version-specific behavior.'));
    return section;
  };

  const renderPackageHelp = () => {
    packageHelpPanel.innerHTML = '';
    packageHelpPanel.classList.toggle('hidden', !c.packageHelpOpen);
    packageHelpBtn.classList.toggle('active', !!c.packageHelpOpen);
    packageHelpBtn.setAttribute('aria-expanded', c.packageHelpOpen ? 'true' : 'false');
    if (!c.packageHelpOpen) return;
    packageHelpPanel.append(el('div', { class: 'package-help-head' },
      el('div', {}, el('h3', {}, 'Tool & package help'),
        el('div', { class: 'muted' }, c.loadedPath || c.selected || 'Select a saved file to detect packages.')),
      el('button', { class: 'ghost sm', type: 'button', onclick: () => { c.packageHelpOpen = false; renderPackageHelp(); packageHelpBtn.focus(); } }, 'Close')));
    packageHelpPanel.append(renderCommandHelp());
    const packageSection = el('section', { class: 'package-help-section' },
      el('div', { class: 'package-help-subhead' },
        el('h4', {}, 'Packages in saved file'),
        el('span', { class: 'muted' }, 'Refreshed after save or reload')));
    if (c.packageHelpLoading) {
      packageSection.append(el('div', { class: 'package-help-empty compact' }, 'Detecting package references locally…'));
    } else if (c.packageHelpError) {
      packageSection.append(el('div', { class: 'package-help-empty compact error' },
        `Package help is unavailable: ${c.packageHelpError}`),
      el('p', { class: 'muted' }, 'The editor remains usable; no file content was sent outside this OmicsANG server.'));
    } else {
      const data = c.packageHelp || {};
      const packages = Array.isArray(data.packages) ? data.packages : [];
      if (data.language) packageSection.append(el('div', { class: 'package-help-context' },
        el('span', { class: 'tag' }, data.language),
        el('span', { class: 'muted' }, `${packages.length} detected package${packages.length === 1 ? '' : 's'}`)));
      if (!packages.length) packageSection.append(el('div', { class: 'package-help-empty compact' }, 'No supported package references were detected in this saved file.'));
      const cards = el('div', { class: 'package-help-grid' });
      packages.forEach((pkg) => {
        if (typeof pkg === 'string') pkg = { name: pkg };
        const docs = officialDocsUrl(pkg.docs_url || pkg.documentation_url || '');
        const commands = asHelpList(pkg.local_help || pkg.commands);
        cards.append(el('article', { class: 'package-help-card' },
          el('div', { class: 'package-help-title' }, el('strong', {}, pkg.name || 'Package'),
            pkg.ecosystem ? el('span', { class: 'tag' }, pkg.ecosystem) : null),
          pkg.summary ? el('p', {}, pkg.summary) : null,
          pkg.detected_from ? el('div', { class: 'muted package-detected' }, `Detected from ${pkg.detected_from}`) : null,
          commands.length ? el('div', { class: 'local-help-commands' },
            el('span', { class: 'muted' }, 'Run locally when appropriate:'),
            ...commands.map(command => el('code', {}, command))) : null,
          docs ? el('a', {
            class: 'official-doc-link', href: docs.href, target: '_blank', rel: 'noopener noreferrer',
            referrerpolicy: 'no-referrer',
            title: `Open official documentation on ${docs.host}`,
          }, `Official docs · ${docs.host} ↗`) : pkg.docs_url
            ? el('div', { class: 'muted docs-withheld' }, 'Documentation link withheld: destination is not on OmicsANG’s official-docs allowlist.') : null));
      });
      packageSection.append(cards);
      const notes = helpListSection('Detection notes', data.notes);
      if (notes) packageSection.append(notes);
    }
    packageHelpPanel.append(packageSection);
    packageHelpPanel.append(el('p', { class: 'package-help-privacy' }, 'Detection and local-help lookup happen on this OmicsANG server. Code is not sent externally. Documentation opens only after you click a vetted HTTPS link, and its destination domain is shown first.'));
  };

  const loadPackageHelp = async (force = false) => {
    const path = c.loadedPath || c.selected || '';
    if (!path || !c.loadedPath) {
      c.packageHelp = null;
      c.packageHelpPath = '';
      c.packageHelpError = '';
      renderPackageHelp();
      return;
    }
    if (!force && c.packageHelp && c.packageHelpPath === path) { renderPackageHelp(); return; }
    const request = ++c.packageHelpRequest;
    c.packageHelpLoading = true;
    c.packageHelpError = '';
    renderPackageHelp();
    try {
      const payload = await api.post(`/api/pipelines/${encodeURIComponent(d.name)}/code/help`, { path });
      if (request !== c.packageHelpRequest || !view.isConnected) return;
      c.packageHelp = payload || { packages: [] };
      c.packageHelpPath = path;
    } catch (e) {
      if (request !== c.packageHelpRequest || !view.isConnected) return;
      c.packageHelp = null;
      c.packageHelpPath = path;
      c.packageHelpError = e.message;
    } finally {
      if (request !== c.packageHelpRequest || !view.isConnected) return;
      c.packageHelpLoading = false;
      renderPackageHelp();
    }
  };

  const refreshPackageHelpIfNeeded = () => {
    if (c.packageHelpOpen && c.packageHelpPath !== (c.loadedPath || c.selected || '')) loadPackageHelp(false);
  };

  const loadCommandHelp = async (command) => {
    const name = String(command || '').toLowerCase();
    if (!CONTEXTUAL_COMMANDS.includes(name)) return;
    if (c.commandHelp && c.commandHelpName === name) { renderPackageHelp(); return; }
    const request = ++c.commandHelpRequest;
    c.commandHelpLoading = true;
    c.commandHelpError = '';
    renderPackageHelp();
    try {
      const payload = await api.get(`/api/code/command-help/${encodeURIComponent(name)}`);
      if (request !== c.commandHelpRequest || !view.isConnected) return;
      c.commandHelp = payload;
      c.commandHelpName = name;
    } catch (e) {
      if (request !== c.commandHelpRequest || !view.isConnected) return;
      c.commandHelp = null;
      c.commandHelpName = name;
      c.commandHelpError = e.message;
    } finally {
      if (request !== c.commandHelpRequest || !view.isConnected) return;
      c.commandHelpLoading = false;
      renderPackageHelp();
    }
  };

  const refreshCommandContext = (force = false) => {
    const context = EditorCore.commandContextAt(
      editor.value, editor.selectionStart || 0, CONTEXTUAL_COMMANDS);
    const previous = c.commandContext;
    const signature = context
      ? `${context.command}:${context.option || ''}:${context.line}:${context.commandLine}` : '';
    const previousSignature = previous
      ? `${previous.command}:${previous.option || ''}:${previous.line}:${previous.commandLine}` : '';
    if (!force && signature === previousSignature) return;
    c.commandContext = context;
    if (c.packageHelpOpen) renderPackageHelp();
    if (context && (!c.commandHelp || c.commandHelpName !== context.command)) {
      loadCommandHelp(context.command);
    }
  };

  const clearSaveConflict = () => {
    c.saveConflict = null;
    saveConflictPanel.innerHTML = '';
    saveConflictPanel.classList.add('hidden');
  };

  const renderSaveConflict = () => {
    saveConflictPanel.innerHTML = '';
    const conflict = c.saveConflict;
    saveConflictPanel.classList.toggle('hidden', !conflict);
    if (!conflict) return;
    const disk = conflict.disk;
    const actions = el('div', { class: 'code-save-conflict-actions' });
    actions.append(
      el('button', {
        class: 'sm', type: 'button', disabled: disk && disk.content != null ? null : '',
        onclick: async () => {
          if (!disk || disk.content == null) return;
          await openFile(conflict.path, false, true);
          clearSaveConflict();
        },
      }, 'Reload disk'),
      el('button', {
        class: 'ghost sm', type: 'button',
        onclick: () => {
          const proposed = duplicatePath(conflict.path || c.selected || 'workflow/new_file.py');
          const path = window.prompt('Save your editor version as:', proposed);
          if (!path) return;
          pathIn.value = path.trim();
          clearSaveConflict();
          updateDirty(true);
          editor.focus();
        },
      }, 'Save as…'),
      el('button', {
        class: 'danger sm', type: 'button',
        onclick: async () => {
          if (!window.confirm('Overwrite the current disk version once? A private backup will be kept when possible.')) return;
          await save(true);
        },
      }, disk ? 'Overwrite once' : 'Recreate once'),
      el('button', { class: 'ghost sm', type: 'button', onclick: clearSaveConflict }, 'Keep editing'));

    saveConflictPanel.append(
      el('div', { class: 'code-save-conflict-head' },
        el('div', {}, el('strong', {}, 'File changed outside this editor'),
          el('div', { class: 'muted' }, conflict.message || 'Review the disk version before choosing how to save.')),
        actions));
    if (conflict.loading) {
      saveConflictPanel.append(el('div', { class: 'muted' }, 'Loading the current disk version for comparison…'));
      return;
    }
    if (conflict.diskError) {
      saveConflictPanel.append(el('div', { class: 'muted' }, `Disk comparison unavailable: ${conflict.diskError}`));
      return;
    }
    if (disk && disk.content != null) {
      saveConflictPanel.append(el('div', { class: 'code-save-compare' },
        el('div', {}, el('h4', {}, 'Your unsaved editor'),
          el('pre', {}, truncateText(editor.value, 8000))),
        el('div', {}, el('h4', {}, 'Current disk file'),
          el('pre', {}, truncateText(disk.content, 8000)))));
    }
  };

  const showSaveConflict = async (error) => {
    const detail = error && error.detail && typeof error.detail === 'object' ? error.detail : {};
    const conflict = {
      path: detail.path || c.selected || c.loadedPath,
      message: detail.message || error.message,
      expectedRevision: detail.expected_revision || c.revision || '',
      currentRevision: detail.current_revision || '',
      disk: null, diskError: '', loading: true,
    };
    c.saveConflict = conflict;
    renderSaveConflict();
    try {
      conflict.disk = await api.post(
        `/api/pipelines/${encodeURIComponent(d.name)}/code/read`,
        { path: conflict.path },
      );
    } catch (readError) {
      conflict.diskError = readError.status === 404 ? 'the file no longer exists' : readError.message;
    } finally {
      conflict.loading = false;
      if (c.saveConflict === conflict && view.isConnected) renderSaveConflict();
    }
  };

  const refreshAssistant = () => {
    const pth = activeCodePath();
    assistantPath.textContent = pth === '__pipeline__' ? d.name : pth;
    assistantNotes.value = loadCodeNote(d.name, pth);
    assistantStatus.textContent = assistantNotes.value ? 'saved notes' : 'no notes';
    refreshPackageHelpIfNeeded();
  };

  const saveActiveNote = () => {
    saveCodeNote(d.name, activeCodePath(), assistantNotes.value);
    assistantStatus.textContent = 'saved';
  };

  const clearSavedNotes = () => {
    const confirmed = window.confirm(
      'Clear all saved Assistant notes for this workspace in this browser?\n\n' +
      'This also clears notes saved by older app versions that could not be scoped to a workspace.',
    );
    if (!confirmed) return;
    const removed = clearWorkspaceCodeNotes();
    assistantNotes.value = '';
    assistantStatus.textContent = removed ? `cleared ${removed} saved note${removed === 1 ? '' : 's'}` : 'no saved notes';
  };

  const selectionText = () => editor.value.slice(editor.selectionStart || 0, editor.selectionEnd || 0);

  const diagnosticContext = () => {
    const items = c.diagnostics && c.diagnostics.items ? c.diagnostics.items : [];
    const active = c.selected || c.loadedPath;
    return items
      .filter(item => !active || !item.path || item.path === active)
      .slice(0, 8)
      .map(item => `${item.level || 'info'} ${item.path || 'pipeline'}${item.line ? ':' + item.line : ''}: ${item.title || item.message || item.code || 'finding'}`)
      .join('\n');
  };

  const buildAssistantPrompt = (mode) => {
    const path = c.selected || c.loadedPath || '(no active file)';
    const selected = selectionText();
    const notes = assistantNotes.value.trim();
    const diagnostics = diagnosticContext();
    const task = {
      review: 'Review the active file and give concrete coding suggestions. Focus on bugs, clarity, tests, and pipeline safety.',
      explain: selected ? 'Explain the selected code and call out any risks or assumptions.' : 'Explain the active file structure and call out any risks or assumptions.',
      tests: 'Suggest focused tests or validation checks for this file and explain what each would catch.',
      notes: 'Improve these side notes into a concise implementation checklist, preserving important assumptions.',
    }[mode] || 'Assist with this code.';
    return [
      `You are assisting inside the OmicsANG code editor for pipeline "${d.name}".`,
      `Repository: ${d.path}`,
      `Active file: ${path}`,
      c.dirty ? 'The editor has unsaved changes; treat the pasted content as newer than disk.' : 'The pasted content matches the editor state.',
      '',
      task,
      'Do not modify files unless I explicitly ask you to in this session. Start with concise suggestions.',
      '',
      notes ? `Side notes:\n${truncateText(notes, 3000)}` : 'Side notes: none',
      diagnostics ? `Diagnostics:\n${diagnostics}` : 'Diagnostics: none loaded or none relevant',
      selected ? `Selected text:\n${truncateText(selected, 5000)}` : 'Selected text: none',
      `File content:\n${truncateText(editor.value || '', 14000)}`,
    ].join('\n\n');
  };

  const launchAssistant = async (mode) => {
    saveActiveNote();
    const prompt = buildAssistantPrompt(mode);
    if (!confirmAgentLaunch(
      assistantTool.value,
      'The active file content, selection, diagnostics, side notes, repository path, and generated prompt will be supplied to the agent. This confirmation summarizes the context categories; Copy prompt exports the Review action prompt for separate inspection.',
    )) {
      assistantStatus.textContent = 'launch canceled';
      return;
    }
    assistantStatus.textContent = 'launching...';
    try {
      const { session } = await api.post('/api/agents', {
        pipeline: d.name,
        tool: assistantTool.value,
        prompt,
        worktree_branch: '',
        acknowledge_external_agent: true,
      });
      assistantStatus.textContent = 'launched';
      openTerminal(session);
    } catch (e) {
      assistantStatus.textContent = 'error: ' + e.message;
    }
  };

  const copyAssistantPrompt = async () => {
    saveActiveNote();
    const prompt = buildAssistantPrompt('review');
    try {
      await navigator.clipboard.writeText(prompt);
      assistantStatus.textContent = 'prompt copied';
    } catch (e) {
      assistantStatus.textContent = 'copy failed';
    }
  };

  const activeTab = () => c.tabs.find(t => t.key === c.activeTab) || null;
  const findTab = (path) => c.tabs.find(t => t.path === path || t.loadedPath === path);
  const comparablePath = (path) => {
    const value = String(path || '');
    if (value.startsWith('/') || value.startsWith('~') || value.includes('\\')) return value;
    return value.split('/').filter(part => part && part !== '.').join('/');
  };
  let editorLines = editor.value.split('\n');
  let editorLineStarts = EditorCore.lineStarts(editor.value);
  let largeFileMode = false;
  let tabMovesFocus = false;
  let highlightFrame = 0;
  let chromeFrame = 0;
  let completionTimer = 0;
  let completionResult = null;
  let dismissedCompletionSignature = '';
  let hoverTimer = 0;
  let hoverLine = -1;
  let hoverClientX = 0;

  const indentFor = (content, language, path) => EditorCore.detectIndentation(
    content, EditorCore.defaultIndentSize(language, path));

  const applyIndentState = (mode = c.indentMode, size = c.indentSize) => {
    c.indentMode = mode === 'tabs' ? 'tabs' : 'spaces';
    c.indentSize = EditorCore.normalizeTabSize(size, EditorCore.defaultIndentSize(c.language, c.selected || c.loadedPath));
    editorWrap.style.setProperty('--editor-tab-size', String(c.indentSize));
    indentSelect.value = `${c.indentMode}:${c.indentSize}`;
  };

  const syncCurrentTab = () => {
    const t = activeTab();
    if (!t) return;
    Object.assign(t, {
      path: c.selected, loadedPath: c.loadedPath, content: c.content,
      saved: c.saved, dirty: c.dirty, language: c.language, size: c.size, mtime: c.mtime,
      revision: c.revision,
      indentMode: c.indentMode, indentSize: c.indentSize,
      selectionStart: editor.selectionStart || 0, selectionEnd: editor.selectionEnd || 0,
      selectionDirection: editor.selectionDirection || 'none',
      scrollTop: editor.scrollTop || 0, scrollLeft: editor.scrollLeft || 0,
    });
  };

  const restoreEditorView = (t) => {
    if (!t) {
      editor.setSelectionRange(0, 0);
      editor.scrollTop = 0;
      editor.scrollLeft = 0;
      return;
    }
    const selectionStart = Math.max(0, Math.min(editor.value.length, Number(t.selectionStart) || 0));
    const selectionEnd = Math.max(selectionStart, Math.min(editor.value.length, Number(t.selectionEnd) || selectionStart));
    editor.setSelectionRange(selectionStart, selectionEnd, t.selectionDirection || 'none');
    editor.scrollTop = Number(t.scrollTop) || 0;
    editor.scrollLeft = Number(t.scrollLeft) || 0;
  };

  const renderTabs = () => {
    tabStrip.innerHTML = '';
    if (!c.tabs.length) {
      tabStrip.append(el('div', { class: 'code-tab empty-tab' }, 'No open files'));
      return;
    }
    c.tabs.forEach((t, index) => {
      const active = t.key === c.activeTab;
      const tabId = `code-file-tab-${codeId}-${index}`;
      const tab = el('button', {
        class: `code-tab ${t.key === c.activeTab ? 'active' : ''} ${t.dirty ? 'dirty' : ''}`,
        id: tabId,
        type: 'button', role: 'tab', 'aria-selected': active ? 'true' : 'false',
        'aria-controls': editorWrap.id, tabindex: active ? '0' : '-1',
        title: t.path || t.loadedPath || 'untitled',
        onclick: () => activateTab(t),
        onkeydown: (e) => {
          let next = null;
          if (e.key === 'ArrowRight') next = (index + 1) % c.tabs.length;
          else if (e.key === 'ArrowLeft') next = (index - 1 + c.tabs.length) % c.tabs.length;
          else if (e.key === 'Home') next = 0;
          else if (e.key === 'End') next = c.tabs.length - 1;
          if (next == null) return;
          e.preventDefault();
          const target = c.tabs[next];
          activateTab(target);
          requestAnimationFrame(() => tabStrip.querySelectorAll('[role="tab"]')[next]?.focus());
        },
      }, codeTabLabel(t.path || t.loadedPath));
      const close = el('button', {
        class: 'tab-close', type: 'button',
        title: `Close ${codeTabLabel(t.path || t.loadedPath)}`,
        'aria-label': `Close ${codeTabLabel(t.path || t.loadedPath)}`,
        onclick: () => closeTab(t),
      }, '×');
      if (active) editorWrap.setAttribute('aria-labelledby', tabId);
      tabStrip.append(el('div', { class: `code-tab-slot ${active ? 'active' : ''}`, role: 'presentation' }, tab, close));
    });
  };

  const activateTab = (t) => {
    clearInlineCompletion();
    hideLineHoverHelp();
    dismissedCompletionSignature = '';
    syncCurrentTab();
    c.activeTab = t.key;
    Object.assign(c, {
      selected: t.path || '', loadedPath: t.loadedPath || '',
      content: t.content || '', saved: t.saved || '',
      dirty: !!t.dirty, language: t.language || '', size: t.size || 0, mtime: t.mtime || null,
      revision: t.revision || '',
      indentMode: t.indentMode || 'spaces', indentSize: t.indentSize || EditorCore.defaultIndentSize(t.language, t.path || t.loadedPath),
    });
    pathIn.value = c.selected;
    editor.value = c.content;
    editorLines = editor.value.split('\n');
    editorLineStarts = EditorCore.lineStarts(editor.value);
    applyIndentState();
    editor.setAttribute('aria-label', `Edit ${c.selected || c.loadedPath || 'untitled file'}`);
    restoreEditorView(t);
    if (c.selected || c.loadedPath) revealPath(c.selected || c.loadedPath);
    clearSaveConflict();
    refreshAssistant();
    updateDirty(true);
    renderTree();
    editor.focus();
  };

  const closeTab = (t) => {
    if (t.dirty && !window.confirm(`Close ${t.path || 'untitled'} with unsaved changes?`)) return;
    const idx = c.tabs.indexOf(t);
    if (idx >= 0) c.tabs.splice(idx, 1);
    if (c.activeTab === t.key) {
      const next = c.tabs[Math.min(idx, c.tabs.length - 1)] || c.tabs[c.tabs.length - 1];
      if (next) activateTab(next);
      else {
        Object.assign(c, {
          activeTab: '', selected: '', loadedPath: '', content: '', saved: '',
          dirty: false, language: '', size: 0, mtime: null, revision: '',
        });
        pathIn.value = '';
        editor.value = '';
        editorLines = [''];
        editor.setSelectionRange(0, 0);
        editor.scrollTop = 0;
        editor.scrollLeft = 0;
        clearSaveConflict();
        refreshAssistant();
        updateDirty(true);
        renderTree();
      }
    }
    renderTabs();
  };

  let renderedLineCount = -1;

  const editorMetrics = () => {
    const style = getComputedStyle(editor);
    return {
      lineHeight: parseFloat(style.lineHeight) || 20,
      paddingTop: parseFloat(style.paddingTop) || 12,
      paddingLeft: parseFloat(style.paddingLeft) || 58,
      fontSize: parseFloat(style.fontSize) || 13,
    };
  };

  const syncEditorAssistControls = () => {
    completionBtn.classList.toggle('active', !!state.uiPrefs.codeInlineCompletions);
    completionBtn.setAttribute('aria-pressed', state.uiPrefs.codeInlineCompletions ? 'true' : 'false');
    hoverHelpBtn.classList.toggle('active', !!state.uiPrefs.codeHoverHelp);
    hoverHelpBtn.setAttribute('aria-pressed', state.uiPrefs.codeHoverHelp ? 'true' : 'false');
  };

  const clearInlineCompletion = (dismiss = false) => {
    clearTimeout(completionTimer);
    completionTimer = 0;
    if (dismiss && completionResult) dismissedCompletionSignature = completionResult.signature;
    completionResult = null;
    inlineCompletion.textContent = '';
    inlineCompletion.classList.add('hidden');
    completionBtn.title = 'Toggle local inline completions (Ctrl/Command+Space requests one)';
  };

  const updateInlineCompletion = (force = false) => {
    completionTimer = 0;
    syncEditorAssistControls();
    if (!state.uiPrefs.codeInlineCompletions || largeFileMode ||
        editor.selectionStart !== editor.selectionEnd || !editor.value) {
      clearInlineCompletion();
      return;
    }
    const point = editor.selectionStart || 0;
    const result = EditorCore.inlineCompletionAt(editor.value, point, {
      language: c.language,
      path: c.selected || c.loadedPath,
    });
    if (!result) {
      clearInlineCompletion();
      return;
    }
    const signature = `${point}:${result.prefix}:${result.candidate}:${editor.value.length}`;
    if (force) dismissedCompletionSignature = '';
    if (signature === dismissedCompletionSignature) {
      clearInlineCompletion();
      return;
    }
    const position = EditorCore.cursorPositionFromStarts(
      editor.value, point, editorLineStarts, c.indentSize);
    inlineCompletion.textContent = `${'\n'.repeat(position.lineIndex)}${' '.repeat(Math.max(0, position.visualColumn - 1))}${result.suffix}`;
    inlineCompletion.style.transform = `translate(${-editor.scrollLeft}px, ${-editor.scrollTop}px)`;
    inlineCompletion.classList.remove('hidden');
    completionResult = { ...result, point, signature };
    completionBtn.title = `Tab completes “${result.candidate}” · Ctrl/Command+Space refreshes`;
  };

  const scheduleInlineCompletion = (force = false) => {
    clearTimeout(completionTimer);
    completionTimer = setTimeout(() => {
      if (view.isConnected) updateInlineCompletion(force);
    }, force ? 0 : 90);
  };

  const acceptInlineCompletion = () => {
    const result = completionResult;
    const point = editor.selectionStart || 0;
    if (!result || point !== result.point || point !== editor.selectionEnd) return false;
    editor.setRangeText(result.suffix, point, point, 'end');
    dismissedCompletionSignature = '';
    clearInlineCompletion();
    updateDirty();
    setStatus(`completed ${result.candidate}`, 'ok');
    return true;
  };

  const hideLineHoverHelp = () => {
    clearTimeout(hoverTimer);
    hoverTimer = 0;
    hoverLine = -1;
    lineHoverHelp.classList.add('hidden');
    lineHoverHelp.replaceChildren();
  };

  const showLineHoverHelp = (lineIndex, clientX = 0) => {
    if (!state.uiPrefs.codeHoverHelp || !view.isConnected || lineIndex < 0 || lineIndex >= editorLines.length) {
      hideLineHoverHelp();
      return;
    }
    const explanation = EditorCore.explainLine(editorLines[lineIndex], {
      language: c.language,
      path: c.selected || c.loadedPath,
      lineNumber: lineIndex + 1,
    });
    if (!explanation) {
      hideLineHoverHelp();
      return;
    }
    const metrics = editorMetrics();
    const rect = editor.getBoundingClientRect();
    const desiredTop = metrics.paddingTop + ((lineIndex + 1) * metrics.lineHeight) - editor.scrollTop + 4;
    const desiredLeft = clientX ? clientX - rect.left + 14 : metrics.paddingLeft + 24;
    const maxLeft = Math.max(8, editorWrap.clientWidth - 370);
    lineHoverHelp.replaceChildren(
      el('strong', {}, `Line ${lineIndex + 1}`),
      el('span', {}, explanation.replace(/^Line \d+:\s*/, '')),
      el('small', {}, 'Local explanation · no upload'),
    );
    lineHoverHelp.classList.remove('hidden');
    const tooltipHeight = lineHoverHelp.offsetHeight || 104;
    const maxTop = Math.max(8, editorWrap.clientHeight - tooltipHeight - 8);
    lineHoverHelp.style.top = `${Math.max(8, Math.min(maxTop, desiredTop))}px`;
    lineHoverHelp.style.left = `${Math.max(8, Math.min(maxLeft, desiredLeft))}px`;
  };

  const scheduleLineHoverHelp = (event) => {
    if (!state.uiPrefs.codeHoverHelp) {
      hideLineHoverHelp();
      return;
    }
    const metrics = editorMetrics();
    const rect = editor.getBoundingClientRect();
    const rawIndex = Math.floor(
      (event.clientY - rect.top + editor.scrollTop - metrics.paddingTop) / metrics.lineHeight);
    if (rawIndex < 0 || rawIndex >= editorLines.length) {
      hideLineHoverHelp();
      return;
    }
    const lineIndex = rawIndex;
    hoverClientX = event.clientX;
    if (lineIndex === hoverLine && (hoverTimer || !lineHoverHelp.classList.contains('hidden'))) return;
    clearTimeout(hoverTimer);
    lineHoverHelp.classList.add('hidden');
    hoverLine = lineIndex;
    hoverTimer = setTimeout(() => {
      hoverTimer = 0;
      showLineHoverHelp(hoverLine, hoverClientX);
    }, 320);
  };

  const updateLineNumbers = () => {
    if (largeFileMode) {
      const metrics = editorMetrics();
      const first = Math.max(0, Math.floor((editor.scrollTop - metrics.paddingTop) / metrics.lineHeight) - 1);
      const last = Math.min(
        editorLines.length - 1,
        Math.ceil((editor.scrollTop + editor.clientHeight - metrics.paddingTop) / metrics.lineHeight) + 1,
      );
      const visible = [];
      for (let index = first; index <= last; index += 1) visible.push(String(index + 1));
      lineNumbers.textContent = visible.join('\n');
      lineNumbers.style.transform = `translateY(${first * metrics.lineHeight - editor.scrollTop}px)`;
      renderedLineCount = -1;
    } else {
      lineNumbers.style.transform = `translateY(${-editor.scrollTop}px)`;
      if (renderedLineCount !== editorLines.length) {
        lineNumbers.textContent = editorLines.map((_, index) => String(index + 1)).join('\n');
        renderedLineCount = editorLines.length;
      }
    }
    const digits = String(Math.max(1, editorLines.length)).length;
    const width = Math.max(46, Math.ceil(digits * (editorMetrics().fontSize * .62) + 22));
    editorWrap.style.setProperty('--editor-gutter-width', `${width}px`);
  };

  const renderVisibleGuides = (position) => {
    indentLayer.innerHTML = '';
    if (largeFileMode || !state.uiPrefs.codeIndentGuides || !editorLines.length) return;
    const metrics = editorMetrics();
    const first = Math.max(0, Math.floor((editor.scrollTop - metrics.paddingTop) / metrics.lineHeight) - 1);
    const last = Math.min(
      editorLines.length - 1,
      Math.ceil((editor.scrollTop + editor.clientHeight - metrics.paddingTop) / metrics.lineHeight) + 1,
    );
    const scope = EditorCore.activeIndentScope(editorLines, position.lineIndex, c.indentSize);
    const fragment = document.createDocumentFragment();
    for (let index = first; index <= last; index += 1) {
      const line = editorLines[index] || '';
      const columns = EditorCore.guideColumns(line, c.indentSize);
      if (!line.trim() && scope.column && index >= scope.start && index <= scope.end) columns.push(scope.column);
      if (!columns.length) continue;
      const row = el('div', {
        class: 'code-indent-row',
        style: `top:${metrics.paddingTop + index * metrics.lineHeight - editor.scrollTop}px;left:${metrics.paddingLeft - editor.scrollLeft}px;height:${metrics.lineHeight}px`,
      });
      [...new Set(columns)].forEach((column) => row.append(el('span', {
        class: `code-indent-guide ${scope.column === column && index >= scope.start && index <= scope.end ? 'active' : ''}`,
        style: `--guide-column:${column}`,
      })));
      fragment.append(row);
    }
    indentLayer.append(fragment);
  };

  const refreshAssistantSelection = () => {
    assistantPreview.textContent = 'Inline completions and hover explanations stay local. ' + (selectionText()
      ? 'AI prompts will include the current selection, active file, diagnostics, and notes.'
      : 'AI prompts include the active file, diagnostics, and notes. Select text for narrower help.') +
      ' Saved notes stay in this browser and are scoped to this workspace root.';
  };

  const updateSelectionChrome = () => {
    if (!view.isConnected) return;
    const position = EditorCore.cursorPositionFromStarts(
      editor.value, editor.selectionStart || 0, editorLineStarts, c.indentSize);
    const metrics = editorMetrics();
    cursorStatus.textContent = `Ln ${position.line}, Col ${position.visualColumn}`;
    const selected = Math.abs((editor.selectionEnd || 0) - (editor.selectionStart || 0));
    selectionStatus.textContent = selected ? `${selected.toLocaleString()} selected` : '';
    selectionStatus.classList.toggle('hidden', !selected);
    currentLineNumber.textContent = String(position.line);
    currentLineNumber.style.transform = `translateY(${metrics.paddingTop + position.lineIndex * metrics.lineHeight - editor.scrollTop}px)`;
    activeLine.style.transform = `translateY(${metrics.paddingTop + position.lineIndex * metrics.lineHeight - editor.scrollTop}px)`;
    activeLine.style.height = `${metrics.lineHeight}px`;
    languageStatus.textContent = editorLanguageForPath(c.selected || c.loadedPath, c.language || 'text');
    guidesBtn.classList.toggle('active', !!state.uiPrefs.codeIndentGuides);
    guidesBtn.setAttribute('aria-pressed', state.uiPrefs.codeIndentGuides ? 'true' : 'false');
    syncEditorAssistControls();
    refreshAssistantSelection();
    renderVisibleGuides(position);
    refreshCommandContext();
    syncCurrentTab();
    scheduleInlineCompletion();
  };

  const scheduleSelectionChrome = () => {
    if (chromeFrame) return;
    chromeFrame = requestAnimationFrame(() => {
      chromeFrame = 0;
      updateSelectionChrome();
    });
  };

  const updateHighlight = () => {
    if (largeFileMode) {
      highlight.textContent = '';
      largeFileStatus.textContent = 'Large file · syntax colors paused';
      largeFileStatus.classList.remove('hidden');
    } else {
      const html = highlightCode(editor.value || '', c.language || '', c.selected || c.loadedPath || '');
      highlight.innerHTML = html || '<br>';
      largeFileStatus.textContent = lastHighlightWasCapped
        ? 'Dense file · syntax colors paused' : 'Large file · syntax colors paused';
      largeFileStatus.classList.toggle('hidden', !lastHighlightWasCapped);
    }
  };

  const scheduleHighlight = () => {
    if (highlightFrame) return;
    highlightFrame = requestAnimationFrame(() => {
      highlightFrame = 0;
      if (!view.isConnected) return;
      updateHighlight();
      syncHighlightScroll();
    });
  };

  const syncHighlightScroll = () => {
    highlight.style.transform = `translate(${-editor.scrollLeft}px, ${-editor.scrollTop}px)`;
    inlineCompletion.style.transform = `translate(${-editor.scrollLeft}px, ${-editor.scrollTop}px)`;
    if (largeFileMode) updateLineNumbers();
    else lineNumbers.style.transform = `translateY(${-editor.scrollTop}px)`;
    hideLineHoverHelp();
    syncCurrentTab();
    scheduleSelectionChrome();
  };

  const updateDirty = (forceTabs = false) => {
    const previousPath = c.selected;
    const previousDirty = c.dirty;
    const path = pathIn.value.trim();
    c.selected = path;
    c.content = editor.value;
    if (!c.loadedPath) c.language = editorLanguageForPath(path, c.language || 'text');
    c.dirty = c.content !== c.saved || path !== c.loadedPath;
    editorLines = editor.value.split('\n');
    editorLineStarts = EditorCore.lineStarts(editor.value);
    largeFileMode = editor.value.length > 300000 || editorLines.length > 15000;
    editorWrap.classList.toggle('large-file-mode', largeFileMode);
    largeFileStatus.classList.toggle('hidden', !largeFileMode);
    saveBtn.disabled = !path || !c.dirty;
    reloadBtn.disabled = !c.loadedPath;
    renameBtn.disabled = !c.loadedPath || c.dirty;
    duplicateBtn.disabled = !c.loadedPath || c.dirty;
    deleteBtn.disabled = !c.loadedPath || c.dirty;
    pathIn.classList.toggle('dirty', c.dirty);
    const bits = [];
    if (c.loadedPath) bits.push(c.language || 'text', fmtBytes(c.size || 0), fmtTime(c.mtime));
    if (c.dirty) bits.push('unsaved');
    meta.textContent = bits.join(' · ');
    editor.setAttribute('aria-label', `Edit ${path || c.loadedPath || 'untitled file'}`);
    updateLineNumbers();
    scheduleHighlight();
    scheduleSelectionChrome();
    syncCurrentTab();
    if (forceTabs || previousPath !== c.selected || previousDirty !== c.dirty) renderTabs();
  };

  const visibleInfo = () => {
    const query = c.filter.trim();
    const visiblePaths = new Set(['']);
    const matchCounts = new Map();
    const matchingFiles = [];
    for (const f of c.files || []) {
      const score = fuzzyScore(f.path, query);
      if (score === -Infinity) continue;
      matchingFiles.push(f.path);
      visiblePaths.add(f.path);
      for (const a of ancestors(f.path)) {
        visiblePaths.add(a);
        matchCounts.set(a, (matchCounts.get(a) || 0) + 1);
      }
    }
    return { query, visiblePaths, matchCounts, matchingFiles };
  };

  const revealPath = (path) => {
    ancestors(path).forEach(a => c.expanded.add(a));
    c.focusPath = path;
  };

  const jumpToLine = (line) => {
    const pos = lineOffset(editor.value || '', line);
    editor.focus();
    editor.setSelectionRange(pos, pos);
    const lh = parseFloat(getComputedStyle(editor).lineHeight) || 20;
    editor.scrollTop = Math.max(0, (Math.max(1, Number(line) || 1) - 1) * lh - 90);
    syncHighlightScroll();
  };

  const renderTree = () => {
    c.filter = filterIn.value;
    fileList.innerHTML = '';
    fileList.removeAttribute('aria-activedescendant');
    c.visibleTree = [];
    if (!c.loaded) {
      fileList.append(el('div', { class: 'empty slim' }, 'Loading files…'));
      return;
    }
    if (!(c.files || []).length) {
      fileList.append(el('div', { class: 'empty slim' }, 'No editable files found.'));
      return;
    }
    const tree = buildCodeTree(c.files);
    const info = visibleInfo();
    if (info.query && !info.matchingFiles.length) {
      fileList.append(el('div', { class: 'empty slim' }, 'No matching files.'));
      return;
    }

    const addNode = (node, depth) => {
      if (node.path && info.query && !info.visiblePaths.has(node.path)) return;
      const domId = node.path ? `code-tree-${codeId}-${c.visibleTree.length}` : '';
      if (node.path) c.visibleTree.push({ path: node.path, type: node.type, domId });
      const active = node.path === c.selected || node.path === c.loadedPath;
      const focused = node.path === c.focusPath;
      const row = el('div', {
        id: domId || null,
        class: `code-node ${node.type} ${active ? 'active' : ''} ${focused ? 'focused' : ''} ${active && c.dirty ? 'dirty' : ''}`,
        style: `--depth:${depth}`,
        title: node.path || state.detail.path,
        role: 'treeitem',
        'aria-level': String(depth + 1),
        'aria-selected': active ? 'true' : 'false',
        onclick: (e) => {
          e.stopPropagation();
          c.focusPath = node.path;
          if (node.type === 'dir') {
            c.expanded.has(node.path) ? c.expanded.delete(node.path) : c.expanded.add(node.path);
            renderTree();
          } else {
            openFile(node.path);
          }
        },
      });
      if (node.type === 'dir') {
        const expanded = info.query || c.expanded.has(node.path);
        row.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        row.append(
          el('span', { class: 'tree-twist' }, expanded ? 'v' : '>'),
          el('span', { class: 'tree-kind' }, 'dir'),
          el('span', { class: 'tree-name' }, node.name),
          el('span', { class: 'tree-count' }, String(info.query ? (info.matchCounts.get(node.path) || 0) : node.count)),
          el('button', {
            class: 'tree-action',
            title: `New file in ${node.path}`,
            onclick: (e) => { e.stopPropagation(); newFile(node.path); },
          }, '+'));
        fileList.append(row);
        if (expanded) node.children.forEach(child => addNode(child, depth + 1));
      } else {
        row.append(
          el('span', { class: 'tree-twist' }, ''),
          el('span', { class: `tree-kind lang-${node.language || 'text'}` }, node.language || 'txt'),
          el('span', { class: 'tree-name' }, node.name),
          el('span', { class: 'tree-subpath' }, parentPath(node.path)),
          el('span', { class: 'tree-size' }, fmtBytes(node.size || 0)));
        fileList.append(row);
      }
    };
    tree.children.forEach(child => addNode(child, 0));
    if (!c.focusPath && c.visibleTree.length) c.focusPath = c.loadedPath || c.visibleTree[0].path;
    const focusedNode = c.visibleTree.find(node => node.path === c.focusPath);
    if (focusedNode && focusedNode.domId) fileList.setAttribute('aria-activedescendant', focusedNode.domId);
    const visible = info.query ? `${info.matchingFiles.length}/${c.files.length}` : `${c.files.length}`;
    setStatus(`${visible} editable files`);
  };

  const loadFiles = async (force = false) => {
    if (c.loaded && !force) {
      renderTree();
      return;
    }
    c.loaded = false;
    renderTree();
    setStatus('scanning files…');
    try {
      const res = await api.get(`/api/pipelines/${d.name}/files?limit=1200`);
      c.files = res.files || [];
      c.loaded = true;
      renderTree();
      if (!c.tabs.length && !c.loadedPath && c.files.length) openFile(c.files[0].path, false);
    } catch (e) {
      c.loaded = true;
      setStatus('error: ' + e.message, 'err');
      renderTree();
    }
  };

  const resetTo = (r) => {
    syncCurrentTab();
    let t = findTab(r.path);
    if (!t) {
      t = { key: r.path };
      c.tabs.push(t);
    }
    const indent = t.indentMode
      ? { mode: t.indentMode, size: t.indentSize }
      : indentFor(r.content, r.language, r.path);
    Object.assign(t, {
      key: r.path, path: r.path, loadedPath: r.path, content: r.content, saved: r.content,
      dirty: false, language: r.language, size: r.size, mtime: r.mtime, revision: r.revision || '',
      indentMode: indent.mode, indentSize: indent.size,
    });
    c.activeTab = t.key;
    Object.assign(c, {
      selected: r.path, loadedPath: r.path, content: r.content, saved: r.content,
      dirty: false, language: r.language, size: r.size, mtime: r.mtime, revision: r.revision || '',
      indentMode: indent.mode, indentSize: indent.size,
    });
    pathIn.value = c.selected;
    editor.value = c.content;
    editorLines = editor.value.split('\n');
    editorLineStarts = EditorCore.lineStarts(editor.value);
    applyIndentState();
    restoreEditorView(t);
    revealPath(c.selected);
    clearSaveConflict();
    c.packageHelp = null;
    c.packageHelpPath = '';
    refreshAssistant();
    updateDirty(true);
    renderTree();
    if (c.packageHelpOpen) loadPackageHelp(true);
  };

  const openFile = async (path, confirmDirty = true, force = false, line = 0) => {
    const existing = findTab(path);
    if (existing && !force) {
      activateTab(existing);
      if (line) jumpToLine(line);
      setStatus(existing.dirty ? 'open tab has unsaved changes' : 'loaded');
      return;
    }
    if (force && c.dirty && c.loadedPath === path && confirmDirty &&
        !window.confirm('Discard unsaved changes and reload from disk?')) return;
    setStatus('loading…');
    try {
      const r = await api.post(`/api/pipelines/${encodeURIComponent(d.name)}/code/read`, { path });
      resetTo(r);
      if (line) jumpToLine(line);
      setStatus('loaded');
      editor.focus();
    } catch (e) {
      setStatus('error: ' + e.message, 'err');
    }
  };

  const save = async (overwrite = false) => {
    updateDirty();
    if (!c.selected) {
      setStatus('path is required', 'err');
      return;
    }
    const currentTab = activeTab();
    const selectedPath = comparablePath(c.selected);
    const conflictingTab = c.tabs.find(tab => tab !== currentTab &&
      (comparablePath(tab.path) === selectedPath || comparablePath(tab.loadedPath) === selectedPath));
    if (conflictingTab) {
      setStatus('that path is already open in another tab; close it before saving', 'err');
      return;
    }
    setStatus('saving…');
    try {
      const r = await api.post(`/api/pipelines/${d.name}/code`, {
        path: c.selected,
        content: editor.value,
        create_dirs: true,
        expected_revision: comparablePath(c.selected) === comparablePath(c.loadedPath) ? c.revision : '',
        overwrite: !!overwrite,
      });
      const t = activeTab() || { key: r.path };
      if (!c.tabs.includes(t)) c.tabs.push(t);
      Object.assign(t, {
        key: r.path, path: r.path, loadedPath: r.path, content: editor.value, saved: editor.value,
        dirty: false, language: r.language, size: r.size, mtime: r.mtime, revision: r.revision || '',
        indentMode: c.indentMode, indentSize: c.indentSize,
      });
      c.activeTab = t.key;
      Object.assign(c, {
        selected: r.path, loadedPath: r.path, content: editor.value, saved: editor.value,
        dirty: false, language: r.language, size: r.size, mtime: r.mtime, revision: r.revision || '',
      });
      pathIn.value = c.selected;
      clearSaveConflict();
      revealPath(c.selected);
      refreshAssistant();
      updateDirty(true);
      setStatus(r.backup ? `saved · backup ${r.backup}` : 'saved');
      await loadFiles(true);
      if (c.packageHelpOpen) await loadPackageHelp(true);
      renderTree();
    } catch (e) {
      if ([409, 428].includes(e.status) && e.detail &&
          ['external-file-change', 'save-precondition-required'].includes(e.detail.code)) {
        setStatus('save paused: file changed on disk', 'err');
        await showSaveConflict(e);
        return;
      }
      setStatus('error: ' + e.message, 'err');
    }
  };

  const newFile = (baseDir = '') => {
    const start = baseDir ? `${baseDir}/new_file.py` : (c.selected || 'workflow/new_rule.smk');
    const path = window.prompt('New file path:', start);
    if (!path) return;
    const requestedPath = comparablePath(path.trim());
    const existing = c.tabs.find(tab =>
      comparablePath(tab.path) === requestedPath || comparablePath(tab.loadedPath) === requestedPath);
    if (existing) {
      activateTab(existing);
      setStatus('that path is already open');
      return;
    }
    syncCurrentTab();
    const language = editorLanguageForPath(path, 'text');
    const indentSize = EditorCore.defaultIndentSize(language, path);
    const t = {
      key: `new:${Date.now()}:${Math.random().toString(16).slice(2)}`,
      path: path.trim(), loadedPath: '', content: '', saved: '',
      dirty: true, language, size: 0, mtime: null, revision: '',
      indentMode: 'spaces', indentSize, selectionStart: 0, selectionEnd: 0,
      scrollTop: 0, scrollLeft: 0,
    };
    c.tabs.push(t);
    c.activeTab = t.key;
    Object.assign(c, {
      selected: t.path, loadedPath: '', content: '', saved: '',
      dirty: true, language, size: 0, mtime: null, revision: '',
      indentMode: 'spaces', indentSize,
    });
    pathIn.value = c.selected;
    editor.value = '';
    editorLines = [''];
    editor.setSelectionRange(0, 0);
    editor.scrollTop = 0;
    editor.scrollLeft = 0;
    applyIndentState();
    revealPath(c.selected);
    clearSaveConflict();
    refreshAssistant();
    updateDirty(true);
    renderTree();
    setStatus('new file');
    editor.focus();
  };

  const newFolder = async () => {
    const start = parentPath(c.selected || c.loadedPath) || 'workflow';
    const path = window.prompt('New folder path:', start);
    if (!path) return;
    setStatus('creating folder…');
    try {
      const r = await api.post(`/api/pipelines/${d.name}/code/mkdir`, { path });
      c.expanded.add(r.path);
      setStatus('folder created');
      await loadFiles(true);
    } catch (e) {
      setStatus('error: ' + e.message, 'err');
    }
  };

  const renameFile = async () => {
    if (!c.loadedPath || c.dirty) return;
    const dst = window.prompt('Move/rename to:', c.loadedPath);
    if (!dst || dst === c.loadedPath) return;
    setStatus('renaming…');
    try {
      const r = await api.post(`/api/pipelines/${d.name}/code/move`, { src: c.loadedPath, dst, create_dirs: true });
      const t = activeTab();
      if (t) Object.assign(t, { key: r.path, path: r.path, loadedPath: r.path, size: r.size, mtime: r.mtime, language: r.language });
      c.activeTab = r.path;
      Object.assign(c, { selected: r.path, loadedPath: r.path, language: r.language, size: r.size, mtime: r.mtime });
      pathIn.value = r.path;
      revealPath(r.path);
      refreshAssistant();
      setStatus('renamed');
      updateDirty(true);
      await loadFiles(true);
    } catch (e) {
      setStatus('error: ' + e.message, 'err');
    }
  };

  const duplicateFile = async () => {
    if (!c.loadedPath || c.dirty) return;
    const dst = window.prompt('Duplicate to:', duplicatePath(c.loadedPath));
    if (!dst) return;
    setStatus('duplicating…');
    try {
      const r = await api.post(`/api/pipelines/${d.name}/code/copy`, { src: c.loadedPath, dst, create_dirs: true });
      await loadFiles(true);
      await openFile(r.path, false, true);
      setStatus('duplicated');
    } catch (e) {
      setStatus('error: ' + e.message, 'err');
    }
  };

  const deleteFile = async () => {
    if (!c.loadedPath || c.dirty) return;
    if (!window.confirm(`Move ${c.loadedPath} to OmicsANG trash?`)) return;
    const doomed = c.loadedPath;
    setStatus('deleting…');
    try {
      const r = await api.post(`/api/pipelines/${d.name}/code/delete`, { path: doomed });
      c.tabs = c.tabs.filter(t => t.path !== doomed && t.loadedPath !== doomed);
      const next = c.tabs[0];
      if (next) activateTab(next);
      else {
        Object.assign(c, {
          activeTab: '', selected: '', loadedPath: '', content: '', saved: '',
          dirty: false, language: '', size: 0, mtime: null,
        });
        pathIn.value = '';
        editor.value = '';
        editorLines = [''];
        editor.setSelectionRange(0, 0);
        editor.scrollTop = 0;
        editor.scrollLeft = 0;
        refreshAssistant();
        updateDirty(true);
      }
      setStatus(`deleted · trash ${r.trashed_to.split('/').slice(-1)[0]}`);
      await loadFiles(true);
    } catch (e) {
      setStatus('error: ' + e.message, 'err');
    }
  };

  const applyDiagnosticFix = async (item, fix) => {
    if (!fix) return;
    setStatus('applying quick fix...');
    try {
      const res = await api.post(`/api/pipelines/${d.name}/diagnostics/fix`, fix);
      setStatus(res.backup ? `fixed · backup ${res.backup}` : 'fixed', 'ok');
      await loadFiles(true);
      if (res.path && fix.kind !== 'dir') await openFile(res.path, false, true, item.line || 1);
      await runDiagnostics();
    } catch (e) {
      setStatus('fix failed: ' + e.message, 'err');
    }
  };

  const dryRunFromDiagnostic = async (item) => {
    if (d.kind !== 'snakemake') return;
    const f = { ...runFormFor(d), dryrun: true };
    if (item && item.dry_run_target) f.target = item.dry_run_target;
    await launchRun(d.name, f, true);
  };

  const renderDiagnostics = () => {
    diagnosticsPanel.innerHTML = '';
    const data = c.diagnostics;
    if (!data) {
      diagnosticsPanel.classList.add('hidden');
      return;
    }
    diagnosticsPanel.classList.remove('hidden');
    const s = data.summary || {};
    diagnosticsPanel.append(el('div', { class: 'diag-head' },
      el('strong', {}, 'Diagnostics'),
      el('span', { class: 'diag-summary' },
        `${s.error || 0} errors · ${s.warning || 0} warnings · ${data.scanned || 0} files scanned`),
      el('button', { class: 'ghost sm', onclick: () => { c.diagnostics = null; renderDiagnostics(); } }, 'Hide')));
    if (!(data.items || []).length) {
      diagnosticsPanel.append(el('div', { class: 'diag-empty' }, 'No issues found by the lightweight scanner.'));
      return;
    }
    const list = el('div', { class: 'diag-list' });
    data.items.forEach((item) => {
      const actions = el('span', { class: 'diag-actions' });
      (item.fixes || []).forEach((fix) => {
        actions.append(el('button', {
          class: 'ghost sm',
          onclick: (e) => { e.stopPropagation(); applyDiagnosticFix(item, fix); },
        }, fix.label || 'Fix'));
      });
      if (d.kind === 'snakemake') {
        actions.append(el('button', {
          class: 'ghost sm',
          onclick: (e) => { e.stopPropagation(); dryRunFromDiagnostic(item); },
        }, 'Dry-run'));
      }
      list.append(el('div', {
        class: `diag-item ${item.level || 'info'}`,
        onclick: () => item.path ? openFile(item.path, false, false, item.line || 1) : null,
      },
        el('span', { class: `diag-level ${item.level || 'info'}` }, item.level || 'info'),
        el('span', { class: 'diag-title' }, item.title || item.code || 'Finding'),
        el('span', { class: 'diag-loc' }, item.path ? `${item.path}${item.line ? ':' + item.line : ''}` : 'pipeline'),
        el('span', { class: 'diag-msg' }, item.message || ''),
        actions));
    });
    diagnosticsPanel.append(list);
  };

  const runDiagnostics = async () => {
    diagnosticsPanel.classList.remove('hidden');
    diagnosticsPanel.innerHTML = '';
    diagnosticsPanel.append(el('div', { class: 'muted' }, 'running diagnostics…'));
    setStatus('running diagnostics…');
    try {
      c.diagnostics = await api.get(`/api/pipelines/${d.name}/diagnostics`);
      const s = c.diagnostics.summary || {};
      setStatus(`${s.error || 0} errors · ${s.warning || 0} warnings`, s.error ? 'err' : 'ok');
      renderDiagnostics();
    } catch (e) {
      diagnosticsPanel.innerHTML = '';
      diagnosticsPanel.append(el('div', { class: 'diag-empty err' }, e.message));
      setStatus('error: ' + e.message, 'err');
    }
  };

  const moveFocus = (delta) => {
    if (!c.visibleTree.length) return;
    let idx = c.visibleTree.findIndex(n => n.path === c.focusPath);
    if (idx < 0) idx = c.visibleTree.findIndex(n => n.path === c.selected);
    idx = Math.min(c.visibleTree.length - 1, Math.max(0, idx + delta));
    c.focusPath = c.visibleTree[idx].path;
    renderTree();
  };

  const focusedNode = () => c.visibleTree.find(n => n.path === c.focusPath);

  fileList.onkeydown = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveFocus(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveFocus(-1); }
    else if (e.key === 'ArrowRight') {
      const n = focusedNode(); if (!n) return;
      e.preventDefault();
      if (n.type === 'dir') { c.expanded.add(n.path); renderTree(); }
      else openFile(n.path);
    } else if (e.key === 'ArrowLeft') {
      const n = focusedNode(); if (!n) return;
      e.preventDefault();
      if (n.type === 'dir' && c.expanded.has(n.path)) c.expanded.delete(n.path);
      else c.focusPath = parentPath(n.path);
      renderTree();
    } else if (e.key === 'Enter' || e.key === ' ') {
      const n = focusedNode(); if (!n) return;
      e.preventDefault();
      if (n.type === 'dir') { c.expanded.has(n.path) ? c.expanded.delete(n.path) : c.expanded.add(n.path); renderTree(); }
      else openFile(n.path);
    }
  };

  const applyEditorResult = (result) => {
    if (!result || !result.handled) return false;
    const original = editor.value;
    const next = String(result.value || '');
    let prefix = 0;
    const prefixLimit = Math.min(original.length, next.length);
    while (prefix < prefixLimit && original[prefix] === next[prefix]) prefix += 1;
    let oldSuffix = original.length;
    let newSuffix = next.length;
    while (oldSuffix > prefix && newSuffix > prefix && original[oldSuffix - 1] === next[newSuffix - 1]) {
      oldSuffix -= 1;
      newSuffix -= 1;
    }
    if (original !== next) editor.setRangeText(next.slice(prefix, newSuffix), prefix, oldSuffix, 'preserve');
    editor.setSelectionRange(result.start, result.end, editor.selectionDirection || 'none');
    dismissedCompletionSignature = '';
    clearInlineCompletion();
    updateDirty();
    editor.focus();
    return true;
  };

  const cycleCodeTab = (delta) => {
    if (c.tabs.length < 2) return;
    const index = Math.max(0, c.tabs.findIndex(tab => tab.key === c.activeTab));
    activateTab(c.tabs[(index + delta + c.tabs.length) % c.tabs.length]);
  };

  filterIn.oninput = () => { c.filter = filterIn.value; renderTree(); };
  pathIn.oninput = () => { updateDirty(); revealPath(pathIn.value.trim()); renderTree(); };
  editor.oninput = () => {
    dismissedCompletionSignature = '';
    clearInlineCompletion();
    updateDirty();
  };
  editor.onselect = () => { clearInlineCompletion(); scheduleSelectionChrome(); scheduleInlineCompletion(); };
  editor.onclick = () => { clearInlineCompletion(); scheduleSelectionChrome(); scheduleInlineCompletion(); };
  editor.onkeyup = () => { clearInlineCompletion(); scheduleSelectionChrome(); scheduleInlineCompletion(); };
  editor.onmousemove = scheduleLineHoverHelp;
  editor.onmouseleave = hideLineHoverHelp;
  editor.onscroll = syncHighlightScroll;
  editor.addEventListener('omicsang:editor-metrics', () => requestAnimationFrame(() => {
    if (!view.isConnected) return;
    updateLineNumbers();
    scheduleSelectionChrome();
    scheduleInlineCompletion();
  }));
  editor.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.code === 'Space' || e.key === ' ')) {
      e.preventDefault();
      dismissedCompletionSignature = '';
      scheduleInlineCompletion(true);
      return;
    }
    if (e.altKey && e.key === 'F1' && !e.ctrlKey && !e.metaKey && !e.isComposing) {
      e.preventDefault();
      const position = EditorCore.cursorPositionFromStarts(
        editor.value, editor.selectionStart || 0, editorLineStarts, c.indentSize);
      showLineHoverHelp(position.lineIndex);
      return;
    }
    if (e.key === 'F1' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing) {
      e.preventDefault();
      c.packageHelpOpen = true;
      refreshCommandContext(true);
      loadPackageHelp(false);
      packageHelpPanel.scrollIntoView({ block: 'nearest' });
      return;
    }
    if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing) {
      if (completionResult) {
        e.preventDefault();
        clearInlineCompletion(true);
        setStatus('inline completion dismissed');
        e.stopPropagation();
        return;
      }
      if (!lineHoverHelp.classList.contains('hidden')) {
        e.preventDefault();
        hideLineHoverHelp();
        e.stopPropagation();
        return;
      }
      tabMovesFocus = true;
      setStatus('Press Tab or Shift+Tab to move focus out of the editor');
      e.stopPropagation();
      return;
    }
    if (e.key === 'Tab' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing && tabMovesFocus) {
      tabMovesFocus = false;
      return;
    }
    if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
    tabMovesFocus = false;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      save();
    } else if ((e.ctrlKey || e.metaKey) && e.key === '/') {
      const result = EditorCore.toggleLineComment(
        editor.value, editor.selectionStart, editor.selectionEnd,
        { language: c.language, path: c.selected || c.loadedPath },
      );
      if (result.handled) { e.preventDefault(); applyEditorResult(result); }
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'w') {
      const tab = activeTab();
      if (tab) { e.preventDefault(); closeTab(tab); }
    } else if (e.ctrlKey && (e.key === 'PageUp' || e.key === 'PageDown')) {
      e.preventDefault();
      e.stopPropagation();
      cycleCodeTab(e.key === 'PageDown' ? 1 : -1);
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'g') {
      e.preventDefault();
      const target = window.prompt('Go to line:', String(EditorCore.cursorPosition(editor.value, editor.selectionStart, c.indentSize).line));
      if (target) jumpToLine(target);
    } else if ((e.ctrlKey || e.metaKey) && (e.key === ']' || e.key === '[')) {
      e.preventDefault();
      applyEditorResult(EditorCore.applyIndentEdit(
        editor.value, editor.selectionStart, editor.selectionEnd,
        { mode: c.indentMode, size: c.indentSize, outdent: e.key === '[' },
      ));
    } else if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey &&
        !e.isComposing && completionResult && acceptInlineCompletion()) {
      e.preventDefault();
    } else if (e.key === 'Tab' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing) {
      e.preventDefault();
      applyEditorResult(EditorCore.applyIndentEdit(
        editor.value, editor.selectionStart, editor.selectionEnd,
        { mode: c.indentMode, size: c.indentSize, outdent: e.shiftKey },
      ));
    } else if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing) {
      e.preventDefault();
      applyEditorResult(EditorCore.applyEnterEdit(
        editor.value, editor.selectionStart, editor.selectionEnd,
        { mode: c.indentMode, size: c.indentSize, language: c.language, path: c.selected || c.loadedPath },
      ));
    } else if (e.key === 'Home' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing &&
        editor.selectionStart === editor.selectionEnd) {
      const caret = editor.selectionStart;
      const lineStart = caret === 0 ? 0 : editor.value.lastIndexOf('\n', caret - 1) + 1;
      const rest = editor.value.slice(lineStart);
      const indent = (rest.match(/^[ \t]*/) || [''])[0].length;
      const firstCode = lineStart + indent;
      const target = caret <= firstCode ? lineStart : firstCode;
      e.preventDefault();
      if (e.shiftKey) editor.setSelectionRange(
        Math.min(caret, target), Math.max(caret, target), target < caret ? 'backward' : 'forward');
      else editor.setSelectionRange(target, target);
      scheduleSelectionChrome();
    }
  };
  indentSelect.onchange = () => {
    const [mode, size] = indentSelect.value.split(':');
    applyIndentState(mode, size);
    syncCurrentTab();
    scheduleSelectionChrome();
  };
  guidesBtn.onclick = () => {
    setUiPref('codeIndentGuides', !state.uiPrefs.codeIndentGuides);
    scheduleSelectionChrome();
    editor.focus();
  };
  completionBtn.onclick = () => {
    setUiPref('codeInlineCompletions', !state.uiPrefs.codeInlineCompletions);
    dismissedCompletionSignature = '';
    if (state.uiPrefs.codeInlineCompletions) scheduleInlineCompletion(true);
    else clearInlineCompletion();
    syncEditorAssistControls();
    editor.focus();
  };
  hoverHelpBtn.onclick = () => {
    setUiPref('codeHoverHelp', !state.uiPrefs.codeHoverHelp);
    if (!state.uiPrefs.codeHoverHelp) hideLineHoverHelp();
    syncEditorAssistControls();
    editor.focus();
  };
  assistantNotes.oninput = () => {
    saveActiveNote();
  };
  saveBtn.onclick = () => save(false);
  reloadBtn.onclick = () => c.loadedPath ? openFile(c.loadedPath, true, true) : loadFiles(true);
  refreshBtn.onclick = () => loadFiles(true);
  collapseBtn.onclick = () => { c.expanded = new Set(['']); renderTree(); };
  revealBtn.onclick = () => { if (c.selected || c.loadedPath) { revealPath(c.selected || c.loadedPath); renderTree(); fileList.focus(); } };
  newBtn.onclick = () => newFile();
  mkdirBtn.onclick = newFolder;
  renameBtn.onclick = renameFile;
  duplicateBtn.onclick = duplicateFile;
  deleteBtn.onclick = deleteFile;
  diagnosticsBtn.onclick = runDiagnostics;
  packageHelpBtn.onclick = () => {
    c.packageHelpOpen = !c.packageHelpOpen;
    renderPackageHelp();
    if (c.packageHelpOpen) {
      refreshCommandContext(true);
      loadPackageHelp(false);
    }
  };

  const assistantPanel = el('aside', {
    id: codeAssistantId, class: 'code-assistant', 'aria-label': 'Code Assistant',
  },
    el('div', { class: 'assistant-head' },
      el('div', {}, el('h3', {}, 'Assistant'), assistantPath),
      el('div', { class: 'assistant-head-actions' }, assistantTool, hideAssistantBtn)),
    assistantPreview,
    agentDisclosure('Code Assistant confirmations identify the file content, selection, diagnostics, notes, and prompt prepared for the selected tool.'),
    el('label', {}, 'Notes', assistantNotes),
    el('div', { class: 'toolbar assistant-actions' },
      el('button', { class: 'sm', onclick: () => launchAssistant('review') }, 'Review'),
      el('button', { class: 'ghost sm', onclick: () => launchAssistant('explain') }, 'Explain'),
      el('button', { class: 'ghost sm', onclick: () => launchAssistant('tests') }, 'Tests'),
      el('button', { class: 'ghost sm', onclick: () => launchAssistant('notes') }, 'Notes'),
      el('button', { class: 'ghost sm', onclick: copyAssistantPrompt }, 'Copy prompt'),
      el('button', { class: 'ghost sm danger-outline', onclick: clearSavedNotes }, 'Clear saved notes')),
    el('div', { class: 'assistant-foot' }, assistantStatus));

  view.append(
    el('div', { class: 'code-toolbar' },
      filterLabel,
      fileTreeToggleBtn, assistantToggleBtn,
      newBtn, mkdirBtn, revealBtn, collapseBtn, refreshBtn, diagnosticsBtn, status),
    el('div', { class: 'code-workbench' },
      el('aside', { id: codeSidebarId, class: 'code-sidebar', 'aria-label': 'Project file tree' },
        el('div', { class: 'code-panel-head' }, el('strong', {}, 'Files'), hideFileTreeBtn),
        fileList),
      el('div', {
        class: 'resize-handle code-resizer',
        title: 'Resize file tree',
        onpointerdown: (e) => startPreferenceResize(e, 'codeSidebarWidth', 220, 560),
      }),
      el('section', { class: 'code-main' },
        tabStrip,
        el('div', { class: 'code-pathbar' }, pathLabel,
          packageHelpBtn, renameBtn, duplicateBtn, deleteBtn,
          reloadBtn,
          saveBtn),
        meta,
        packageHelpPanel,
        saveConflictPanel,
        diagnosticsPanel,
        editorShell),
      el('div', {
        class: 'resize-handle assistant-resizer',
        title: 'Resize assistant',
        onpointerdown: (e) => startPreferenceResize(e, 'codeAssistantWidth', 260, 620, 'x', true),
      }),
      assistantPanel));
  applyPanelVisibility(state.uiPrefs);
  applyIndentState();
  syncEditorAssistControls();
  restoreEditorView(activeTab());
  updateDirty(true);
  refreshAssistant();
  renderPackageHelp();
  renderDiagnostics();
  loadFiles();
}

/* ---------------- resources ---------------- */
function renderSlurmJobs(host, jobs, scopedPipeline = '') {
  host.innerHTML = '';
  const filtered = scopedPipeline ? (jobs || []).filter(j => !j.pipeline || j.pipeline === scopedPipeline) : (jobs || []);
  if (!filtered.length) {
    host.append(el('div', { class: 'muted' }, 'No Slurm jobs known to OmicsANG.'));
    return;
  }
  const tbl = el('table', { class: 'qc-table resource-table' });
  tbl.append(el('thead', {}, el('tr', {}, ...['Job', 'Pipeline', 'State', 'Resources', 'Files', 'Actions'].map(h => el('th', {}, h)))));
  const tb = el('tbody');
  const canCancel = slurmCapabilities(state.slurm).cancel;
  filtered.slice(0, 40).forEach((j) => {
    const tr = el('tr');
    tr.append(
      el('td', { class: 'samp' }, j.job_id || j.id || '—', el('div', { class: 'muted' }, j.name || j.title || '')),
      el('td', {}, j.pipeline || '—'),
      el('td', {}, j.state || j.status || 'submitted'),
      el('td', {}, `${j.cpus || j.cores || '—'} cpu · ${j.mem || 'mem —'} · ${j.partition || 'partition —'}`),
      el('td', {}, j.output ? el('span', { class: 'mono' }, j.output.split('/').slice(-1)[0]) : '—'));
    const acts = el('td', {});
    const terminal = /^(completed|succeeded|failed|cancelled|timeout|blocked)$/i.test(j.state || j.status || '');
    const unresolved = /^(submission[-_]unknown|cancel_requested)$/i.test(j.state || j.status || '');
    if (j.job_id && j.benchtop_owned && !terminal && canCancel) acts.append(el('button', { class: 'ghost sm danger-outline', onclick: async (e) => {
      if (!window.confirm(`Cancel Slurm job ${j.job_id}?`)) return;
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = 'Canceling…';
      try {
        const path = j.durable_job_id ? `/api/jobs/${j.durable_job_id}/cancel` : `/api/slurm/jobs/${j.job_id}/cancel`;
        await api.post(path, {}); await refreshSlurm(true);
      }
      catch (e) { window.alert('Cancel failed: ' + e.message); }
    } }, 'Cancel'));
    else if (j.job_id && j.benchtop_owned && !terminal && !canCancel) {
      acts.append(el('span', { class: 'muted', title: 'Cancellation requires both squeue and scancel' }, 'cancel unavailable'));
    }
    else if (j.durable_job_id && unresolved) {
      acts.append(
        el('button', { class: 'ghost sm', onclick: async () => {
          try {
            await api.post(`/api/jobs/${j.durable_job_id}/resolve`, { action: 'reconcile_now' });
            await refreshSlurm(true);
          } catch (e) { window.alert('Reconcile failed: ' + e.message); }
        } }, 'Reconcile'),
        el('button', { class: 'ghost sm danger-outline', onclick: async () => {
          const reason = window.prompt('Only after checking scheduler evidence: why was this job not submitted?');
          if (!reason || !reason.trim()) return;
          if (!window.confirm('Mark this unresolved intent as not submitted and allow a new intent?')) return;
          try {
            await api.post(`/api/jobs/${j.durable_job_id}/resolve`, {
              action: 'mark_not_submitted', reason: reason.trim(),
            });
            await refreshSlurm(true);
          } catch (e) { window.alert('Resolve failed: ' + e.message); }
        } }, 'Mark not submitted'),
      );
    }
    else if (j.job_id && !j.benchtop_owned) acts.append(el('span', { class: 'muted' }, 'external · read only'));
    tr.append(acts);
    tb.append(tr);
  });
  tbl.append(tb);
  host.append(el('div', { class: 'qc-scroll' }, tbl));
}

function viewResources(view) {
  const d = state.detail;
  if (!state.slurm) refreshSlurm(true);
  const slurm = state.slurm || { available: false, tools: {}, partitions: [], jobs: [] };
  const capabilities = slurmCapabilities(slurm);
  const capabilityLabel = slurm.error
    ? '○ status unavailable'
    : capabilities.submit
      ? (capabilities.monitor ? '● sbatch + squeue' : '● sbatch only')
      : (capabilities.monitor ? '○ squeue only' : '○ unavailable');
  const jobsHost = el('div', {});
  view.append(
    el('h2', {}, `Resources · ${d.name}`),
    runQueuePanel(),
    el('div', { class: 'card' },
      el('div', { class: 'toolbar' },
        el('strong', {}, 'Slurm'),
        el('span', { class: capabilities.submit ? 'badge on' : 'badge off' }, capabilityLabel),
        el('span', { class: 'muted' }, Object.entries(slurm.tools || {}).map(([k, v]) => `${k}:${v ? 'yes' : 'no'}`).join(' · ')),
        el('button', { class: 'ghost sm', onclick: () => refreshSlurm(true) }, 'Refresh')),
      (slurm.partitions || []).length ? el('div', { class: 'resource-grid' },
        ...slurm.partitions.map(p => el('div', { class: 'resource-tile' },
          el('strong', {}, p.partition || 'partition'),
          el('span', {}, p.available || '—'),
          el('span', { class: 'muted' }, `${p.nodes || '?'} nodes · ${p.cpus || 'cpu ?'} · ${p.memory || 'mem ?'}`),
          el('span', { class: 'muted' }, p.gres || 'gres —')))) :
        el('div', { class: 'muted' }, 'No Slurm partition data available.')),
    el('h3', {}, 'Slurm jobs'),
    jobsHost);
  renderSlurmJobs(jobsHost, slurm.jobs || [], d.name);
}

/* ---------------- GitHub repo management ---------------- */
async function viewGithub(view) {
  const d = state.detail;
  view.append(el('div', { class: 'view-head' },
    el('div', {}, el('h2', {}, `GitHub · ${d.name}`),
      el('div', { class: 'muted' }, 'Inspect local Git state. Outbound GitHub and remote mutations are disabled in this release.')),
    el('button', { class: 'ghost sm', onclick: () => render() }, 'Refresh')));
  const slot = el('div', {}, el('div', { class: 'muted' }, 'loading GitHub state...'));
  view.append(slot);
  try {
    const data = await api.get(`/api/pipelines/${d.name}/github`);
    state.github[d.name] = data;
    if (state.detail && state.detail.name === d.name && state.tab === 'github') renderGithubContent(slot, d, data);
  } catch (e) {
    slot.innerHTML = '';
    slot.append(el('div', { class: 'diag-empty err' }, e.message));
  }
}

function renderGithubContent(slot, d, gh, initialOutput = '', initialOk = true) {
  const git = gh.git || {};
  const repo = gh.repo || {};
  const output = el('pre', { class: 'github-output hidden' });
  const setOutput = (msg, ok = true) => {
    output.classList.remove('hidden');
    output.classList.toggle('err', !ok);
    output.textContent = msg || (ok ? 'ok' : 'failed');
  };
  const runAction = async (label, endpoint, body = {}, confirmText = '') => {
    if (confirmText && !window.confirm(confirmText)) return;
    setOutput(`${label}...`);
    try {
      const res = await api.post(`/api/pipelines/${d.name}/github/${endpoint}`, body);
      const text = res.output || JSON.stringify(res, null, 2);
      setOutput(text, !!res.ok);
      if (res.ok) {
        try {
          const fresh = await api.get(`/api/pipelines/${d.name}/github`);
          state.github[d.name] = fresh;
          if (state.detail && state.detail.name === d.name && state.tab === 'github') {
            renderGithubContent(slot, d, fresh, text, true);
          }
        } catch (refreshErr) {
          setOutput(`${text}\n\nRefresh failed: ${refreshErr.message}`, false);
        }
      }
    } catch (e) {
      setOutput(e.message, false);
    }
  };

  const branch = git.branch || '';
  const repoUrl = repo.url || (gh.github_slug ? `https://github.com/${gh.github_slug}` : '');
  const safeRepoUrl = httpsUrl(repoUrl);

  slot.innerHTML = '';
  slot.append(
    el('div', { class: 'github-grid' },
      el('section', { class: 'panel' },
        el('h3', {}, 'Connection'),
        el('div', { class: 'kv compact' },
          el('div', { class: 'k' }, 'Local repo'), el('div', {}, git.is_repo ? 'yes' : 'no'),
          el('div', { class: 'k' }, 'Branch'), el('div', {}, branch || '—'),
          el('div', { class: 'k' }, 'Remote'), el('div', { class: 'mono breakable' }, git.remote || '—'),
          el('div', { class: 'k' }, 'Upstream'), el('div', {}, git.upstream || '—'),
          el('div', { class: 'k' }, 'Ahead/behind'), el('div', {}, `${git.ahead || 0} ahead · ${git.behind || 0} behind`),
          el('div', { class: 'k' }, 'Dirty files'), el('div', {}, String(git.dirty || 0)))),
      el('section', { class: 'panel' },
        el('h3', {}, 'GitHub'),
        el('div', { class: 'kv compact' },
          el('div', { class: 'k' }, 'gh CLI'), el('div', {}, gh.gh_available ? 'available' : 'missing'),
          el('div', { class: 'k' }, 'Network'), el('div', {}, gh.outbound_disabled ? 'disabled' : 'available'),
          el('div', { class: 'k' }, 'Repo'), el('div', {}, gh.github_slug || '—'),
          el('div', { class: 'k' }, 'Visibility'), el('div', {}, repo.nameWithOwner ? (repo.isPrivate ? 'private' : 'public') : '—'),
          el('div', { class: 'k' }, 'Default'), el('div', {}, repo.defaultBranchRef ? repo.defaultBranchRef.name : '—'),
          el('div', { class: 'k' }, 'Permission'), el('div', {}, repo.viewerPermission || '—')))),
    el('section', { class: 'panel' },
      el('h3', {}, 'Repository actions'),
      el('div', { class: 'toolbar github-actions' },
        el('button', { class: 'ghost sm', disabled: '' }, 'Fetch disabled'),
        el('button', { class: 'ghost sm', disabled: '' }, 'Pull disabled'),
        el('button', { class: 'ghost sm', disabled: '' }, 'Push disabled'),
        safeRepoUrl ? el('button', { class: 'ghost sm', onclick: () => openExternalHttps(safeRepoUrl) }, 'Open GitHub') : null),
      output),
    gh.outbound_disabled ? el('section', { class: 'panel' },
      el('h3', {}, 'Outbound actions unavailable'),
      el('p', { class: 'muted' }, 'Connecting remotes, fetching, pulling, pushing, creating repositories, and opening pull requests are intentionally unavailable. Use a reviewed command-line Git workflow outside OmicsANG when needed.')) : githubRepoCreatePanel(d, gh, runAction),
    gh.outbound_disabled ? null : githubPrPanel(d, gh, runAction),
    githubBranchPanel(gh),
    githubDirtyPanel(git),
    githubPrListPanel(gh));
  if (initialOutput) setOutput(initialOutput, initialOk);
}

function githubRepoCreatePanel(d, gh, runAction) {
  const git = gh.git || {};
  const defaultName = gh.github_slug || (gh.gh_user ? `${gh.gh_user}/${d.name}` : '');
  const nameIn = el('input', { type: 'text', value: defaultName, placeholder: 'owner/repository' });
  const descIn = el('input', { type: 'text', value: '', placeholder: 'description' });
  const privateIn = el('input', { type: 'checkbox' }); privateIn.checked = true;
  const pushIn = el('input', { type: 'checkbox' });
  const createDisabled = gh.gh_available && gh.gh_authenticated ? null : '';
  const connectDisabled = git.is_repo ? null : '';
  return el('section', { class: 'panel' },
    el('h3', {}, 'Create or connect GitHub repo'),
    el('div', { class: 'grid2' },
      el('div', {}, el('label', {}, 'Repository'), nameIn),
      el('div', {}, el('label', {}, 'Description'), descIn)),
    el('div', { class: 'switch-row' },
      el('label', { class: 'inline switch' }, privateIn, ' private'),
      el('label', { class: 'inline switch' }, pushIn, ' push current branch after create')),
    el('div', { class: 'toolbar', style: 'margin-top:10px' },
      el('button', {
        class: 'ghost sm',
        disabled: connectDisabled,
        onclick: () => runAction('Setting origin', 'connect', {
          full_name: nameIn.value,
        }, `Set origin to https://github.com/${nameIn.value}.git?`),
      }, gh.github_slug ? 'Update origin' : 'Set origin'),
      el('button', {
        class: 'ghost sm',
        disabled: createDisabled,
        onclick: () => runAction('Creating GitHub repo', 'repo', {
          full_name: nameIn.value,
          private: privateIn.checked,
          description: descIn.value,
          push_initial: pushIn.checked,
        }, `Create GitHub repository "${nameIn.value}" and set origin?`),
      }, 'Create repo')));
}

function githubPrPanel(d, gh, runAction) {
  const git = gh.git || {};
  const titleIn = el('input', { type: 'text', value: `${d.name}: update pipeline`, placeholder: 'PR title' });
  const bodyIn = el('textarea', { class: 'github-pr-body', spellcheck: 'false' }, '');
  const draftIn = el('input', { type: 'checkbox' }); draftIn.checked = true;
  const disabled = gh.gh_available && gh.gh_authenticated && gh.github_slug && git.branch ? null : '';
  return el('section', { class: 'panel' },
    el('h3', {}, 'Open pull request'),
    el('div', {}, el('label', {}, 'Title'), titleIn),
    el('div', {}, el('label', {}, 'Body'), bodyIn),
    el('div', { class: 'toolbar', style: 'margin-top:10px' },
      el('label', { class: 'inline switch' }, draftIn, ' draft'),
      el('button', {
        class: 'sm',
        disabled,
        onclick: () => runAction('Creating PR', 'pr', {
          title: titleIn.value,
          body: bodyIn.value,
          draft: draftIn.checked,
        }, `Create a pull request from branch "${git.branch || ''}"?`),
      }, 'Create PR')));
}

function githubBranchPanel(gh) {
  const branches = gh.branches || { local: [], remote: [] };
  return el('section', { class: 'panel' },
    el('h3', {}, 'Branches'),
    el('div', { class: 'github-branches' },
      branchList('Local', branches.local),
      branchList('Remote', branches.remote)));
}

function branchList(title, rows) {
  const box = el('div', { class: 'branch-list' }, el('strong', {}, title));
  if (!rows.length) box.append(el('div', { class: 'empty slim' }, 'None'));
  rows.slice(0, 40).forEach(b => box.append(el('div', { class: 'branch-row mono' }, b)));
  return box;
}

function githubDirtyPanel(git) {
  const rows = git.dirty_files || [];
  const ul = el('ul', { class: 'filelist' });
  if (!rows.length) ul.append(el('li', { class: 'muted' }, 'Working tree clean.'));
  rows.forEach(r => ul.append(el('li', {}, el('span', { class: 'mono' }, r))));
  return el('section', { class: 'panel' }, el('h3', {}, 'Working tree'), ul);
}

function githubPrListPanel(gh) {
  const rows = gh.prs || [];
  const tbl = el('table', { class: 'qc-table github-pr-table' });
  tbl.append(el('thead', {}, el('tr', {}, ...['PR', 'State', 'Branch', 'Updated', ''].map(h => el('th', {}, h)))));
  const tb = el('tbody');
  rows.forEach(pr => {
    const prUrl = httpsUrl(pr.url);
    tb.append(el('tr', {},
    el('td', { class: 'samp' }, `#${pr.number} ${pr.title || ''}`),
    el('td', {}, pr.isDraft ? 'draft' : (pr.state || 'open')),
    el('td', {}, `${pr.headRefName || '—'} → ${pr.baseRefName || '—'}`),
    el('td', {}, pr.updatedAt || '—'),
    el('td', {}, prUrl ? el('button', { class: 'ghost sm', onclick: () => openExternalHttps(prUrl) }, 'Open') : '')));
  });
  tbl.append(tb);
  return el('section', { class: 'panel' },
    el('h3', {}, 'Pull requests'),
    rows.length ? el('div', { class: 'qc-scroll' }, tbl) : el('div', { class: 'empty slim' }, gh.github_slug ? 'No open pull requests.' : 'Connect a GitHub repo to list pull requests.'));
}

/* ---------------- agents ---------------- */
function viewAgents(view) {
  const d = state.detail, g = d.git || {};
  view.append(el('h2', {}, `Agents · ${d.name}`),
    el('div', { class: 'muted' }, `Launch Claude Code / Codex in this repo${g.branch ? ` (branch ${g.branch})` : ''}.`),
    agentDisclosure('A separate Git worktree can keep agent changes off the current branch, but it does not restrict the agent\'s OS permissions.'));

  const cwdNote = el('div', {});
  const launch = (tool, worktreeBranch = '') => async () => {
    const context = worktreeBranch ? `The agent will run in the worktree for branch "${worktreeBranch}".` : 'The agent will run in the repository root.';
    if (!confirmAgentLaunch(tool, context)) return;
    try {
      const { session } = await api.post('/api/agents', {
        pipeline: d.name,
        tool,
        worktree_branch: worktreeBranch,
        acknowledge_external_agent: true,
      });
      openTerminal(session);
    } catch (e) {
      cwdNote.textContent = `Launch failed: ${e.message}`;
    }
  };
  view.append(el('div', { class: 'card' },
    el('h3', {}, 'Repo root'),
    el('div', { class: 'toolbar' },
      el('button', { onclick: launch('claude') }, '✦ Claude Code'),
      el('button', { class: 'ghost', onclick: launch('codex') }, '✧ Codex'),
      el('button', { class: 'ghost', onclick: launch('shell') }, '❯ Shell')), cwdNote));

  // worktrees
  const wtCard = el('div', { class: 'card' }, el('h3', {}, 'Separate Git worktrees'),
    el('div', { class: 'muted' }, 'Worktrees separate branches and file changes; they are not OS security sandboxes.'));
  const branchIn = el('input', { type: 'text', placeholder: 'branch name e.g. fix/de-thresholds', style: 'max-width:340px' });
  const wtStatus = el('span', { class: 'muted' });
  const list = el('ul', { class: 'filelist' });
  const refreshWt = () => {
    list.innerHTML = '';
    (d.worktrees || []).forEach(w => {
      const li = el('li', {}, el('span', {}, `${w.branch || '(detached)'} `),
        el('span', { class: 'tag' }, w.path.split('/').slice(-2).join('/')));
      li.append(el('button', { class: 'sm', style: 'margin-left:auto',
        disabled: w.branch ? null : '', onclick: launch('claude', w.branch || '') }, 'Claude here'));
      list.append(li);
    });
    if (!(d.worktrees || []).length) list.append(el('li', { class: 'muted' }, 'No worktrees.'));
  };
  const create = async () => {
    const requestedBranch = branchIn.value.trim();
    if (!requestedBranch) return;
    if (!confirmAgentLaunch('claude', `A worktree for branch "${requestedBranch}" will be created and the agent will run inside it.`)) return;
    wtStatus.textContent = 'creating…';
    try {
      const res = await api.post('/api/worktrees', { pipeline: d.name, branch: requestedBranch });
      wtStatus.textContent = res.ok ? 'created ✓' : 'error';
      state.detail = await api.get(`/api/pipelines/${d.name}`); d.worktrees = state.detail.worktrees; refreshWt();
      if (res.ok) {
        const { session } = await api.post('/api/agents', {
          pipeline: d.name,
          tool: 'claude',
          worktree_branch: res.branch || requestedBranch,
          acknowledge_external_agent: true,
        });
        openTerminal(session);
      }
    } catch (e) { wtStatus.textContent = e.message; }
  };
  wtCard.append(el('div', { class: 'toolbar' }, branchIn,
    el('button', { class: 'sm', onclick: create }, 'Create + Claude'), wtStatus), list);
  view.append(wtCard);
  refreshWt();
}

/* ---------------- fleet (cross-pipeline orchestration) ---------------- */
async function loadHealth(host) {
  host.innerHTML = '';
  host.append(el('div', { class: 'muted' }, 'loading pipeline health...'));
  try {
    state.health = await api.get('/api/health');
  } catch (e) {
    host.innerHTML = '';
    host.append(el('div', { class: 'diag-empty err' }, e.message));
    return;
  }
  host.innerHTML = '';
  const rows = state.health.pipelines || [];
  const tbl = el('table', { class: 'qc-table health-table' });
  tbl.append(el('thead', {}, el('tr', {}, ...['Pipeline', 'State', 'Git', 'Runs', 'Diagnostics', 'Env', 'Results'].map(h => el('th', {}, h)))));
  const tb = el('tbody');
  rows.forEach((p) => {
    const q = p.run_state || {};
    const d = p.diagnostics || {};
    const last = p.last_run || {};
    const fresh = p.result_freshness || {};
    const tr = el('tr', { onclick: () => selectPipeline(p.name) });
    tr.append(
      el('td', { class: 'samp' }, p.name, el('div', { class: 'muted' }, p.kind)),
      el('td', {}, el('span', { class: `health-pill ${p.status}` }, p.status)),
      el('td', {}, p.git && p.git.is_repo ? `${p.git.branch || 'repo'}${p.git.dirty ? ' · dirty' : ''}` : '—'),
      el('td', {}, `${(q.running || []).length} running · ${(q.queued || []).length} queued`, el('div', { class: 'muted' }, last.status ? `last ${last.status}` : 'no history')),
      el('td', {}, d.error == null ? '—' : `${d.error || 0} err · ${d.warning || 0} warn`),
      el('td', {}, p.env_resolved || '—'),
      el('td', {}, fresh.latest ? fmtTime(fresh.latest) : '—'));
    tb.append(tr);
  });
  tbl.append(tb);
  const slurm = state.health.slurm || {};
  host.append(el('div', { class: 'card health-card' },
    el('div', { class: 'toolbar' },
      el('strong', {}, 'Pipeline health'),
      el('span', { class: 'muted' }, `${rows.length} pipelines · Slurm ${slurm.available ? 'available' : 'unavailable'}`),
      el('button', { class: 'ghost sm', onclick: () => loadHealth(host) }, 'Refresh')),
    el('div', { class: 'qc-scroll' }, tbl)));
}

function viewFleet(view) {
  if (state.fleetMode === 'health') {
    view.append(el('div', { class: 'view-head' },
      el('div', {}, el('h2', {}, 'Fleet Health'), el('div', { class: 'muted' }, 'Scan all pipelines for git state, diagnostics, run queue, environments, and result freshness.')),
      el('button', { onclick: () => { state.fleetMode = 'task'; render(); } }, 'Launch fleet task')));
    const healthHost = el('div', {});
    const board = el('div', {});
    view.append(healthHost, board);
    loadHealth(healthHost);
    loadFleet(board);
    return;
  }

  view.append(el('div', { class: 'view-head' },
    el('div', {}, el('h2', {}, 'Fleet Task'), el('div', { class: 'muted' }, 'Run one Claude/Codex task across selected pipelines using separate Git worktrees.')),
    el('button', { class: 'ghost sm', onclick: () => { state.fleetMode = 'health'; render(); } }, 'Health')));

  const f = state.fleetForm || (state.fleetForm = {
    prompt: '', tool: 'claude', use_worktree: true, branch: '', selected: new Set(),
  });

  const promptIn = el('textarea', { class: 'fleet-prompt', spellcheck: 'false',
    placeholder: 'e.g. "Add a CITATION.cff and a reproducibility section to the README; match the existing style."' });
  promptIn.value = f.prompt;
  const toolSel = el('select', {}, ...['claude', 'codex'].map(t =>
    el('option', { value: t, selected: t === f.tool ? '' : null }, t === 'claude' ? 'Claude Code' : 'Codex')));
  const wtIn = el('input', { type: 'checkbox' }); wtIn.checked = f.use_worktree;
  const branchIn = el('input', { type: 'text', value: f.branch, placeholder: 'branch (blank = auto-generated)', style: 'max-width:260px' });
  const count = el('span', { class: 'muted' }, `${f.selected.size} selected`);
  const status = el('span', { class: 'muted' });

  const grid = el('div', { class: 'fleet-picks' });
  state.pipelines.forEach(p => {
    const cb = el('input', { type: 'checkbox' }); cb.checked = f.selected.has(p.name);
    cb.onchange = () => { cb.checked ? f.selected.add(p.name) : f.selected.delete(p.name); count.textContent = `${f.selected.size} selected`; };
    grid.append(el('label', { class: 'fleet-pick' }, cb, ` ${p.name} `, el('span', { class: 'pl-kind' }, p.kind)));
  });

  const launch = async () => {
    f.prompt = promptIn.value; f.tool = toolSel.value; f.use_worktree = wtIn.checked; f.branch = branchIn.value;
    if (!f.prompt.trim()) { status.textContent = 'enter a prompt'; return; }
    if (!f.selected.size) { status.textContent = 'select at least one pipeline'; return; }
    if (!confirmAgentLaunch(
      f.tool,
      `The full task prompt will be sent to ${f.selected.size} agent${f.selected.size === 1 ? '' : 's'} across the selected repositories:\n\n${truncateText(f.prompt, 3000)}`,
    )) {
      status.textContent = 'launch canceled';
      return;
    }
    status.textContent = `launching ${f.selected.size} agents…`;
    try {
      const job = await api.post('/api/fleet', { prompt: f.prompt, tool: f.tool,
        pipelines: [...f.selected], use_worktree: f.use_worktree, branch: f.branch,
        acknowledge_external_agent: true });
      state.fleetActive = job.id; render();
    } catch (e) { status.textContent = 'error: ' + e.message; }
  };

  view.append(el('div', { class: 'card' },
    el('label', {}, 'Task prompt (sent to every selected pipeline)'), promptIn,
    agentDisclosure('Fleet confirmation lists the selected repositories and task prompt. Separate Git worktrees reduce branch collisions but do not restrict OS permissions.'),
    el('div', { class: 'toolbar', style: 'margin-top:10px' },
      el('label', { class: 'inline' }, 'Agent ', toolSel),
      el('label', { class: 'inline' }, wtIn, ' use separate Git worktrees'), branchIn),
    el('label', { style: 'margin-top:8px' }, 'Pipelines ', count), grid,
    el('div', { class: 'toolbar', style: 'margin-top:12px' },
      el('button', { onclick: launch }, '✦ Launch fleet'),
      el('button', { class: 'ghost sm', onclick: () => { state.pipelines.forEach(p => f.selected.add(p.name)); render(); } }, 'Select all'),
      el('button', { class: 'ghost sm', onclick: () => { f.selected.clear(); render(); } }, 'Clear'), status)));

  const board = el('div', {});
  view.append(board);
  loadFleet(board);
}

async function loadFleet(board) {
  board.innerHTML = '';
  let jobs = [];
  try { jobs = await api.get('/api/fleet'); } catch (e) { return; }
  if (!jobs.length) { board.append(el('div', { class: 'muted', style: 'margin-top:12px' }, 'No fleet jobs yet — launch one above.')); return; }
  if (!state.fleetActive || !jobs.find(j => j.id === state.fleetActive)) state.fleetActive = jobs[0].id;
  const sel = el('select', { style: 'max-width:520px' }, ...jobs.map(j => el('option', { value: j.id, selected: j.id === state.fleetActive ? '' : null },
    `${new Date(j.created * 1000).toLocaleTimeString()} · ${j.tool} · ${j.n} pipelines · ${j.prompt.slice(0, 46)}`)));
  sel.onchange = () => { state.fleetActive = sel.value; loadFleet(board); };
  board.append(el('h3', {}, 'Fleet jobs'),
    el('div', { class: 'toolbar' }, sel, el('button', { class: 'ghost sm', onclick: () => loadFleet(board) }, '↻ Refresh')));
  const detail = el('div', {}); board.append(detail);
  await renderFleetDetail(detail);
}

async function renderFleetDetail(host) {
  host.innerHTML = '';
  if (!state.fleetActive) return;
  let job; try { job = await api.get(`/api/fleet/${state.fleetActive}`); }
  catch (e) { host.append(el('div', { class: 'empty' }, e.message)); return; }
  host.append(el('div', { class: 'cmd', style: 'margin:8px 0' }, job.prompt));
  const tbl = el('table', { class: 'qc-table fleet-table' });
  tbl.append(el('thead', {}, el('tr', {}, ...['Pipeline', 'Branch', 'Agent', 'Changes', 'Actions'].map(h => el('th', {}, h)))));
  const tb = el('tbody');
  job.targets.forEach((t, i) => {
    const ds = t.diffstat || {};
    const tr = el('tr');
    tr.append(el('td', { class: 'samp' }, t.pipeline));
    tr.append(el('td', {}, t.branch || '—'));
    tr.append(el('td', {}, el('span', { class: `dot ${t.status}` }), ' ' + t.status));
    tr.append(el('td', {}, ds.files ? `${ds.files} files${ds.shortstat ? ' · ' + ds.shortstat : ''}` : '—'));
    const acts = el('td', {});
    if (t.session_id) acts.append(el('button', { class: 'sm ghost', onclick: () =>
      focusOrOpen({ id: t.session_id, title: `${job.tool} · ${t.pipeline}`, kind: 'agent', status: t.status, meta: { pipeline: t.pipeline } }) }, 'Open'));
    if (t.can_diff) acts.append(el('button', { class: 'sm ghost', onclick: () => showFleetDiff(job.id, i, t.pipeline) }, 'Diff'));
    if (t.branch && (ds.files || 0) > 0) acts.append(el('button', { class: 'sm', onclick: () => fleetPR(job.id, i, t) }, 'Commit + PR'));
    if (t.error) acts.append(el('span', { style: 'color:var(--err);font-size:11px' }, ' ' + t.error));
    tr.append(acts); tb.append(tr);
  });
  tbl.append(tb);
  host.append(el('div', { class: 'qc-scroll' }, tbl), el('div', { class: 'fleet-diff hidden' }));
}

async function showFleetDiff(jobId, index, pipeline) {
  const box = document.querySelector('.fleet-diff');
  box.classList.remove('hidden'); box.textContent = 'loading diff…';
  try {
    const d = await api.get(`/api/fleet/${jobId}/diff/${index}`);
    box.innerHTML = ''; box.append(el('div', { class: 'qc-title' }, `diff · ${pipeline}`),
      el('pre', { class: 'diff-pre' }, d.diff || '(no changes yet)'));
  } catch (e) { box.textContent = e.message; }
}

async function fleetPR(jobId, index, t) {
  const msg = window.prompt(
    `Commit + push + open PR for "${t.pipeline}" on branch ${t.branch}.\nThis PUSHES to GitHub. Commit message:`,
    'OmicsANG: apply fleet task');
  if (msg === null) return;
  try {
    const res = await api.post(`/api/fleet/${jobId}/pr`, { index, message: msg });
    const pr = res.steps && res.steps.pr;
    window.alert(pr && pr.url ? 'PR opened:\n' + pr.url : 'Result:\n' + JSON.stringify(res.steps, null, 1).slice(0, 600));
    render();
  } catch (e) { window.alert('error: ' + e.message); }
}

/* ---------------- terminal ---------------- */
function viewTerminal(view) {
  const sid = state.focusedSession;
  if (!sid || !terminals.has(sid)) {
    view.append(el('div', { class: 'empty' }, 'No active terminal. Start a run or launch an agent.'));
    return;
  }
  const rec = terminals.get(sid);
  const s = state.sessions.find(x => x.id === sid) || rec.session;
  rec.session = s;
  const status = s.status || (rec.exited ? 'exited' : 'running');
  const live = ['created', 'queued', 'running'].includes(status);
  const terminalStageId = `terminal-stage-${String(sid).replace(/[^A-Za-z0-9_-]/g, '-')}`;
  const terminalToggleBtn = el('button', {
    class: 'ghost sm', type: 'button', 'aria-controls': terminalStageId,
    'data-panel-pref': 'terminalOpen', 'data-panel-label': 'terminal', 'data-dynamic-label': '',
    onclick: () => setUiPref('terminalOpen', !state.uiPrefs.terminalOpen),
  }, 'Hide terminal');
  const bar = el('div', { class: 'term-toolbar' },
    el('span', { class: `dot ${status}` }),
    el('strong', {}, s.title), el('span', { class: 'tag' }, s.kind),
    el('span', { class: 'tag' }, status),
    terminalToggleBtn,
    el('button', { class: 'ghost sm', style: 'margin-left:auto',
      onclick: () => fitTerminal(sid) }, 'Fit'),
    el('button', { class: 'danger sm', disabled: live ? null : '', onclick: async (e) => {
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = 'Canceling…';
      try {
        await api.post(`/api/sessions/${sid}/kill`);
        await refreshRunQueue();
      } catch (err) {
        window.alert('Cancel failed: ' + err.message);
        e.currentTarget.disabled = false;
        e.currentTarget.textContent = 'Cancel';
      }
    } }, 'Cancel'));
  if (s.kind === 'run' && !live)
    bar.insertBefore(el('button', { class: 'ghost sm', onclick: () => debugWithClaude(sid) }, '✦ Debug w/ Claude'), bar.lastChild);
  const mount = el('div', { class: 'term-mount' });
  const heightHandle = el('div', {
    class: 'resize-handle terminal-resizer',
    title: 'Resize terminal',
    onpointerdown: (e) => startPreferenceResize(e, 'terminalHeight', 260, 1000, 'y'),
  });
  const stage = el('div', { id: terminalStageId, class: 'terminal-stage' }, heightHandle, mount);
  view.append(bar, stage);
  mount.appendChild(rec.host);
  applyPanelVisibility(state.uiPrefs);
  if (state.uiPrefs.terminalOpen) setTimeout(() => fitTerminal(sid), 20);
}

function openTerminal(session) {
  if (!terminals.has(session.id)) {
    const host = el('div', { class: 'term-host' });
    dock.appendChild(host);
    const term = new Terminal({ fontFamily: fontStack(CODE_FONTS, state.uiPrefs.codeFont), fontSize: state.uiPrefs.terminalFontSize, cursorBlink: true,
      scrollback: 20000, theme: { background: '#0b0e14', foreground: '#d6deeb', cursor: '#4f9cff' } });
    const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(host);
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/term/${session.id}`);
    ws.binaryType = 'arraybuffer';
    const rec = { term, fit, ws, host, session, exited: false };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { const m = JSON.parse(ev.data);
          if (m.type === 'exit') { rec.exited = true;
            rec.session.status = m.status;
            term.write(`\r\n\x1b[90m── process ${m.status} (exit ${m.code}) ──\x1b[0m\r\n`);
            pollSessions(); }
        } catch (e) {}
      } else term.write(new Uint8Array(ev.data));
    };
    term.onData((dd) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'input', data: dd })); });
    rec.sendResize = () => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols })); };
    term.onResize(rec.sendResize);
    terminals.set(session.id, rec);
  }
  state.view = 'pipeline';
  state.focusedSession = session.id;
  state.tab = 'terminal';
  render();
  pollSessions();
}

function fitTerminal(sid) {
  const rec = terminals.get(sid); if (!rec) return;
  if (!state.uiPrefs.terminalOpen || !rec.host.closest('.term-mount')) return;
  try { rec.fit.fit(); rec.sendResize(); rec.term.focus(); } catch (e) {}
}
window.addEventListener('resize', () => { if (state.tab === 'terminal' && state.focusedSession) fitTerminal(state.focusedSession); });

async function debugWithClaude(sessionId) {
  try {
    const response = await api.post('/api/agents/debug', {
      session_id: sessionId,
      tool: 'claude',
      preview_only: true,
      acknowledge_external_agent: false,
    });
    const preview = response.preview === undefined ? response : response.preview;
    const previewText = typeof preview === 'string'
      ? preview
      : JSON.stringify(preview, null, 2);
    const confirmed = confirmAgentLaunch(
      'claude',
      `Exact debug material prepared for the agent:\n\n${previewText || '(empty preview)'}`,
    );
    if (!confirmed) return;
    const { session } = await api.post('/api/agents/debug', {
      session_id: sessionId,
      tool: 'claude',
      preview_only: false,
      acknowledge_external_agent: true,
    });
    openTerminal(session);
  } catch (e) {
    window.alert(`Could not launch debug agent:\n${e.message}`);
  }
}

/* ---------------- sessions bar ---------------- */
async function pollSessions() {
  try {
    const [sessions, queue] = await Promise.all([
      api.get('/api/sessions'),
      api.get('/api/run_queue').catch(() => state.runQueue),
    ]);
    state.sessions = sessions;
    state.runQueue = queue || state.runQueue;
  } catch (e) { return; }
  const wrap = $('#session-chips'); wrap.innerHTML = '';
  for (const s of state.sessions.slice(0, 12)) {
    if (terminals.has(s.id)) {
      const rec = terminals.get(s.id);
      rec.session = s;
      rec.exited = !['created', 'queued', 'running'].includes(s.status);
    }
    const chip = el('div', { class: `chip ${state.focusedSession === s.id ? 'active' : ''}`,
      onclick: () => { focusOrOpen(s); } },
      el('span', { class: `dot ${s.status}` }), s.title);
    if (s.status === 'failed' && s.kind === 'run')
      chip.append(el('span', { style: 'color:var(--err)', title: 'debug with Claude',
        onclick: (e) => { e.stopPropagation(); debugWithClaude(s.id); } }, ' ✦'));
    wrap.append(chip);
  }
  renderMonitor();
}

async function focusOrOpen(s) {
  const pipeline = s.meta && s.meta.pipeline;
  if (pipeline && state.selected !== pipeline) {
    state.selected = pipeline;
    state.detail = await api.get(`/api/pipelines/${pipeline}`);
    renderSidebar();
  }
  state.view = 'pipeline';
  if (terminals.has(s.id)) { state.focusedSession = s.id; state.tab = 'terminal'; render(); }
  else openTerminal(s);   // re-attach to a session started earlier / by another tab
}

window.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    openCommandPalette();
  } else if (e.ctrlKey && (e.key === 'PageUp' || e.key === 'PageDown')) {
    e.preventDefault();
    cycleNavigation(e.key === 'PageDown' ? 1 : -1);
  } else if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault();
    e.key === 'ArrowLeft' ? window.history.back() : window.history.forward();
  } else if (e.key === 'Escape') {
    if (state.help.open) { closeHelp(); return; }
    if (state.commandOpen) closeCommandPalette();
    if (state.viewSettingsOpen) { state.viewSettingsOpen = false; renderViewSettings(); }
    closeToolMenu(true);
    closeRecentMenu(true);
    closeLightbox();
  }
});

window.addEventListener('popstate', (e) => {
  const navigation = navigationFromHistoryState(e.state);
  if (!navigation || !navigation.snapshot) return;
  rememberRecentNavigation(navigationSnapshot());
  state.navigation.index = Number(navigation.index) || 0;
  applyNavigationSnapshot(navigation.snapshot, true);
});

window.addEventListener('beforeunload', (e) => {
  const dirty = Object.values(state.code || {}).some(workspace =>
    (workspace.tabs || []).some(tab => tab && tab.dirty));
  if (!dirty) return;
  e.preventDefault();
  e.returnValue = '';
});

boot().catch(e => {
  document.body.replaceChildren(el('pre', { class: 'boot-error' }, `OmicsANG failed to start:\n${e.message}`));
});
