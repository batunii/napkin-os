// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The loading sign as plain HTML, for pages React never reaches: a recipient
// viewer, a boot screen, anything composed as a string. Same mark, same
// quarter-turn, same reduced-motion pulse as <LogoSpinner> (LogoSpinner.css).
//
// Two hand-kept copies live where TypeScript cannot be imported — keep them in
// step with this file:
//   app/index.html                             the boot screen
//   crates/napkin-host/assets/home_app.html    the home CLAN app
//
// The colours read the studio tokens and fall back to the light palette, so
// the markup works on a page that defines none of them.

/** The mark: a disc of four quarters turned -45°. `size` in px. */
export function logoSpinnerSvg(size = 28): string {
  return `<svg class="napkin-spinner-mark" width="${size}" height="${size}" viewBox="0 0 22 22" aria-hidden="true">`
    + `<clipPath id="napkin-spinner-clip"><circle cx="11" cy="11" r="11"/></clipPath>`
    + `<g clip-path="url(#napkin-spinner-clip)" transform="rotate(-45 11 11)">`
    + `<rect x="0" y="0" width="11" height="11" fill="var(--plan, #14161B)"/>`
    + `<rect x="11" y="0" width="11" height="11" fill="var(--create, #FF4F2E)"/>`
    + `<rect x="0" y="11" width="11" height="11" fill="var(--learn, #DADDE3)"/>`
    + `<rect x="11" y="11" width="11" height="11" fill="var(--produce, #8B919E)"/>`
    + `</g></svg>`
}

/** Mark plus an optional label, announced politely. */
export function logoSpinnerHtml(label?: string, size = 28): string {
  const text = label
    ? `<span class="napkin-spinner-label">${escapeHtml(label)}</span>`
    : `<span class="napkin-spinner-sr">Loading</span>`
  return `<span class="napkin-spinner" role="status" aria-live="polite">${logoSpinnerSvg(size)}${text}</span>`
}

/** The stylesheet the markup needs. Put it in a <style> once per page. */
export const LOGO_SPINNER_CSS = `
.napkin-spinner{display:inline-flex;align-items:center;gap:10px;color:var(--ink2,#4A4F5C);font:500 13px/1 var(--f-display,'Geist','Helvetica Neue',Arial,sans-serif);letter-spacing:-.01em}
.napkin-spinner-mark{display:block;flex-shrink:0;animation:napkin-spinner-turn 1.6s cubic-bezier(.2,.8,.2,1) infinite}
.napkin-spinner-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@keyframes napkin-spinner-turn{0%{transform:rotate(0)}25%{transform:rotate(90deg)}50%{transform:rotate(180deg)}75%{transform:rotate(270deg)}100%{transform:rotate(360deg)}}
@keyframes napkin-spinner-breathe{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(.86);opacity:.55}}
@media (prefers-reduced-motion:reduce){.napkin-spinner .napkin-spinner-mark{animation:napkin-spinner-breathe 2.4s ease-in-out infinite!important}}
`

function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!))
}
