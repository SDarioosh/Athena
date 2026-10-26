package org.barvis.sound.relay.store

import android.annotation.SuppressLint
import android.content.Context
import android.os.Build
import org.barvis.sound.relay.BuildConfig

/**
 * Keeps the last crash (exception types, messages and stack frames; no tokens, audio or
 * recordings) in private, non-backed-up storage, so the owner can copy it from the main
 * screen without adb. Shown once, then cleared.
 */
object CrashLog {
    private const val PREFS = "crash"
    private const val KEY = "last"

    @SuppressLint("ApplySharedPref") // the process is about to die: write now
    fun install(context: Context) {
        val app = context.applicationContext
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            try {
                app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                    .putString(KEY, format(thread.name, error)).commit()
            } catch (ignored: Throwable) {
                // Never let the crash report hide the crash.
            }
            previous?.uncaughtException(thread, error)
        }
    }

    /** The saved report, removed so it is shown once; null when there is none. */
    fun take(context: Context): String? {
        val p = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val text = p.getString(KEY, null) ?: return null
        p.edit().remove(KEY).apply()
        return text
    }

    fun format(threadName: String, error: Throwable): String = buildString {
        append("Athena Relay ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}), ")
        append("Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}), ${Build.MANUFACTURER} ${Build.MODEL}\n")
        append("thread $threadName\n")
        var e: Throwable? = error
        var depth = 0
        while (e != null && depth < 5) {
            append(if (depth == 0) "" else "Caused by: ").append(e.javaClass.name)
            e.message?.let { append(": ").append(it.take(300)) }
            append('\n')
            for (f in e.stackTrace.take(25)) append("  at ").append(f).append('\n')
            e = e.cause.takeIf { it !== e }
            depth++
        }
    }.take(12_000)
}
