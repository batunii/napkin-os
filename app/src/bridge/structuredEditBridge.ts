// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Structured edit bridge for AUTHORED Napkin apps. Injected by the viewer
// (trusted). Unlike the legacy bridge it does NOT patch rendered HTML — it
// writes the DATA layer via clan://patch-data with attribution
// (agent: human, pinned: true), so human edits are co-equal, typed entries in
// the decision chain (the provenance-native co-authorship model).
//
// Two ways an authored app participates:
//  1. Declarative: annotate elements with data-clan-field="dot.path" (and
//     optionally data-clan-type="number"). In edit mode the bridge makes them
//     editable and commits the value on blur.
//  2. Programmatic: call window.clan.patchData(path, value, opts) directly, or
//     window.clan.apiProxy(kind, input) for AI inference, etc.
//
// On a successful write the bridge updates window.__CLAN__.data in place and
// dispatches a `clan:dataupdated` CustomEvent — no iframe reload, so the app's
// own state is preserved.
export const STRUCTURED_EDIT_BRIDGE = `
(function() {
  var clanScheme = window.navigator.userAgent.includes('Windows') ? 'http://clan.localhost' : 'clan://localhost';
  if (window.__clan_structured_bridge) return;
  window.__clan_structured_bridge = true;
  window.__CLAN__ = window.__CLAN__ || { data: {} };
  window.__clan_edit_mode = false;

  function setByPath(obj, path, value) {
    var parts = path.split('.');
    var cur = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      var k = parts[i];
      if (cur[k] == null || typeof cur[k] !== 'object') cur[k] = {};
      cur = cur[k];
    }
    cur[parts[parts.length - 1]] = value;
  }
  function getByPath(obj, path) {
    return path.split('.').reduce(function(o, k) { return (o == null) ? undefined : o[k]; }, obj);
  }
  function nest(path, value) {
    var root = {};
    setByPath(root, path, value);
    return root;
  }

  function patchData(path, value, opts) {
    opts = opts || {};
    var body = {
      patch: nest(path, value),
      agent: opts.actor || 'human',
      action: opts.action || 'edit',
      rationale: opts.rationale || '',
      pinned: opts.pinned !== false
    };
    return fetch(clanScheme + '/patch-data', { method: 'POST', body: JSON.stringify(body) })
      .then(function(r) { return r.json(); })
      .then(function(res) {
        if (res && res.ok) {
          setByPath(window.__CLAN__.data, path, value);
          window.dispatchEvent(new CustomEvent('clan:dataupdated', { detail: { path: path, value: value } }));
        }
        return res;
      });
  }

  function apiProxy(kind, payload) {
    return fetch(clanScheme + '/api-proxy', {
      method: 'POST',
      body: JSON.stringify({ request_kind: kind || 'agent', payload: payload })
    }).then(function(r) { return r.json(); });
  }

  function uploadAsset(name, bytes, agent) {
    var q = '?name=' + encodeURIComponent(name) + (agent ? '&agent=' + encodeURIComponent(agent) : '');
    return fetch(clanScheme + '/upload-asset' + q, { method: 'POST', body: bytes })
      .then(function(r) { return r.json(); });
  }

  function fork(agents, contexts) {
    return fetch(clanScheme + '/fork', { method: 'POST', body: JSON.stringify({ agents: agents, contexts: contexts }) })
      .then(function(r) { return r.json(); });
  }

  // Public API authored apps can call.
  window.clan = {
    data: function() { return window.__CLAN__.data; },
    patchData: patchData,
    apiProxy: apiProxy,
    uploadAsset: uploadAsset,
    fork: fork,
    isEditing: function() { return window.__clan_edit_mode; }
  };

  // Scoped host capabilities — populated ONLY for trusted (signed) apps. An
  // untrusted app sees window.napkin.trusted === false and no privileged
  // methods. The allowlist is host-enforced; this just mirrors it.
  window.napkin = { trusted: false, allowed: [] };
  fetch(clanScheme + '/capabilities').then(function(r){ return r.json(); }).then(function(c){
    window.napkin.trusted = !!c.trusted;
    window.napkin.allowed = c.allowed || [];
    if ((c.allowed || []).indexOf('notify') !== -1) {
      window.napkin.notify = function(title, body) {
        return fetch(clanScheme + '/notify', { method: 'POST', body: JSON.stringify({ title: title, body: body }) })
          .then(function(r){ return r.json(); });
      };
    }
    window.dispatchEvent(new CustomEvent('napkin:ready', { detail: c }));
  }).catch(function(){});

  // --- Declarative data-clan-field editing ---
  function fieldValue(el) {
    var raw = (el.textContent || '').trim();
    if (el.getAttribute('data-clan-type') === 'number') {
      var n = Number(raw);
      return isNaN(n) ? raw : n;
    }
    return raw;
  }

  function activate() {
    document.querySelectorAll('[data-clan-field]').forEach(function(el) {
      if (!el.dataset.clanFieldSetup) {
        el.dataset.clanFieldSetup = 'true';
        el.dataset.origOutline = el.style.outline || '';
        el.addEventListener('focus', function() {
          if (!window.__clan_edit_mode) return;
          el.__clanOrig = fieldValue(el);
        });
        el.addEventListener('blur', function() {
          if (!window.__clan_edit_mode) return;
          el.removeAttribute('contenteditable');
          var v = fieldValue(el);
          if (v === el.__clanOrig) return;
          patchData(el.getAttribute('data-clan-field'), v, { actor: 'human', pinned: true, action: 'edit field' });
        });
        el.addEventListener('keydown', function(e) {
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); el.blur(); }
        });
        el.addEventListener('click', function() {
          if (!window.__clan_edit_mode) return;
          el.setAttribute('contenteditable', 'true');
          el.focus();
        });
      }
      el.style.outline = '2px solid rgba(99,102,241,0.55)';
      el.style.cursor = 'text';
    });
  }
  function deactivate() {
    document.querySelectorAll('[data-clan-field]').forEach(function(el) {
      el.style.outline = el.dataset.origOutline || '';
      el.style.cursor = '';
      el.removeAttribute('contenteditable');
    });
  }

  setInterval(function() {
    fetch(clanScheme + '/edit-mode').then(function(r) { return r.text(); }).then(function(t) {
      var active = t === 'true';
      if (active !== window.__clan_edit_mode) {
        window.__clan_edit_mode = active;
        window.dispatchEvent(new CustomEvent('clan:editmode', { detail: { active: active } }));
        if (active) activate(); else deactivate();
      } else if (active) {
        activate();
      }
    }).catch(function() {});
  }, 400);
})();
`
