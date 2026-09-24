// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The template apps draw the shell's figures from a generated snippet. These
// hold the committed snippet to the figures it was generated from, so a
// change to AgentFigure.tsx or its CSS cannot leave the apps drawing the old
// ones, and check the snippet is safe to splice into a page.

import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { test } from 'node:test'

import { figureSnippet } from '../src/studio/figureSnippet.tsx'
import { AGENT_KEYS } from '../src/studio/model.ts'

// app/, found from wherever the runner put the bundled test.
let root = new URL('.', import.meta.url)
while (!existsSync(new URL('package.json', root))) root = new URL('..', root)
const read = (p: string) => readFileSync(new URL(p, root), 'utf8')

test('the committed snippet is the one the figures generate', () => {
  const want = figureSnippet(read('src/studio/AgentFigure.css'))
  assert.equal(read('templates/shared/agent-figures.html'), want,
    'templates/shared/agent-figures.html is stale: run `npm run figures` in app/')
})

test('the snippet never closes the page early', () => {
  // The host splices its bridge in at the last </body>; a snippet carrying
  // one would move that point into the middle of a script.
  const s = read('templates/shared/agent-figures.html')
  assert.ok(!/<\/(body|html|head)/i.test(s))
  assert.equal(s.match(/<\/script>/g)?.length, 1)
})

test('both templates mark where the snippet goes', () => {
  for (const t of ['brief-maker', 'campaign-research']) {
    const html = read(`templates/${t}/index.html`)
    const at = html.indexOf('<!-- @napkin:agent-figures -->')
    assert.ok(at > 0 && at < html.indexOf('</head>'), `${t} has the marker in its <head>`)
  }
})

test('every agent is in the snippet, and the helper sets state and label', () => {
  const s = read('templates/shared/agent-figures.html')
  const script = s.slice(s.indexOf('<script>') + 8, s.indexOf('</script>'))
  const agentFigure = new Function(`${script}; return agentFigure`)() as
    (k: string, o?: Record<string, unknown>) => string
  for (const k of AGENT_KEYS) assert.match(agentFigure(k), /^<svg class="agent-figure"/, k)
  const w = agentFigure('drafter', { state: 'working', label: 'Drafter · "Insight"' })
  assert.match(w, /data-state="working"/)
  assert.match(w, /aria-label="Drafter · &quot;Insight&quot; — working"/)
  assert.match(agentFigure('judge', { state: 'needs-you' }), /class="af-badge"/)
  assert.match(agentFigure('extract', { decorative: true }), /aria-hidden="true"/)
  assert.equal(agentFigure('nobody'), '')
})
