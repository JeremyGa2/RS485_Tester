"""Liaison RS485 simulée (SIM-A <-> SIM-B).

Sert à :
  * essayer le logiciel sans matériel (formation, démonstration) ;
  * **valider le testeur lui-même** : on injecte des erreurs connues (BER,
    temps de retournement, latence, débit différent de chaque côté) et on
    vérifie que les tests les détectent.

Le modèle respecte la durée réelle de chaque caractère sur la ligne
(bits de start + données + parité + stop) au débit choisi.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque

import serial


class SimParams:
    def __init__(self):
        self.latency_ms = 1.0        # latence aller (USB + modules), répartie émission/réception
        self.turnaround_ms = 0.5     # temps de libération du bus par le module après une réception
        self.ber = 0.0               # taux d'erreur binaire injecté
        self.loss_prob = 0.0         # probabilité de perdre un octet entier


class SimLink:
    def __init__(self):
        self.params = SimParams()
        self.cond = threading.Condition()
        self.ports = {}
        self.line_free = {"A": 0.0, "B": 0.0}
        self.bus_busy_until = {"A": 0.0, "B": 0.0}
        self.rng = random.Random()

    def open(self, device: str, settings, timeout: float):
        side = device[-1].upper()
        with self.cond:
            p = self.ports.get(side)
            if p is not None and p.is_open:
                raise serial.SerialException(f"{device} déjà ouvert")
            p = SimPort(self, side, settings, timeout)
            self.ports[side] = p
            return p

    # ------------------------------------------------------------------
    def transmit(self, side: str, data: bytes) -> None:
        peer = "B" if side == "A" else "A"
        prm = self.params
        with self.cond:
            src = self.ports[side]
            dst = self.ports.get(peer)
            now = time.perf_counter()
            ct = src.char_time()
            half_lat = prm.latency_ms / 2000.0
            start = max(now + half_lat, self.line_free[side])
            blocked_until = self.bus_busy_until[side] + prm.turnaround_ms / 1000.0
            mismatch = dst is None or not _compatible(src, dst)
            items = []
            for i, b in enumerate(data):
                t_start = start + i * ct
                t_end = t_start + ct
                if t_start < blocked_until:
                    # le module côté émetteur pilote encore le bus : collision, octet perdu
                    continue
                if prm.loss_prob and self.rng.random() < prm.loss_prob:
                    continue
                if mismatch:
                    b = self.rng.randrange(256)
                elif prm.ber:
                    p_byte = 1.0 - (1.0 - prm.ber) ** 8
                    if self.rng.random() < p_byte:
                        b ^= 1 << self.rng.randrange(8)
                if dst is not None and dst.bytesize < 8:
                    b &= (1 << dst.bytesize) - 1
                items.append((t_end + half_lat, b))
            end = start + len(data) * ct
            self.line_free[side] = end
            # le module côté destinataire pilote son bus RS485 jusqu'à la fin de la trame
            self.bus_busy_until[peer] = max(self.bus_busy_until[peer], end)
            if dst is not None and dst.is_open:
                dst.rx.extend(items)
            self.cond.notify_all()


def _compatible(a, b) -> bool:
    if abs(a.baudrate - b.baudrate) / max(a.baudrate, b.baudrate) > 0.03:
        return False
    return (a.bytesize, a.parity, float(a.stopbits)) == (b.bytesize, b.parity, float(b.stopbits))


class SimPort:
    """Imite l'interface de serial.Serial utilisée par le testeur."""

    def __init__(self, link: SimLink, side: str, s, timeout: float):
        self.link = link
        self.side = side
        self.port = f"SIM-{side}"
        self.baudrate = int(s.baudrate)
        self.bytesize = int(s.bytesize)
        self.parity = s.parity
        self.stopbits = float(s.stopbits)
        self.timeout = timeout
        self.write_timeout = None
        self.rts = False
        self.dtr = False
        self.is_open = True
        self.rx = deque()

    def char_time(self) -> float:
        bits = 1 + self.bytesize + (0 if self.parity == "N" else 1) + float(self.stopbits)
        return bits / self.baudrate

    # -- API serial ----------------------------------------------------
    def _available(self, now: float) -> int:
        n = 0
        for t, _ in self.rx:
            if t > now:
                break
            n += 1
        return n

    @property
    def in_waiting(self) -> int:
        with self.link.cond:
            return self._available(time.perf_counter())

    def read(self, size: int = 1) -> bytes:
        deadline = None if self.timeout is None else time.perf_counter() + self.timeout
        out = bytearray()
        with self.link.cond:
            while True:
                now = time.perf_counter()
                while self.rx and self.rx[0][0] <= now and len(out) < size:
                    out.append(self.rx.popleft()[1])
                if len(out) >= size or not self.is_open:
                    return bytes(out)
                if deadline is not None and now >= deadline:
                    return bytes(out)
                wait = 0.01
                if self.rx:
                    wait = max(0.0, min(wait, self.rx[0][0] - now))
                if deadline is not None:
                    wait = min(wait, deadline - now)
                self.link.cond.wait(max(wait, 0.0002))

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise serial.SerialException("port fermé")
        # tampon d'émission limité : on bloque si plus de 0,25 s en attente
        while self.link.line_free[self.side] - time.perf_counter() > 0.25:
            time.sleep(0.005)
        self.link.transmit(self.side, bytes(data))
        return len(data)

    def flush(self) -> None:
        while time.perf_counter() < self.link.line_free[self.side]:
            time.sleep(0.0005)

    def reset_input_buffer(self) -> None:
        with self.link.cond:
            self.rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def send_break(self, duration: float = 0.25) -> None:
        time.sleep(duration)

    def close(self) -> None:
        with self.link.cond:
            self.is_open = False
            self.rx.clear()
            self.link.cond.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


_LINK = None


def get_sim_link() -> SimLink:
    global _LINK
    if _LINK is None:
        _LINK = SimLink()
    return _LINK
