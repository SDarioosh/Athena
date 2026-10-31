package org.athena.relay.net

import java.util.concurrent.ConcurrentHashMap

/**
 * Where the relay sends the watch's audio and the T5's module reads.
 *
 * - [DIRECT]: the Athena server itself (`/v1/…`, its `CONVO_INGEST_TOKEN`); `/health`
 *   needs no token there.
 * - [ODYSSEUS]: through Odysseus after pairing (`/api/codex/athena/…`, the phone's `ody_…`
 *   token with `athena:ingest`, `athena:read`, `athena:write`, or their old names `sound:*`,
 *   which a phone paired before them holds and the hub still accepts). Odysseus forwards to
 *   the module's MCP server; its `health` needs the token and also checks that the module is
 *   reachable. A hub from before the Athena routes has only `/api/codex/sound/…`:
 *   [HubRoutes] falls back to those.
 *
 * Both answer with the module's own JSON, so the rest of the relay doesn't change.
 */
data class Routes(
    /** Prefix of the module routes: `conversations`, `action-items`, `digests/…`. */
    val prefix: String,
    val health: String,
    val healthNeedsToken: Boolean,
    val chunk: String,
    /** Session end; `%s` is the session id. */
    val endSession: String,
    /** A cheap authenticated read for "Test connection". */
    val tokenCheck: String,
    /** The server's name in messages. */
    val name: String,
) {
    fun end(sessionId: String): String = endSession.replace("%s", sessionId)

    companion object {
        val DIRECT = Routes(
            prefix = "/v1",
            health = "/health",
            healthNeedsToken = false,
            chunk = "/v1/ingest/chunk",
            endSession = "/v1/sessions/%s/end",
            tokenCheck = "/v1/sessions?limit=1",
            name = "Athena",
        )
        /** The hub's relay routes, and their old name (every hub serves it, for good). */
        const val RELAY = "/api/codex/athena"
        const val OLD_RELAY = "/api/codex/sound"

        val ODYSSEUS = Routes(
            prefix = RELAY,
            health = "$RELAY/health",
            healthNeedsToken = true,
            chunk = "$RELAY/chunk",
            endSession = "$RELAY/sessions/%s/end",
            tokenCheck = "$RELAY/health",
            name = "Odysseus",
        )
    }
}

/**
 * Which relay routes each hub serves, remembered per hub URL while the app runs.
 *
 * A call under [Routes.RELAY] goes there first. A hub from before those routes answers 404,
 * and then the same call goes to [Routes.OLD_RELAY]. The first answer that shows a route
 * exists (2xx, or a 4xx other than 401, 404, 408, 429) decides which routes the hub has, so
 * later calls go straight there and no call is sent twice. Only a 404 falls back: 401, 403,
 * 5xx and network errors are answered as they are. A 404 from a hub already known to have the
 * new routes (an unknown conversation) is answered as it is too. Other paths are sent as they are.
 */
class HubRoutes {
    /** Hub base URL -> true: the old routes, false: the new ones. */
    private val known = ConcurrentHashMap<String, Boolean>()

    /** Whether [base] is known to have only the old routes (null: not known yet). */
    fun usesOld(base: String): Boolean? = known[base.trimEnd('/')]

    /**
     * Sends [path] with [send] (which gets the path actually used) and returns its answer;
     * [code] gives an answer's HTTP status, or null when there is none (a network error).
     */
    suspend fun <T> call(base: String, path: String, code: (T) -> Int?, send: suspend (String) -> T): T {
        if (!path.startsWith(Routes.RELAY + "/")) return send(path)
        val key = base.trimEnd('/')
        val old = Routes.OLD_RELAY + path.removePrefix(Routes.RELAY)
        when (known[key]) {
            true -> return send(old)
            false -> return send(path)
            null -> Unit
        }
        val first = send(path)
        val firstCode = code(first)
        if (firstCode != 404) {
            if (provesRoute(firstCode)) known[key] = false
            return first
        }
        val second = send(old)
        if (provesRoute(code(second))) known[key] = true
        return second
    }

    private fun provesRoute(code: Int?): Boolean =
        code != null && code in 200..499 && code != 401 && code != 404 && code != 408 && code != 429

    companion object {
        /** The app's one memory: uploaders and API clients are made per connection. */
        val SHARED = HubRoutes()
    }
}
