/*
 * Voice profile: guided 60-90 s recording through the watch microphone.
 *
 * The owner reads the prompts aloud (Next shows the next one). Done becomes available
 * after 45 s; the recording stops on its own after 2 min. Upload sends the file to the
 * phone, which posts it to /v1/speakers/enroll.json with is_owner=true. The screen stays
 * on for the whole flow: with the screen off, Zepp OS closes the app after about 10 s.
 */
import * as hmUI from '@zos/ui'
import { px } from '@zos/utils'
import { codec } from '@zos/media'
import { rmSync } from '@zos/fs'
import { back } from '@zos/router'
import { BasePage } from '@zeppos/zml/base-page'
import { createEnrollment, PROMPTS, MIN_MS, TARGET_MS } from '../shared/enroll'
import { buildEnvelope } from '../shared/envelope'
import { UPLOAD_OPTIONS } from '../shared/messaging'
import {
  getRecorder, releaseRecorder, currentRecorder, withMic, errText, step, readFile,
  applyDisplay, resetDisplay, vibrate,
} from '../utils/device'

const W = 480

function fmt(ms) {
  const sec = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

function removeFile(file) {
  if (!file) return
  try {
    rmSync({ path: file })
  } catch (e) {}
}

Page(
  BasePage({
    state: {
      widgets: [], info: null, prompt: null, beat: null, renderTimer: null, destroyed: false,
    },

    onInit() {
      this.enroll = createEnrollment({
        recorder: {
          start: (file) => {
            const r = step('create recorder', () => getRecorder((msg) => this.enroll.fail(msg)))
            step('setFormat', () => r.setFormat(codec.OPUS, { target_file: `data://${file}` }))
            step('recorder.start', () => r.start())
          },
          stop: () => {
            if (currentRecorder()) currentRecorder().stop()
          },
        },
        now: () => Date.now(),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (t) => clearTimeout(t),
        onChange: () => this.scheduleRender(),
      })
    },

    build() {
      this.render()
      this.state.beat = setInterval(() => this.updateInfo(), 1000)
    },

    onDestroy() {
      // Set first: cancel() below and a late upload answer call onChange -> scheduleRender,
      // which must not draw on a page that is gone.
      this.state.destroyed = true
      if (this.state.beat) clearInterval(this.state.beat)
      if (this.state.renderTimer) clearTimeout(this.state.renderTimer)
      this.state.renderTimer = null
      this.enroll.cancel()
      removeFile(this.enroll.state.file)
      releaseRecorder()
      resetDisplay()
    },

    scheduleRender() {
      if (this.state.destroyed || this.state.renderTimer) return
      this.state.renderTimer = setTimeout(() => {
        this.state.renderTimer = null
        this.render()
      }, 10)
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

    text(y, h, text, size, color) {
      return this.add(hmUI.widget.TEXT, {
        x: px(50), y: px(y), w: px(W - 100), h: px(h), text, text_size: px(size), color,
        align_h: hmUI.align.CENTER_H, text_style: hmUI.text_style.WRAP,
      })
    },

    render() {
      if (this.state.destroyed) return
      for (const w of this.state.widgets) hmUI.deleteWidget(w)
      this.state.widgets = []
      this.state.info = null
      this.state.prompt = null
      const st = this.enroll.state
      this.text(24, 40, 'Voice profile', 28, 0x7fd4ff)
      this.state.info = this.text(64, 40, this.infoText(), 24, 0xaaaaaa)

      if (st.phase === 'idle') {
        this.text(110, 190, 'Read short sentences aloud for about a minute, in your normal '
          + 'voice, where it is quiet. Your server learns how you sound on this watch.', 24,
        0xffffff)
        this.button('Start', 140, 320, 200, 80, 0x1e8449, () => this.start())
        this.button('Back', 170, 408, 140, 50, 0x424949, () => back())
      } else if (st.phase === 'recording') {
        this.state.prompt = this.text(104, 200, this.enroll.promptText(), 26, 0xffffff)
        const last = st.prompt >= PROMPTS.length - 1
        this.button(last ? 'Again' : 'Next', 40, 316, 195, 76, 0x1f618d, () => this.next())
        this.button('Done', 245, 316, 195, 76,
          this.enroll.canFinish() ? 0xc0392b : 0x5d6d7e, () => this.finish())
        this.button('Cancel', 170, 404, 140, 50, 0x424949, () => this.cancel())
      } else if (st.phase === 'recorded') {
        this.text(110, 190, st.error ? `Upload failed: ${st.error}`
          : `Recorded ${fmt(this.enroll.elapsedMs())}. Upload it to your server now `
            + '(keep the phone nearby).', 24, st.error ? 0xff8888 : 0xffffff)
        this.button('Upload', 40, 316, 195, 76, 0x6c3483, () => this.upload())
        this.button('Redo', 245, 316, 195, 76, 0x424949, () => this.start())
      } else if (st.phase === 'uploading') {
        this.text(140, 120, 'Uploading…\nthis can take a minute.', 26, 0xffffff)
      } else if (st.phase === 'done') {
        const r = st.result || {}
        this.text(110, 190, `Saved as ${r.name || 'you'} (${r.speech || '?'} s of speech). `
          + 'New recordings will recognise your voice.', 24, 0x82e0aa)
        this.button('Back', 140, 330, 200, 76, 0x1e8449, () => back())
      } else {
        this.text(110, 190, `ERROR\n${st.error}`, 24, 0xff8888)
        this.button('Retry', 140, 330, 200, 76, 0x1f618d, () => this.start())
      }
    },

    infoText() {
      const st = this.enroll.state
      if (st.phase !== 'recording') return ''
      const left = MIN_MS - this.enroll.elapsedMs()
      return `REC ${fmt(this.enroll.elapsedMs())} / ${fmt(TARGET_MS)}`
        + (left > 0 ? ` · Done in ${Math.ceil(left / 1000)}s` : '')
    },

    updateInfo() {
      if (this.state.info) this.state.info.setProperty(hmUI.prop.TEXT, this.infoText())
      // the Done button changes colour once it can be used
      if (this.enroll.state.phase === 'recording'
          && Math.abs(this.enroll.elapsedMs() - MIN_MS) < 1000) this.scheduleRender()
    },

    start() {
      withMic(() => {
        removeFile(this.enroll.state.file)
        applyDisplay('screen-on')
        this.enroll.start()
        if (this.enroll.state.phase === 'recording') vibrate()
        else resetDisplay()
      }, (why) => this.enroll.fail(`mic permission: ${why}`))
    },

    next() {
      this.enroll.next()
    },

    finish() {
      if (this.enroll.finish()) vibrate()
    },

    cancel() {
      this.enroll.cancel()
      removeFile(this.enroll.state.file)
      resetDisplay()
    },

    upload() {
      const st = this.enroll.state
      let env
      try {
        const audio = readFile(st.file)
        if (!audio) throw new Error('recording file is empty')
        env = buildEnvelope(this.enroll.uploadHeader(), audio)
      } catch (e) {
        this.enroll.fail(`read: ${errText(e)}`)
        return
      }
      if (!this.enroll.uploadStarted()) return
      this.request(env, { timeout: 300000, ...UPLOAD_OPTIONS })
        .then((r) => this.enroll.uploadFinished(r))
        .catch((e) => this.enroll.uploadFinished({ ok: false, error: `ble: ${errText(e)}` }))
        .finally(() => {
          if (this.enroll.state.phase === 'done') {
            removeFile(st.file)
            resetDisplay()
            vibrate()
          }
        })
    },
  }),
)
