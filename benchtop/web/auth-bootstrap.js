/* SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla */
/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
/* Capture and clear the one-time fragment before any third-party code executes. */
'use strict';

(() => {
  const fragment = window.location.hash.slice(1);
  let token = '';
  if (fragment) {
    const params = new URLSearchParams(fragment);
    token = params.get('bootstrap') || params.get('token') || '';
    if (!token && !fragment.includes('=')) {
      try { token = decodeURIComponent(fragment); }
      catch (e) { token = ''; }
    }
  }
  window.history.replaceState(
    null,
    document.title,
    `${window.location.pathname}${window.location.search}`,
  );
  Object.defineProperty(window, '__benchtopTakeBootstrapToken', {
    configurable: true,
    enumerable: false,
    value: () => {
      const captured = token;
      token = '';
      delete window.__benchtopTakeBootstrapToken;
      return captured;
    },
    writable: false,
  });
})();
