// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// What the shell does with offline copies, apart from showing them.

import { fetchOpenDocument } from '../host/http'
import { persistStorage } from '../pwa/pwa'
import { copyBytes, putCopy, type OfflineCopy } from './store'

/** Where a document open on the device came from — what its banner says. */
export type DeviceSource =
  | { kind: 'offline'; copy: OfflineCopy }
  | { kind: 'file'; name: string }

/**
 * "Present offline": take the open server document's current `.clan` and keep
 * it on this device. Asks the browser to keep storage from being evicted,
 * since a copy that vanishes before the pitch is worse than none.
 */
export async function keepOffline(docId: string): Promise<OfflineCopy> {
  const { manifest, bytes } = await fetchOpenDocument()
  const copy: OfflineCopy = {
    id: docId,
    title: manifest.title || 'Untitled',
    revision: manifest.version,
    updatedAt: manifest.updated_at,
    sha256: manifest.sha256,
    appName: manifest.app?.name ?? null,
    savedAt: new Date().toISOString(),
    size: bytes.byteLength,
  }
  await putCopy(copy, bytes)
  // Not awaited: Firefox answers with a permission prompt, and the copy is
  // already saved whatever the answer — it just may not survive eviction.
  void persistStorage()
  return copy
}

function saveBytes(bytes: Uint8Array, filename: string) {
  const url = URL.createObjectURL(new Blob([bytes as unknown as BlobPart], { type: 'application/vnd.clan' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** The copy exactly as it was taken, as a file. */
export async function downloadCopy(copy: OfflineCopy) {
  const stem = copy.title.trim().replace(/[^\w.-]+/g, '-') || 'document'
  saveBytes(await copyBytes(copy.id), `${stem}.clan`)
}

/** Ask for a `.clan` from this device. `null` if cancelled. */
export function pickFile(): Promise<File | null> {
  return new Promise(resolve => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.clan,application/vnd.clan'
    input.onchange = () => resolve(input.files?.[0] ?? null)
    input.oncancel = () => resolve(null)
    input.click()
  })
}

export function isClanFile(file: File): boolean {
  return /\.clan$/i.test(file.name)
}

/** "3 Sept, 14:05" — when a copy was taken or last changed. */
export function when(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
