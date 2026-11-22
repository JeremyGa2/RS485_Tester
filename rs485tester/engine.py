"""Moteur de tests RS485, sans interface graphique.

Chaque test reçoit un TestContext (ports, réglages, journal, arrêt) et renvoie
un TestResult. Les échanges avec l'interface passent par ctx.emit(), appelé
depuis le thread de test ; l'interface les dépile dans son propre thread.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import serial

from . import core
from .core import (SerialSettings, compare, diagnose, drain, fmt_rate, fmt_time,
                   read_exact, rx_timeout, send, to_hex)

OK, NOK, WARN, ERROR, ABORTED = "OK", "NOK", "ATTENTION", "ERREUR", "ANNULÉ"


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------
@dataclass
class Param:
    key: str
    label: str
    kind: str            # int, float, bool, choice, floatlist, intlist
    default: object
    help: str = ""
    choices: Tuple[str, ...] = ()
    vmin: Optional[float] = None
    vmax: Optional[float] = None


@dataclass
class TestResult:
    test_id: str
    title: str
    status: str = OK
    summary: str = ""
    details: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    measures: Dict[str, object] = field(default_factory=dict)
    started: float = 0.0
    duration: float = 0.0
    settings: str = ""


@dataclass
class TestDef:
    test_id: str
    title: str
    short: str
    diagram: str
    explanation: str
    oscillo: str
    params: List[Param]
    func: Callable


class StopTest(Exception):
    pass


class TestContext:
    def __init__(self, port_a: str, port_b: str, settings: SerialSettings,
                 infos: Dict[str, core.PortInfo], emit: Callable, stop_event: threading.Event):
        self.port_a = port_a
        self.port_b = port_b
        self.s = settings
        self.infos = infos
        self._emit = emit
        self.stop_event = stop_event

    # -- communication avec l'interface --------------------------------
    def emit(self, kind: str, payload=None):
        self._emit(kind, payload)

    def log(self, msg: str, level: str = "info"):
        self._emit("log", (msg, level))

    def progress(self, frac: float, text: str = ""):
        self._emit("progress", (max(0.0, min(1.0, frac)), text))

    def check_stop(self):
        if self.stop_event.is_set():
            raise StopTest()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    # -- ports -----------------------------------------------------------
    def open_pair(self, s: Optional[SerialSettings] = None):
        s = s or self.s
        a = core.open_port(self.port_a, s)
        try:
            b = core.open_port(self.port_b, s)
        except Exception:
            a.close()
            raise
        return a, b

    def names(self, direction: str) -> Tuple[str, str]:
        return (self.port_a, self.port_b) if direction == "AB" else (self.port_b, self.port_a)


def _dirs(choice: str) -> List[str]:
    return {"A→B et B→A": ["AB", "BA"], "A→B": ["AB"], "B→A": ["BA"]}.get(choice, ["AB", "BA"])


def _arrow(d: str) -> str:
    return "A→B" if d == "AB" else "B→A"


DIR_CHOICES = ("A→B et B→A", "A→B", "B→A")


# ---------------------------------------------------------------------------
# Échange élémentaire avec vérification
# ---------------------------------------------------------------------------
def exchange(ctx: TestContext, src, dst, data: bytes, s: SerialSettings,
             label: str = "", verbose: bool = True, check_extra: bool = True):
    src.reset_input_buffer()
    dst.reset_input_buffer()
    t0 = send(src, data, s)
    rx, tf, tl = read_exact(dst, len(data), rx_timeout(s, len(data)), ctx.stop_event)
    if check_extra and len(rx) == len(data):
        rx += drain(dst, idle=max(0.005, 3 * s.char_time()), max_time=0.05)
    cmp = compare(data, rx)
    if verbose:
        ctx.log(f"  {label} TX {len(data):4d} o : {to_hex(data, 32)}", "tx")
        ctx.log(f"  {label} RX {len(rx):4d} o : {to_hex(rx, 32) if rx else '(rien)'}", "rx")
        if cmp.ok:
            ctx.log(f"  ✔ {cmp.summary()}", "ok")
        else:
            ctx.log(f"  ✘ {cmp.summary()}", "err")
            for i, e, r in cmp.diffs[:6]:
                ctx.log(f"     index {i:4d} : attendu {e:02X} ({e:08b})  reçu {r:02X} ({r:08b})  "
                        f"bits faux {bin(e ^ r).count('1')}", "err")
    return cmp, rx, t0, tf, tl


def _fail_tips(ctx, cmp, d):
    for t in diagnose(cmp, _arrow(d)):
        ctx.log("    " + t, "warn")


# ---------------------------------------------------------------------------
# 1. Connectivité
# ---------------------------------------------------------------------------
def test_connectivity(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("connect", "Connectivité A↔B")
    s = ctx.s
    mask = (1 << s.bytesize) - 1
    frame = core.modbus_frame(bytes(b & mask for b in b"\x55\xAA\x00\xFF\x0F\xF0RS485"))
    if s.bytesize < 8:
        frame = bytes(b & mask for b in frame)
    reps = int(p["reps"])
    r.columns = ["Sens", "Essai", "Résultat", "Envoyé", "Reçu"]
    a, b = ctx.open_pair()
    try:
        ok_all = True
        dirs = _dirs(p["dirs"])
        total = reps * len(dirs)
        k = 0
        for d in dirs:
            src, dst = (a, b) if d == "AB" else (b, a)
            ctx.log(f"Sens {_arrow(d)} ({ctx.names(d)[0]} → {ctx.names(d)[1]})", "title")
            for i in range(reps):
                ctx.check_stop()
                cmp, rx, *_ = exchange(ctx, src, dst, frame, s, _arrow(d))
                r.rows.append([_arrow(d), str(i + 1), "OK" if cmp.ok else "NOK", to_hex(frame, 16), to_hex(rx, 16) or "—"])
                if not cmp.ok:
                    ok_all = False
                    _fail_tips(ctx, cmp, d)
                k += 1
                ctx.progress(k / total)
                time.sleep(0.01)
        r.status = OK if ok_all else NOK
        r.summary = ("Liaison établie dans " + ("les deux sens" if len(dirs) == 2 else "le sens testé")
                     if ok_all else "Au moins un échange a échoué : voir les pistes de dépannage.")
    finally:
        a.close()
        b.close()
    return r


# ---------------------------------------------------------------------------
# 2. Intégrité / transparence des motifs
# ---------------------------------------------------------------------------
def test_patterns(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("patterns", "Intégrité des données (motifs de stress)")
    s = ctx.s
    pats = core.stress_patterns(s.bytesize)
    dirs = _dirs(p["dirs"])
    reps = int(p["reps"])
    r.columns = ["Motif", "Sens", "Octets", "Octets faux", "Bits faux", "Résultat"]
    total_bits = total_err = 0
    a, b = ctx.open_pair()
    try:
        steps = len(pats) * len(dirs) * reps
        k = 0
        bad = 0
        for name, data, why in pats:
            ctx.log(f"Motif « {name} » — {why}", "title")
            for d in dirs:
                src, dst = (a, b) if d == "AB" else (b, a)
                for _ in range(reps):
                    ctx.check_stop()
                    cmp, *_ = exchange(ctx, src, dst, data, s, _arrow(d))
                    total_bits += len(data) * s.bytesize
                    total_err += cmp.bit_errors + 8 * (cmp.missing + cmp.extra)
                    if not cmp.ok:
                        bad += 1
                    r.rows.append([name, _arrow(d), f"{cmp.received}/{cmp.sent}", str(cmp.byte_errors),
                                   str(cmp.bit_errors), "OK" if cmp.ok else "NOK"])
                    k += 1
                    ctx.progress(k / steps, name)
        r.measures = {"bits_testés": total_bits, "bits_faux": total_err}
        r.status = OK if bad == 0 else NOK
        r.summary = (f"{k} transferts, {total_bits} bits vérifiés, aucune erreur." if bad == 0 else
                     f"{bad}/{k} transferts en erreur ({total_err} bits faux sur {total_bits}).")
    finally:
        a.close()
        b.close()
    return r


# ---------------------------------------------------------------------------
# 3. Trames Modbus RTU typiques
# ---------------------------------------------------------------------------
def test_modbus(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("modbus", "Trames Modbus RTU typiques")
    s = ctx.s
    if s.bytesize != 8:
        r.status = ERROR
        r.summary = "Modbus RTU impose 8 bits de données."
        return r
    frames = core.typical_modbus_frames()
    dirs = _dirs(p["dirs"])
    r.columns = ["Trame", "Sens", "Octets", "CRC reçu", "Identique", "Résultat"]
    a, b = ctx.open_pair()
    try:
        bad = 0
        k = 0
        steps = len(frames) * len(dirs)
        t35 = s.modbus_t35()
        ctx.log(f"Silence inter-trame Modbus t3,5 = {fmt_time(t35)} respecté entre chaque trame.", "dim")
        for name, fr in frames:
            ctx.log(f"{name}", "title")
            for d in dirs:
                ctx.check_stop()
                src, dst = (a, b) if d == "AB" else (b, a)
                core.precise_sleep(t35)
                cmp, rx, *_ = exchange(ctx, src, dst, fr, s, _arrow(d))
                crc_ok = core.modbus_crc_ok(rx)
                good = cmp.ok and crc_ok
                bad += 0 if good else 1
                r.rows.append([name, _arrow(d), f"{len(rx)}/{len(fr)}", "valide" if crc_ok else "INVALIDE",
                               "oui" if cmp.ok else "NON", "OK" if good else "NOK"])
                if not good:
                    _fail_tips(ctx, cmp, d)
                k += 1
                ctx.progress(k / steps, name)
        r.status = OK if bad == 0 else NOK
        r.summary = (f"{k} trames Modbus transmises à l'identique, CRC valides." if bad == 0
                     else f"{bad}/{k} trames altérées.")
    finally:
        a.close()
        b.close()
    return r


# ---------------------------------------------------------------------------
# 4. Retournement (half-duplex) : balayage du délai de réponse
# ---------------------------------------------------------------------------
def test_turnaround(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("turnaround", "Temps de retournement (aller-retour half-duplex)")
    s = ctx.s
    delays = sorted({float(x) for x in p["delays_ms"]}, reverse=True)
    reps = int(p["reps"])
    n = int(p["length"])
    mask = (1 << s.bytesize) - 1
    r.columns = ["Délai demandé", "Réaction PC mesurée (méd.)", "Aller OK", "Retour OK", "Résultat"]
    ctx.log(f"Trame aller {n} o ({fmt_time(s.wire_time(n))} sur la ligne), {reps} essais par délai.", "dim")
    a, b = ctx.open_pair()
    best = None
    try:
        steps = len(delays) * reps
        k = 0
        for dms in delays:
            ok_go = ok_back = 0
            reactions = []
            for i in range(reps):
                ctx.check_stop()
                # avant chaque essai : attendre au moins le plus long délai testé, pour que les modules
                # aient relâché le bus (activité précédente) et que seul le retour soit mesuré
                core.precise_sleep(max(delays) / 1000.0)
                q = bytes((0xA0 + j + i) & mask for j in range(n))
                resp = bytes((~x) & mask for x in q)
                a.reset_input_buffer()
                b.reset_input_buffer()
                send(a, q, s)
                rx, tf, tl = read_exact(b, n, rx_timeout(s, n), ctx.stop_event)
                if rx == q:
                    ok_go += 1
                    target = tl + dms / 1000.0
                    core.precise_sleep(target - time.perf_counter())
                    t_tx = send(b, resp, s)
                    reactions.append(t_tx - tl)
                    rr, _, _ = read_exact(a, n, rx_timeout(s, n), ctx.stop_event)
                    if rr == resp:
                        ok_back += 1
                drain(a, idle=0.005, max_time=0.05)
                drain(b, idle=0.005, max_time=0.05)
                k += 1
                ctx.progress(k / steps, f"délai {dms} ms")
            med = core.stats(reactions).get("median") if reactions else None
            good = ok_go == reps and ok_back == reps
            if good:
                best = dms
            r.rows.append([f"{dms:g} ms", fmt_time(med) if med is not None else "—",
                           f"{ok_go}/{reps}", f"{ok_back}/{reps}", "OK" if good else "NOK"])
            ctx.log(f"Délai {dms:>6g} ms : aller {ok_go}/{reps}, retour {ok_back}/{reps}"
                    + (f", réaction PC médiane {fmt_time(med)}" if med is not None else ""),
                    "ok" if good else "err")
        r.measures["délai_min_fiable_ms"] = best
        all_ok = all(row[-1] == "OK" for row in r.rows)
        if best is None:
            r.status = NOK
            r.summary = "Aucun délai ne fonctionne de façon fiable : vérifier la liaison (test de connectivité)."
        elif all_ok:
            r.status = OK
            r.summary = (f"Tous les délais passent, jusqu'à {min(delays):g} ms. Le module bascule plus vite "
                         "que ce que le PC sait produire : mesurez le temps réel à l'oscilloscope.")
        else:
            # un délai plus court qu'un délai qui échoue peut passer par chance : on prend
            # le plus petit délai au-dessus duquel TOUT passe
            reliable = None
            for row, dms in zip(r.rows, delays):
                if row[-1] == "OK":
                    reliable = dms
                else:
                    break
            r.measures["délai_min_fiable_ms"] = reliable
            r.status = WARN if reliable is not None else NOK
            r.summary = (f"Délai de réponse minimal fiable côté PC : {reliable:g} ms. En dessous, la réponse "
                         "du port B est perdue ou corrompue (module encore en émission)." if reliable is not None
                         else "Le délai le plus long échoue déjà : liaison instable.")
        ctx.log("Le délai est appliqué par le PC après réception du dernier octet. Le silence réel sur la ligne "
                "vaut ce délai + les latences USB (≈ 1 ms par adaptateur avec latency timer à 1 ms). "
                "Mesure exacte à l'oscilloscope : fin du bit de stop aller → bit de start retour.", "dim")
    finally:
        a.close()
        b.close()
    return r


# ---------------------------------------------------------------------------
# 5. Balayage des débits
# ---------------------------------------------------------------------------
def test_baud_sweep(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("sweep", "Balayage des débits")
    bauds = [int(x) for x in p["bauds"]]
    dirs = _dirs(p["dirs"])
    ia = ctx.infos.get(ctx.port_a)
    ib = ctx.infos.get(ctx.port_b)
    r.columns = ["Débit demandé", "Durée bit", "Réel estimé A", "Réel estimé B", "Résultat", "Détail"]
    mask = (1 << ctx.s.bytesize) - 1
    payload = bytes(b & mask for b in bytes(range(256)) + b"\x55" * 32 + core.prbs15(64, 0x2A2A))
    n_ok = n_bad = n_uns = 0
    for k, baud in enumerate(bauds):
        ctx.check_stop()
        s = ctx.s.copy(baudrate=baud)
        real_a, note_a = core.baud_note(baud, ia)
        real_b, note_b = core.baud_note(baud, ib)
        ctx.log(f"{baud} bauds — bit = {fmt_time(s.bit_time())}, {len(payload)} o = {fmt_time(s.wire_time(len(payload)))}",
                "title")
        try:
            a, b = ctx.open_pair(s)
        except (serial.SerialException, ValueError, OSError) as e:
            n_uns += 1
            r.rows.append([str(baud), fmt_time(s.bit_time()), "—", "—", "NON SUPPORTÉ", str(e)[:60]])
            ctx.log(f"  Débit refusé par le driver : {e}", "warn")
            continue
        try:
            res = []
            for d in dirs:
                src, dst = (a, b) if d == "AB" else (b, a)
                cmp, *_ = exchange(ctx, src, dst, payload, s, _arrow(d), verbose=False)
                res.append((d, cmp))
                ctx.log(f"  {_arrow(d)} : {cmp.summary()}", "ok" if cmp.ok else "err")
            good = all(c.ok for _, c in res)
            n_ok += good
            n_bad += not good
            detail = "; ".join(f"{_arrow(d)} {c.summary()}" for d, c in res if not c.ok)
            if real_a and real_b and real_a != real_b:
                detail = (detail + "; " if detail else "") + "écart de débit entre adaptateurs"
            r.rows.append([str(baud), fmt_time(s.bit_time()), _short(note_a, real_a), _short(note_b, real_b),
                           "OK" if good else "NOK", detail or "identique"])
        except (serial.SerialException, OSError) as e:
            n_bad += 1
            r.rows.append([str(baud), fmt_time(s.bit_time()), "—", "—", "ERREUR", str(e)[:60]])
        finally:
            a.close()
            b.close()
        ctx.progress((k + 1) / len(bauds), f"{baud} bauds")
    r.measures = {"ok": n_ok, "nok": n_bad, "non_supportés": n_uns}
    r.status = OK if n_bad == 0 and n_ok else (NOK if n_bad else WARN)
    r.summary = f"{n_ok} débit(s) OK, {n_bad} en échec, {n_uns} non supporté(s) par les adaptateurs."
    r.details.append("Attention : si les deux adaptateurs arrondissent le débit de la même façon, le test réussit "
                     "même si la ligne ne tourne pas exactement au débit demandé. Vérifiez la durée d'un bit à "
                     "l'oscilloscope (générateur continu, motif 0x55).")
    ctx.log(r.details[-1], "dim")
    return r


def _short(note: str, real) -> str:
    if real is None:
        return "inconnu"
    if note == "simulé":
        return str(real)
    return note.replace("FTDI ≈ ", "")


# ---------------------------------------------------------------------------
# 6. Latence
# ---------------------------------------------------------------------------
def test_latency(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("latency", "Latence de bout en bout")
    s = ctx.s
    n = int(p["samples"])
    size = int(p["size"])
    limit = float(p["max_ms"])
    dirs = _dirs(p["dirs"])
    mask = (1 << s.bytesize) - 1
    data = bytes(b & mask for b in core.prbs15(size, 0x1357))
    wire = s.wire_time(size)
    r.columns = ["Sens", "Échantillons", "Min", "Médiane", "P95", "Max", "Écart-type", "Surcoût min"]
    a, b = ctx.open_pair()
    try:
        all_lat = {}
        lost = 0
        for di, d in enumerate(dirs):
            src, dst = (a, b) if d == "AB" else (b, a)
            lat = []
            ctx.log(f"Sens {_arrow(d)} : {n} mesures de {size} o (durée ligne théorique {fmt_time(wire)})", "title")
            for i in range(n):
                ctx.check_stop()
                dst.reset_input_buffer()
                t0 = send(src, data, s)
                rx, tf, tl = read_exact(dst, size, rx_timeout(s, size), ctx.stop_event)
                if rx == data and tl is not None:
                    lat.append(tl - t0)
                else:
                    lost += 1
                    drain(dst, 0.005, 0.05)
                core.precise_sleep(0.002)
                if i % 5 == 0:
                    ctx.progress((di * n + i + 1) / (n * len(dirs)), f"{_arrow(d)} {i + 1}/{n}")
            all_lat[d] = lat
            st = core.stats(lat)
            if st:
                r.rows.append([_arrow(d), f"{st['n']}/{n}", fmt_time(st["min"]), fmt_time(st["median"]),
                               fmt_time(st["p95"]), fmt_time(st["max"]), fmt_time(st["std"]),
                               fmt_time(st["min"] - wire)])
                ctx.log(f"  min {fmt_time(st['min'])}  méd {fmt_time(st['median'])}  p95 {fmt_time(st['p95'])}  "
                        f"max {fmt_time(st['max'])}  σ {fmt_time(st['std'])}", "ok")
                ctx.emit("histogram", {"title": f"Latence {_arrow(d)}", "values_ms": [x * 1000 for x in lat],
                                       "wire_ms": wire * 1000})
        meds = [core.stats(v)["median"] for v in all_lat.values() if v]
        r.measures = {"perdus": lost, "durée_ligne_s": wire}
        if not meds:
            r.status = NOK
            r.summary = "Aucune réponse reçue."
            return r
        overhead = max(meds) - wire
        if 0.012 < overhead < 0.020:
            ctx.log("Surcoût ≈ 16 ms : c'est le « Latency Timer » FTDI par défaut. Gestionnaire de périphériques → "
                    "Port COM → Paramètres → Avancé → Latency Timer = 1 ms.", "warn")
            r.details.append("Surcoût proche de 16 ms : Latency Timer FTDI à régler sur 1 ms.")
        worst = max(max(v) for v in all_lat.values() if v)
        if lost:
            r.status = NOK
            r.summary = f"{lost} message(s) perdus ou altérés."
        elif limit > 0 and worst * 1000 > limit:
            r.status = NOK
            r.summary = f"Latence max {fmt_time(worst)} > limite {limit:g} ms."
        else:
            r.status = OK
            r.summary = (f"Latence médiane {fmt_time(max(meds))} dont {fmt_time(wire)} de transmission ligne ; "
                         f"max {fmt_time(worst)}.")
        r.details.append("Latence = du write() côté PC à l'arrivée du dernier octet côté PC. Elle inclut la durée "
                         "de transmission ligne, les latences USB des deux adaptateurs et celle des modules. "
                         "Mesure par horloge haute résolution (perf_counter, résolution < 1 µs).")
    finally:
        a.close()
        b.close()
    return r


# ---------------------------------------------------------------------------
# 7/8. Flux continu par paquets : débit soutenu et endurance / BER
# ---------------------------------------------------------------------------
SYNC = b"\xA5\x5A"
PKT_OVERHEAD = 9   # sync(2) + seq(4) + len(1) + crc(2)


def build_packet(seq: int, plen: int) -> bytes:
    body = (seq & 0xFFFFFFFF).to_bytes(4, "big") + bytes([plen]) + core.seeded_payload(seq, plen)
    return SYNC + body + core.crc16_modbus(body).to_bytes(2, "little")


class PacketChecker:
    """Analyse un flux de paquets reçus ; résiste aux pertes et à la corruption."""

    def __init__(self, plen: int):
        self.plen = plen
        self.L = plen + PKT_OVERHEAD
        self.buf = bytearray()
        self.ok = self.corrupt = self.dup = 0
        self.bit_errors = 0
        self.bits_checked = 0
        self.garbage = 0
        self.expected = 0
        self.rx_bytes = 0
        self.last_errors: List[str] = []

    def feed(self, data: bytes):
        self.rx_bytes += len(data)
        self.buf += data
        L = self.L
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                keep = 1 if self.buf[-1:] == SYNC[:1] else 0
                self.garbage += len(self.buf) - keep
                del self.buf[:len(self.buf) - keep]
                return
            if i:
                self.garbage += i
                del self.buf[:i]
            if len(self.buf) < L:
                return
            pkt = bytes(self.buf[:L])
            body = pkt[2:-2]
            if core.crc16_modbus(body) == int.from_bytes(pkt[-2:], "little") and body[4] == self.plen:
                seq = int.from_bytes(body[:4], "big")
                if seq >= self.expected:
                    self.ok += 1
                    self.expected = seq + 1
                else:
                    self.dup += 1
                self.bits_checked += L * 8
                del self.buf[:L]
                continue
            # paquet faux : tronqué (un autre SYNC à l'intérieur) ou bits altérés
            j = self.buf.find(SYNC, 2, L + 1)
            seq = int.from_bytes(body[:4], "big")
            ref_seq = seq if self.expected <= seq < self.expected + 64 else self.expected
            ref = build_packet(ref_seq, self.plen)
            if j > 0:
                self.corrupt += 1
                self._note(f"paquet #{ref_seq} tronqué ({j}/{L} octets)")
                self.bit_errors += 8 * (L - j)
                self.bits_checked += L * 8
                del self.buf[:j]
            else:
                be = sum(bin(x ^ y).count("1") for x, y in zip(pkt, ref))
                self.bit_errors += be
                self.bits_checked += L * 8
                self.corrupt += 1
                self._note(f"paquet #{ref_seq} corrompu : {be} bit(s) faux")
                del self.buf[:L]
            self.expected = ref_seq + 1

    def _note(self, msg: str):
        self.last_errors.append(msg)
        if len(self.last_errors) > 50:
            self.last_errors.pop(0)


def _stream_one_dir(ctx, a, b, d, s, plen, gap_s, duration, pace) -> Dict:
    src, dst = (a, b) if d == "AB" else (b, a)
    checker = PacketChecker(plen)
    L = plen + PKT_OVERHEAD
    state = {"sent": 0, "sent_bytes": 0, "err": None, "done": False}
    stop_w = threading.Event()
    src.reset_input_buffer()
    dst.reset_input_buffer()
    t_start = time.perf_counter()

    def writer():
        seq = 0
        try:
            while not stop_w.is_set() and not ctx.stopped:
                if duration and time.perf_counter() - t_start >= duration:
                    break
                if pace:
                    ahead = state["sent_bytes"] * s.char_time() - (time.perf_counter() - t_start)
                    if ahead > 0.15:
                        time.sleep(min(ahead - 0.1, 0.05))
                        continue
                send(src, build_packet(seq, plen), s)
                seq += 1
                state["sent"] = seq
                state["sent_bytes"] += L
                if gap_s > 0:
                    core.precise_sleep(gap_s)
        except Exception as e:     # noqa
            state["err"] = e
        finally:
            state["done"] = True

    th = threading.Thread(target=writer, daemon=True)
    th.start()
    t_first = t_last = None
    first_len = 0
    last_emit = 0.0
    win = []   # (t, rx_bytes) fenêtre glissante pour le débit instantané
    while True:
        w = dst.in_waiting
        chunk = dst.read(w) if w else dst.read(1)
        now = time.perf_counter()
        if chunk:
            if t_first is None:
                t_first = now
                first_len = len(chunk)   # arrivés avant t_first : exclus du calcul de débit
            t_last = now
            checker.feed(chunk)
        if state["done"]:
            # attendre la fin d'acheminement : durée ligne restante + marge
            if not chunk and now - (t_last or now) > 0.3 + 3 * s.char_time() * L:
                break
            if ctx.stopped and now - (t_last or now) > 0.3:
                break
        if now - last_emit > 0.5:
            last_emit = now
            win.append((now, checker.rx_bytes))
            win = [x for x in win if now - x[0] <= 3.0]
            inst = (win[-1][1] - win[0][1]) / (win[-1][0] - win[0][0]) if len(win) > 1 else 0.0
            in_flight = max(0, state["sent"] - checker.ok - checker.corrupt)
            ctx.emit("stream", {
                "dir": _arrow(d), "elapsed": now - t_start, "sent": state["sent"], "ok": checker.ok,
                "corrupt": checker.corrupt, "pending": in_flight, "bit_errors": checker.bit_errors,
                "bits": checker.bits_checked, "rate": inst, "theory": s.max_bytes_per_s(),
                "garbage": checker.garbage})
            if duration:
                ctx.progress((now - t_start) / duration,
                             f"{_arrow(d)} {min(now - t_start, duration):.0f}/{duration:.0f} s")
            for m in checker.last_errors:
                ctx.log("  ✘ " + m, "err")
            checker.last_errors.clear()
    stop_w.set()
    th.join(1.0)
    if state["err"]:
        raise state["err"]
    elapsed = (t_last - t_first) if (t_first and t_last and t_last > t_first) else 0.0
    lost = max(0, state["sent"] - checker.ok - checker.corrupt)
    return {"dir": d, "sent": state["sent"], "ok": checker.ok, "corrupt": checker.corrupt, "lost": lost,
            "dup": checker.dup, "bit_errors": checker.bit_errors, "bits": checker.bits_checked,
            "garbage": checker.garbage, "rx_bytes": checker.rx_bytes,
            "rate": ((checker.rx_bytes - first_len) / elapsed) if elapsed else 0.0, "elapsed": elapsed}


def _stream_test(ctx: TestContext, p: dict, r: TestResult, gap_s: float, duration: float, pace: bool):
    s = ctx.s
    if s.bytesize != 8:
        r.status = ERROR
        r.summary = "Ce test nécessite 8 bits de données."
        return r
    plen = int(p["payload"])
    L = plen + PKT_OVERHEAD
    dirs = _dirs(p["dirs"])
    ctx.log(f"Paquet : SYNC A5 5A | N° séquence 4 o | longueur 1 o | {plen} o pseudo-aléatoires | CRC16 = {L} o "
            f"({fmt_time(s.wire_time(L))} sur la ligne)", "dim")
    r.columns = ["Sens", "Envoyés", "Reçus OK", "Corrompus", "Perdus", "Bits vérifiés", "Bits faux",
                 "BER", "Débit utile", "Efficacité"]
    a, b = ctx.open_pair()
    try:
        results = []
        for d in dirs:
            ctx.log(f"Flux {_arrow(d)}" + (f" pendant {duration:g} s" if duration else " (jusqu'à l'arrêt)"), "title")
            res = _stream_one_dir(ctx, a, b, d, s, plen, gap_s, duration, pace)
            results.append(res)
            ber = res["bit_errors"] / res["bits"] if res["bits"] else 0.0
            ub = core.ber_upper_bound(res["bits"], res["bit_errors"])
            eff = res["rate"] / s.max_bytes_per_s() * 100 if res["rate"] else 0.0
            ber_s = f"{ber:.2e}" if res["bit_errors"] else (f"< {ub:.1e} (95 %)" if ub else "—")
            r.rows.append([_arrow(d), str(res["sent"]), str(res["ok"]), str(res["corrupt"]), str(res["lost"]),
                           f"{res['bits']:,}".replace(",", " "), str(res["bit_errors"]), ber_s,
                           fmt_rate(res["rate"]), f"{eff:.1f} %"])
            ctx.log(f"  {res['ok']}/{res['sent']} paquets OK, {res['corrupt']} corrompus, {res['lost']} perdus, "
                    f"BER {ber_s}, débit {fmt_rate(res['rate'])} (max théorique {fmt_rate(s.max_bytes_per_s())})",
                    "ok" if res["corrupt"] == res["lost"] == 0 else "err")
            if ctx.stopped and duration:
                break
        r.measures = {"résultats": results}
        return results
    finally:
        a.close()
        b.close()


def test_throughput(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("throughput", "Débit soutenu (flux continu sans pause)")
    s = ctx.s
    results = _stream_test(ctx, p, r, 0.0, float(p["duration"]), pace=True)
    if r.status == ERROR or results is None:
        return r
    thr = float(p["min_eff"])
    errs = sum(x["corrupt"] + x["lost"] for x in results)
    effs = [x["rate"] / s.max_bytes_per_s() * 100 for x in results]
    if errs:
        r.status = NOK
        r.summary = f"{errs} paquet(s) corrompu(s) ou perdu(s) à pleine charge."
    elif min(effs) < thr:
        r.status = WARN
        r.summary = (f"Aucune erreur, mais l'efficacité est de {min(effs):.1f} % (< {thr:g} %) : des pauses s'insèrent "
                     "dans le flux (adaptateur USB, driver ou PC).")
    else:
        r.status = OK
        r.summary = f"Flux continu sans erreur à {min(effs):.1f} % du débit théorique ({fmt_rate(s.max_bytes_per_s())})."
    r.details.append(f"Débit théorique {s.describe()} = {s.baudrate} / {s.bits_per_char():g} bits = "
                     f"{fmt_rate(s.max_bytes_per_s())}.")
    return r


def test_endurance(ctx: TestContext, p: dict) -> TestResult:
    r = TestResult("endurance", "Endurance / taux d'erreur binaire (BER)")
    dur = float(p["duration"])
    results = _stream_test(ctx, p, r, float(p["gap_ms"]) / 1000.0, dur, pace=True)
    if r.status == ERROR or results is None:
        return r
    bits = sum(x["bits"] for x in results)
    be = sum(x["bit_errors"] for x in results)
    bad = sum(x["corrupt"] + x["lost"] for x in results)
    if ctx.stopped and dur:
        r.details.append("Test interrompu avant la fin de la durée prévue.")
    if bad:
        r.status = NOK
        r.summary = f"{bad} paquet(s) en erreur, BER mesuré {be / bits:.2e} sur {bits:,} bits.".replace(",", " ")
    else:
        ub = core.ber_upper_bound(bits, 0)
        r.status = OK if bits else NOK
        r.summary = (f"{bits:,} bits sans erreur : BER < {ub:.1e} avec 95 % de confiance.".replace(",", " ")
                     if bits else "Aucune donnée reçue.")
    r.details.append("Borne BER (règle des 3) : sans erreur sur N bits, BER < 3/N à 95 % de confiance. "
                     "Pour prouver BER < 1e-9, il faut ≈ 3e9 bits sans erreur.")
    return r


# ---------------------------------------------------------------------------
# Catalogue des tests
# ---------------------------------------------------------------------------
TESTS: List[TestDef] = [
    TestDef(
        "connect", "1. Connectivité A ↔ B", "Une trame courte dans chaque sens, comparée octet par octet.",
        "path_both",
        "Le port A émet une trame de 13 octets (55 AA 00 FF 0F F0 « RS485 » + CRC16). Elle traverse "
        "l'adaptateur A, le module 1, la fibre, le module 2 et l'adaptateur B. Le port B doit recevoir "
        "exactement les mêmes octets, ni plus ni moins. On recommence ensuite dans l'autre sens (B → A), "
        "ce qui vérifie les deux fibres et le basculement émission/réception de chaque module.\n\n"
        "C'est le test à lancer en premier. S'il échoue, les autres n'ont pas de sens.",
        "Sonde sur la sortie RS485 du module 2 (A/B en différentiel ou RO du transceiver). Déclenchement sur "
        "front descendant (bit de start), mode Normal. Vous devez voir 13 caractères.",
        [Param("reps", "Répétitions par sens", "int", 3, vmin=1, vmax=1000),
         Param("dirs", "Sens", "choice", "A→B et B→A", choices=DIR_CHOICES)],
        test_connectivity),
    TestDef(
        "patterns", "2. Intégrité des données", "256 valeurs, 0x00, 0xFF, 0x55, bits baladeurs, PRBS-15.",
        "path_both",
        "Vérifie que la liaison est transparente, c'est-à-dire que n'importe quelle valeur d'octet ressort "
        "identique. Les motifs sont choisis pour stresser la chaîne :\n"
        "• 00→FF : les 256 valeurs possibles.\n"
        "• 0x55 / 0xAA : alternance 0101, la fréquence la plus haute (débit/2). Révèle la distorsion de largeur "
        "d'impulsion des convertisseurs optiques.\n"
        "• 0x00 : 9 bits bas consécutifs. Teste le maintien d'un niveau long (couplage AC, polarisation).\n"
        "• 0xFF : seul le start est bas, ce qui donne une impulsion isolée d'un bit (la plus difficile pour un "
        "récepteur optique lent).\n"
        "• Bits baladeurs et PRBS-15 : toutes les combinaisons de transitions.\n"
        "La comparaison est faite bit par bit. Chaque erreur est localisée (index, valeur attendue, valeur reçue).",
        "Générateur continu avec 0x55 : vous devez voir un signal carré de fréquence débit/2 (4800 Hz à 9600 "
        "bauds). Comparez la largeur des niveaux haut et bas : l'écart de largeur d'impulsion doit rester "
        "inférieur à environ 10 % de la durée d'un bit.",
        [Param("reps", "Répétitions par motif", "int", 1, vmin=1, vmax=100),
         Param("dirs", "Sens", "choice", "A→B et B→A", choices=DIR_CHOICES)],
        test_patterns),
    TestDef(
        "modbus", "3. Trames Modbus RTU typiques", "Requêtes et réponses réelles avec CRC, silence t3,5.",
        "path_both",
        "Envoie des trames Modbus RTU représentatives d'un usage réel (lecture/écriture de registres, bobines, "
        "exception, broadcast, réponse longue de 255 octets). Chaque trame doit ressortir identique et son CRC16 "
        "doit être valide à la réception. Le silence inter-trame de 3,5 caractères imposé par Modbus est "
        "respecté entre chaque trame.",
        "La trame la plus longue (255 o) dure 266 ms à 9600 bauds. Vérifiez qu'il n'y a pas de trou au milieu : "
        "un silence de plus de 1,5 caractère à l'intérieur d'une trame la rend invalide en Modbus.",
        [Param("dirs", "Sens", "choice", "A→B et B→A", choices=DIR_CHOICES)],
        test_modbus),
    TestDef(
        "turnaround", "4. Temps de retournement", "A envoie, B répond après un délai : on cherche le plus court qui marche.",
        "turnaround",
        "En RS485 2 fils (half-duplex), un seul équipement émet à la fois. Après avoir transmis une trame vers "
        "l'adaptateur B, le module 2 doit relâcher le bus (passer DE à 0) pour que B puisse répondre. Si B "
        "répond trop tôt, il y a collision et la réponse est perdue ou corrompue.\n\n"
        "Pour chaque délai de la liste, on fait N allers-retours : A envoie, B reçoit, attend le délai, puis "
        "répond avec l'inverse des octets, et A vérifie la réponse. Le résultat est le plus petit délai "
        "au-dessus duquel 100 % des échanges réussissent.\n\n"
        "Le délai est appliqué par le PC (attente précise sur horloge haute résolution). Sur la ligne, le silence "
        "réel vaut ce délai plus les latences USB. Seul l'oscilloscope donne le temps de retournement exact "
        "du module.",
        "2 voies : voie 1 sur la ligne côté A, voie 2 sur la ligne côté B. Mesurez le temps entre la fin du "
        "dernier bit de stop de la trame aller (sur B) et le bit de start de la réponse (sur B). Vérifiez "
        "aussi que la sortie DE du module 2 retombe avant la réponse.",
        [Param("delays_ms", "Délais à tester (ms)", "floatlist", "50, 20, 10, 5, 2, 1, 0.5, 0",
               "Liste séparée par des virgules, testée du plus long au plus court"),
         Param("reps", "Essais par délai", "int", 20, vmin=1, vmax=10000),
         Param("length", "Longueur de trame (octets)", "int", 8, vmin=1, vmax=255)],
        test_turnaround),
    TestDef(
        "sweep", "5. Balayage des débits", "Même motif de stress à chaque débit, dans les deux sens.",
        "path_both",
        "Pour chaque débit de la liste, les deux ports sont rouverts et 352 octets (256 valeurs + 0x55 + PRBS) "
        "sont échangés dans chaque sens. Le tableau donne pour chaque débit la durée d'un bit et le débit réel "
        "estimé de l'adaptateur. Pour les FTDI, c'est l'algorithme exact du diviseur d'horloge : par exemple "
        "1 100 000 demandés donnent 1 090 909 réels.\n\n"
        "Limite importante : si les deux adaptateurs sont identiques, ils font la même erreur d'arrondi et le "
        "test passe quand même. Pour valider le débit réel, mesurez la durée d'un bit à l'oscilloscope.",
        "Générateur continu, motif 0x55, au débit voulu. Fréquence attendue = débit / 2. Écart toléré entre "
        "émetteur et récepteur UART : environ ±2 %.",
        [Param("bauds", "Débits (bauds)", "intlist", ", ".join(str(x) for x in core.DEFAULT_SWEEP)),
         Param("dirs", "Sens", "choice", "A→B et B→A", choices=DIR_CHOICES)],
        test_baud_sweep),
    TestDef(
        "latency", "6. Latence", "N mesures du temps écriture → réception complète, avec statistiques.",
        "path_one",
        "Mesure le temps entre l'appel write() sur le port émetteur et l'arrivée du dernier octet sur le port "
        "récepteur, avec l'horloge haute résolution du PC (perf_counter, précision inférieure à la µs). La V5.1 "
        "utilisait time.time(), dont la résolution sous Windows est de 15,6 ms : le résultat affiché n'était "
        "qu'un tick d'horloge.\n\n"
        "La latence contient la durée de transmission sur la ligne (calculée et affichée), la latence USB des "
        "deux adaptateurs (souvent 1 à 16 ms selon le « Latency Timer » FTDI) et le temps de traversée des modules. "
        "On donne min, médiane, P95, max et l'histogramme.",
        "La latence propre des modules (quelques centaines de ns à quelques µs) ne se mesure qu'à l'oscilloscope : "
        "voie 1 sur l'entrée du module 1, voie 2 sur la sortie du module 2. Mesurez le décalage entre les "
        "mêmes fronts.",
        [Param("samples", "Nombre de mesures", "int", 100, vmin=1, vmax=100000),
         Param("size", "Taille du message (octets)", "int", 8, vmin=1, vmax=4096),
         Param("max_ms", "Latence max admise (ms, 0 = pas de limite)", "float", 0.0, vmin=0),
         Param("dirs", "Sens", "choice", "A→B", choices=DIR_CHOICES)],
        test_latency),
    TestDef(
        "throughput", "7. Débit soutenu", "Flux continu à 100 % de charge, paquets numérotés + CRC.",
        "stream",
        "Envoie en continu, sans pause, des paquets numérotés et protégés par CRC16. On mesure le débit utile "
        "réellement reçu et on le compare au maximum théorique : débit / (bits par caractère), soit 960 o/s "
        "à 9600 bauds en 8N1, et non 1024 comme l'indiquait la V5.1. Les paquets perdus, corrompus ou en "
        "double sont comptés. Ce test vérifie que rien ne déborde à pleine charge (tampons des modules, "
        "adaptateurs).",
        "Le signal ne doit présenter aucun silence entre caractères. Des trous réguliers indiquent une "
        "limitation de l'adaptateur USB ou du PC, pas des modules.",
        [Param("duration", "Durée par sens (s)", "float", 10.0, vmin=1),
         Param("payload", "Charge utile par paquet (octets)", "int", 64, vmin=1, vmax=246),
         Param("min_eff", "Efficacité minimale attendue (%)", "float", 90.0, vmin=0, vmax=100),
         Param("dirs", "Sens", "choice", "A→B", choices=DIR_CHOICES)],
        test_throughput),
    TestDef(
        "endurance", "8. Endurance / BER", "Test longue durée (ou infini) avec taux d'erreur binaire et borne de confiance.",
        "stream",
        "Test de fond pour la qualification. Des paquets numérotés et protégés par CRC sont envoyés en continu "
        "pendant la durée choisie (0 = jusqu'à l'arrêt), avec un intervalle réglable. Chaque paquet reçu est "
        "comparé bit à bit à ce qui a été envoyé. On compte :\n"
        "• les paquets corrompus et le nombre exact de bits faux (BER = bits faux / bits vérifiés) ;\n"
        "• les paquets perdus (envoyés mais jamais reçus).\n"
        "Sans aucune erreur, on donne la borne supérieure du BER à 95 % de confiance (3 / nombre de bits). "
        "C'est l'indicateur à utiliser pendant une perturbation réelle : CEM, torsion de fibre, variation "
        "d'alimentation, température.",
        "Laissez tourner pendant que vous sondez : persistance infinie pour repérer la gigue, mesure de l'œil, "
        "déclenchement sur glitch ou largeur d'impulsion inférieure à 0,8 × la durée d'un bit pour attraper "
        "une impulsion anormale.",
        [Param("duration", "Durée par sens (s, 0 = infini)", "float", 60.0, vmin=0),
         Param("gap_ms", "Intervalle entre paquets (ms)", "float", 0.0, vmin=0),
         Param("payload", "Charge utile par paquet (octets)", "int", 32, vmin=1, vmax=246),
         Param("dirs", "Sens", "choice", "A→B et B→A", choices=DIR_CHOICES)],
        test_endurance),
]

TESTS_BY_ID = {t.test_id: t for t in TESTS}


def parse_param(prm: Param, raw) -> object:
    """Convertit une saisie utilisateur. Lève ValueError avec un message clair."""
    if prm.kind == "bool":
        return bool(raw)
    if prm.kind == "choice":
        if raw not in prm.choices:
            raise ValueError(f"{prm.label} : choix invalide")
        return raw
    text = str(raw).strip().replace(";", ",")
    if prm.kind in ("floatlist", "intlist"):
        items = [x.strip() for x in text.split(",") if x.strip()]
        if not items:
            raise ValueError(f"{prm.label} : liste vide")
        try:
            vals = [float(x.replace(" ", "")) for x in items]
        except ValueError:
            raise ValueError(f"{prm.label} : « {text} » n'est pas une liste de nombres")
        if any(v < 0 for v in vals):
            raise ValueError(f"{prm.label} : valeurs négatives interdites")
        if prm.kind == "intlist":
            if any(v <= 0 for v in vals):
                raise ValueError(f"{prm.label} : valeurs > 0 attendues")
            return [int(v) for v in vals]
        return vals
    try:
        v = float(text.replace(",", ".").replace(" ", ""))
    except ValueError:
        raise ValueError(f"{prm.label} : « {text} » n'est pas un nombre")
    if prm.kind == "int":
        if v != int(v):
            raise ValueError(f"{prm.label} : nombre entier attendu")
        v = int(v)
    if prm.vmin is not None and v < prm.vmin:
        raise ValueError(f"{prm.label} : minimum {prm.vmin:g}")
    if prm.vmax is not None and v > prm.vmax:
        raise ValueError(f"{prm.label} : maximum {prm.vmax:g}")
    return v


def run_test(tdef: TestDef, ctx: TestContext, params: dict) -> TestResult:
    """Exécute un test en capturant les erreurs. Ne lève jamais."""
    t0 = time.time()
    try:
        res = tdef.func(ctx, params)
        if ctx.stopped and res.status in (OK, WARN) and tdef.test_id != "endurance":
            res.status = ABORTED
            res.summary = "Interrompu par l'utilisateur. " + res.summary
    except StopTest:
        res = TestResult(tdef.test_id, tdef.title, ABORTED, "Interrompu par l'utilisateur.")
    except core.EchoError as e:
        res = TestResult(tdef.test_id, tdef.title, ERROR, f"Écho local : {e}. Désactivez l'option « écho local » "
                                                          "si l'adaptateur ne renvoie pas sa propre émission.")
    except (serial.SerialException, OSError) as e:
        res = TestResult(tdef.test_id, tdef.title, ERROR, f"Erreur port série : {e}")
    except Exception as e:     # noqa
        import traceback
        res = TestResult(tdef.test_id, tdef.title, ERROR, f"Erreur interne : {e!r}")
        res.details.append(traceback.format_exc())
    res.title = tdef.title
    res.started = t0
    res.duration = time.time() - t0
    res.settings = ctx.s.describe()
    return res


# ---------------------------------------------------------------------------
# Générateur continu (oscilloscope) avec vérification en temps réel
# ---------------------------------------------------------------------------
class Generator:
    """Envoie une trame en boucle sur un port ; vérifie optionnellement sur l'autre."""

    def __init__(self, tx_port: str, rx_port: Optional[str], s: SerialSettings, frame: bytes,
                 period_s: float, count: int, emit: Callable, increment: bool = False):
        self.tx_port, self.rx_port, self.s = tx_port, rx_port, s
        self.frame = frame
        self.period = period_s
        self.count = count
        self.emit = emit
        self.increment = increment
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.stats = {"sent": 0, "ok": 0, "bad": 0, "rx_bytes": 0}

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _frame_for(self, i: int) -> bytes:
        if not self.increment or not self.frame:
            return self.frame
        f = bytearray(self.frame)
        f[-1] = (f[-1] + i) & ((1 << self.s.bytesize) - 1)
        return bytes(f)

    def _run(self):
        tx = rx = None
        try:
            tx = core.open_port(self.tx_port, self.s)
            if self.rx_port:
                rx = core.open_port(self.rx_port, self.s)
        except Exception as e:   # noqa
            self.emit("gen_error", f"Ouverture impossible : {e}")
            if tx:
                tx.close()
            self.emit("gen_done", self.stats)
            return
        expected = bytearray()
        rxbuf = bytearray()
        verify_lock = threading.Lock()
        stop_rx = threading.Event()

        def reader():
            while not stop_rx.is_set():
                try:
                    w = rx.in_waiting
                    data = rx.read(w) if w else rx.read(1)
                except Exception as e:   # noqa
                    self.emit("gen_error", f"Lecture : {e}")
                    return
                if data:
                    with verify_lock:
                        rxbuf.extend(data)
                        self.stats["rx_bytes"] += len(data)
                        self._verify(expected, rxbuf)

        rth = None
        if rx is not None:
            rth = threading.Thread(target=reader, daemon=True)
            rth.start()
        i = 0
        t_next = t_begin = time.perf_counter()
        sent_bytes = 0
        last_emit = 0.0
        try:
            while not self.stop_event.is_set() and (self.count == 0 or i < self.count):
                fr = self._frame_for(i)
                if rx is not None:
                    with verify_lock:
                        expected.extend(fr)
                send(tx, fr, self.s)
                i += 1
                self.stats["sent"] = i
                now = time.perf_counter()
                if now - last_emit > 0.25:
                    last_emit = now
                    self.emit("gen_stats", dict(self.stats))
                if self.period > 0:
                    t_next += self.period
                    core.precise_sleep(t_next - time.perf_counter())
                    if time.perf_counter() - t_next > 0.5:    # retard trop important : on recale
                        t_next = time.perf_counter()
                else:
                    # dos à dos : on garde au plus ~0,1 s d'avance sur la ligne
                    sent_bytes += len(fr)
                    ahead = sent_bytes * self.s.char_time() - (time.perf_counter() - t_begin)
                    if ahead > 0.1:
                        time.sleep(ahead - 0.05)
            if rx is not None:
                deadline = time.perf_counter() + 0.5 + 2 * self.s.wire_time(len(self.frame) * 4)
                while time.perf_counter() < deadline:
                    with verify_lock:
                        if not expected:
                            break
                    time.sleep(0.01)
                with verify_lock:
                    if expected:
                        missing = len(expected) // max(1, len(self.frame))
                        self.stats["bad"] += max(1, missing)
                        self.emit("gen_bad", f"{len(expected)} octet(s) jamais reçus en fin de génération")
        except Exception as e:   # noqa
            self.emit("gen_error", f"Émission : {e}")
        finally:
            stop_rx.set()
            if rth:
                rth.join(0.5)
            tx.close()
            if rx:
                rx.close()
            self.emit("gen_stats", dict(self.stats))
            self.emit("gen_done", dict(self.stats))

    def _verify(self, expected: bytearray, rxbuf: bytearray):
        L = len(self.frame)
        while len(rxbuf) >= L and len(expected) >= L:
            exp = bytes(expected[:L])
            got = bytes(rxbuf[:L])
            if got == exp:
                self.stats["ok"] += 1
                del expected[:L]
                del rxbuf[:L]
                continue
            self.stats["bad"] += 1
            cmp = compare(exp, got)
            self.emit("gen_bad", f"trame n°{self.stats['ok'] + self.stats['bad']} : {cmp.summary()} — "
                                 f"attendu {to_hex(exp, 24)} / reçu {to_hex(got, 24)}")
            # resynchronisation : chercher le début d'une trame attendue dans le tampon reçu
            del expected[:L]
            nxt = bytes(expected[:min(4, L)])
            j = rxbuf.find(nxt, 1) if nxt else -1
            del rxbuf[:(j if j > 0 else L)]
        if len(expected) > 64 * max(L, 1) + 65536:
            # rien ne revient : éviter de grossir indéfiniment
            self.stats["bad"] += len(expected) // max(L, 1)
            del expected[:]


# ---------------------------------------------------------------------------
# Moniteur (deux ports ouverts, envoi manuel, vérification E/S)
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, port_a: str, port_b: str, s: SerialSettings, emit: Callable, gap_ms: float = 10.0):
        self.ports = {"A": port_a, "B": port_b}
        self.s = s
        self.emit = emit
        self.gap = max(gap_ms / 1000.0, 3 * s.char_time())
        self.ser = {}
        self.stop_event = threading.Event()
        self.threads = []
        self.lock = threading.Lock()
        self.expect = None    # (côté destinataire, données attendues, échéance)
        self.got = bytearray()
        self.got_t = 0.0
        self.t0 = time.perf_counter()

    def open(self):
        for side, dev in self.ports.items():
            if dev:
                try:
                    self.ser[side] = core.open_port(dev, self.s)
                except Exception:
                    self.close()
                    raise
        for side in self.ser:
            th = threading.Thread(target=self._reader, args=(side,), daemon=True)
            th.start()
            self.threads.append(th)

    def close(self):
        self.stop_event.set()
        for th in self.threads:
            th.join(0.5)
        for s in self.ser.values():
            try:
                s.close()
            except Exception:
                pass
        self.ser = {}

    def send(self, side: str, data: bytes, verify: bool = True):
        ser = self.ser.get(side)
        if ser is None:
            raise serial.SerialException(f"port {side} non ouvert")
        other = "B" if side == "A" else "A"
        if verify and other in self.ser:
            with self.lock:
                self.got = bytearray()
                self.expect = (other, data, time.perf_counter() + rx_timeout(self.s, len(data), 0.4))
        t = send(ser, data, self.s)
        self.emit("mon_frame", (side, "TX", data, t - self.t0))

    def _reader(self, side: str):
        ser = self.ser[side]
        buf = bytearray()
        t_first = t_last = 0.0
        while not self.stop_event.is_set():
            try:
                w = ser.in_waiting
                data = ser.read(w) if w else ser.read(1)
            except Exception as e:   # noqa
                self.emit("mon_error", f"Port {side} : {e}")
                return
            now = time.perf_counter()
            if data:
                if not buf:
                    t_first = now
                buf += data
                t_last = now
                with self.lock:
                    if self.expect and self.expect[0] == side:
                        self.got += data
                        self.got_t = now
            if buf and now - t_last >= self.gap:
                self.emit("mon_frame", (side, "RX", bytes(buf), t_first - self.t0))
                buf = bytearray()
            self._check_expect(now)

    def _check_expect(self, now: float):
        with self.lock:
            if not self.expect:
                return
            side, data, deadline = self.expect
            complete = len(self.got) >= len(data)
            idle = now - self.got_t >= self.gap if self.got else False
            if (complete and idle) or now > deadline:
                cmp = compare(data, bytes(self.got))
                self.emit("mon_verify", (side, data, bytes(self.got), cmp))
                self.expect = None
