// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// What a document is called, the same on the home and in the document bar.
// A new document takes its app's name as its title (clan-sdk instantiate), and
// keeps it until the app sets one from its own data: the Research Tool from the
// campaign's name, Brief Maker from the project name. Until then it says what
// it is, not which app made it.

/**
 * What a document of a known tool is called before it has a title of its own,
 * for a tool that is no longer installed. An installed tool says it itself
 * (`app.home.untitled`).
 */
const UNTITLED: Readonly<Record<string, string>> = {
  'ie.napkin.campaign-research': 'Untitled research',
  'ie.napkin.brief-maker': 'Untitled brief',
}

/**
 * The names the known tools are installed under, for a document whose app is
 * no longer installed: its title is still that name, and still not a title.
 */
const TOOL_NAMES: Readonly<Record<string, string>> = {
  'ie.napkin.campaign-research': 'Research Tool',
  'ie.napkin.brief-maker': 'Brief Maker',
}

/**
 * A document's title, or what it is while it has none. `appName` is the name
 * its app is installed under, and `untitled` what the app calls a new
 * document, when the shell knows them.
 */
export function docTitle(
  title: string | null | undefined,
  appId?: string | null,
  appName?: string,
  untitled?: string,
): { text: string; untitled: boolean } {
  const t = (title ?? '').trim()
  const generic = !t || t === appName || /^untitled\b/i.test(t) || t === TOOL_NAMES[appId ?? '']
  if (!generic) return { text: t, untitled: false }
  return { text: untitled || UNTITLED[appId ?? ''] || 'Untitled document', untitled: true }
}
