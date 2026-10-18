/*
 * Convo Rec Test — T-Rex 3 Pro recording feasibility test.
 *
 * "Screen ON" keeps the display lit for the whole test; "Screen OFF" lets the watch
 * turn the screen off normally (and asks the system to relaunch the app on wake).
 * Chunks rotate every 30 s; see shared/session.js for what is measured.
 */
import * as hmUI from '@zos/ui'
import { px } from '@zos/utils'
import { create, id, codec } from '@zos/media'
import {
  setPageBrightTime, resetPageBrightTime, pauseDropWristScreenOff, resetDropWristScreenOff,
  pausePalmScreenOff, resetPalmScreenOff, setWakeUpRelaunch,
} from '@zos/display'
import { statSync, openSync, readSync, closeSync, rmSync, O_RDONLY } from '@zos/fs'
import { LocalStorage } from '@zos/storage'
import { queryPermission, requestPermission } from '@zos/app'
import { Vibrator } from '@zos/sensor'
import { BasePage } from '@zeppos/zml/base-page'
import { createTester, summarize, BEAT_MS, CHUNK_MS } from '../shared/session'
import { buildEnvelope } from '../shared/envelope'
import { UPLOAD_OPTIONS } from '../shared/messaging'

const DEVICE = 'trex3pro'
const W = 480
const storage = new LocalStorage()

let recorder = null
function getRecorder(onFail) {
  if (recorder) return recorder
  const r = create(id.RECORDER)
  if (!r) throw new Error('create(id.RECORDER) returned nothing')
  try {
    r.addEventListener(r.event.START, (ok) => {
      if (ok === false) onFail('recorder START event reported failure')
    })
  } catch (e) {
    console.log('recorder event listener not available', e)
  }
  recorder = r
  return r
}

const MIC = 'device:os.mic'
const GRANTED = 2

function micStatus() {
  return queryPermission({ permissions: [MIC] })[0]
}

// Calls onGranted once the app may use the microphone, or onDenied with a reason.
// Listing the permission in app.json is not always enough: the watch can still answer
// "PERMISSION DENIED device:os.mic" until the user allows it in a system prompt.
function withMic(onGranted, onDenied) {
  let done = false
  const finish = (ok, why) => {
    if (done) return
    done = true
    if (ok) onGranted()
    else onDenied(why)
  }
  let status
  try {
    status = micStatus()
  } catch (e) {
    // No permission API on this firmware: try the recorder and let it report.
    finish(true)
    return
  }
  if (status === GRANTED) return finish(true)
  const result = requestPermission({
    permissions: [MIC],
    callback: () => {
      let now = null
      try {
        now = micStatus()
      } catch (e) {}
      finish(now === GRANTED, 'microphone not allowed (answer Allow on the watch prompt)')
    },
  })
  if (result === GRANTED) finish(true)
  else if (result === 1) {
    finish(false, 'microphone cannot be requested: allow it for Convo Rec Test in the watch or Zepp app settings, or reinstall')
  }
  // result 0: the watch shows a prompt and the callback decides.
}

function errText(e) {
  return e && e.message ? e.message : String(e)
}

// Runs one recorder call and names it in the error, so the screen says which step failed.
function step(name, fn) {
  try {
    return fn()
  } catch (e) {
    throw new Error(`${name}: ${errText(e)}`)
  }
}

const STATUS_NAMES = ['IDLE', 'PREPARING', 'STARTING', 'RECORDING']

function recorderStatus() {
  if (!recorder || typeof recorder.getStatus !== 'function') return null
  const status = recorder.getStatus()
  const states = recorder.state
  if (!states || states.RECORDING === undefined) return null
  const name = STATUS_NAMES.find((k) => states[k] === status)
  return { status, name: name || `unknown ${status}`, recording: status === states.RECORDING }
}

function fileSize(file) {
  try {
    const st = statSync({ path: file })
    if (st && st.size !== undefined) return st.size
  } catch (e) {}
  return undefined
}

function readFile(file) {
  const size = fileSize(file)
  if (!size) return null
  const fd = openSync({ path: file, flag: O_RDONLY })
  const buf = new ArrayBuffer(size)
  readSync({ fd, buffer: buf })
  closeSync(fd)
  return buf
}

function applyDisplay(mode) {
  try {
    setWakeUpRelaunch({ relaunch: true })
    if (mode === 'screen-on') {
      setPageBrightTime({ brightTime: 2147483000 })
      pauseDropWristScreenOff({ duration: 0 })
      pausePalmScreenOff({ duration: 0 })
    }
  } catch (e) {
    console.log('display policy failed', e)
  }
}

function resetDisplay() {
  try {
    resetPageBrightTime()
    resetDropWristScreenOff()
    resetPalmScreenOff()
    setWakeUpRelaunch({ relaunch: false })
  } catch (e) {}
}

function vibrate() {
  try {
    new Vibrator().start()
  } catch (e) {}
}

function fmt(sec) {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

Page(
  BasePage({
    state: {
      widgets: [], info: null, beat: null, uploading: false, net: '',
      renderTimer: null, checkTimer: null,
    },

    onInit() {
      const failRecording = (msg) => {
        this.tester.error(msg)
        resetDisplay()
      }
      this.tester = createTester({
        recorder: {
          start: (file) => {
            const r = step('create recorder', () => getRecorder(failRecording))
            step('setFormat', () => r.setFormat(codec.OPUS, { target_file: `data://${file}` }))
            step('recorder.start', () => r.start())
            this.checkRecorderSoon()
          },
          stop: () => {
            if (this.state.checkTimer) clearTimeout(this.state.checkTimer)
            this.state.checkTimer = null
            if (recorder) recorder.stop()
          },
        },
        fileSize,
        load: () => storage.getItem('state'),
        save: (s) => storage.setItem('state', s),
        now: () => Date.now(),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (t) => clearTimeout(t),
        // Deferred: rebuilding widgets inside the pressed button's own click handler
        // would delete that button while its callback is still running.
        onChange: () => this.scheduleRender(),
      })
      if (this.tester.resumeIfInterrupted()) {
        applyDisplay(this.tester.state.mode)
        vibrate()
      }
    },

    build() {
      this.render()
      let ticks = 0
      this.state.beat = setInterval(() => {
        ticks += 1
        if ((ticks * 1000) % BEAT_MS === 0) this.tester.heartbeat()
        this.updateInfo()
      }, 1000)
    },

    onDestroy() {
      if (this.state.beat) clearInterval(this.state.beat)
      if (this.state.renderTimer) clearTimeout(this.state.renderTimer)
      if (this.state.checkTimer) clearTimeout(this.state.checkTimer)
      this.state.renderTimer = null
      this.state.checkTimer = null
      recorder = null
      this.tester.pageDestroyed()
    },

    // ------------------------------------------------------------------ UI
    scheduleRender() {
      if (this.state.renderTimer) return
      this.state.renderTimer = setTimeout(() => {
        this.state.renderTimer = null
        this.render()
      }, 10)
    },

    // Two seconds after a chunk starts, the recorder should report RECORDING.
    checkRecorderSoon() {
      if (this.state.checkTimer) clearTimeout(this.state.checkTimer)
      this.state.checkTimer = setTimeout(() => {
        this.state.checkTimer = null
        if (!this.tester.state.recording) return
        try {
          const s = recorderStatus()
          if (s && !s.recording) {
            this.tester.error(`recorder is ${s.name} 2 s after start`)
            resetDisplay()
          }
        } catch (e) {
          this.tester.error(`getStatus: ${errText(e)}`)
          resetDisplay()
        }
      }, 2000)
    },

    clearWidgets() {
      for (const w of this.state.widgets) hmUI.deleteWidget(w)
      this.state.widgets = []
      this.state.info = null
    },

    add(type, props) {
      const w = hmUI.createWidget(type, props)
      this.state.widgets.push(w)
      return w
    },

    button(text, x, y, w, h, color, onClick) {
      return this.add(hmUI.widget.BUTTON, {
        x: px(x), y: px(y), w: px(w), h: px(h), radius: px(h / 2), text, text_size: px(28),
        normal_color: color, press_color: 0x333333, click_func: onClick,
      })
    },

    render() {
      this.clearWidgets()
      const st = this.tester.state
      this.add(hmUI.widget.TEXT, {
        x: px(60), y: px(28), w: px(W - 120), h: px(40), text: 'Convo Rec Test',
        text_size: px(28), color: 0x7fd4ff, align_h: hmUI.align.CENTER_H,
      })
      this.state.info = this.add(hmUI.widget.TEXT, {
        x: px(50), y: px(72), w: px(W - 100), h: px(180), text: this.infoText(),
        text_size: px(26), color: 0xffffff, align_h: hmUI.align.CENTER_H,
        text_style: hmUI.text_style.WRAP,
      })
      if (st.recording) {
        this.button('STOP', 110, 280, 260, 100, 0xc0392b, () => {
          this.tester.stop('stop')
          resetDisplay()
          vibrate()
        })
        return
      }
      this.button('Screen ON', 40, 262, 195, 70, 0x1e8449, () => this.startTest('screen-on'))
      this.button('Screen OFF', 245, 262, 195, 70, 0x1f618d, () => this.startTest('screen-off'))
      this.button(this.state.uploading ? '...' : 'Upload', 40, 342, 195, 64, 0x6c3483,
        () => this.uploadAll())
      this.button('Ping', 245, 342, 195, 64, 0x5d6d7e, () => this.ping())
      this.button('Clear', 160, 414, 160, 50, 0x424949, () => this.clearAll())
    },

    updateInfo() {
      if (this.state.info) this.state.info.setProperty(hmUI.prop.TEXT, this.infoText())
    },

    infoText() {
      const st = this.tester.state
      const s = summarize(st, Date.now())
      if (st.recording) {
        const inChunk = Math.round((Date.now() - st.chunkStart) / 1000)
        const last = st.chunks.length ? st.chunks[st.chunks.length - 1] : null
        return [
          `REC ${st.mode}  ${fmt(s.wallSec)}`,
          `chunk #${st.seq} ${inChunk}/${CHUNK_MS / 1000}s`,
          `done ${s.chunks} · relaunch ${s.relaunches}`,
          st.lastError ? `! ${st.lastError}`
            : last ? `last ${Math.round(last.size / 1024)} KB (${last.note})` : 'first chunk…',
        ].join('\n')
      }
      if (st.lastError) {
        return ['ERROR', st.lastError, this.state.net].join('\n')
      }
      if (!st.chunks.length) {
        return ['Pick a test, record', '10 min, then Stop.', this.state.net].join('\n')
      }
      // A long network message (an upload or ping error) doesn't fit after the five
      // result lines, so show it on its own, with room to wrap.
      if (this.state.net.length > 24) {
        return [`uploaded ${s.uploaded}/${s.chunks}`, this.state.net].join('\n')
      }
      return [
        `${st.mode}: wall ${fmt(s.wallSec)} rec ${fmt(s.recordedSec)}`,
        `${s.chunks} chunks · ${s.interrupted} interr. · ${s.late} late`,
        `relaunch ${s.relaunches} · max gap ${s.maxGapSec}s`,
        `${s.bytesPerSec} B/s · ${s.empty} empty`,
        `uploaded ${s.uploaded}/${s.chunks} ${this.state.net}`,
      ].join('\n')
    },

    startTest(mode) {
      withMic(
        () => this.beginTest(mode),
        (why) => {
          this.tester.error(`mic permission: ${why}`)
          this.scheduleRender()
        },
      )
    },

    beginTest(mode) {
      for (const c of this.tester.state.chunks) {
        try {
          rmSync({ path: c.file })
        } catch (e) {}
      }
      this.state.net = ''
      try {
        applyDisplay(mode)
        this.tester.start(mode)
      } catch (e) {
        this.tester.error(`start: ${errText(e)}`)
      }
      // Buzz only when recording really started, so the buzz means "recording".
      if (this.tester.state.recording) vibrate()
      else resetDisplay()
      this.scheduleRender()
    },

    clearAll() {
      for (const c of this.tester.state.chunks) {
        try {
          rmSync({ path: c.file })
        } catch (e) {}
      }
      this.state.net = ''
      this.tester.clear()
    },

    // --------------------------------------------------------------- network
    ping() {
      this.state.net = 'ping…'
      this.updateInfo()
      this.request({ method: 'ping' }, { timeout: 30000 })
        .then((r) => {
          this.state.net = r && r.ok ? `server OK (${r.ms} ms)` : `ping failed: ${r && r.error}`
        })
        .catch((e) => {
          this.state.net = `phone? ${e && e.message ? e.message : e}`
        })
        .finally(() => this.render())
    },

    uploadAll() {
      if (this.state.uploading) return
      const st = this.tester.state
      const todo = st.chunks.filter((c) => c.uploaded !== true && c.size > 0)
      if (!todo.length) {
        this.state.net = 'nothing to upload'
        this.render()
        return
      }
      this.state.uploading = true
      this.render()
      const next = (i) => {
        if (i >= todo.length) {
          this.request({ method: 'end', params: { session_id: st.sessionId } }, { timeout: 30000 })
            .catch(() => {})
          this.state.uploading = false
          // Say what failed: "upload done" alone hid errors behind "uploaded 0/1".
          const failed = todo.filter((c) => c.uploaded !== true)
          this.state.net = failed.length
            ? `${failed.length} failed: ${String(failed[failed.length - 1].uploaded || 'no answer').slice(0, 90)}`
            : 'upload done'
          this.render()
          return
        }
        const c = todo[i]
        this.state.net = `up ${i + 1}/${todo.length}`
        this.updateInfo()
        let env
        try {
          const audio = readFile(c.file)
          if (!audio) throw new Error('file missing')
          env = buildEnvelope({
            device: DEVICE, session_id: st.sessionId, sequence: c.seq,
            timestamp: new Date(c.start).toISOString(), codec: 'opus',
          }, audio)
        } catch (e) {
          this.tester.markUploaded(c.seq, `read error: ${e.message || e}`)
          next(i + 1)
          return
        }
        this.request(env, { timeout: 180000, ...UPLOAD_OPTIONS })
          .then((r) => {
            this.tester.markUploaded(c.seq, r && r.ok ? true : `server: ${r && r.error}`)
            if (r && r.stop) this.state.net = `server says stop (${r.reason})`
          })
          .catch((e) => this.tester.markUploaded(c.seq, `ble: ${e && e.message ? e.message : e}`))
          .finally(() => next(i + 1))
      }
      next(0)
    },
  }),
)
