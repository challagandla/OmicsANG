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

  const api = { createDisplayCache, createPoller };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OmicsPerformance = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
