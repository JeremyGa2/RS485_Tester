"""Auto-tests du testeur (sans matériel) : python -m unittest discover tests

Vérifie les calculs (CRC, timings, FTDI) et que chaque test détecte bien
les défauts injectés dans la liaison simulée.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rs485tester import core, engine  # noqa: E402
from rs485tester.simport import get_sim_link  # noqa: E402


def run(test_id, settings=None, **over):
    tdef = engine.TESTS_BY_ID[test_id]
    params = {p.key: engine.parse_param(p, p.default) for p in tdef.params}
    params.update(over)
    logs = []
    ctx = engine.TestContext("SIM-A", "SIM-B", settings or core.SerialSettings(baudrate=115200),
                             {}, lambda k, p: logs.append((k, p)), threading.Event())
    return engine.run_test(tdef, ctx, params), logs


def sim(**kw):
    prm = get_sim_link().params
    prm.latency_ms, prm.turnaround_ms, prm.ber, prm.loss_prob = 1.0, 0.5, 0.0, 0.0
    for k, v in kw.items():
        setattr(prm, k, v)


class TestCore(unittest.TestCase):
    def test_crc_modbus(self):
        # exemple de la spécification Modbus : 01 03 00 00 00 0A -> C5 CD
        self.assertEqual(core.modbus_request(1, 3, 0, 10), bytes.fromhex("01030000000AC5CD"))
        self.assertTrue(core.modbus_crc_ok(bytes.fromhex("01030000000AC5CD")))

    def test_crc_ccitt(self):
        self.assertEqual(core.crc16_ccitt(b"123456789"), 0x29B1)

    def test_timings(self):
        s = core.SerialSettings(baudrate=9600)
        self.assertAlmostEqual(s.max_bytes_per_s(), 960.0)
        self.assertAlmostEqual(s.char_time(), 10 / 9600)
        s2 = core.SerialSettings(baudrate=9600, parity="E", stopbits=2)
        self.assertEqual(s2.bits_per_char(), 12)

    def test_ftdi(self):
        self.assertEqual(core.ftdi_actual_baud(9600), 9600)
        self.assertEqual(core.ftdi_actual_baud(1100000), 1090909)
        self.assertEqual(core.ftdi_actual_baud(3000000), 3000000)
        self.assertEqual(core.ftdi_actual_baud(2500000), 2000000)

    def test_parse(self):
        self.assertEqual(core.parse_hex("01 03 0x0A,ff"), b"\x01\x03\x0a\xff")
        self.assertEqual(core.parse_ascii(r"AB\r\n\x55"), b"AB\r\n\x55")
        with self.assertRaises(ValueError):
            core.parse_hex("01 GG")

    def test_compare(self):
        c = core.compare(b"\x00\xFF\x55", b"\x01\xFF")
        self.assertFalse(c.ok)
        self.assertEqual(c.bit_errors, 1)
        self.assertEqual(c.missing, 1)

    def test_uart_bits(self):
        bits = core.uart_bits(0x55, core.SerialSettings(parity="E"))
        self.assertEqual([b for _, b in bits], [0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 1])

    def test_packet_checker(self):
        chk = engine.PacketChecker(16)
        stream = b"".join(engine.build_packet(i, 16) for i in range(10))
        bad = bytearray(stream)
        bad[25 * 3 + 12] ^= 0x10            # 1 bit faux dans la charge du paquet 3
        del bad[25 * 6 + 10]                # 1 octet perdu dans le paquet 6
        chk.feed(bytes(bad))
        self.assertEqual(chk.corrupt, 2)
        self.assertEqual(chk.ok, 8)


class TestSimulated(unittest.TestCase):
    def test_clean_link_all_pass(self):
        sim()
        for tid, over in [("connect", {}), ("patterns", {}), ("modbus", {}),
                          ("latency", {"samples": 30}), ("throughput", {"duration": 1.5}),
                          ("endurance", {"duration": 1.0})]:
            res, _ = run(tid, **over)
            self.assertEqual(res.status, engine.OK, f"{tid}: {res.summary}")

    def test_errors_detected(self):
        sim(ber=1e-3)
        res, _ = run("patterns")
        self.assertEqual(res.status, engine.NOK)
        res, _ = run("endurance", duration=1.0)
        self.assertEqual(res.status, engine.NOK)
        self.assertGreater(sum(x["bit_errors"] for x in res.measures["résultats"]), 0)
        sim()

    def test_baud_mismatch_detected(self):
        sim()
        a = core.open_port("SIM-A", core.SerialSettings(baudrate=9600))
        b = core.open_port("SIM-B", core.SerialSettings(baudrate=19200))
        core.send(a, b"hello", core.SerialSettings(baudrate=9600))
        rx, *_ = core.read_exact(b, 5, 0.2)
        a.close()
        b.close()
        self.assertNotEqual(rx, b"hello")

    def test_turnaround_found(self):
        # marges larges devant la gigue d'ordonnancement d'une machine chargée (plusieurs ms)
        sim(turnaround_ms=50.0, latency_ms=0.2)
        res, _ = run("turnaround", delays_ms=[100, 70, 20, 0], reps=5)
        self.assertEqual(res.status, engine.WARN, res.summary)
        self.assertEqual(res.measures["délai_min_fiable_ms"], 70)
        sim()

    def test_sweep(self):
        sim()
        res, _ = run("sweep", bauds=[9600, 115200, 921600])
        self.assertEqual(res.status, engine.OK, res.summary)

    def test_generator_verify(self):
        sim()
        events = []
        g = engine.Generator("SIM-A", "SIM-B", core.SerialSettings(baudrate=115200),
                             core.modbus_request(1, 3, 0, 10), 0.002, 100, lambda k, p: events.append((k, p)))
        g.start()
        g.thread.join(10)
        self.assertEqual(g.stats["sent"], 100)
        self.assertEqual(g.stats["ok"], 100, events[-5:])
        self.assertEqual(g.stats["bad"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
