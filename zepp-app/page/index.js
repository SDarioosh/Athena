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
import { Vibrator } from '@zos/sensor'
import { BasePage } from '@zeppos/zml/base-page'
import { createTester, summarize, BEAT_MS, CHUNK_MS } from '../shared/session'
import { buildEnvelope } from '../shared/envelope'

const DEVICE = 'trex3pro'
const W = 480
const storage = new LocalStorage()

let recorder = null
function getRecorder(tester) {
  if (recorder) return recorder
  recorder = create(id.RECORDER)
  try {
    recorder.addEventListener(recorder.event.START, (ok) => {
      if (ok === false) tester.log('recorder START reported failure')
    })
  } catch (e) {
    console.log('recorder event listener not available', e)
  }
  return recorder
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
    state: { widgets: [], info: null, beat: null, uploading: false, net: '' },

    onInit() {
      this.tester = createTester({
        recorder: {
          start: (file) => {
            const r = getRecorder(this.tester)
            r.setFormat(codec.OPUS, { target_file: `data://${file}` })
            r.start()
          },
          stop: () => getRecorder(this.tester).stop(),
        },
        fileSize,
        load: () => storage.getItem('state'),
        save: (s) => storage.setItem('state', s),
        now: () => Date.now(),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (t) => clearTimeout(t),
        onChange: () => this.render(),
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
      this.tester.pageDestroyed()
    },

    // ------------------------------------------------------------------ UI
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
          last ? `last ${Math.round(last.size / 1024)} KB (${last.note})` : 'first chunk…',
        ].join('\n')
      }
      if (!st.chunks.length) {
        return ['Pick a test, record', '10 min, then Stop.', this.state.net].join('\n')
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
      for (const c of this.tester.state.chunks) {
        try {
          rmSync({ path: c.file })
        } catch (e) {}
      }
      this.state.net = ''
      applyDisplay(mode)
      this.tester.start(mode)
      vibrate()
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
          this.state.net = 'upload done'
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
        this.request(env, { timeout: 180000, dataType: 'json' })
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
