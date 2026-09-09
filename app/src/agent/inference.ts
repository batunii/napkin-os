// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Inference, performed by the shell on the app's behalf.
//
// The split is deliberate. The host owns provenance and assembles the prompt —
// schema, digests, the document's data and decision history, attachment text,
// divided into a cacheable half and a volatile one. The shell owns the
// credentials and makes the call. The app owns neither, and asks.
//
// The result is that a `.clan` app written against `clan://api-proxy` works
// unchanged whether inference happens in a desktop host, on a server, or right
// here in the page with the visitor's own key.

import Anthropic from '@anthropic-ai/sdk'

import { host } from '../host'
import { getKey } from './key'

/** What `window.clan.apiProxy` resolves to — unchanged from the server's shape. */
export interface Envelope {
  ok: boolean
  status: number
  data: unknown
  error: string | null
}

const MODEL = 'claude-opus-5'

function fail(status: number, error: string): Envelope {
  return { ok: false, status, data: null, error }
}

/** Pull a JSON object out of the model's text, tolerating fences and prose. */
function extractJson(text: string): unknown {
  let t = text.trim()
  if (t.startsWith('```')) {
    const fenced = t.slice(3)
    const end = fenced.indexOf('```')
    t = (end === -1 ? fenced : fenced.slice(0, end)).trim()
    if (t.toLowerCase().startsWith('json')) t = t.slice(4).trim()
  }
  const start = t.indexOf('{')
  const stop = t.lastIndexOf('}')
  if (start !== -1 && stop !== -1) t = t.slice(start, stop + 1)
  return JSON.parse(t)
}

let client: Anthropic | null = null
let clientKey: string | null = null

function anthropic(key: string): Anthropic {
  if (!client || clientKey !== key) {
    client = new Anthropic({
      apiKey: key,
      // The visitor's own key, in the visitor's own browser, spent only by
      // them. This would be indefensible with *our* key in the bundle; it is
      // the normal shape for a bring-your-own-key tool.
      dangerouslyAllowBrowser: true,
    })
    clientKey = key
  }
  return client
}

/**
 * Run one inference for the app. `payload` is what the app passed to
 * `clan.apiProxy` — the task, the field, what the human typed, attachments.
 */
export async function runInference(payload: unknown): Promise<Envelope> {
  const key = getKey()
  if (!key) {
    return fail(401, 'No Anthropic API key set — add one from the toolbar to generate.')
  }

  let prompt: { system: string; user: string }
  try {
    prompt = await host.buildAgentPrompt(payload)
  } catch (e) {
    return fail(502, `could not assemble the prompt: ${e}`)
  }

  try {
    // Streamed because a full brief with adaptive thinking can outrun a
    // non-streaming request; the cache breakpoint sits on the system half,
    // which is identical on every call and most of what this costs.
    const message = await anthropic(key).messages.stream({
      model: MODEL,
      max_tokens: 16000,
      system: [{ type: 'text', text: prompt.system, cache_control: { type: 'ephemeral' } }],
      messages: [{ role: 'user', content: prompt.user }],
      thinking: { type: 'adaptive' },
    }).finalMessage()

    if (message.stop_reason === 'refusal') {
      return fail(403, 'The model declined this request.')
    }
    // With thinking on, content[0] is a thinking block — select by type.
    const text = message.content
      .filter(b => b.type === 'text')
      .map(b => (b as { text: string }).text)
      .join('')

    return { ok: true, status: 200, data: extractJson(text), error: null }
  } catch (e) {
    if (e instanceof Anthropic.AuthenticationError) {
      return fail(401, 'That API key was rejected. Check it, or paste a new one.')
    }
    if (e instanceof Anthropic.RateLimitError) {
      return fail(429, 'Rate limited by Anthropic — wait a moment and try again.')
    }
    if (e instanceof Anthropic.APIConnectionError) {
      return fail(0, 'Could not reach Anthropic. Check your connection.')
    }
    if (e instanceof Anthropic.APIError) {
      return fail(e.status ?? 500, e.message)
    }
    // A model that answers with prose instead of JSON lands here.
    return fail(502, `${e}`)
  }
}
