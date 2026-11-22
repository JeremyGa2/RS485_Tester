"""Schémas dessinés sur un tk.Canvas : chaîne de test, trame UART, chronogrammes."""
from __future__ import annotations

from typing import List, Optional, Sequence

from . import core

# Palette (fond sombre)
BG = "#1e1f22"
FG = "#e6e6e6"
DIM = "#8b8f97"
GRID = "#33363c"
BOX = "#2b2d31"
BOX_EDGE = "#5a5f69"
MODULE = "#5b3a8c"
MODULE_EDGE = "#a07ee0"
GREEN = "#3ddc84"
ORANGE = "#ffb02e"
BLUE = "#4ea1ff"
RED = "#ff5c5c"
CYAN = "#3ad1d1"
YELLOW = "#f5d547"

FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_S = ("Segoe UI", 8)
MONO = ("Consolas", 9)


def _size(c, w=None, h=None):
    return w or max(c.winfo_width(), 300), h or max(c.winfo_height(), 120)


def _box(c, x, y, w, h, title, sub="", fill=BOX, edge=BOX_EDGE, tfill=FG):
    c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=edge, width=1.5)
    c.create_text(x + w / 2, y + h / 2 - (7 if sub else 0), text=title, fill=tfill, font=FONT_B, width=w - 6)
    if sub:
        c.create_text(x + w / 2, y + h / 2 + 9, text=sub, fill=DIM, font=FONT_S, width=w - 4)


def _arrow(c, x1, y1, x2, y2, color, width=2.5, dash=None, text="", tpos="above"):
    c.create_line(x1, y1, x2, y2, fill=color, width=width, arrow="last", arrowshape=(10, 12, 4), dash=dash)
    if text:
        dy = -9 if tpos == "above" else 10
        c.create_text((x1 + x2) / 2, (y1 + y2) / 2 + dy, text=text, fill=color, font=FONT_S)


def _probe(c, x, y, label="sonde"):
    c.create_oval(x - 6, y - 6, x + 6, y + 6, outline=YELLOW, width=2)
    c.create_line(x, y + 6, x, y + 16, fill=YELLOW, width=2)
    c.create_text(x, y + 24, text=label, fill=YELLOW, font=FONT_S)


# ---------------------------------------------------------------------------
# Chaîne de test
# ---------------------------------------------------------------------------
def draw_topology(c, mode: str = "path_both", port_a: str = "Port A", port_b: str = "Port B",
                  w: Optional[int] = None, h: Optional[int] = None):
    """mode : path_both, path_one, turnaround, stream, generator, monitor."""
    c.delete("all")
    W, H = _size(c, w, h)
    c.configure(bg=BG)
    n = 6
    gap = max(14, W * 0.035)
    gaps = [gap, gap, max(64, gap * 2.6), gap, gap]     # plus d'espace pour les fibres
    bw = (W - 20 - sum(gaps)) / n
    bh = min(56, H * 0.36)
    y = H * 0.30
    xs = [10.0]
    for g in gaps:
        xs.append(xs[-1] + bw + g)
    titles = [("PC", port_a), ("Adaptateur A", "USB ↔ RS485"), ("Module 1", "RS485 ↔ fibre"),
              ("Module 2", "fibre ↔ RS485"), ("Adaptateur B", "USB ↔ RS485"), ("PC", port_b)]
    for i, (t, sub) in enumerate(titles):
        is_mod = i in (2, 3)
        _box(c, xs[i], y, bw, bh, t, sub, fill=MODULE if is_mod else BOX,
             edge=MODULE_EDGE if is_mod else BOX_EDGE)
    cy = y + bh / 2
    # liaisons physiques
    for i, lab in [(0, "USB"), (1, "RS485"), (3, "RS485"), (4, "USB")]:
        x1, x2 = xs[i] + bw, xs[i + 1]
        c.create_line(x1, cy, x2, cy, fill=DIM, width=2)
        c.create_text((x1 + x2) / 2, cy - 8, text=lab, fill=DIM, font=FONT_S)
    # fibres croisées
    fx1, fx2 = xs[2] + bw, xs[3]
    c.create_line(fx1, cy - 10, fx2, cy - 10, fill=ORANGE, width=2)
    c.create_line(fx1, cy + 10, fx2, cy + 10, fill=ORANGE, width=2)
    c.create_text((fx1 + fx2) / 2, y - 8, text="fibres", fill=ORANGE, font=FONT_S)
    c.create_text(fx1 + 2, cy - 18, text="OUT1", fill=DIM, font=("Segoe UI", 7), anchor="w")
    c.create_text(fx2 - 2, cy - 18, text="IN2", fill=DIM, font=("Segoe UI", 7), anchor="e")
    c.create_text(fx1 + 2, cy + 18, text="IN1", fill=DIM, font=("Segoe UI", 7), anchor="w")
    c.create_text(fx2 - 2, cy + 18, text="OUT2", fill=DIM, font=("Segoe UI", 7), anchor="e")

    top = y - 26
    bot = y + bh + 22
    xl, xr = xs[0] + bw / 2, xs[5] + bw / 2
    if mode in ("path_both", "turnaround", "monitor"):
        _arrow(c, xl, top, xr, top, GREEN, text="A → B : trame émise par A, comparée octet par octet sur B")
        _arrow(c, xr, bot, xl, bot, BLUE, text="B → A : même vérification dans l'autre sens", tpos="below")
    elif mode == "path_one":
        _arrow(c, xl, top, xr, top, GREEN, text="write() sur A  ⟶  dernier octet reçu sur B   (Δt mesuré)")
    elif mode == "stream":
        _arrow(c, xl, top, xr, top, GREEN, width=5,
               text="flux continu de paquets  [A5 5A | n° | lg | données | CRC]")
        c.create_text((xl + xr) / 2, bot, text="Comptage : reçus OK · corrompus (bits faux) · perdus · débit",
                      fill=DIM, font=FONT_S)
    elif mode == "generator":
        _arrow(c, xl, top, xr, top, GREEN, width=4, text="trame répétée (période réglable)")
        c.create_text((xl + xr) / 2, bot, text="Vérification optionnelle en temps réel sur l'autre port",
                      fill=DIM, font=FONT_S)
    if mode == "turnaround":
        bx = xs[4] + bw / 2
        c.create_text(bx, y + bh + 8, text="attente du délai", fill=ORANGE, font=FONT_S)
        c.create_oval(xs[3] - 6, y - 6, xs[3] + bw + 6, y + bh + 6, outline=RED, width=2)
        c.create_text(xs[3] + bw / 2, y + bh + 36, text="module testé : doit libérer le bus", fill=RED, font=FONT_S)
    # points de sonde
    if mode in ("path_both", "path_one", "generator", "stream", "turnaround"):
        _probe(c, (xs[1] + bw + xs[2]) / 2, cy + 22, "sonde 1")
        _probe(c, (xs[3] + bw + xs[4]) / 2, cy + 22, "sonde 2")


# ---------------------------------------------------------------------------
# Trame UART
# ---------------------------------------------------------------------------
def draw_uart(c, data: bytes, s: core.SerialSettings, max_chars: int = 3,
              w: Optional[int] = None, h: Optional[int] = None, show_diff: bool = True):
    c.delete("all")
    W, H = _size(c, w, h)
    c.configure(bg=BG)
    data = data[:max_chars] if data else b"\x55"
    bits: List[tuple] = []
    for ch_i, b in enumerate(data):
        for lab, lvl in core.uart_bits(b, s):
            bits.append((lab, lvl, ch_i))
    idle = 1.5
    total = len(bits) + 2 * idle
    x0, x1 = 70, W - 12
    bw = (x1 - x0) / total
    traces = [("TTL (TXD / RO)", GREEN)]
    if show_diff:
        traces.append(("VA − VB", CYAN))
    th = (H - 58) / len(traces)
    for ti, (name, col) in enumerate(traces):
        ytop = 36 + ti * th
        yhi, ylo = ytop + 8, ytop + th - 16
        c.create_text(4, (yhi + ylo) / 2, text=name, fill=col, font=FONT_S, anchor="w")
        if ti == 1:
            c.create_text(x0 - 4, yhi, text="> +200 mV", fill=DIM, font=("Segoe UI", 7), anchor="e")
            c.create_text(x0 - 4, ylo, text="< −200 mV", fill=DIM, font=("Segoe UI", 7), anchor="e")
        pts = [(x0, yhi), (x0 + idle * bw, yhi)]
        x = x0 + idle * bw
        prev = 1
        for lab, lvl, _ in bits:
            yy = yhi if lvl else ylo
            if lvl != prev:
                pts.append((x, yhi if prev else ylo))
            pts.append((x, yy))
            x += bw
            pts.append((x, yy))
            prev = lvl
        if prev != 1:
            pts.append((x, yhi))
        pts.append((x1, yhi))
        flat = [v for p in pts for v in p]
        c.create_line(*flat, fill=col, width=2)
    # repères de bits
    x = x0 + idle * bw
    ylab = H - 14
    cur = -1
    for lab, lvl, ch_i in bits:
        color = ORANGE if lab == "St" else (RED if lab == "Sp" else (YELLOW if lab == "P" else DIM))
        c.create_line(x, 32, x, H - 24, fill=GRID, dash=(2, 3))
        if bw > 13:
            c.create_text(x + bw / 2, ylab, text=lab, fill=color, font=("Segoe UI", 7))
        if ch_i != cur:
            cur = ch_i
            c.create_text(x + 2, 25, text=f"0x{data[ch_i]:02X}", fill=FG, font=FONT_B, anchor="w")
        x += bw
    c.create_text(W - 8, 9, anchor="e", fill=DIM, font=FONT_S,
                  text=f"{s.describe()} · bit = {core.fmt_time(s.bit_time())} · "
                       f"caractère = {core.fmt_time(s.char_time())} ({s.bits_per_char():g} bits) · LSB en premier")


# ---------------------------------------------------------------------------
# Chronogramme de retournement
# ---------------------------------------------------------------------------
def draw_turnaround(c, w: Optional[int] = None, h: Optional[int] = None):
    c.delete("all")
    W, H = _size(c, w, h)
    c.configure(bg=BG)
    x0 = 120
    lanes = [("Ligne côté A", GREEN), ("Ligne côté B", BLUE), ("DE module 2", RED)]
    lh = (H - 30) / len(lanes)
    xa1, xa2 = x0 + 10, x0 + (W - x0) * 0.35
    lat = (W - x0) * 0.04
    xb1, xb2 = xa1 + lat, xa2 + lat
    xr1 = xb2 + (W - x0) * 0.18
    xr2 = xr1 + (xa2 - xa1) * 0.8
    for i, (name, col) in enumerate(lanes):
        y = 14 + i * lh + lh / 2
        c.create_text(6, y, text=name, fill=col, font=FONT_S, anchor="w")
        c.create_line(x0, y, W - 8, y, fill=GRID)
    ya = 14 + lh / 2
    yb = 14 + lh * 1.5
    yd = 14 + lh * 2.5
    hh = lh * 0.28
    c.create_rectangle(xa1, ya - hh, xa2, ya + hh, outline=GREEN, fill="#123d27")
    c.create_text((xa1 + xa2) / 2, ya, text="trame aller", fill=FG, font=FONT_S)
    c.create_rectangle(xb1, yb - hh, xb2, yb + hh, outline=GREEN, fill="#123d27")
    c.create_text((xb1 + xb2) / 2, yb, text="aller (reçue par B)", fill=FG, font=FONT_S)
    c.create_rectangle(xr1, yb - hh, xr2, yb + hh, outline=BLUE, fill="#13304f")
    c.create_text((xr1 + xr2) / 2, yb, text="réponse de B", fill=FG, font=FONT_S)
    c.create_rectangle(xr1 + lat, ya - hh, xr2 + lat, ya + hh, outline=BLUE, fill="#13304f")
    c.create_text((xr1 + xr2) / 2 + lat, ya, text="réponse reçue par A", fill=FG, font=FONT_S)
    # DE du module 2
    xde = xb2 + (xr1 - xb2) * 0.35
    c.create_line(x0, yd + hh, xb1, yd + hh, xb1, yd - hh, xde, yd - hh, xde, yd + hh, W - 8, yd + hh,
                  fill=RED, width=2)
    c.create_text(xde + 6, yd, text="← fin du retournement", fill=RED, font=FONT_S, anchor="w")
    # cote du délai (entre les voies A et B, zone libre)
    yq = (ya + yb) / 2
    c.create_line(xb2, yq, xr1, yq, fill=ORANGE, arrow="both")
    c.create_text((xb2 + xr1) / 2, yq - 9, text="silence = délai PC + latences USB", fill=ORANGE, font=FONT_S)
    c.create_line(xb2, yq - 4, xb2, yd + hh, fill=ORANGE, dash=(2, 2))
    c.create_line(xr1, yq - 4, xr1, yd + hh, fill=ORANGE, dash=(2, 2))
    c.create_text(W - 10, yd - hh - 6, text="si B répond avant que DE retombe → collision",
                  fill=DIM, font=FONT_S, anchor="e")


# ---------------------------------------------------------------------------
# Format des paquets de flux
# ---------------------------------------------------------------------------
def draw_packet(c, plen: int = 32, w: Optional[int] = None, h: Optional[int] = None):
    c.delete("all")
    W, H = _size(c, w, h)
    c.configure(bg=BG)
    fields = [("SYNC", "A5 5A", 2, ORANGE), ("N° séquence", "4 o", 4, BLUE), ("Long.", "1 o", 1.5, CYAN),
              ("Données pseudo-aléatoires", f"{plen} o (déduites du n°)", 9, GREEN), ("CRC16", "2 o", 2, RED)]
    tot = sum(f[2] for f in fields)
    x = 10
    y = H / 2 - 20
    for name, sub, wgt, col in fields:
        fw = (W - 20) * wgt / tot
        c.create_rectangle(x, y, x + fw, y + 40, outline=col, fill=BOX, width=2)
        c.create_text(x + fw / 2, y + 13, text=name, fill=col, font=FONT_B, width=fw - 4)
        c.create_text(x + fw / 2, y + 29, text=sub, fill=DIM, font=FONT_S, width=fw - 4)
        x += fw
    c.create_text(W / 2, y + 56, fill=DIM, font=FONT_S,
                  text="Le récepteur connaît le contenu exact de chaque paquet grâce à son numéro : "
                       "comparaison bit à bit, pertes et doublons détectés.")


# ---------------------------------------------------------------------------
# Histogramme
# ---------------------------------------------------------------------------
def draw_histogram(c, values_ms: Sequence[float], wire_ms: float = 0.0, title: str = "",
                   w: Optional[int] = None, h: Optional[int] = None):
    c.delete("all")
    W, H = _size(c, w, h)
    c.configure(bg=BG)
    if not values_ms:
        c.create_text(W / 2, H / 2, text="aucune donnée", fill=DIM, font=FONT)
        return
    lo, hi = min(values_ms), max(values_ms)
    if hi - lo < 1e-6:
        hi = lo + 0.001
    nb = min(40, max(8, int(len(values_ms) ** 0.5) * 2))
    step = (hi - lo) / nb
    counts = [0] * nb
    for v in values_ms:
        counts[min(nb - 1, int((v - lo) / step))] += 1
    m = max(counts)
    x0, x1, y0, y1 = 44, W - 10, 22, H - 26
    c.create_text(x0, 10, text=title, fill=FG, font=FONT_B, anchor="w")
    bwid = (x1 - x0) / nb
    for i, n in enumerate(counts):
        bh = (y1 - y0) * n / m
        c.create_rectangle(x0 + i * bwid + 1, y1 - bh, x0 + (i + 1) * bwid - 1, y1, fill=BLUE, outline="")
    c.create_line(x0, y1, x1, y1, fill=DIM)
    c.create_line(x0, y0, x0, y1, fill=DIM)
    for frac in (0, 0.5, 1):
        v = lo + (hi - lo) * frac
        c.create_text(x0 + (x1 - x0) * frac, y1 + 10, text=f"{v:.3f} ms", fill=DIM, font=FONT_S)
    c.create_text(x0 - 4, y0, text=str(m), fill=DIM, font=FONT_S, anchor="e")
    if wire_ms and lo <= wire_ms <= hi:
        xw = x0 + (wire_ms - lo) / (hi - lo) * (x1 - x0)
        c.create_line(xw, y0, xw, y1, fill=ORANGE, dash=(3, 2))
    c.create_text(x1, 10, anchor="e", fill=ORANGE, font=FONT_S,
                  text=f"durée ligne théorique : {wire_ms:.3f} ms")
