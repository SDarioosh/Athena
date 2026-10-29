/*
 * Athena Rec Test — T-Rex 3 Pro recording feasibility test.
 *
 * "Screen ON" keeps the display lit for the whole test; "Screen OFF" lets the watch
 * turn the screen off normally (and asks the system to relaunch the app on wake).
 * Chunks rotate every 30 s; see shared/session.js for what is measured.
 */
import * as hmUI from '@zos/ui'
import { px } from '@zos/utils'
import { codec } from '@zos/media'
import { rmSync } from '@zos/fs'
import { LocalStorage } from '@zos/storage'
import { push } from '@zos/router'
import { BasePage } from '@zeppos/zml/base-page'
import { createTester, summarize, BEAT_MS, CHUNK_MS } from '../shared/session'
import { buildEnvelope } from '../shared/envelope'
import { UPLOAD_OPTIONS } from '../shared/messaging'
import {
  getRecorder, releaseRecorder, currentRecorder, withMic, errText, step, recorderStatus,
  fileSize, readFile, applyDisplay, resetDisplay, vibrate,
} from '../utils/device'

const DEVICE = 'trex3pro'
const W = 480
const storage = new LocalStorage()

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
            if (currentRecorder()) currentRecorder().stop()
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
      releaseRecorder()
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
        x: px(60), y: px(28), w: px(W - 120), h: px(40), text: 'Athena Rec Test',
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
      // While uploading, the buttons that delete chunks or start a new session are grey
      // and do nothing: the upload loop is still reading this session's files.
      const busy = this.state.uploading
      const idle = (color) => (busy ? 0x333333 : color)
      this.button('Screen ON', 40, 262, 195, 70, idle(0x1e8449), () => this.startTest('screen-on'))
      this.button('Screen OFF', 245, 262, 195, 70, idle(0x1f618d),
        () => this.startTest('screen-off'))
      this.button(busy ? '...' : 'Upload', 40, 342, 195, 64, 0x6c3483,
        () => this.uploadAll())
      this.button('Ping', 245, 342, 195, 64, 0x5d6d7e, () => this.ping())
      this.button('Clear', 60, 414, 170, 50, idle(0x424949), () => this.clearAll())
      this.button('Voice', 250, 414, 170, 50, idle(0xb9770e), () => {
        if (!this.state.uploading) push({ url: 'page/enroll' })
      })
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
      if (this.state.uploading) return
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
      if (this.state.uploading) return
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
      // Keep the screen on while uploading: with the screen off, Zepp OS closes the app
      // after about 10 s, which drops the upload halfway with no message.
      applyDisplay('screen-on')
      this.render()
      let serverStop = null // shown in the final message, which replaces the progress line
      const next = (i) => {
        if (i >= todo.length) {
          this.request({ method: 'end', params: { session_id: st.sessionId } }, { timeout: 30000 })
            .catch(() => {})
          this.state.uploading = false
          if (!this.tester.state.recording) resetDisplay()
          // Say what failed: "upload done" alone hid errors behind "uploaded 0/1".
          const failed = todo.filter((c) => c.uploaded !== true)
          this.state.net = failed.length
            ? `${failed.length} failed: ${String(failed[failed.length - 1].uploaded || 'no answer').slice(0, 90)}`
            : 'upload done'
          if (serverStop) this.state.net += `; server says stop (${serverStop})`
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
          this.tester.markUploaded(c.seq, `read error: ${e.message || e}`, st.sessionId)
          next(i + 1)
          return
        }
        this.request(env, { timeout: 180000, ...UPLOAD_OPTIONS })
          .then((r) => {
            this.tester.markUploaded(c.seq, r && r.ok ? true : `server: ${r && r.error}`,
              st.sessionId)
            if (r && r.stop) serverStop = r.reason
          })
          .catch((e) => this.tester.markUploaded(c.seq, `ble: ${e && e.message ? e.message : e}`,
            st.sessionId))
          .finally(() => next(i + 1))
      }
      next(0)
    },
  }),
)
