// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useRef, useState } from 'react'

/**
 * A file dropped anywhere on the window.
 *
 * An app frame is another document, and the page sees nothing dragged over it.
 * So the moment a drag reaches the shell's own chrome, frames stop taking
 * pointer events (`dv-dragging` in offline.css) and the rest of the drag is
 * ours. A drag that starts and ends entirely over a frame is the frame's, and
 * a sandboxed app frame cannot hand files to the shell — deliberately: it
 * would let any app make the shell open a file of its choosing.
 */
export function useFileDrop(onFile: (file: File) => void): boolean {
  const [dragging, setDragging] = useState(false)
  const onFileRef = useRef(onFile)
  useEffect(() => { onFileRef.current = onFile }, [onFile])

  useEffect(() => {
    let depth = 0
    const hasFiles = (e: DragEvent) => !!e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')
    const set = (on: boolean) => {
      document.documentElement.classList.toggle('dv-dragging', on)
      setDragging(on)
    }
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth++
      set(true)
    }
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    }
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth = Math.max(0, depth - 1)
      if (depth === 0) set(false)
    }
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      depth = 0
      set(false)
      const file = e.dataTransfer?.files[0]
      if (file) onFileRef.current(file)
    }
    window.addEventListener('dragenter', enter)
    window.addEventListener('dragover', over)
    window.addEventListener('dragleave', leave)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragenter', enter)
      window.removeEventListener('dragover', over)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('drop', drop)
      document.documentElement.classList.remove('dv-dragging')
    }
  }, [])

  return dragging
}
