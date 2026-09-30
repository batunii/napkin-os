// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The apps' own rules, read out of their HTML as they ship: the functions
// are lifted from the page's script and run against stand-ins for what they
// read (no DOM here). Client review's part words and aliases in the shared
// fields snippet; the Research Tool's title and a locked report; the brief's
// aliases, its name for the research it started from, and the override
// offered on a part the Judge failed, empty or not.

import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

/** app/templates, found from wherever the runner put this bundle. */
function templates(): string {
  let dir = dirname(fileURLToPath(import.meta.url))
  for (let i = 0; i < 6; i++) {
    const t = join(dir, 'templates')
    if (existsSync(join(t, 'shared', 'clan-fields.html'))) return t
    dir = dirname(dir)
  }
  return join(process.cwd(), 'templates')
}
const page = (rel: string) => readFileSync(join(templates(), rel), 'utf8')

/** From `start` (an opening brace) to its closing brace, skipping strings and comments. */
function balanced(src: string, start: number): number {
  let depth = 0
  for (let i = start; i < src.length; i++) {
    const c = src[i]
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); if (i < 0) break; continue }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 1; continue }
    if (c === "'" || c === '"' || c === '`') {
      for (i++; i < src.length && src[i] !== c; i++) if (src[i] === '\\') i++
      continue
    }
    if (c === '{') depth++
    else if (c === '}' && --depth === 0) return i + 1
  }
  throw new Error('unbalanced')
}

/** The source of `function <name>(…){…}` in the page. */
function fn(src: string, name: string): string {
  const at = src.indexOf(`function ${name}(`)
  assert.ok(at >= 0, `function ${name} is in the page`)
  return src.slice(at, balanced(src, src.indexOf('{', at)))
}

/** The source of `var <name>={…};`. */
function obj(src: string, name: string): string {
  const at = src.indexOf(`var ${name}={`)
  assert.ok(at >= 0, `var ${name} is in the page`)
  return src.slice(at, balanced(src, src.indexOf('{', at))) + ';'
}

/** Run the named functions with `env` as the names they read. */
function lift<T>(src: string, names: string[], env: Record<string, unknown>): T {
  const body = `${names.map(n => fn(src, n)).join('\n')}\nreturn {${names.join(',')}};`
  return new Function(...Object.keys(env), body)(...Object.values(env)) as T
}

const arr = (x: unknown) => (Array.isArray(x) ? x : [])
const isObj = (x: unknown) => !!x && typeof x === 'object' && !Array.isArray(x)
const esc = (s: unknown) => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`)
const listJoin = (a: string[]) => (a.length < 2 ? a.join('') : `${a.slice(0, -1).join(', ')} and ${a[a.length - 1]}`)

// ── the shared fields snippet: client review ───────────────────────────────

const CLF = page('shared/clan-fields.html')

test('a part declares its aliases, and they go up with the part', () => {
  type F = { aliasesOf(el: unknown): string[]; partRef(p: unknown): unknown }
  const f = lift<F>(CLF, ['aliasesOf', 'partRef'], {})
  const el = (v: string | null) => ({ getAttribute: (a: string) => (a === 'data-clan-part-aliases' ? v : null) })
  assert.deepEqual(f.aliasesOf(el(' budget,  Cost ,, spend , BUDGET,scope ')), ['budget', 'Cost', 'spend', 'scope'])
  assert.deepEqual(f.aliasesOf(el('the   line')), ['the line'])
  assert.deepEqual(f.aliasesOf(el(null)), [])
  assert.deepEqual(f.partRef({ address: 'audience', label: 'Audience', aliases: ['target', 'who'], el: {} }),
    { address: 'audience', label: 'Audience', aliases: ['target', 'who'] })
  assert.deepEqual(f.partRef({ address: 'insight', label: 'Insight', aliases: [], el: {} }), { address: 'insight', label: 'Insight' })
  // Collected from the page, and a change to them is seen.
  assert.match(fn(CLF, 'declaredParts'), /aliases:aliasesOf\(el\)/)
  assert.match(CLF, /attributeFilter:\['data-clan-part','data-clan-part-label','data-clan-part-aliases'\]/)
  assert.match(fn(CLF, 'postParts'), /declaredParts\(\)\.map\(partRef\)/)
})

const partEnv = (parts: unknown[], answers: unknown[], blocks: Record<string, unknown> = {}) => {
  const clientPart = (path: string) => parts.find(p => (p as { address: string }).address === path) ?? null
  const clientAnswerOf = (id: string) => answers.find(a => (a as { decision: string }).decision === id) ?? null
  const clientName = (c: { name?: string } | null) => (c?.name ?? '').trim() || 'the client'
  const blockOf = (id: string) => blocks[id] ?? null
  return { clientPart, clientAnswerOf, clientName, blockOf }
}

test('a part’s own words are shown as typed, and start the reason for its change', () => {
  type F = { partSaid(p: unknown): string; askedReason(path: string): string }
  const words = '  Too young,\n\n  and too urban.  '
  const parts = [
    { address: 'audience', review: 'd_A', decision: 'd_P1', said: words, client: { name: 'Mary Kelly' } },
    { address: 'insight', review: 'd_A', decision: 'd_P2', client: { name: 'Mary Kelly' } },
    { address: 'tone_and_world', review: 'd_A', decision: 'd_P3', quote: 'It feels flat.', client: { name: 'Mary Kelly' } },
    { address: 'budget_and_scope', review: 'd_A', decision: 'd_P4', said: ' \n ', client: { name: 'Mary Kelly' } },
  ]
  const answers = [{ decision: 'd_A', said: 'Honestly, not the brief\nwe talked about.', client: { name: 'Mary Kelly' } }]
  // An older view without `said` on the part: its decision holds it.
  const blocks = { d_P2: { decision: { said: 'The insight is a platitude.' } } }
  const f = lift<F>(CLF, ['partSaid', 'askedReason'], partEnv(parts, answers, blocks))
  assert.equal(f.partSaid(parts[0]), words) // verbatim: not trimmed inside or out
  assert.equal(f.partSaid(parts[1]), 'The insight is a platitude.')
  assert.equal(f.partSaid(parts[3]), '')
  assert.equal(f.askedReason('audience'), 'Mary Kelly asked: Too young, and too urban.')
  assert.equal(f.askedReason('insight'), 'Mary Kelly asked: The insight is a platitude.')
  // A quote is not the part's own words: the document's words.
  assert.equal(f.askedReason('tone_and_world'), 'Mary Kelly asked: Honestly, not the brief we talked about.')
  // Only whitespace under the part: the document's words.
  assert.equal(f.askedReason('budget_and_scope'), 'Mary Kelly asked: Honestly, not the brief we talked about.')
  assert.equal(f.askedReason('nowhere'), '')
  // The popover shows them before the quote and the document's words.
  const pop = fn(CLF, 'popOf')
  assert.ok(pop.indexOf('partSaid(p)') < pop.indexOf('p.quote') && pop.indexOf('p.quote') < pop.indexOf('an.said'))
  assert.match(fn(CLF, 'makeChange'), /res\.reason\|\|askedReason\(path\)/)
  // Shown as typed: the quoted words keep their line breaks and spaces.
  assert.match(CLF, /\.clf-q\.said\{white-space:pre-wrap;overflow-wrap:anywhere\}/)
})

test('a suggestion by the host’s matcher is Ellis’s', () => {
  type F = { crewKey(d: unknown): string | null }
  const crew = () => ({ forWork: (w: string) => (w === 'read' ? 'extract' : ''), ofDecision: () => 'judge' })
  const f = lift<F>(CLF, ['ellisKey', 'crewKey'], { crew }) as F & { ellisKey(): string }
  assert.equal(f.crewKey({ handler: 'client_parts_match@1', actor: 'process:host', action: 'suggest_part' }), 'extract')
  assert.equal(f.crewKey({ handler: 'client_parts_match@1' }), 'extract')
  assert.equal(f.crewKey({ handler: 'find_client_parts@1.0' }), 'extract') // an older chain
  assert.equal(f.crewKey({ handler: 'judge_brief@1' }), 'judge')
})

// ── the Research Tool ──────────────────────────────────────────────────────

const RT = page('campaign-research/index.html')
const MARKET = { IE: 'Ireland', GB: 'Great Britain' }

function researchEnv(campaign: Record<string, unknown>, manifest: Record<string, unknown>) {
  const calls: string[] = []
  const TITLE = { done: '', last: '' }
  const window = { __CLAN__: { manifest }, clan: { setTitle: (t: string) => { calls.push(t); return Promise.resolve({ ok: true }) } } }
  const document = { title: '' }
  const camp = () => campaign
  const marketNames = () => arr((campaign.markets as { value?: unknown })?.value).map(m => MARKET[m as keyof typeof MARKET] ?? m)
  return { calls, document, env: { camp, marketNames, listJoin, isObj, TITLE, window, document, MARKET } }
}
type TitleFns = { researchTitle(): string; titleIsOurs(t: string): boolean; syncTitle(): void }
const TITLE_FNS = ['brandName', 'workName', 'isMarketList', 'researchTitle', 'titleIsOurs', 'syncTitle']

test('the research is titled by its brand and markets, else its working name', () => {
  const t = (c: Record<string, unknown>) => lift<TitleFns>(RT, TITLE_FNS, researchEnv(c, {}).env).researchTitle()
  const brand = { value: { name: 'Lunasa', ref: 'b_1' } }
  assert.equal(t({ brand, markets: { value: ['IE', 'GB'] }, name: { value: 'Summer' } }), 'Lunasa · Ireland and Great Britain')
  assert.equal(t({ brand, markets: { value: ['IE'] } }), 'Lunasa · Ireland')
  assert.equal(t({ brand, name: { value: ' Summer push ' } }), 'Summer push')
  assert.equal(t({ brand }), 'Lunasa')
  assert.equal(t({ name: { value: 'Summer push' }, markets: { value: ['IE'] } }), 'Summer push')
  assert.equal(t({}), '')
})

test('the title is set over the tool’s name, never over one a person chose', async () => {
  const c = { brand: { value: { name: 'Lunasa' } }, markets: { value: ['IE'] }, name: { value: 'Summer push' } }
  for (const [title, set] of [
    ['Research Tool', true], ['Untitled research', true], ['', true], ['Summer push', true],
    ['Lunasa · Great Britain', true], ['Lunasa', true], ['Our pitch for Lunasa', false],
    ['Lunasa · Ireland and Great Britain', true], ['Lunasa · Great Britain, PL and Ireland', true],
    // A person's title that only starts like ours is theirs.
    ['Lunasa · Summer push', false], ['Lunasa · Ireland launch', false], ['Lunasa · Ireland and more', false],
  ] as const) {
    const r = researchEnv(c, { title })
    lift<TitleFns>(RT, TITLE_FNS, r.env).syncTitle()
    assert.deepEqual(r.calls, set ? ['Lunasa · Ireland'] : [], `over ${JSON.stringify(title)}`)
    assert.equal(r.document.title, 'Lunasa · Ireland · Research')
  }
})

type ReportFns = { reportStale(r: unknown): string[]; refreshBtn(stale: string[], title: string): string }
function reportEnv(locked: boolean, chain: unknown[]) {
  const pathOf = (a: unknown) => { const s = String(a), i = s.indexOf('#'); return i < 0 ? s : s.slice(i + 1) }
  return {
    arr, isObj, esc, pathOf, proj: () => ({ built_from: {} }), decs: () => chain, human: (a: unknown) => String(a),
    CHAT: { reportBusy: false }, ...((CF: unknown) => ({ ClanFields: CF, window: { ClanFields: CF } }))({ locked: () => (locked ? { decision: {} } : null) }),
  }
}
const REPORT_FNS = ['touchesAsk', 'reportWriter', 'reportLocked', 'reportStale', 'refreshBtn']

test('a report is out of date by what came after it in the chain, never by the clock', () => {
  // Newest first. The edit after the report carries an older stamp: the chain says it came after.
  const chain = [
    { action: 'edit', actor: 'human:aoife', targets: ['doc#campaign.objective'], timestamp: '2020-01-01T00:00:00Z' },
    { action: 'compose_report', actor: 'process:middleware', targets: ['doc#report'], timestamp: '2026-09-30T10:00:00Z' },
    { action: 'edit', actor: 'human:aoife', targets: ['doc#campaign.problem'], timestamp: '2030-01-01T00:00:00Z' },
  ]
  const env = reportEnv(false, chain)
  const f = lift<ReportFns>(RT, REPORT_FNS, env)
  const r = { built_at: '2026-09-30T10:00:00Z', based_on: {} }
  const stale = f.reportStale(r)
  assert.equal(stale.length, 1)
  assert.match(stale[0], /^1 decision about the ask or the selection since \(edit by human:aoife\)\.$/)
  // No decision wrote the report: nothing is said to be newer than it.
  const none = reportEnv(false, [chain[0], chain[2]])
  assert.deepEqual(lift<ReportFns>(RT, REPORT_FNS, none).reportStale(r), [])
  assert.doesNotMatch(fn(RT, 'reportStale'), /Date\.parse|timestamp|built_at/)
  // Out of date and open: Refresh report is the thing to press.
  assert.match(f.refreshBtn(stale, ''), /class="btn sm primary"/)
  assert.doesNotMatch(f.refreshBtn(stale, ''), / disabled/)
})

test('a locked report is the version agreed to: never out of date, Refresh not offered', () => {
  const chain = [
    { action: 'edit', actor: 'human:aoife', targets: ['doc#campaign.objective'] },
    { action: 'compose_report', targets: ['doc#report'] },
  ]
  const env = reportEnv(true, chain)
  const f = lift<ReportFns>(RT, REPORT_FNS, env)
  assert.deepEqual(f.reportStale({ based_on: { facts_sha256: 'a' } }), [])
  const b = f.refreshBtn(['anything'], 'compose_report: re-compose')
  assert.doesNotMatch(b, /primary/)
  assert.match(b, / disabled/)
  assert.match(b, /title="The report is locked: this version is the one agreed to\."/)
  // And "moved on since" is said only of a stale report, which a locked one never is.
  assert.match(fn(RT, 'renderReport'), /stale\.length\?'<span style="color:var\(--create-ink\)">The document has moved on since\.<\/span>'/)
  assert.match(fn(RT, 'doRefreshReport'), /if\(reportLocked\(\)\)\{ toast\(/)
})

test('the Research Tool’s parts declare their aliases', () => {
  const PART_ALIASES = new Function(`${obj(RT, 'PART_ALIASES')} return PART_ALIASES;`)() as Record<string, string>
  assert.equal(PART_ALIASES.audience_stated, 'audience, target, who')
  assert.match(fn(RT, 'partAttrs'), /data-clan-part-aliases=/)
})

// ── Brief Maker ────────────────────────────────────────────────────────────

const BM = page('brief-maker/index.html')

test('the brief’s parts declare the words a client calls them by', () => {
  const A = new Function(`${obj(BM, 'PART_ALIASES')} return PART_ALIASES;`)() as Record<string, string>
  assert.equal(A.audience, 'audience, target, who')
  assert.equal(A.budget_and_scope, 'budget, cost, spend, scope')
  assert.equal(A.tone_and_world, 'tone, voice, feel')
  assert.equal(A.single_minded_proposition, 'proposition, line, idea, smp')
  // Every field the brief declares as a part has some; each list is words.
  const keys = [...BM.matchAll(/\{sec:'[^']+',key:'([^']+)'/g)].map(m => m[1])
  assert.ok(keys.length > 10)
  for (const k of keys) {
    assert.ok(A[k], `aliases for ${k}`)
    assert.ok(A[k].split(',').every(w => w.trim().length > 0 && w.trim().length <= 60), k)
  }
  assert.match(fn(BM, 'fieldRow'), /row\.setAttribute\('data-clan-part-aliases', PART_ALIASES\[f\.key\]\)/)
})

test('a brief from research names the research by the title the research gave itself', () => {
  type F = { upTitle(): string }
  const UP_MARKET = { IE: 'Ireland', GB: 'Great Britain' }
  const envVal = (v: unknown) => (isObj(v) && 'value' in (v as object) ? (v as { value: unknown }).value : v)
  const listWords = listJoin
  const t = (store: string, campaign: Record<string, unknown>) =>
    lift<F>(BM, ['upTitle'], { upData: () => ({ campaign }), upStoreTitle: () => store, envVal, arr, listWords, UP_MARKET }).upTitle()
  const c = { brand: { value: { name: 'Lunasa' } }, markets: { value: ['IE', 'GB'] }, name: { value: 'Summer push' } }
  assert.equal(t('Lunasa · Ireland and Great Britain', c), 'Lunasa · Ireland and Great Britain')
  assert.equal(t('Our pitch', c), 'Our pitch')
  // The store only knows the tool's name: the research's own rule, from its frozen copy.
  assert.equal(t('Research Tool', c), 'Lunasa · Ireland and Great Britain')
  assert.equal(t('Untitled research', c), 'Lunasa · Ireland and Great Britain')
  assert.equal(t('', { name: { value: 'Summer push' } }), 'Summer push')
  assert.equal(t('', { brand: { value: { name: 'Lunasa' } } }), 'Lunasa research')
})

test('a part the Judge failed can be kept with a reason, empty or not', () => {
  const notes = fn(BM, 'notesHtml')
  assert.match(notes, /act-override/)
  assert.match(notes, /'Leave it empty, and say why'/)
  assert.match(notes, /'Keep it as it is, and say why'/)
  // It opens the OS's drawer on the part, where the override is recorded.
  assert.match(fn(BM, 'bindNotes'), /act-override'\); if\(o\) o\.addEventListener\('click',function\(\)\{ var F=OSF\(\); if\(F\) F\.open\(f\.key\); \}\)/)
  // The drawer offers the override on a path whatever its value.
  const vb = fn(CLF, 'verdictBlock')
  assert.match(vb, /data-clf="override"/)
  assert.doesNotMatch(vb.slice(0, vb.indexOf('data-clf="override"')), /editableValue|textOf/)
})

test("a person's verdict on a part is theirs, not the Judge's", () => {
  type F = { provHtml(f: { key: string }): string; notesHtml(f: { key: string }): string }
  const person = { id: 'd_p', kind: 'verdict', polarity: 'bad', agent: 'human:aoife', rationale: 'It needs mandatories.' }
  const judge = { id: 'd_j', kind: 'verdict', polarity: 'bad', agent: 'judge_brief', rationale: 'Too thin.' }
  const make = (v: Record<string, unknown>) => lift<F>(BM, ['provHtml', 'notesHtml'], {
    CHAIN: { state: 'ok' }, OPEN_WHY: {}, JUDE: 'Jude', DARA: 'Dara', YOU: '<you>',
    get: () => '', hasVal: () => false, decisionsFor: () => [], esc: (s: string) => s, arr: (a: unknown) => (Array.isArray(a) ? a : []),
    verdictOf: () => ({ d: v, bad: true, open: true }), ownVerdictOf: () => ({ d: v, bad: true, open: true }),
    crossVerdictsOf: () => [], proposalOf: () => null, docLocked: () => false, OSF: () => ({}),
    hasReasoning: () => false, plainJudge: (s: string) => s, fig: (k: string) => `<fig ${k}>`,
    isHuman: (d: { agent?: string }) => /^human:/.test(String(d && d.agent)),
    whoName: (d: { agent?: string }) => (/^human:/.test(String(d.agent)) ? 'You' : 'Jude'),
    whoMark: (d: { agent?: string }) => (/^human:/.test(String(d.agent)) ? '<you>' : '<fig judge>'),
  })
  const p = make(person), j = make(judge)
  assert.match(p.provHtml({ key: 'mandatories' }), /<you>You/)
  assert.match(p.notesHtml({ key: 'mandatories' }), /You said this part needs another look/)
  assert.doesNotMatch(p.notesHtml({ key: 'mandatories' }), /Jude: this part/)
  assert.match(j.provHtml({ key: 'mandatories' }), /<fig judge>Jude/)
  assert.match(j.notesHtml({ key: 'mandatories' }), /Jude: this part needs another look/)
})

test('a part judged after the page drew it gets its mark when the chain comes', () => {
  const src = fn(BM, 'refreshWhy')
  assert.match(src, /if\(p\) p\.outerHTML=ph; else if\(ph&&lab\) lab\.insertAdjacentHTML\('afterend',ph\)/)
})
