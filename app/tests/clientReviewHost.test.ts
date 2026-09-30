// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The web host reaches client review the way the app frame reaches the
// host: on the frame's token URL, so a review is recorded on the document
// the shell shows (OS-layer contract §8.2). These pin the routes, the
// bodies, and that a refusal reaches the person in the host's own words.

import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'

import { httpHost } from '../src/host/http.ts'

const TENANT = '00000000-0000-4000-8000-000000000000'
const FRAME = 'https://studio.test/s/doc-a-token'

interface Seen { url: string; method: string; body: unknown; contentType: string | null }

/** Answer the web API; every sandbox route answers what `routes` says. */
function serve(routes: Record<string, { status?: number; body: unknown }>) {
  const seen: Seen[] = []
  globalThis.fetch = (async (input: string | URL, init?: RequestInit) => {
    const url = String(input)
    const headers = new Headers(init?.headers)
    seen.push({ url, method: init?.method ?? 'GET', body: init?.body, contentType: headers.get('content-type') })
    const json = (body: unknown, status = 200) =>
      new Response(typeof body === 'string' ? body : JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
    if (url === '/api/session') return json({ tenant: TENANT, sandbox_origin: null, quota: { used: 0, cap: 40 } })
    if (url.endsWith('/d/doc-a')) {
      return json({
        path: 'doc-a', token: 'doc-a-token', sandbox_origin: null, manifest: { title: 'doc-a' },
        validation: 'OK', has_human_view: true, render_model: 'authored', is_template: false, trusted: false,
      })
    }
    if (url.startsWith(FRAME)) {
      const r = routes[url.slice(FRAME.length)]
      if (r) return json(r.body, r.status)
    }
    throw new Error(`unexpected ${url}`)
  }) as typeof fetch
  return seen
}

beforeEach(async () => {
  ;(globalThis as { window?: unknown }).window = { location: { origin: 'https://studio.test' } }
  serve({})
  await httpHost.openClan('doc-a')
})

test('a client review is posted to the frame’s own document, as JSON, and its reply returned', async () => {
  const reply = { ok: true, decision: 'd_A', parts: [], suggestions: { status: 'found', decisions: ['d_S'], dropped: 0 } }
  const seen = serve({ '/client-review': { body: reply } })
  const body = {
    answer: 'rejected' as const, client: { name: 'Mary Kelly', email: 'mary@lunasa.ie' }, reasons: ['tone' as const],
    channel: 'pasted_email' as const, said: '  The line is flat.\n\nThanks, Mary ',
    parts: [{ address: 'single_minded_proposition', label: 'Single-minded proposition' }],
  }
  const got = await httpHost.clientReview(body)
  assert.deepEqual(got, reply)
  const call = seen.at(-1)!
  assert.equal(call.url, `${FRAME}/client-review`)
  assert.equal(call.method, 'POST')
  // Sent as the app's own calls are: no content type, a plain request.
  assert.equal(call.contentType, null)
  const sent = JSON.parse(String(call.body))
  assert.deepEqual(sent, body)
  // The client's words go exactly as typed.
  assert.equal(sent.said, '  The line is flat.\n\nThanks, Mary ')
  // The recorder is never the body's to say.
  assert.ok(!('actor' in sent) && !('recorded_by' in sent))
})

test('a refused review throws the host’s own words', async () => {
  serve({ '/client-review': { status: 409, body: { error: 'a client answers a locked document: lock it first' } } })
  await assert.rejects(
    httpHost.clientReview({ answer: 'accepted', client: { name: 'Mary' }, channel: 'none', parts: [] }),
    { message: 'a client answers a locked document: lock it first' },
  )
})

test('confirming, dismissing and marking later post to /client-review/confirm', async () => {
  const seen = serve({ '/client-review/confirm': { body: { ok: true, decision: 'd_P', targets: ['x#audience'] } } })
  await httpHost.clientReviewConfirm({ suggestion: 'd_S', confirm: true })
  assert.deepEqual(JSON.parse(String(seen.at(-1)!.body)), { suggestion: 'd_S', confirm: true })
  await httpHost.clientReviewConfirm({ suggestion: 'd_S2', confirm: false })
  assert.deepEqual(JSON.parse(String(seen.at(-1)!.body)), { suggestion: 'd_S2', confirm: false })
  const got = await httpHost.clientReviewConfirm({ review: 'd_A', address: 'x#audience', answer: 'rejected' })
  assert.equal(got.decision, 'd_P')
  assert.equal(seen.at(-1)!.url, `${FRAME}/client-review/confirm`)
})

test('reopening a part sends the part answer and gives back what edit mode opens with', async () => {
  const reply = {
    ok: true, decision: 'd_U', address: 'x#audience', label: 'Audience', answers: 'd_P',
    reason: 'Mary Kelly asked: The audience is wrong.',
  }
  const seen = serve({ '/client-review/reopen': { body: reply } })
  const got = await httpHost.clientReviewReopen('d_P')
  assert.deepEqual(JSON.parse(String(seen.at(-1)!.body)), { answer: 'd_P' })
  assert.equal(got.reason, reply.reason)
  assert.equal(got.answers, 'd_P')
})

test('what changed upstream is a read', async () => {
  const view = { document_id: 'doc', carried: null, upstream: [] }
  const seen = serve({ '/upstream': { body: view } })
  assert.deepEqual(await httpHost.upstream(), view)
  assert.equal(seen.at(-1)!.method, 'GET')
  assert.equal(seen.at(-1)!.url, `${FRAME}/upstream`)
})

test('the client’s file goes up as its bytes, named in the query', async () => {
  const seen = serve({ '/upload-asset?name=client-20260930T101500Z-Re-brief.eml': {
    body: { ok: true, internal_path: 'human/assets/client-20260930T101500Z-Re-brief.eml', extracted_chars: 0 },
  } })
  const bytes = new TextEncoder().encode('From: mary@lunasa.ie\r\n\r\nNot like this.')
  const got = await httpHost.uploadAsset('client-20260930T101500Z-Re-brief.eml', bytes)
  assert.equal(got.internal_path, 'human/assets/client-20260930T101500Z-Re-brief.eml')
  assert.equal(seen.at(-1)!.body, bytes)
  assert.equal(seen.at(-1)!.method, 'POST')
})
