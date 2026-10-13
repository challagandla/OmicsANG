/* SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla */
/* SPDX-License-Identifier: MIT */
(function (root) {
  'use strict';

  // Only presentation reads belong here; never cache a launch plan or gate.
  function createDisplayCache({ ttl = 15000, capacity = 48, now = Date.now } = {}) {
    const entries = new Map();
    return {
      clear() { entries.clear(); },
      async read(key, loader, force = false) {
        let entry = entries.get(key);
        if (force || !entry || (entry.completed && now() - entry.at >= ttl)) {
          entry = { completed: false, at: 0 };
          entries.delete(key);
          entries.set(key, entry);
          while (entries.size > capacity) entries.delete(entries.keys().next().value);
          entry.promise = Promise.resolve().then(loader).then(value => {
            entry.completed = true;
            entry.at = now();
            return value;
          }).catch(error => {
            if (entries.get(key) === entry) entries.delete(key);
            throw error;
          });
        }
        // Callers may edit their view model without corrupting the cache.
        return structuredClone(await entry.promise);
      },
    };
  }

  function createPoller({ task, visible, delay, schedule = setTimeout, cancel = clearTimeout }) {
    let timer = null;
    let pending = null;
    function clearTimer() {
      if (timer !== null) cancel(timer);
      timer = null;
    }
    function run() {
      clearTimer();
      if (!visible()) return Promise.resolve();
      if (pending) return pending;
      pending = Promise.resolve().then(task).finally(() => {
        pending = null;
        if (visible()) timer = schedule(() => { run().catch(() => {}); }, delay());
      });
      return pending;
    }
    return {
      run,
      visibilityChanged() {
        clearTimer();
        if (visible()) run().catch(() => {});
      },
    };
  }

  // Each attached terminal keeps an xterm buffer and a WebSocket open. Release
  // the focused or live ones never; release terminals the server no longer
  // lists, and keep only the most recently used `keep` ended ones.
  function terminalsToRelease(records, { focused, listed, keep = 6 }) {
    const release = [];
    const ended = [];
    for (const [id, rec] of records) {
      if (id === focused) continue;
      if (!listed.has(id)) release.push(id);
      else if (rec.exited) ended.push([id, rec.usedAt || 0]);
    }
    ended.sort((a, b) => b[1] - a[1]);
    for (const [id] of ended.slice(keep)) release.push(id);
    return release;
  }

  const api = { createDisplayCache, createPoller, terminalsToRelease };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OmicsPerformance = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
