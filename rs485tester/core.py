"""Briques de base : réglages série, timings UART, CRC, motifs, comparaison,
ouverture des ports (réels ou simulés) et temporisation précise.

Aucune dépendance à l'interface graphique : ce module est testable seul.
"""
from __future__ import annotations

import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

import serial
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
BAUD_PRESETS = [300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
                115200, 230400, 460800, 500000, 576000, 921600, 1000000,
                1500000, 2000000, 3000000]
DEFAULT_SWEEP = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400,
                 460800, 921600, 1000000, 1500000, 2000000]

PARITIES = {"N": "Aucune", "E": "Paire", "O": "Impaire", "M": "Mark (1)", "S": "Space (0)"}
SIM_PORTS = ("SIM-A", "SIM-B")


# ---------------------------------------------------------------------------
# Réglages série + timings
# ---------------------------------------------------------------------------
@dataclass
class SerialSettings:
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    # Contrôle du sens de transmission (DE/RE) :
    #  "auto"     : adaptateur à contrôle automatique (cas le plus courant)
    #  "rts_high" : RTS=1 pendant l'émission (pilotage manuel)
    #  "rts_low"  : RTS=0 pendant l'émission
    direction: str = "auto"
    rts_pre_ms: float = 0.0     # délai entre activation DE et 1er bit
    rts_post_ms: float = 0.0    # délai après le dernier bit avant libération
    # Adaptateur 2 fils qui reçoit sa propre émission : on lit et on vérifie l'écho
    local_echo: bool = False

    # -- timings ---------------------------------------------------------
    def bits_per_char(self) -> float:
        return 1 + self.bytesize + (0 if self.parity == "N" else 1) + self.stopbits

    def bit_time(self) -> float:
        return 1.0 / self.baudrate

    def char_time(self) -> float:
        return self.bits_per_char() / self.baudrate

    def wire_time(self, nbytes: int) -> float:
        """Durée théorique sur la ligne de n octets envoyés sans pause."""
        return nbytes * self.char_time()

    def max_bytes_per_s(self) -> float:
        return self.baudrate / self.bits_per_char()

    def modbus_t35(self) -> float:
        """Silence inter-trame Modbus RTU (3,5 caractères, 1,75 ms au-delà de 19200)."""
        return 0.00175 if self.baudrate > 19200 else 3.5 * self.char_time()

    def format_str(self) -> str:
        sb = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.bytesize}{self.parity}{sb}"

    def describe(self) -> str:
        return f"{self.baudrate} bauds {self.format_str()}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SerialSettings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def copy(self, **changes) -> "SerialSettings":
        d = self.to_dict()
        d.update(changes)
        return SerialSettings.from_dict(d)


def fmt_time(seconds: float) -> str:
    """Affichage lisible d'une durée (ns / µs / ms / s)."""
    a = abs(seconds)
    if a < 1e-6:
        return f"{seconds * 1e9:.0f} ns"
    if a < 1e-3:
        return f"{seconds * 1e6:.2f} µs"
    if a < 1:
        return f"{seconds * 1e3:.3f} ms"
    return f"{seconds:.3f} s"


def fmt_rate(bps: float) -> str:
    if bps >= 1e6:
        return f"{bps / 1e6:.3f} Mo/s"
    if bps >= 1e3:
        return f"{bps / 1e3:.2f} ko/s"
    return f"{bps:.1f} o/s"


# ---------------------------------------------------------------------------
# Estimation du débit réel (adaptateurs FTDI)
# ---------------------------------------------------------------------------
FTDI_VID = 0x0403
_FTDI_HS_PIDS = {0x6010, 0x6011, 0x6014}   # FT2232H / FT4232H / FT232H (horloge 120 MHz)


def _ftdi_clkbits(baud: int, clk: int, clk_div: int) -> int:
    """Port de ftdi_to_clkbits() de libftdi (même algorithme que le driver)."""
    if baud >= clk // clk_div:
        return clk // clk_div
    if baud >= clk // (clk_div + clk_div // 2):
        return clk // (clk_div + clk_div // 2)
    if baud >= clk // (2 * clk_div):
        return clk // (2 * clk_div)
    divisor = clk * 16 // clk_div // baud
    best = divisor // 2 + 1 if divisor & 1 else divisor // 2
    best = min(best, 0x1FFFF)
    best_baud = clk * 16 // clk_div // best
    return best_baud // 2 + 1 if best_baud & 1 else best_baud // 2


def ftdi_actual_baud(requested: int, pid: Optional[int] = None) -> int:
    """Débit réellement généré par une puce FTDI pour un débit demandé."""
    if pid in _FTDI_HS_PIDS and requested * 10 > 120_000_000 // 0x3FFF:
        return _ftdi_clkbits(requested, 120_000_000, 10)
    return _ftdi_clkbits(requested, 48_000_000, 16)


@dataclass
class PortInfo:
    device: str
    description: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: str = ""
    manufacturer: str = ""

    @property
    def is_ftdi(self) -> bool:
        return self.vid == FTDI_VID

    @property
    def is_sim(self) -> bool:
        return self.device in SIM_PORTS

    def chip(self) -> str:
        if self.is_sim:
            return "Port simulé"
        known = {0x0403: "FTDI", 0x10C4: "Silicon Labs CP210x", 0x1A86: "WCH CH34x",
                 0x067B: "Prolific PL2303", 0x0483: "STMicro", 0x2341: "Arduino"}
        name = known.get(self.vid or -1, self.manufacturer or "inconnu")
        if self.vid is not None:
            name += f" (VID {self.vid:04X} / PID {self.pid or 0:04X})"
        return name

    def label(self) -> str:
        return f"{self.device} — {self.description}" if self.description else self.device


def list_ports(include_sim: bool = True) -> List[PortInfo]:
    out = []
    for p in serial.tools.list_ports.comports():
        out.append(PortInfo(p.device, p.description or "", p.vid, p.pid,
                            p.serial_number or "", p.manufacturer or ""))
    out.sort(key=lambda p: _natural_key(p.device))
    if include_sim:
        out += [PortInfo("SIM-A", "Port simulé A (sans matériel)"),
                PortInfo("SIM-B", "Port simulé B (sans matériel)")]
    return out


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def baud_note(requested: int, info: Optional[PortInfo]) -> Tuple[Optional[int], str]:
    """Retourne (débit réel estimé, commentaire)."""
    if info is None or info.is_sim:
        return requested, "simulé"
    if info.is_ftdi:
        real = ftdi_actual_baud(requested, info.pid)
        err = (real - requested) / requested * 100
        return real, f"FTDI ≈ {real} bauds ({err:+.2f} %)"
    return None, "débit réel non estimable pour cette puce : mesurez 0x55 à l'oscilloscope"


# ---------------------------------------------------------------------------
# Ouverture des ports
# ---------------------------------------------------------------------------
_PARITY_MAP = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD,
               "M": serial.PARITY_MARK, "S": serial.PARITY_SPACE}
_STOP_MAP = {1.0: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2.0: serial.STOPBITS_TWO}


def open_port(device: str, s: SerialSettings, timeout: float = 0.05):
    """Ouvre un port réel ou simulé avec les réglages donnés."""
    if device in SIM_PORTS:
        from .simport import get_sim_link
        return get_sim_link().open(device, s, timeout)
    ser = serial.Serial()
    ser.port = device
    apply_settings(ser, s)
    ser.timeout = timeout
    ser.write_timeout = None
    ser.dtr = False
    if s.direction == "auto":
        ser.rts = False
    else:
        ser.rts = _rts_level(s, transmit=False)
    ser.open()
    if hasattr(ser, "set_buffer_size"):     # Windows : tampons driver plus grands
        try:
            ser.set_buffer_size(rx_size=65536, tx_size=65536)
        except Exception:
            pass
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser


def apply_settings(ser, s: SerialSettings) -> None:
    ser.baudrate = int(s.baudrate)
    ser.bytesize = int(s.bytesize)
    ser.parity = _PARITY_MAP[s.parity]
    ser.stopbits = _STOP_MAP[float(s.stopbits)]


def _rts_level(s: SerialSettings, transmit: bool) -> bool:
    if s.direction == "rts_high":
        return transmit
    return not transmit


# ---------------------------------------------------------------------------
# Temporisation précise
# ---------------------------------------------------------------------------
_timer_enabled = False


def enable_high_res_timer() -> None:
    """Sous Windows, passe le tick système à 1 ms (sinon sleep() = 15,6 ms)."""
    global _timer_enabled
    if _timer_enabled or not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
        _timer_enabled = True
    except Exception:
        pass


def precise_sleep(seconds: float) -> None:
    """Attente précise : sleep() grossier puis attente active sur perf_counter."""
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    coarse = seconds - 0.002
    if coarse > 0:
        time.sleep(coarse)
    while time.perf_counter() < end:
        pass


# ---------------------------------------------------------------------------
# Emission / réception bas niveau
# ---------------------------------------------------------------------------
class EchoError(Exception):
    pass


def send(ser, data: bytes, s: SerialSettings) -> float:
    """Émet data. Gère RTS manuel et écho local. Retourne t (perf_counter) du write."""
    manual = s.direction in ("rts_high", "rts_low")
    if manual:
        ser.rts = _rts_level(s, transmit=True)
        precise_sleep(s.rts_pre_ms / 1000.0)
    t0 = time.perf_counter()
    ser.write(data)
    if manual:
        ser.flush()
        # flush() rend la main quand le driver a tout transmis à l'USB, pas quand
        # le dernier bit est sorti : on attend la durée théorique sur la ligne.
        remaining = t0 + s.wire_time(len(data)) - time.perf_counter()
        precise_sleep(max(0.0, remaining) + s.rts_post_ms / 1000.0)
        ser.rts = _rts_level(s, transmit=False)
    if s.local_echo:
        echo = read_exact(ser, len(data), 0.2 + 2 * s.wire_time(len(data)))[0]
        if echo != data:
            raise EchoError(f"écho local incorrect ({len(echo)}/{len(data)} octets)")
    return t0


def read_exact(ser, n: int, timeout: float, stop_event=None) -> Tuple[bytes, Optional[float], Optional[float]]:
    """Lit n octets max avant timeout. Retourne (données, t_premier, t_dernier)."""
    buf = bytearray()
    t_first = t_last = None
    deadline = time.perf_counter() + timeout
    while len(buf) < n:
        if stop_event is not None and stop_event.is_set():
            break
        now = time.perf_counter()
        if now >= deadline:
            break
        waiting = ser.in_waiting
        if waiting:
            chunk = ser.read(min(waiting, n - len(buf)))
        else:
            chunk = ser.read(1)
        if chunk:
            t = time.perf_counter()
            if t_first is None:
                t_first = t
            t_last = t
            buf += chunk
    return bytes(buf), t_first, t_last


def drain(ser, idle: float = 0.02, max_time: float = 0.5) -> bytes:
    """Lit tout ce qui arrive jusqu'à un silence de `idle` secondes."""
    buf = bytearray()
    end = time.perf_counter() + max_time
    last = time.perf_counter()
    while time.perf_counter() < end:
        w = ser.in_waiting
        if w:
            buf += ser.read(w)
            last = time.perf_counter()
        elif time.perf_counter() - last >= idle:
            break
        else:
            time.sleep(0.001)
    return bytes(buf)


def rx_timeout(s: SerialSettings, nbytes: int, margin: float = 0.25) -> float:
    """Timeout de réception raisonnable : 2 × durée ligne + marge (latence USB)."""
    return 2.0 * s.wire_time(nbytes) + margin


# ---------------------------------------------------------------------------
# Comparaison entrée / sortie
# ---------------------------------------------------------------------------
@dataclass
class CompareResult:
    sent: int
    received: int
    byte_errors: int = 0
    bit_errors: int = 0
    first_diff: Optional[int] = None
    diffs: List[Tuple[int, int, int]] = field(default_factory=list)  # (index, attendu, reçu)

    @property
    def ok(self) -> bool:
        return self.sent == self.received and self.byte_errors == 0

    @property
    def missing(self) -> int:
        return max(0, self.sent - self.received)

    @property
    def extra(self) -> int:
        return max(0, self.received - self.sent)

    def summary(self) -> str:
        if self.ok:
            return f"IDENTIQUE ({self.sent} octets)"
        parts = []
        if self.received == 0:
            return f"RIEN REÇU (0/{self.sent} octets)"
        if self.missing:
            parts.append(f"{self.missing} octet(s) manquant(s)")
        if self.extra:
            parts.append(f"{self.extra} octet(s) en trop")
        if self.byte_errors:
            parts.append(f"{self.byte_errors} octet(s) faux / {self.bit_errors} bit(s) faux")
        if self.first_diff is not None:
            parts.append(f"1re différence à l'index {self.first_diff}")
        return "DIFFÉRENT : " + ", ".join(parts)


def compare(tx: bytes, rx: bytes, max_diffs: int = 16) -> CompareResult:
    r = CompareResult(len(tx), len(rx))
    for i in range(min(len(tx), len(rx))):
        if tx[i] != rx[i]:
            r.byte_errors += 1
            r.bit_errors += bin(tx[i] ^ rx[i]).count("1")
            if r.first_diff is None:
                r.first_diff = i
            if len(r.diffs) < max_diffs:
                r.diffs.append((i, tx[i], rx[i]))
    if r.first_diff is None and len(tx) != len(rx):
        r.first_diff = min(len(tx), len(rx))
    return r


def diff_lines(tx: bytes, rx: bytes, width: int = 16) -> List[Tuple[str, str, bool]]:
    """Lignes hexadécimales côte à côte (offset, attendu | reçu, ligne_ok)."""
    lines = []
    n = max(len(tx), len(rx))
    for off in range(0, n, width):
        a = tx[off:off + width]
        b = rx[off:off + width]
        ha = " ".join(f"{x:02X}" for x in a)
        hb = " ".join(f"{x:02X}" for x in b)
        lines.append((f"{off:04X}", f"{ha:<{width * 3}}| {hb}", a == b))
    return lines


def diagnose(cmp: CompareResult, direction: str) -> List[str]:
    """Pistes de dépannage selon le type d'échec."""
    if cmp.ok:
        return []
    tips = []
    if cmp.received == 0:
        tips += [f"Aucun octet reçu ({direction}).",
                 "• Modules alimentés ? voyants ?",
                 "• Fibres croisées : OUT du module 1 → IN du module 2 et inversement.",
                 "• Bon port COM sélectionné (étiquette du câble) ?",
                 "• Câblage RS485 : A/B et masse (GND) entre adaptateur et module.",
                 "• Adaptateur sans contrôle auto du sens : réglez RTS manuel (onglet Config)."]
    elif cmp.byte_errors and cmp.received >= cmp.sent:
        tips += ["Octets reçus mais différents :",
                 "• Même débit / parité / bits de stop des deux côtés et sur les modules ?",
                 "• Polarité A/B inversée sur un tronçon (donne des octets incohérents) ?",
                 "• Terminaison 120 Ω, longueur de câble, masse commune.",
                 "• Distorsion de largeur d'impulsion dans la chaîne fibre (vérifier 0x55 à l'oscillo)."]
    else:
        tips += ["Réception incomplète :",
                 "• Module trop lent à basculer (retournement) ou trame tronquée.",
                 "• Débit trop élevé pour un élément de la chaîne.",
                 "• Tampon / latence USB : réessayer avec un timeout plus long."]
    return tips


# ---------------------------------------------------------------------------
# CRC et sommes de contrôle
# ---------------------------------------------------------------------------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


CHECKSUMS = {
    "Aucun": lambda d: b"",
    "CRC16 Modbus (LSB d'abord)": lambda d: crc16_modbus(d).to_bytes(2, "little"),
    "CRC16 CCITT-FALSE (MSB d'abord)": lambda d: crc16_ccitt(d).to_bytes(2, "big"),
    "XOR 8 bits": lambda d: bytes([_xor8(d)]),
    "Somme 8 bits": lambda d: bytes([sum(d) & 0xFF]),
    "LRC Modbus ASCII": lambda d: bytes([(-sum(d)) & 0xFF]),
}


def _xor8(d: bytes) -> int:
    x = 0
    for b in d:
        x ^= b
    return x


def modbus_crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16_modbus(frame[:-2]) == int.from_bytes(frame[-2:], "little")


# ---------------------------------------------------------------------------
# Saisie / affichage
# ---------------------------------------------------------------------------
def parse_hex(text: str) -> bytes:
    """'01 03 0x00,0A' -> b'\\x01\\x03\\x00\\x0a'. Lève ValueError si invalide."""
    t = re.sub(r"0x", " ", text, flags=re.I)
    t = re.sub(r"[\s,;:\-]+", " ", t).strip()
    if not t:
        return b""
    out = bytearray()
    for tok in t.split(" "):
        if not re.fullmatch(r"[0-9A-Fa-f]+", tok):
            raise ValueError(f"octet hexadécimal invalide : '{tok}'")
        if len(tok) % 2:
            tok = "0" + tok
        out += bytes.fromhex(tok)
    return bytes(out)


def parse_ascii(text: str) -> bytes:
    """Texte avec échappements \\r \\n \\t \\0 \\xNN \\\\."""
    out = bytearray()
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            simple = {"r": 13, "n": 10, "t": 9, "0": 0, "\\": 92}
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
            if n in "xX" and i + 3 < len(text) + 0 and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 2:i + 4]):
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
        out += c.encode("latin-1", errors="replace")
        i += 1
    return bytes(out)


def to_hex(data: bytes, limit: int = 0) -> str:
    d = data if not limit or len(data) <= limit else data[:limit]
    s = " ".join(f"{b:02X}" for b in d)
    if limit and len(data) > limit:
        s += f" … (+{len(data) - limit})"
    return s


def to_ascii(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "·" for b in data)


# ---------------------------------------------------------------------------
# Motifs de test
# ---------------------------------------------------------------------------
def prbs15(n: int, seed: int = 0x7FFF) -> bytes:
    """Séquence pseudo-aléatoire PRBS-15 (x^15 + x^14 + 1), norme ITU-T O.150."""
    state = seed & 0x7FFF or 1
    out = bytearray(n)
    for i in range(n):
        byte = 0
        for bit in range(8):
            newbit = ((state >> 14) ^ (state >> 13)) & 1
            state = ((state << 1) | newbit) & 0x7FFF
            byte |= newbit << bit
        out[i] = byte
    return bytes(out)


def seeded_payload(seed: int, n: int) -> bytes:
    if n <= 0:
        return b""
    return random.Random(seed).getrandbits(8 * n).to_bytes(n, "little")


def stress_patterns(bytesize: int = 8) -> List[Tuple[str, bytes, str]]:
    """(nom, données, ce que ça vérifie). Masqués au nombre de bits de données."""
    mask = (1 << bytesize) - 1
    pats = [
        ("Toutes les valeurs 00→FF", bytes(range(256)),
         "Transparence : chaque valeur d'octet passe sans altération."),
        ("0x55 × 64 (horloge)", b"\x55" * 64,
         "Alternance 0101… : fréquence maximale = débit/2. Sensible à la distorsion de largeur d'impulsion."),
        ("0xAA × 64", b"\xAA" * 64, "Alternance inversée."),
        ("0x00 × 64 (longs 0)", b"\x00" * 64,
         "9 bits à 0 consécutifs par caractère : teste le maintien du niveau bas (couplage, polarisation)."),
        ("0xFF × 64 (longs 1)", b"\xFF" * 64,
         "Seul le bit de start est bas : impulsions isolées très courtes."),
        ("0x0F / 0xF0", b"\x0F\xF0" * 32, "Demi-octets : transitions espacées de 4 bits."),
        ("Bit 1 baladeur", bytes(1 << (i % 8) for i in range(64)), "Un seul bit à 1 qui se décale."),
        ("Bit 0 baladeur", bytes(0xFF ^ (1 << (i % 8)) for i in range(64)), "Un seul bit à 0 qui se décale."),
        ("PRBS-15 (512 o)", prbs15(512), "Pseudo-aléatoire normalisé : toutes les combinaisons de transitions."),
    ]
    if bytesize < 8:
        pats = [(n, bytes(b & mask for b in d), w) for n, d, w in pats]
    return pats


# ---------------------------------------------------------------------------
# Trames Modbus RTU
# ---------------------------------------------------------------------------
def modbus_frame(pdu: bytes) -> bytes:
    return pdu + crc16_modbus(pdu).to_bytes(2, "little")


def modbus_request(slave: int, func: int, addr: int, qty_or_value: int,
                   values: Sequence[int] = ()) -> bytes:
    if func in (1, 2, 3, 4, 5, 6):
        pdu = bytes([slave, func]) + addr.to_bytes(2, "big") + (qty_or_value & 0xFFFF).to_bytes(2, "big")
    elif func == 16:
        vals = list(values) or [0] * qty_or_value
        pdu = bytes([slave, 16]) + addr.to_bytes(2, "big") + len(vals).to_bytes(2, "big") + bytes([2 * len(vals)])
        for v in vals:
            pdu += (v & 0xFFFF).to_bytes(2, "big")
    elif func == 15:
        n = qty_or_value
        nbytes = (n + 7) // 8
        pdu = bytes([slave, 15]) + addr.to_bytes(2, "big") + n.to_bytes(2, "big") + bytes([nbytes]) + bytes(nbytes)
    else:
        raise ValueError("fonction Modbus non gérée")
    return modbus_frame(pdu)


def typical_modbus_frames() -> List[Tuple[str, bytes]]:
    regs = [0x1234, 0x0000, 0xFFFF, 0x00FF, 0xFF00, 0x5555, 0xAAAA, 0x0A0D, 0x8000, 0x7FFF]
    resp03 = bytes([0x01, 0x03, 2 * len(regs)]) + b"".join(r.to_bytes(2, "big") for r in regs)
    return [
        ("Lecture 10 registres (01 03)", modbus_request(1, 3, 0x0000, 10)),
        ("Réponse 10 registres (01 03 14 …)", modbus_frame(resp03)),
        ("Écriture 1 registre (01 06)", modbus_request(1, 6, 0x0010, 0x0A0D)),
        ("Écriture 4 registres (01 10)", modbus_request(1, 16, 0x0100, 4, [0x0000, 0xFFFF, 0x5555, 0xAAAA])),
        ("Écriture bobine ON (01 05 FF00)", modbus_request(1, 5, 0x0020, 0xFF00)),
        ("Lecture entrées esclave 247 (F7 04)", modbus_request(247, 4, 0x7530, 125)),
        ("Exception (01 83 02)", modbus_frame(bytes([0x01, 0x83, 0x02]))),
        ("Broadcast écriture (00 06)", modbus_request(0, 6, 0x0001, 0x0001)),
        ("Réponse longue 125 registres", modbus_frame(bytes([0x01, 0x03, 250]) + prbs15(250, 0x1ACE))),
    ]


# ---------------------------------------------------------------------------
# Trame UART bit à bit (pour le dessin oscilloscope)
# ---------------------------------------------------------------------------
def uart_bits(byte: int, s: SerialSettings) -> List[Tuple[str, int]]:
    """Liste (étiquette, niveau logique TTL) d'un caractère : start, données LSB d'abord, parité, stop."""
    bits = [("St", 0)]
    data = [(byte >> i) & 1 for i in range(s.bytesize)]
    bits += [(f"D{i}", b) for i, b in enumerate(data)]
    if s.parity != "N":
        ones = sum(data)
        p = {"E": ones & 1, "O": 1 - (ones & 1), "M": 1, "S": 0}[s.parity]
        bits.append(("P", p))
    nstop = 2 if s.stopbits >= 2 else 1
    bits += [("Sp", 1)] * nstop
    return bits


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------
def stats(values: Sequence[float]) -> Dict[str, float]:
    v = sorted(values)
    n = len(v)
    if not n:
        return {}
    mean = sum(v) / n
    var = sum((x - mean) ** 2 for x in v) / (n - 1) if n > 1 else 0.0

    def pct(p):
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return v[f] + (v[c] - v[f]) * (k - f)
    return {"n": n, "min": v[0], "max": v[-1], "mean": mean, "median": pct(0.5),
            "p95": pct(0.95), "std": var ** 0.5}


def ber_upper_bound(bits: int, errors: int) -> Optional[float]:
    """Borne supérieure BER à 95 % de confiance (règle des 3 si aucune erreur)."""
    if bits <= 0:
        return None
    if errors == 0:
        return 3.0 / bits
    return None
