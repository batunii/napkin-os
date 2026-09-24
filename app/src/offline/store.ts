// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Offline copies: documents kept on this device for presenting with no network.
//
// A copy is a snapshot of what the server held when it was taken — the packed
// `.clan`, its title and its revision — and nothing more. The server stays
// authoritative (N3): a copy is never written back, and nothing done to one
// while it is open reaches the studio. Taking a new copy replaces the old one.
//
// IndexedDB rather than OPFS: every browser we care about has it, including
// Safari, and a copy is one blob, not a file tree. Listing reads the small
// records only; the bytes are in a store of their own and read on open.

export interface OfflineCopy {
  /** The server's document id. */
  id: string
  title: string
  /** The manifest's version, as the server had it. */
  revision: string
  /** The manifest's `updated_at`: when the document last changed. */
  updatedAt: string
  sha256: string
  appName: string | null
  /** When this device took the copy (ISO). */
  savedAt: string
  size: number
}

const DB = 'napkin-offline'
const COPIES = 'copies'
const BYTES = 'bytes'

let opened: Promise<IDBDatabase> | null = null

function db(): Promise<IDBDatabase> {
  opened ??= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => {
      req.result.createObjectStore(COPIES, { keyPath: 'id' })
      req.result.createObjectStore(BYTES)
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  opened.catch(() => { opened = null })
  return opened
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error ?? new Error('aborted'))
  })
}

function result<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

// ── Change notification, so every list on the page stays current ────────────

const listeners = new Set<() => void>()

export function onCopiesChanged(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function changed() {
  listeners.forEach(fn => fn())
}

// ── The store ────────────────────────────────────────────────────────────────

/** Every copy, most recently taken first. */
export async function listCopies(): Promise<OfflineCopy[]> {
  const tx = (await db()).transaction(COPIES, 'readonly')
  const all = await result(tx.objectStore(COPIES).getAll() as IDBRequest<OfflineCopy[]>)
  return all.sort((a, b) => b.savedAt.localeCompare(a.savedAt))
}

export async function getCopy(id: string): Promise<OfflineCopy | null> {
  const tx = (await db()).transaction(COPIES, 'readonly')
  return (await result(tx.objectStore(COPIES).get(id) as IDBRequest<OfflineCopy | undefined>)) ?? null
}

/** Keep a copy, replacing any earlier one of the same document. */
export async function putCopy(copy: OfflineCopy, bytes: ArrayBuffer): Promise<void> {
  const tx = (await db()).transaction([COPIES, BYTES], 'readwrite')
  tx.objectStore(COPIES).put(copy)
  tx.objectStore(BYTES).put(bytes, copy.id)
  await done(tx)
  changed()
}

export async function copyBytes(id: string): Promise<Uint8Array> {
  const tx = (await db()).transaction(BYTES, 'readonly')
  const bytes = await result(tx.objectStore(BYTES).get(id) as IDBRequest<ArrayBuffer | undefined>)
  if (!bytes) throw new Error('This offline copy is no longer on this device.')
  return new Uint8Array(bytes)
}

export async function removeCopy(id: string): Promise<void> {
  const tx = (await db()).transaction([COPIES, BYTES], 'readwrite')
  tx.objectStore(COPIES).delete(id)
  tx.objectStore(BYTES).delete(id)
  await done(tx)
  changed()
}
