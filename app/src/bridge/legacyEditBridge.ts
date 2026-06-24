// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Edit bridge for LEGACY (AI-generated HTML) views. Injected by the viewer
// (trusted — not from the agent). Makes elements with data-adf-id
// contenteditable and POSTs clan://patch (an HTML-fragment patch into
// human/patches.yaml) on blur.
//
// Authored Napkin apps do NOT use this — they use the structured edit bridge,
// which writes the DATA layer via clan://patch-data with attribution.
export const LEGACY_EDIT_BRIDGE = `
(function() {
  var clanScheme = window.navigator.userAgent.includes('Windows') ? 'http://clan.localhost' : 'clan://localhost';
  if (window.__clan_bridge_listening) return;
  window.__clan_bridge_listening = true;

  window.__clan_edit_mode = false;

  var EDITABLE_TAGS = ['h1','h2','h3','h4','h5','h6','p','li','td','th'];
  function injectMissingIds() {
    EDITABLE_TAGS.forEach(function(tag) {
      var existing = document.querySelectorAll(tag + '[data-adf-id^="auto-' + tag + '-"]');
      var count = existing.length;
      document.querySelectorAll(tag + ':not([data-adf-id])').forEach(function(el) {
        el.setAttribute('data-adf-id', 'auto-' + tag + '-' + count);
        count++;
      });
    });
  }

  function cleanEditableHtml(el) {
    var clone = el.cloneNode(true);
    var nodes = [clone].concat(Array.prototype.slice.call(clone.querySelectorAll('*')));
    nodes.forEach(function(n) {
      if (!n.removeAttribute) return;
      n.removeAttribute('contenteditable');
      n.removeAttribute('data-clan-edit-setup');
      if (n.style) {
        n.style.outline = (n.dataset && n.dataset.origOutline) || '';
        n.style.cursor = (n.dataset && n.dataset.origCursor) || '';
        if (!n.getAttribute('style')) n.removeAttribute('style');
      }
      n.removeAttribute('data-orig-outline');
      n.removeAttribute('data-orig-cursor');
    });
    return clone.innerHTML;
  }

  function activateEditing() {
    document.querySelectorAll('[data-adf-id]').forEach(function(el) {
      if (!el.dataset.clanEditSetup) {
        el.dataset.clanEditSetup = 'true';
        el.dataset.origOutline = el.style.outline || '';
        el.dataset.origCursor = el.style.cursor || '';

        el.addEventListener('click', function(e) {
          if (!window.__clan_edit_mode) return;
          e.preventDefault();
          e.stopPropagation();
          el.__clanOrig = cleanEditableHtml(el);
          el.setAttribute('contenteditable', 'true');
          el.focus();
        });

        el.addEventListener('blur', function() {
          if (!window.__clan_edit_mode) return;
          el.removeAttribute('contenteditable');
          var id = el.getAttribute('data-adf-id');
          var content = cleanEditableHtml(el);
          if (content === el.__clanOrig) return;
          fetch(clanScheme + '/patch', {
            method: 'POST',
            body: JSON.stringify({ id: id, content: content })
          }).catch(function(err) { console.error('Patch failed:', err); });
        });

        el.addEventListener('keydown', function(e) {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            el.blur();
          }
        });
      }
      el.style.outline = '2px solid rgba(59, 130, 246, 0.5)';
      el.style.cursor = 'text';
    });
  }

  function deactivateEditing() {
    document.querySelectorAll('[data-adf-id]').forEach(function(el) {
      el.style.outline = el.dataset.origOutline || '';
      el.style.cursor = el.dataset.origCursor || '';
      el.removeAttribute('contenteditable');
    });
  }

  var snapshotTick = 0;

  setInterval(function() {
    injectMissingIds();

    snapshotTick++;
    if (snapshotTick === 2 && !window.__clan_snapshot_sent) {
      window.__clan_snapshot_sent = true;
      document.querySelectorAll('[data-adf-id]').forEach(function(el) {
        el.style.outline = el.dataset.origOutline || '';
        el.style.cursor = el.dataset.origCursor || '';
      });
      var snap = document.documentElement.outerHTML;
      if (window.__clan_edit_mode) {
        document.querySelectorAll('[data-adf-id]').forEach(function(el) {
          el.style.outline = '2px solid rgba(59, 130, 246, 0.5)';
          el.style.cursor = 'text';
        });
      }
      fetch(clanScheme + '/snapshot', { method: 'POST', body: snap }).catch(function() {});
    }

    fetch(clanScheme + '/edit-mode')
      .then(function(res) { return res.text(); })
      .then(function(text) {
        var active = text === 'true';
        if (active !== window.__clan_edit_mode) {
          window.__clan_edit_mode = active;
          if (active) activateEditing();
          else deactivateEditing();
        } else if (active) {
          activateEditing();
        }
      })
      .catch(function() {});
  }, 300);
})();
`
