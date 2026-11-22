import { writeFileSync } from 'node:fs'
import path from 'node:path'

import { app, BrowserWindow, desktopCapturer, ipcMain, screen } from 'electron'
import type { IpcMainEvent, IpcMainInvokeEvent } from 'electron'

import type { CaptureRegionResult } from './screenshot-region-types'

/** The renderer's pick/cancel IPC names; the overlay page in OVERLAY_HTML sends them. */
const READY_CHANNEL = 'hermes:screenshot:region:ready'
const SHOT_CHANNEL = 'hermes:screenshot:region:shot'
const PICK_CHANNEL = 'hermes:screenshot:region:pick'
const CANCEL_CHANNEL = 'hermes:screenshot:region:cancel'

/** Abandon a forgotten overlay instead of pinning a full-screen window forever. */
const PICK_TIMEOUT_MS = 180_000

/** The attach menu fades out over ~100ms; capture after it is gone from the pixels. */
const MENU_SETTLE_MS = 140

const MAX_SHOT_WIDTH = 4096

interface PendingPick {
  display: Electron.Display
  hint: string
  image: Electron.NativeImage
  resolve: (result: CaptureRegionResult) => void
  timer: NodeJS.Timeout
  win: BrowserWindow
}

/**
 * Self-contained overlay page (no build input, no network). It renders the
 * frozen screenshot, dims everything outside the drag rectangle, and reports
 * the pick in CSS pixels — main does the crop on the native image so the raw
 * pixels never round-trip through the renderer.
 */
const OVERLAY_HTML = `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Capture</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #000; cursor: crosshair; user-select: none; -webkit-user-select: none; }
  #shot { position: fixed; inset: 0; width: 100%; height: 100%; object-fit: fill; display: none; pointer-events: none; }
  .mask { position: fixed; background: rgba(0, 0, 0, 0.45); display: none; pointer-events: none; }
  #sel { position: fixed; display: none; box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.95), 0 0 0 2px rgba(0, 0, 0, 0.35); pointer-events: none; }
  #size { position: fixed; display: none; padding: 3px 7px; border-radius: 5px; background: rgba(18, 18, 20, 0.88); color: #fff;
          font: 600 12px/1.2 -apple-system, "Segoe UI", sans-serif; letter-spacing: 0.02em; white-space: nowrap; pointer-events: none; }
  #hint { position: fixed; left: 50%; top: 22px; transform: translateX(-50%); padding: 7px 14px; border-radius: 999px;
          background: rgba(18, 18, 20, 0.85); color: #fff; font: 500 13px/1.2 -apple-system, "Segoe UI", sans-serif;
          display: none; pointer-events: none; box-shadow: 0 6px 22px rgba(0, 0, 0, 0.35); }
</style>
</head>
<body>
<img id="shot" alt="">
<div class="mask" id="mTop"></div>
<div class="mask" id="mBottom"></div>
<div class="mask" id="mLeft"></div>
<div class="mask" id="mRight"></div>
<div id="sel"></div>
<div id="size"></div>
<div id="hint"></div>
<script>
const { ipcRenderer } = require('electron')

var el = function (id) { return document.getElementById(id) }
var armable = false
var dragging = false
var done = false
var startX = 0
var startY = 0
var rect = null

function clampInt(value, min, max) {
  return Math.max(min, Math.min(max, Math.round(value)))
}

function normalize(x0, y0, x1, y1) {
  var width = window.innerWidth
  var height = window.innerHeight
  var x = clampInt(Math.min(x0, x1), 0, width)
  var y = clampInt(Math.min(y0, y1), 0, height)
  var w = clampInt(Math.abs(x1 - x0), 0, width - x)
  var h = clampInt(Math.abs(y1 - y0), 0, height - y)
  return { x: x, y: y, w: w, h: h }
}

function paint() {
  var width = window.innerWidth
  var height = window.innerHeight
  var mTop = el('mTop')
  var mBottom = el('mBottom')
  var mLeft = el('mLeft')
  var mRight = el('mRight')
  var sel = el('sel')
  var size = el('size')

  if (!rect || rect.w < 1 || rect.h < 1) {
    mTop.style.display = 'block'
    mTop.style.left = '0px'
    mTop.style.top = '0px'
    mTop.style.width = width + 'px'
    mTop.style.height = height + 'px'
    mBottom.style.display = 'none'
    mLeft.style.display = 'none'
    mRight.style.display = 'none'
    sel.style.display = 'none'
    size.style.display = 'none'
    return
  }

  var x = rect.x
  var y = rect.y
  var w = rect.w
  var h = rect.h
  var x2 = Math.min(width, x + w)
  var y2 = Math.min(height, y + h)

  mTop.style.display = 'block'
  mTop.style.left = '0px'
  mTop.style.top = '0px'
  mTop.style.width = width + 'px'
  mTop.style.height = Math.max(0, y) + 'px'

  mBottom.style.display = 'block'
  mBottom.style.left = '0px'
  mBottom.style.top = y2 + 'px'
  mBottom.style.width = width + 'px'
  mBottom.style.height = Math.max(0, height - y2) + 'px'

  mLeft.style.display = 'block'
  mLeft.style.left = '0px'
  mLeft.style.top = y + 'px'
  mLeft.style.width = Math.max(0, x) + 'px'
  mLeft.style.height = Math.max(0, h) + 'px'

  mRight.style.display = 'block'
  mRight.style.left = x2 + 'px'
  mRight.style.top = y + 'px'
  mRight.style.width = Math.max(0, width - x2) + 'px'
  mRight.style.height = Math.max(0, h) + 'px'

  sel.style.display = 'block'
  sel.style.left = x + 'px'
  sel.style.top = y + 'px'
  sel.style.width = w + 'px'
  sel.style.height = h + 'px'

  size.style.display = 'block'
  size.textContent = w + ' × ' + h
  size.style.left = Math.min(Math.max(4, x), Math.max(4, width - 120)) + 'px'
  size.style.top = (y - 26 < 4 ? y + h + 6 : y - 26) + 'px'
}

function confirmPick() {
  if (done || !rect || rect.w < 1 || rect.h < 1) {
    return
  }

  done = true
  document.body.style.cursor = 'default'
  ipcRenderer.send('${PICK_CHANNEL}', {
    height: rect.h,
    viewportHeight: window.innerHeight,
    viewportWidth: window.innerWidth,
    width: rect.w,
    x: rect.x,
    y: rect.y
  })
}

function cancel() {
  if (done) {
    return
  }

  done = true
  ipcRenderer.send('${CANCEL_CHANNEL}')
}

ipcRenderer.on('${SHOT_CHANNEL}', function (_event, payload) {
  if (!payload || typeof payload.dataUrl !== 'string') {
    return
  }

  var shot = el('shot')
  shot.onload = function () {
    shot.style.display = 'block'
    var hint = el('hint')

    if (typeof payload.hint === 'string' && payload.hint) {
      hint.textContent = payload.hint
      hint.style.display = 'block'
    }

    armable = true
    paint()
  }
  shot.src = payload.dataUrl
})

window.addEventListener('mousedown', function (event) {
  if (event.button !== 0 || !armable || done) {
    return
  }

  dragging = true
  startX = event.clientX
  startY = event.clientY
  rect = normalize(startX, startY, startX, startY)
  paint()
})

window.addEventListener('mousemove', function (event) {
  if (!dragging || done) {
    return
  }

  rect = normalize(startX, startY, event.clientX, event.clientY)
  paint()
})

window.addEventListener('mouseup', function (event) {
  if (!dragging || done) {
    return
  }

  dragging = false
  var next = normalize(startX, startY, event.clientX, event.clientY)

  if (next.w < 4 || next.h < 4) {
    // A stray click is not a selection; treat it as backing out.
    rect = null
    paint()
    cancel()

    return
  }

  rect = next
  paint()
  confirmPick()
})

window.addEventListener('dblclick', function () {
  if (!armable || done || !rect) {
    return
  }

  rect = { x: 0, y: 0, w: window.innerWidth, h: window.innerHeight }
  paint()
  confirmPick()
})

window.addEventListener('keydown', function (event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    cancel()

    return
  }

  if (event.key === 'Enter' && rect) {
    event.preventDefault()
    confirmPick()
  }
})

window.addEventListener('contextmenu', function (event) {
  event.preventDefault()
  cancel()
})

window.addEventListener('resize', paint)

ipcRenderer.send('${READY_CHANNEL}')
</script>
</body>
</html>
`

function wait(ms: number) {
  return new Promise<void>(resolve => {
    setTimeout(resolve, ms)
  })
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** The overlay reports CSS pixels; anything malformed is a pick we refuse rather than guess. */
function parseRect(value: unknown): null | {
  height: number
  viewportHeight: number
  viewportWidth: number
  width: number
  x: number
  y: number
} {
  if (!value || typeof value !== 'object') {
    return null
  }

  const { height, viewportHeight, viewportWidth, width, x, y } = value as Record<string, unknown>

  if (!isFiniteNumber(x) || !isFiniteNumber(y) || !isFiniteNumber(width) || !isFiniteNumber(height)) {
    return null
  }

  if (width < 1 || height < 1) {
    return null
  }

  const viewportW = isFiniteNumber(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0
  const viewportH = isFiniteNumber(viewportHeight) && viewportHeight > 0 ? viewportHeight : 0

  return {
    height,
    viewportHeight: viewportH,
    viewportWidth: viewportW,
    width,
    x,
    y
  }
}

/**
 * Menu-driven region capture for the composer's attach menu: freeze the display
 * under the cursor, let the user drag a rectangle, hand back the cropped PNG.
 * Installed on every platform — unlike the macOS-only gesture in
 * `command-screenshot.ts`, this path starts from an explicit renderer click.
 */
export function installRegionScreenshot({ rendererUrl }: { rendererUrl: string }): () => void {
  const expectedUrl = new URL(rendererUrl)
  let pending: null | PendingPick = null
  let overlayPath = ''

  const trustedWindow = (event: IpcMainEvent | IpcMainInvokeEvent): BrowserWindow | null => {
    const win = BrowserWindow.fromWebContents(event.sender)

    if (!win || win.isDestroyed() || event.senderFrame !== event.sender.mainFrame) {
      return null
    }

    try {
      const url = new URL(event.senderFrame.url)

      return url.protocol === expectedUrl.protocol &&
        url.host === expectedUrl.host &&
        url.pathname === expectedUrl.pathname
        ? win
        : null
    } catch {
      return null
    }
  }

  const writeOverlay = () => {
    const candidates = [path.join(app.getPath('temp'), 'hermes-region-capture.html'), path.join(app.getPath('userData'), 'hermes-region-capture.html')]

    for (const candidate of candidates) {
      try {
        writeFileSync(candidate, OVERLAY_HTML, 'utf8')
        overlayPath = candidate

        return true
      } catch {
        // Fall through to the next writable location.
      }
    }

    return false
  }

  const finish = (result: CaptureRegionResult) => {
    const current = pending

    if (!current) {
      return
    }

    // Clear first: destroying the window fires 'closed', which must not re-finish.
    pending = null
    clearTimeout(current.timer)

    if (!current.win.isDestroyed()) {
      current.win.destroy()
    }

    current.resolve(result)
  }

  const crop = (
    current: PendingPick,
    rect: { height: number; viewportHeight: number; viewportWidth: number; width: number; x: number; y: number }
  ): CaptureRegionResult => {
    const size = current.image.getSize()

    if (size.width < 1 || size.height < 1) {
      return { ok: false, reason: 'failed' }
    }

    // The shot keeps the display's aspect ratio but not necessarily its pixel
    // size (thumbnailSize is a hint, DPI scales vary), so map by the overlay's
    // OWN viewport: that is the rectangle the user actually dragged in, whether
    // or not the OS let the window reach the display's full size.
    const viewportWidth = rect.viewportWidth || current.display.size.width || size.width
    const viewportHeight = rect.viewportHeight || current.display.size.height || size.height
    const scaleX = size.width / viewportWidth
    const scaleY = size.height / viewportHeight
    const x = Math.max(0, Math.min(size.width - 1, Math.round(rect.x * scaleX)))
    const y = Math.max(0, Math.min(size.height - 1, Math.round(rect.y * scaleY)))
    const width = Math.max(1, Math.min(size.width - x, Math.round(rect.width * scaleX)))
    const height = Math.max(1, Math.min(size.height - y, Math.round(rect.height * scaleY)))
    const cropped = current.image.crop({ height, width, x, y })
    const croppedSize = cropped.getSize()

    if (croppedSize.width < 1 || croppedSize.height < 1) {
      return { ok: false, reason: 'failed' }
    }

    return { ok: true, height: croppedSize.height, png: cropped.toPNG(), width: croppedSize.width }
  }

  const onReady = (event: IpcMainEvent) => {
    const current = pending

    if (!current || event.sender.id !== current.win.webContents.id) {
      return
    }

    current.win.webContents.send(SHOT_CHANNEL, {
      dataUrl: current.image.toDataURL(),
      height: current.image.getSize().height,
      hint: current.hint,
      width: current.image.getSize().width
    })
  }

  const onPick = (event: IpcMainEvent, value: unknown) => {
    const current = pending

    if (!current || event.sender.id !== current.win.webContents.id) {
      return
    }

    const rect = parseRect(value)

    finish(rect ? crop(current, rect) : { ok: false, reason: 'failed' })
  }

  const onCancel = (event: IpcMainEvent) => {
    const current = pending

    if (!current || event.sender.id !== current.win.webContents.id) {
      return
    }

    finish({ ok: false, reason: 'cancelled' })
  }

  const handle = ipcMain.handle('hermes:screenshot:region', async (event, value) => {
    if (!trustedWindow(event)) {
      throw new Error('Screenshot request from an untrusted frame')
    }

    if (pending) {
      return { ok: false, reason: 'busy' } satisfies CaptureRegionResult
    }

    if (!overlayPath && !writeOverlay()) {
      return { ok: false, reason: 'unavailable' } satisfies CaptureRegionResult
    }

    const hint =
      value && typeof value === 'object' && typeof (value as { hint?: unknown }).hint === 'string'
        ? (value as { hint: string }).hint.slice(0, 120)
        : ''

    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint())

    // Let the clicked menu finish closing so it is not frozen into the shot.
    await wait(MENU_SETTLE_MS)

    const shotWidth = Math.min(MAX_SHOT_WIDTH, Math.round(display.size.width * display.scaleFactor))
    const shotHeight = Math.max(1, Math.round((shotWidth * display.size.height) / display.size.width))

    let image: Electron.NativeImage

    try {
      const sources = await desktopCapturer.getSources({
        types: ['screen'],
        thumbnailSize: { height: shotHeight, width: shotWidth },
        fetchWindowIcons: false
      })
      const source = sources.find(item => item.display_id === String(display.id)) ?? sources[0]

      if (!source || source.thumbnail.isEmpty()) {
        return { ok: false, reason: 'unavailable' } satisfies CaptureRegionResult
      }

      image = source.thumbnail
    } catch {
      return { ok: false, reason: 'unavailable' } satisfies CaptureRegionResult
    }

    if (pending) {
      return { ok: false, reason: 'busy' } satisfies CaptureRegionResult
    }

    return await new Promise<CaptureRegionResult>(resolve => {
      const win = new BrowserWindow({
        x: display.bounds.x,
        y: display.bounds.y,
        // display.size (not bounds): bounds is clamped to the work area on
        // Windows, which would letterbox the frozen shot against the taskbar.
        width: display.size.width,
        height: display.size.height,
        alwaysOnTop: true,
        backgroundColor: '#000000',
        frame: false,
        fullscreenable: false,
        hasShadow: false,
        maximizable: false,
        minimizable: false,
        movable: false,
        resizable: false,
        show: false,
        skipTaskbar: true,
        title: 'Capture',
        webPreferences: {
          backgroundThrottling: false,
          contextIsolation: false,
          nodeIntegration: true,
          sandbox: false,
          spellcheck: false
        }
      })

      const current: PendingPick = {
        display,
        hint,
        image,
        resolve,
        timer: setTimeout(() => finish({ ok: false, reason: 'cancelled' }), PICK_TIMEOUT_MS),
        win
      }

      pending = current

      win.setAlwaysOnTop(true, 'screen-saver')
      win.setMenuBarVisibility(false)
      win.setSkipTaskbar(true)

      win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
      win.webContents.on('will-navigate', event => event.preventDefault())

      win.on('closed', () => {
        finish({ ok: false, reason: 'cancelled' })
      })

      void win
        .loadFile(overlayPath)
        .then(() => {
          if (pending !== current || win.isDestroyed()) {
            return
          }

          win.show()
          win.focus()
          win.setAlwaysOnTop(true, 'screen-saver')
        })
        .catch(() => {
          finish({ ok: false, reason: 'failed' })
        })
    })
  })

  ipcMain.on(READY_CHANNEL, onReady)
  ipcMain.on(PICK_CHANNEL, onPick)
  ipcMain.on(CANCEL_CHANNEL, onCancel)

  const dispose = () => {
    finish({ ok: false, reason: 'cancelled' })
    ipcMain.removeHandler('hermes:screenshot:region')
    ipcMain.removeListener(READY_CHANNEL, onReady)
    ipcMain.removeListener(PICK_CHANNEL, onPick)
    ipcMain.removeListener(CANCEL_CHANNEL, onCancel)
    app.removeListener('will-quit', dispose)
  }

  app.once('will-quit', dispose)

  return dispose
}
