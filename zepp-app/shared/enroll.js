/*
 * Guided voice-profile recording (pure logic, unit-tested in Node).
 *
 * The owner reads short prompts aloud for about 60-90 s while the watch records one
 * Opus file through the same microphone as daily recordings. The phone then uploads it
 * to POST /v1/speakers/enroll.json with is_owner=true, so the server recognises the
 * wearer's voice as it sounds on this watch.
 *
 * Phases: idle -> recording -> recorded -> uploading -> done | error.
 */

// Varied sounds, numbers, names and questions, so the profile covers how the owner
// really talks. Read at a normal pace; each takes about 10-15 s.
export const PROMPTS = [
  'Hi, this is my voice profile. I am recording it on my watch, so my assistant knows when I am the one talking.',
  'On Tuesday I walked to the market, bought apples, fresh bread and a jar of honey, and was home again by half past ten.',
  'Could you call me back tomorrow morning? My number ends in four, seven, two, nine, and I am free after eight thirty.',
  'The quick brown fox jumps over the lazy dog, while six bright yellow birds sing loudly in the old oak tree.',
  'We should move the project meeting to Thursday. Sarah, Jonas and Priya can join, but Michael is still on holiday.',
  'Honestly, I think that went really well! What do you think? Should we try it again next week, or wait a little?',
  'Numbers and dates: the fifteenth of March, twenty twenty six, at three fifteen in the afternoon, for ninety minutes.',
  'Thanks, that is everything. I am finishing the recording now.',
]

export const MIN_MS = 45_000 // the server needs a solid amount of speech; Done waits until then
export const TARGET_MS = 75_000
export const MAX_MS = 120_000 // stops on its own after this

export function enrollFileName(nowMs) {
  return `enroll-${nowMs}.opus`
}

export function createEnrollment(deps) {
  const { recorder, now, setTimeout, clearTimeout, onChange = () => {} } = deps
  const state = {
    phase: 'idle', prompt: 0, file: null, startedAt: 0, stoppedAt: 0, error: null,
    result: null,
  }
  let autoStop = null

  const changed = () => onChange(state)

  function fail(msg) {
    if (autoStop) clearTimeout(autoStop)
    autoStop = null
    if (state.phase === 'recording') {
      try {
        recorder.stop()
      } catch (e) {}
    }
    state.phase = 'error'
    state.error = msg
    changed()
  }

  return {
    state,

    elapsedMs() {
      if (state.phase === 'recording') return now() - state.startedAt
      return state.stoppedAt - state.startedAt
    },

    canFinish() {
      return state.phase === 'recording' && this.elapsedMs() >= MIN_MS
    },

    promptText() {
      return PROMPTS[state.prompt]
    },

    start() {
      if (state.phase === 'recording' || state.phase === 'uploading') return
      state.file = enrollFileName(now())
      state.prompt = 0
      state.error = null
      state.result = null
      try {
        recorder.start(state.file)
      } catch (e) {
        fail(`start: ${e && e.message ? e.message : e}`)
        return
      }
      state.phase = 'recording'
      state.startedAt = now()
      autoStop = setTimeout(() => this.finish(true), MAX_MS)
      changed()
    },

    next() {
      if (state.phase !== 'recording') return
      state.prompt = Math.min(state.prompt + 1, PROMPTS.length - 1)
      changed()
    },

    // force: the max-duration timer, which finishes even before MIN_MS (it can't happen).
    finish(force = false) {
      if (state.phase !== 'recording') return false
      if (!force && !this.canFinish()) return false
      if (autoStop) clearTimeout(autoStop)
      autoStop = null
      try {
        recorder.stop()
      } catch (e) {
        fail(`stop: ${e && e.message ? e.message : e}`)
        return false
      }
      state.stoppedAt = now()
      state.phase = 'recorded'
      changed()
      return true
    },

    cancel() {
      if (autoStop) clearTimeout(autoStop)
      autoStop = null
      if (state.phase === 'recording') {
        try {
          recorder.stop()
        } catch (e) {}
      }
      state.phase = 'idle'
      state.prompt = 0
      changed()
    },

    // Header of the envelope sent to the phone; the phone adds the owner's name.
    uploadHeader() {
      return { kind: 'enroll', is_owner: true, codec: 'opus',
               duration_ms: state.stoppedAt - state.startedAt }
    },

    uploadStarted() {
      if (state.phase !== 'recorded') return false
      state.phase = 'uploading'
      changed()
      return true
    },

    uploadFinished(result) {
      if (state.phase !== 'uploading') return
      if (result && result.ok) {
        state.phase = 'done'
        state.result = result
      } else {
        // keep the recording so the upload can be retried
        state.phase = 'recorded'
        state.error = (result && result.error) || 'no answer from the phone'
      }
      changed()
    },

    fail,
  }
}
