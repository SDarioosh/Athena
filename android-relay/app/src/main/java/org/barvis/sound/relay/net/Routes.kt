package org.barvis.sound.relay.net

/**
 * Where the relay sends the watch's audio and the T5's module reads.
 *
 * - [DIRECT]: the odysseus-sound server itself (`/v1/…`, its `CONVO_INGEST_TOKEN`); `/health`
 *   needs no token there.
 * - [ODYSSEUS]: through Odysseus after pairing (`/api/codex/sound/…`, the phone's `ody_…`
 *   token with `sound:ingest`, `sound:read`, `sound:write`). Odysseus forwards to the module's
 *   MCP server; its `health` needs the token and also checks that the module is reachable.
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
            name = "odysseus-sound",
        )
        val ODYSSEUS = Routes(
            prefix = "/api/codex/sound",
            health = "/api/codex/sound/health",
            healthNeedsToken = true,
            chunk = "/api/codex/sound/chunk",
            endSession = "/api/codex/sound/sessions/%s/end",
            tokenCheck = "/api/codex/sound/health",
            name = "Odysseus",
        )
    }
}
