package org.athena.relay.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** docs/twatch-ble-protocol.md §8: encode and decode must match byte for byte. */
class ProtocolVectorsTest {
    private fun h(s: String) = hexToBytes(s)
    private val session = h("3f9a0c11d2e4b507")

    @Test fun listCommand() {
        assertArrayEquals(h("01 00000000 2000"), Command.List(0, 32).encode())
        assertEquals(Command.List(0, 32), Command.decode(h("01 00000000 2000")))
    }

    @Test fun readCommand() {
        assertArrayEquals(h("02 07000000 00000000 00000000"), Command.Read(7, 0, 0).encode())
        assertEquals(Command.Read(7, 0, 0), Command.decode(h("02 07000000 00000000 00000000")))
    }

    @Test fun ackCommand() {
        assertArrayEquals(h("03 07000000"), Command.Ack(7).encode())
    }

    @Test fun timeCommand() {
        assertEquals(1792411200000L, java.time.Instant.parse("2026-10-19T12:00:00Z").toEpochMilli())
        assertArrayEquals(h("04 000000 006a0854a1010000"), Command.Time(1792411200000L).encode())
        assertEquals(Command.Time(1792411200000L), Command.decode(h("04 000000 006a0854a1010000")))
    }

    @Test fun byeCommand() {
        assertArrayEquals(h("05"), Command.Bye.encode())
    }

    @Test fun entry() {
        val hex = "81 01 0000 07000000 3f9a0c11d2e4b507 02000000 60540954a1010000 34300000 68600000 2639f4cb"
        val e = ChunkEntry(ChunkEntry.FLAG_LAST, 7, session, 2, 1792411260000L, 12340, 24680, 0xcbf43926L)
        assertArrayEquals(h(hex), e.encode())
        val d = (Response.decode(h(hex)) as Response.Entry).entry
        assertEquals(e, d)
        assertEquals("3f9a0c11d2e4b507", d.sessionHex)
        assertTrue(d.isLast)
        assertFalse(d.timeUnset)
        assertFalse(d.isNote)
        assertEquals(0xcbf43926L, d.crc32)
    }

    /** NOTE ENTRY vector: chunk 8, LAST+NOTE, seq 0 (a voice note's only chunk). */
    @Test fun noteEntry() {
        val hex = "81 05 0000 08000000 3f9a0c11d2e4b507 00000000 60540954a1010000 34300000 68600000 2639f4cb"
        val e = ChunkEntry(ChunkEntry.FLAG_LAST or ChunkEntry.FLAG_NOTE, 8, session, 0, 1792411260000L,
            12340, 24680, 0xcbf43926L)
        assertArrayEquals(h(hex), e.encode())
        val d = (Response.decode(h(hex)) as Response.Entry).entry
        assertEquals(e, d)
        assertTrue(d.isNote)
        assertTrue(d.isLast)
        assertFalse(d.timeUnset)
        assertEquals("chunk 8 (session 3f9a0c11d2e4b507 seq 0, 24680 B, 12340 ms, last, note)", d.toString())
    }

    @Test fun data() {
        val r = Response.decode(h("82 07000000 f9010000 aabb")) as Response.Data
        assertEquals(7L, r.chunkId)
        assertEquals(505L, r.offset)
        assertArrayEquals(h("aabb"), r.payload)
        assertArrayEquals(h("82 07000000 f9010000 aabb"), Response.Data(7, 505, h("aabb")).encode())
    }

    @Test fun readEnd() {
        val hex = "83 00 0000 07000000 68600000 2639f4cb"
        assertEquals(Response.ReadEnd(0, 7, 24680, 0xcbf43926L), Response.decode(h(hex)))
        assertArrayEquals(h(hex), Response.ReadEnd(0, 7, 24680, 0xcbf43926L).encode())
    }

    @Test fun acked() {
        assertEquals(Response.Acked(0, 7), Response.decode(h("84 00 0000 07000000")))
        assertArrayEquals(h("84 00 0000 07000000"), Response.Acked(0, 7).encode())
    }

    @Test fun listEnd() {
        assertEquals(Response.ListEnd(3, 5), Response.decode(h("85 00 0300 05000000")))
        assertArrayEquals(h("85 00 0300 05000000"), Response.ListEnd(3, 5).encode())
    }

    @Test fun timeOk() {
        assertEquals(Response.TimeOk(2), Response.decode(h("86 00 0200")))
        assertArrayEquals(h("86 00 0200"), Response.TimeOk(2).encode())
    }

    @Test fun byeOk() {
        assertEquals(Response.ByeOk, Response.decode(h("87")))
    }

    @Test fun error() {
        val r = Response.decode(h("ee 02 03 00 09000000")) as Response.Error
        assertEquals(Cmd.READ, r.cmd)
        assertEquals(ErrorCode.NOT_FOUND, r.code)
        assertEquals(9L, r.arg)
        assertArrayEquals(h("ee 02 03 00 09000000"), Response.Error(Cmd.READ, ErrorCode.NOT_FOUND, 9).encode())
    }

    @Test fun status() {
        val hex = "01 25 51 00 6e0f 0200 c0d40100 60ea0000 4006d66a 04030201"
        val s = WatchStatus.decode(h(hex))
        assertEquals(1, s.proto)
        assertTrue(s.recording)
        assertFalse(s.muted)
        assertTrue(s.timeValid)
        assertTrue(s.sdOk)
        assertFalse(s.charging)
        assertEquals(81, s.batteryPercent)
        assertEquals(3950, s.batteryMv)
        assertEquals(2, s.queuedChunks)
        assertEquals(120000L, s.queuedBytes)
        assertEquals(60000L, s.queuedMs)
        assertEquals(1792411200L, s.clockEpochS)
        assertEquals(0x01020304L, s.bootId)
        assertArrayEquals(h(hex), s.encode())
    }

    @Test fun statusUnknownBattery() {
        val b = h("01 00 ff 00 0000 0000 00000000 00000000 00000000 00000000")
        assertEquals(-1, WatchStatus.decode(b).batteryPercent)
    }

    @Test fun info() {
        val i = WatchInfo.parse("proto=1;device=twatch-ultra;fw=0.2.0;board=twatch_ultra")
        assertEquals(1, i.proto)
        assertEquals("twatch-ultra", i.device)
        assertEquals("0.2.0", i.fw)
        assertEquals("twatch_ultra", i.board)
        assertEquals(null, i.reset)
        assertEquals("task_wdt", WatchInfo.parse("proto=1;device=d;reset=task_wdt").reset) // §4.1
        val j = WatchInfo.parse("fw=1;future=x=y;proto=1;device=d;")
        assertEquals(1, j.proto)
        assertEquals("d", j.device)
    }

    @Test fun crc32MatchesZlib() {
        assertEquals(0xcbf43926L, crc32("123456789".toByteArray()))
    }

    @Test fun longerFramesStillDecode() {
        // §9: fields may be appended without a proto bump.
        val e = h("81 01 0000 07000000 3f9a0c11d2e4b507 02000000 60540954a1010000 34300000 68600000 2639f4cb 00ff")
        assertEquals(7L, (Response.decode(e) as Response.Entry).entry.chunkId)
    }

    @Test fun u32HighBit() {
        val e = ChunkEntry(0, 0xFFFFFFF0L, session, 0xFFFFFFFFL, 0, 0, 0xFFFFFFFFL, 0xFFFFFFFFL)
        assertEquals(e, ChunkEntry.decode(e.encode()))
    }

    @Test(expected = ProtocolException::class)
    fun shortFrameRejected() {
        Response.decode(h("84 00 00"))
    }
}
