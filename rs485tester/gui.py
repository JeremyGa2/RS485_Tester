"""Interface graphique — simple et guidée.

Pages : Diagnostic (en un clic) · Envoi continu (oscilloscope) · Envoi manuel ·
Tests avancés · Aide. La logique de test est dans diagnostic.py et engine.py.
"""
from __future__ import annotations

import dataclasses
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

import customtkinter as ctk
import serial

from . import APP_NAME, APP_VERSION, core, diagnostic, diagrams, engine, report
from .core import SerialSettings, fmt_rate, fmt_time, to_ascii, to_hex
from .engine import ABORTED, ERROR, NOK, OK, WARN, TESTS_BY_ID
from .simport import get_sim_link

IS_WIN = sys.platform.startswith("win")
MONO = ("Consolas", 12) if IS_WIN else ("Menlo", 12)
MONO_S = ("Consolas", 10) if IS_WIN else ("Menlo", 10)

# palette
BG = "#141518"
CARD = diagrams.BG            # même fond que les schémas
CARD2 = "#26282d"
LINE = "#33363c"
TXT = "#e8e9eb"
MUTED = "#9aa0a8"
ACCENT = "#3b82f6"
ACCENT_D = "#2563eb"
GREEN = "#22c55e"
GREEN_D = "#15803d"
RED = "#ef4444"
RED_D = "#b91c1c"
ORANGE = "#f59e0b"
NAV_SEL = "#2a3446"

STATUS_STYLE = {  # statut -> (icône, couleur)
    "": ("○", "#5b6068"), "EN COURS": ("◌", ORANGE), OK: ("✓", GREEN), NOK: ("✗", RED),
    WARN: ("!", ORANGE), ERROR: ("✗", RED), ABORTED: ("■", MUTED), diagnostic.SKIP: ("–", "#5b6068"),
}
FORMATS = ["8N1", "8E1", "8O1", "8N2", "7E1", "7O1", "7N2"]
LOG_COLORS = {"info": TXT, "ok": "#4ade80", "err": "#f87171", "warn": "#fbbf24", "tx": "#60a5fa",
              "rx": "#22d3ee", "dim": "#8b8f97", "title": "#ffffff"}


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RS485_COM_Tester")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~")
    return os.path.join(d, "settings_v6.json")


def F(size: int = 13, bold: bool = False):
    return ctk.CTkFont(size=size, weight="bold" if bold else "normal")


def card(master, **kw):
    return ctk.CTkFrame(master, fg_color=CARD, corner_radius=12, border_width=1, border_color=LINE, **kw)


def page_title(master, title: str, sub: str, row: int = 0):
    ctk.CTkLabel(master, text=title, font=F(22, True), anchor="w").grid(row=row, column=0, columnspan=2,
                                                                         sticky="ew", padx=4, pady=(4, 0))
    ctk.CTkLabel(master, text=sub, font=F(13), text_color=MUTED, anchor="w", justify="left",
                 wraplength=980).grid(row=row + 1, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 12))


def build_frame(text: str, mode: str, add_crc: bool) -> bytes:
    data = core.parse_hex(text) if mode == "HEX" else core.parse_ascii(text)
    if add_crc and data:
        data += core.crc16_modbus(data).to_bytes(2, "little")
    return data


class Redrawable(tk.Canvas):
    """Canvas qui se redessine quand sa largeur change."""

    def __init__(self, master, height: int, draw):
        super().__init__(master, height=height, bg=CARD, highlightthickness=0)
        self.draw = draw
        self._lastw = None
        self.bind("<Configure>", self._cfg)

    def _cfg(self, e):
        if e.width != self._lastw:
            self._lastw = e.width
            self.redraw()

    def redraw(self):
        if self.winfo_width() > 50:
            try:
                self.draw(self)
            except Exception:   # noqa  un schéma ne doit jamais bloquer l'appli
                pass


# =====================================================================
# Application
# =====================================================================
class App(ctk.CTk):
    def __init__(self):
        core.enable_high_res_timer()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=BG)
        self.title(f"{APP_NAME} {APP_VERSION} — Diagnostic RS485")
        self._set_icon()
        self.geometry("1320x860")
        self.minsize(1100, 720)
        self.cfg = self._load_cfg()
        self.q: "queue.Queue" = queue.Queue()
        self.port_infos: Dict[str, core.PortInfo] = {}
        self.activity: Optional[str] = None      # "diag", "adv", "gen", "mon"
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.generator: Optional[engine.Generator] = None
        self.monitor: Optional[engine.Monitor] = None

        c = self.cfg
        self.v_dir = tk.StringVar(value=c.get("direction", "Automatique"))
        self.v_echo = tk.BooleanVar(value=c.get("echo", False))
        self.v_show_sim = tk.BooleanVar(value=c.get("show_sim", True))
        self.v_sim_ber = tk.StringVar(value=str(c.get("sim_ber", "0")))
        self.v_sim_turn = tk.StringVar(value=str(c.get("sim_turn", "0.5")))
        self.trace = c.get("trace", {})

        self._style_tree()
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_sidebar()
        self._build_header()
        self.pages_host = ctk.CTkFrame(self, fg_color="transparent")
        self.pages_host.grid(row=1, column=1, sticky="nsew", padx=(0, 16), pady=(0, 4))
        self.pages_host.grid_columnconfigure(0, weight=1)
        self.pages_host.grid_rowconfigure(0, weight=1)
        self.pages = {
            "diag": DiagPage(self.pages_host, self),
            "stream": StreamPage(self.pages_host, self),
            "manual": ManualPage(self.pages_host, self),
            "adv": AdvancedPage(self.pages_host, self),
            "help": HelpPage(self.pages_host, self),
        }
        self._build_footer()
        self.current = None
        self._refresh_ports(force=True)
        self._update_info()
        self.show("diag")
        self.after(40, self._poll)
        self.after(2000, self._auto_ports)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log(f"{APP_NAME} {APP_VERSION} prêt. Astuce : les ports SIM-A / SIM-B simulent une liaison pour "
                 "essayer sans matériel.", "dim")

    def _set_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ico = os.path.join(base, "assets", "app.ico")
        if IS_WIN and os.path.exists(ico):
            try:
                self.iconbitmap(ico)
                self.after(250, lambda: self.iconbitmap(ico))   # customtkinter remet son icône au démarrage
            except tk.TclError:
                pass

    # ------------------------------------------------------------ config
    def _load_cfg(self) -> dict:
        try:
            with open(config_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_cfg(self):
        cfg = {"port_a": self.dev_a(), "port_b": self.dev_b(), "baud": self.v_baud.get(), "fmt": self.v_fmt.get(),
               "direction": self.v_dir.get(), "echo": self.v_echo.get(), "show_sim": self.v_show_sim.get(),
               "sim_ber": self.v_sim_ber.get(), "sim_turn": self.v_sim_turn.get(), "trace": self.trace}
        for p in self.pages.values():
            if hasattr(p, "save"):
                cfg.update(p.save())
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=1, ensure_ascii=False)
        except OSError:
            pass

    def _on_close(self):
        self.stop_activity()
        self._save_cfg()
        self.destroy()

    def _style_tree(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Dark.Treeview", background=CARD, fieldbackground=CARD, foreground=TXT, rowheight=24,
                     borderwidth=0, font=("Segoe UI", 10))
        st.configure("Dark.Treeview.Heading", background=CARD2, foreground=TXT, relief="flat",
                     font=("Segoe UI", 10, "bold"))
        st.map("Dark.Treeview", background=[("selected", NAV_SEL)])
        st.map("Dark.Treeview.Heading", background=[("active", "#33363c")])

    # ------------------------------------------------------------ barre latérale
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, fg_color="#101114", corner_radius=0, width=220)
        sb.grid(row=0, column=0, rowspan=4, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(10, weight=1)
        ctk.CTkLabel(sb, text="RS485 Tester", font=F(20, True)).grid(row=0, column=0, sticky="w", padx=20,
                                                                     pady=(22, 0))
        ctk.CTkLabel(sb, text=f"version {APP_VERSION} · diagnostic de liaison", font=F(11),
                     text_color=MUTED).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 22))
        self.nav = {}
        items = [("diag", "✓   Diagnostic"), ("stream", "∿   Envoi continu"), ("manual", "⇄   Envoi manuel"),
                 ("adv", "⚗   Tests avancés"), ("help", "?    Aide")]
        for i, (key, text) in enumerate(items):
            b = ctk.CTkButton(sb, text=text, anchor="w", height=42, width=190, corner_radius=8, font=F(14),
                              fg_color="transparent", hover_color="#1f2430", text_color=TXT,
                              command=lambda k=key: self.show(k))
            b.grid(row=2 + i, column=0, padx=14, pady=3, sticky="ew")
            self.nav[key] = b
        ctk.CTkButton(sb, text="⚙   Réglages", anchor="w", height=38, width=190, corner_radius=8, font=F(13),
                      fg_color="transparent", hover_color="#1f2430", text_color=MUTED,
                      command=self.open_settings).grid(row=11, column=0, padx=14, pady=(0, 18), sticky="ew")

    def show(self, key: str):
        if self.current == key:
            return
        old = self.current
        if old == "manual" and key != "manual":
            self.pages["manual"].on_leave()
        self.current = key
        for k, b in self.nav.items():
            b.configure(fg_color=NAV_SEL if k == key else "transparent")
        for k, p in self.pages.items():
            if k != key:
                p.grid_forget()
        self.pages[key].grid(row=0, column=0, sticky="nsew")
        if hasattr(self.pages[key], "on_show"):
            self.pages[key].on_show()

    # ------------------------------------------------------------ en-tête connexion
    def _build_header(self):
        h = card(self)
        h.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=(16, 12))
        c = self.cfg
        inner = ctk.CTkFrame(h, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        ctk.CTkLabel(inner, text="Port A", font=F(13, True), text_color="#4ade80").grid(row=0, column=0, padx=(0, 6))
        self.cb_a = ctk.CTkComboBox(inner, width=250, values=[], state="readonly",
                                    command=lambda _v: self._ports_changed())
        self.cb_a.grid(row=0, column=1)
        ctk.CTkLabel(inner, text="⇄", font=F(20), text_color=MUTED).grid(row=0, column=2, padx=10)
        ctk.CTkLabel(inner, text="Port B", font=F(13, True), text_color="#60a5fa").grid(row=0, column=3, padx=(0, 6))
        self.cb_b = ctk.CTkComboBox(inner, width=250, values=[], state="readonly",
                                    command=lambda _v: self._ports_changed())
        self.cb_b.grid(row=0, column=4)
        ctk.CTkButton(inner, text="↻", width=32, fg_color=CARD2, hover_color=LINE,
                      command=lambda: self._refresh_ports(force=True)).grid(row=0, column=5, padx=(6, 22))
        ctk.CTkLabel(inner, text="Vitesse", font=F(13, True)).grid(row=0, column=6, padx=(0, 6))
        self.v_baud = tk.StringVar(value=str(c.get("baud", "9600")))
        ctk.CTkComboBox(inner, width=120, values=[str(b) for b in core.BAUD_PRESETS], variable=self.v_baud,
                        command=lambda _v: self._settings_changed()).grid(row=0, column=7)
        ctk.CTkLabel(inner, text="bauds", text_color=MUTED).grid(row=0, column=8, padx=(4, 18))
        ctk.CTkLabel(inner, text="Format", font=F(13, True)).grid(row=0, column=9, padx=(0, 6))
        self.v_fmt = tk.StringVar(value=c.get("fmt", "8N1") if c.get("fmt") in FORMATS else "8N1")
        ctk.CTkComboBox(inner, width=80, values=FORMATS, variable=self.v_fmt, state="readonly",
                        command=lambda _v: self._settings_changed()).grid(row=0, column=10)
        self.v_baud.trace_add("write", lambda *_: self._settings_changed())
        self.lab_info = ctk.CTkLabel(h, text="", font=F(12), text_color=MUTED, anchor="w")
        self.lab_info.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 10))

    def _label(self, p: core.PortInfo) -> str:
        return f"{p.device} · {p.description}" if p.description and p.description != p.device else p.device

    def dev_a(self) -> str:
        return (self.cb_a.get() or "").split(" · ")[0].strip()

    def dev_b(self) -> str:
        return (self.cb_b.get() or "").split(" · ")[0].strip()

    def _refresh_ports(self, force=False):
        infos = core.list_ports(include_sim=self.v_show_sim.get())
        new = {p.device: p for p in infos}
        if not force and set(new) == set(self.port_infos):
            return
        self.port_infos = new
        labels = [self._label(p) for p in infos]
        first = not getattr(self, "_ports_init", False)
        for cb, saved in ((self.cb_a, self.cfg.get("port_a", "")), (self.cb_b, self.cfg.get("port_b", ""))):
            cur = (cb.get() or "").split(" · ")[0].strip() or (saved if first else "")
            cb.configure(values=labels or ["(aucun port détecté)"])
            if cur in new:
                cb.set(self._label(new[cur]))
            elif cur:
                cb.set(f"{cur} · débranché")
            else:
                cb.set("")
        if first:
            self._ports_init = True
            if not self.dev_a() or not self.dev_b():
                real = [p for p in infos if not p.is_sim]
                pick = real[:2] if len(real) >= 2 else [p for p in infos if p.is_sim]
                if len(pick) == 2:
                    self.cb_a.set(self._label(pick[0]))
                    self.cb_b.set(self._label(pick[1]))
        self._ports_changed()

    def _auto_ports(self):
        try:
            self._refresh_ports()
        finally:
            self.after(2000, self._auto_ports)

    def _ports_changed(self):
        self._update_info()
        for p in self.pages.values() if hasattr(self, "pages") else []:
            if hasattr(p, "on_ports"):
                p.on_ports()

    def _settings_changed(self):
        self._update_info()
        for p in self.pages.values() if hasattr(self, "pages") else []:
            if hasattr(p, "on_settings"):
                p.on_settings()

    def _update_info(self):
        parts = []
        for side, dev in (("A", self.dev_a()), ("B", self.dev_b())):
            info = self.port_infos.get(dev)
            if info:
                txt = info.chip()
                if info.is_ftdi:
                    txt += " (Latency Timer conseillé : 1 ms)"
                parts.append(f"{side} : {txt}")
            else:
                parts.append(f"{side} : {'non branché' if dev else 'à choisir'}")
        s = self.get_settings(quiet=True)
        if s:
            parts.append(f"1 bit = {fmt_time(s.bit_time())} · 1 octet = {fmt_time(s.char_time())} "
                         f"· max {fmt_rate(s.max_bytes_per_s())}")
        else:
            parts.append("vitesse invalide")
        self.lab_info.configure(text="     ".join(parts))

    # ------------------------------------------------------------ réglages lus
    def get_settings(self, quiet=False) -> Optional[SerialSettings]:
        try:
            baud = int(float(self.v_baud.get().replace(" ", "")))
            if not 50 <= baud <= 20_000_000:
                raise ValueError
        except ValueError:
            if not quiet:
                messagebox.showerror("Vitesse", f"Vitesse invalide : « {self.v_baud.get()} »")
            return None
        fmt = self.v_fmt.get()
        direction = {"Automatique": "auto", "RTS actif haut": "rts_high", "RTS actif bas": "rts_low"}.get(
            self.v_dir.get(), "auto")
        return SerialSettings(baudrate=baud, bytesize=int(fmt[0]), parity=fmt[1], stopbits=float(fmt[2:]),
                              direction=direction, local_echo=self.v_echo.get())

    def get_ports(self, need_b=True):
        a, b = self.dev_a(), self.dev_b()
        if not a or a not in self.port_infos:
            messagebox.showerror("Ports", "Choisissez le port A (en haut de la fenêtre).")
            return None
        if need_b:
            if not b or b not in self.port_infos:
                messagebox.showerror("Ports", "Choisissez le port B (en haut de la fenêtre).")
                return None
            if a == b:
                messagebox.showerror("Ports", "Les ports A et B doivent être différents.")
                return None
            if (a in core.SIM_PORTS) != (b in core.SIM_PORTS):
                messagebox.showerror("Ports", "Un port simulé ne fonctionne qu'avec l'autre port simulé.")
                return None
        return a, b

    def apply_sim(self):
        prm = get_sim_link().params
        for attr, var, d in (("ber", self.v_sim_ber, 0.0), ("turnaround_ms", self.v_sim_turn, 0.5)):
            try:
                v = float(var.get().replace(",", "."))
                if v < 0:
                    raise ValueError
            except ValueError:
                v = d
                var.set(str(d))
            setattr(prm, attr, v)

    # ------------------------------------------------------------ activités
    def busy(self) -> bool:
        return self.activity is not None

    def claim(self, name: str) -> bool:
        """Réserve les ports pour une activité ; arrête générateur/écoute si besoin."""
        if self.activity in ("diag", "adv"):
            messagebox.showinfo("Occupé", "Un test est en cours. Attendez la fin ou cliquez sur Arrêter.")
            return False
        if self.activity == "gen" and name != "gen":
            self.pages["stream"].stop(wait=True)
        if self.activity == "mon" and name != "mon":
            self.pages["manual"].close_monitor()
        self.activity = name
        self.apply_sim()
        self._set_status(name)
        return True

    def release(self, name: str):
        if self.activity == name:
            self.activity = None
        self._set_status(self.activity)

    def stop_activity(self):
        self.stop_event.set()
        if self.generator:
            self.generator.stop()
        if self.monitor:
            self.monitor.close()

    def run_worker(self, name: str, target):
        self.stop_event.clear()
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def make_ctx(self, ports, s) -> engine.TestContext:
        return engine.TestContext(ports[0], ports[1], s, dict(self.port_infos), self.emit, self.stop_event)

    # ------------------------------------------------------------ pied de page + journal
    def _build_footer(self):
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=2, column=1, sticky="ew", padx=(0, 16), pady=(0, 8))
        f.grid_columnconfigure(1, weight=1)
        self.lab_status = ctk.CTkLabel(f, text="", font=F(12), anchor="w")
        self.lab_status.grid(row=0, column=0, sticky="w")
        self.progress = ctk.CTkProgressBar(f, height=6, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, sticky="ew", padx=16)
        self.lab_prog = ctk.CTkLabel(f, text="", font=F(11), text_color=MUTED, width=180, anchor="w")
        self.lab_prog.grid(row=0, column=2)
        self.btn_log = ctk.CTkButton(f, text="Journal détaillé ▴", width=150, height=28, fg_color=CARD2,
                                     hover_color=LINE, command=self.toggle_log)
        self.btn_log.grid(row=0, column=3, padx=(8, 0))
        self.logbox = card(self)
        self.logbox.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self.logbox, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 0))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="Journal : chaque octet envoyé et reçu", font=F(12, True)).grid(row=0, column=0,
                                                                                               sticky="w")
        ctk.CTkButton(top, text="Exporter", width=80, height=26, fg_color=CARD2, hover_color=LINE,
                      command=self._export_log).grid(row=0, column=1, padx=4)
        ctk.CTkButton(top, text="Effacer", width=80, height=26, fg_color=CARD2, hover_color=LINE,
                      command=lambda: self.logtxt.delete("1.0", "end")).grid(row=0, column=2, padx=4)
        self.logtxt = tk.Text(self.logbox, bg=CARD, fg=TXT, font=MONO_S, relief="flat", wrap="word", height=11,
                              insertbackground=TXT, highlightthickness=0)
        self.logtxt.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=8)
        sbar = ctk.CTkScrollbar(self.logbox, command=self.logtxt.yview)
        sbar.grid(row=1, column=1, sticky="ns", pady=8, padx=(0, 4))
        self.logtxt.configure(yscrollcommand=sbar.set)
        for tag, col in LOG_COLORS.items():
            self.logtxt.tag_configure(tag, foreground=col)
        self.log_visible = False
        self._set_status(None)

    def toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.logbox.grid(row=3, column=1, sticky="ew", padx=(0, 16), pady=(0, 12))
            self.btn_log.configure(text="Journal détaillé ▾")
            self.logtxt.see("end")
        else:
            self.logbox.grid_remove()
            self.btn_log.configure(text="Journal détaillé ▴")

    def _set_status(self, act):
        txt = {None: ("●  Prêt", GREEN), "diag": ("●  Diagnostic en cours…", ORANGE),
               "adv": ("●  Test en cours…", ORANGE), "gen": ("●  Envoi continu actif", ORANGE),
               "mon": ("●  Ports à l'écoute (envoi manuel)", ACCENT)}.get(act, ("●  Prêt", GREEN))
        self.lab_status.configure(text=txt[0], text_color=txt[1])
        if act is None:
            self.lab_prog.configure(text="")

    def log(self, msg: str, level: str = "info"):
        ts = time.strftime("%H:%M:%S  ") if msg else ""
        self.logtxt.insert("end", ts + msg + "\n", level)
        if int(self.logtxt.index("end-1c").split(".")[0]) > 6000:
            self.logtxt.delete("1.0", "1500.0")
        self.logtxt.see("end")

    def _export_log(self):
        path = filedialog.asksaveasfilename(title="Exporter le journal", defaultextension=".txt",
                                            initialfile=f"journal_RS485_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                                            filetypes=[("Texte", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.logtxt.get("1.0", "end"))

    # ------------------------------------------------------------ événements
    def emit(self, kind, payload=None):
        self.q.put((kind, payload))

    def _poll(self):
        try:
            for _ in range(400):
                kind, p = self.q.get_nowait()
                self._dispatch(kind, p)
        except queue.Empty:
            pass
        except Exception as e:   # noqa  la boucle d'événements ne doit jamais mourir
            self.log(f"Erreur interface : {e!r}", "err")
        self.after(40, self._poll)

    def _dispatch(self, kind, p):
        if kind == "log":
            self.log(*p)
        elif kind == "progress":
            self.progress.set(p[0])
            self.lab_prog.configure(text=p[1])
        elif kind.startswith("diag_"):
            self.pages["diag"].handle(kind, p)
        elif kind in ("test_done", "stream", "histogram", "adv_done"):
            self.pages["adv"].handle(kind, p)
        elif kind.startswith("gen_"):
            self.pages["stream"].handle(kind, p)
        elif kind.startswith("mon_"):
            self.pages["manual"].handle(kind, p)

    # ------------------------------------------------------------ dialogues
    def open_settings(self):
        SettingsDialog(self)

    def save_report(self, results: List[engine.TestResult], title: str):
        ReportDialog(self, results, title)

    def report_meta(self) -> dict:
        a, b = self.dev_a(), self.dev_b()
        ia, ib = self.port_infos.get(a), self.port_infos.get(b)
        s = self.get_settings(quiet=True)

        def port(dev, info):
            if not info:
                return dev
            return f"{dev} — {info.description} — {info.chip()}" + (f" — n° {info.serial_number}"
                                                                     if info.serial_number else "")
        return {"Opérateur": self.trace.get("operator", ""), "N° série module 1": self.trace.get("sn1", ""),
                "N° série module 2": self.trace.get("sn2", ""), "Commentaire": self.trace.get("comment", ""),
                "Port A": port(a, ia), "Port B": port(b, ib), "Liaison": s.describe() if s else ""}


# =====================================================================
# Page 1 : Diagnostic
# =====================================================================
class DiagPage(ctk.CTkScrollableFrame):
    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        page_title(self, "Diagnostic de la liaison",
                   "Vérifie en quelques secondes que tout ce qui part d'un port arrive identique sur l'autre, "
                   "dans les deux sens. Branchez les deux adaptateurs, choisissez les ports en haut, puis lancez.")
        top = card(self)
        top.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        top.grid_columnconfigure(0, weight=1)
        self.topo = Redrawable(top, 160, lambda c: diagrams.draw_topology(
            c, "path_both", f"Port A : {app.dev_a() or '?'}", f"Port B : {app.dev_b() or '?'}"))
        self.topo.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        act = ctk.CTkFrame(self, fg_color="transparent")
        act.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        act.grid_columnconfigure(1, weight=1)
        self.btn = ctk.CTkButton(act, text="▶   Lancer le diagnostic", font=F(16, True), height=54, width=290,
                                 corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_D, command=self.start)
        self.btn.grid(row=0, column=0, sticky="w")
        self.verdict = ctk.CTkLabel(act, text="Pas encore lancé", font=F(16, True), height=54, corner_radius=10,
                                    fg_color=CARD2, text_color=MUTED, anchor="w")
        self.verdict.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        self.list = card(self)
        self.list.grid(row=4, column=0, sticky="ew")
        self.list.grid_columnconfigure(1, weight=1)
        self.rows = {}
        self.steps = diagnostic.new_steps()
        for i, st in enumerate(self.steps):
            r = i * 2
            ic = ctk.CTkLabel(self.list, text="○", font=F(22, True), width=40, text_color="#5b6068")
            ic.grid(row=r, column=0, rowspan=1, padx=(14, 6), pady=(12, 0), sticky="n")
            box = ctk.CTkFrame(self.list, fg_color="transparent")
            box.grid(row=r, column=1, sticky="ew", pady=(10, 0))
            box.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(box, text=st.title, font=F(15, True), anchor="w").grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(box, text=st.what, font=F(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=560).grid(row=1, column=0, sticky="w")
            val = ctk.CTkLabel(self.list, text="", font=F(12), anchor="e", justify="right", wraplength=380)
            val.grid(row=r, column=2, sticky="e", padx=16, pady=(12, 0))
            adv = ctk.CTkLabel(self.list, text="", font=F(12), anchor="w", justify="left", text_color="#fcd34d",
                               wraplength=900)
            adv.grid(row=r + 1, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=(2, 10))
            adv.grid_remove()
            sep = ctk.CTkFrame(self.list, height=1, fg_color=LINE)
            sep.grid(row=r + 1, column=0, columnspan=3, sticky="sew", padx=14)
            self.rows[st.key] = (ic, val, adv)

        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.grid(row=5, column=0, sticky="ew", pady=12)
        self.btn_report = ctk.CTkButton(bot, text="Enregistrer le rapport", height=38, fg_color=CARD2,
                                        hover_color=LINE, state="disabled", command=self._report)
        self.btn_report.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(bot, text="Le détail de chaque octet échangé est dans le « Journal détaillé » (en bas à droite).",
                     text_color=MUTED, font=F(12)).grid(row=0, column=1, padx=14)
        self.results: List[engine.TestResult] = []

    def on_ports(self):
        self.topo.redraw()

    def _reset_rows(self):
        for st in self.steps:
            self._show_step(dataclasses.replace(st, status="", value="", advice=[]))

    def _show_step(self, st: diagnostic.Step):
        ic, val, adv = self.rows[st.key]
        icon, col = STATUS_STYLE.get(st.status, ("○", MUTED))
        ic.configure(text=icon, text_color=col)
        txt = st.value if st.status != "EN COURS" else "en cours…"
        val.configure(text=txt, text_color=col if st.status in (NOK, ERROR, WARN) else (
            TXT if st.status == OK else MUTED))
        if st.advice:
            adv.configure(text="À vérifier :\n" + "\n".join("  •  " + a for a in st.advice),
                          text_color="#fca5a5" if st.status in (NOK, ERROR) else "#fcd34d")
            adv.grid()
        else:
            adv.grid_remove()

    def start(self):
        if self.app.activity == "diag":
            self.app.stop_event.set()
            self.btn.configure(text="Arrêt en cours…", state="disabled")
            return
        ports = self.app.get_ports()
        s = self.app.get_settings()
        if not ports or not s or not self.app.claim("diag"):
            return
        self._reset_rows()
        self.verdict.configure(text="  Diagnostic en cours…", fg_color=CARD2, text_color=ORANGE)
        self.btn.configure(text="■   Arrêter", fg_color=RED_D, hover_color="#991b1b")
        self.btn_report.configure(state="disabled")
        ctx = self.app.make_ctx(ports, s)
        steps = diagnostic.new_steps()
        self.app.log("")
        self.app.log(f"═══ Diagnostic — A = {ports[0]}, B = {ports[1]}, {s.describe()} ═══", "title")
        t0 = time.time()
        emit = self.app.emit

        def work():
            err = None
            try:
                diagnostic.run(ctx, steps, lambda st: emit("diag_step", dataclasses.replace(
                    st, advice=list(st.advice))))
            except engine.StopTest:
                err = "Diagnostic arrêté."
            except core.EchoError as e:
                err = f"Écho local : {e}. Désactivez l'écho local dans Réglages si l'adaptateur ne le renvoie pas."
            except (serial.SerialException, OSError) as e:
                err = f"Impossible d'utiliser un port : {e}. Port déjà utilisé par un autre logiciel ou débranché ?"
            except Exception as e:   # noqa
                err = f"Erreur inattendue : {e!r}"
            emit("diag_done", (steps, err, t0, s.describe()))

        self.app.run_worker("diag", work)

    def handle(self, kind, p):
        if kind == "diag_step":
            self._show_step(p)
        elif kind == "diag_done":
            steps, err, t0, sdesc = p
            for st in steps:
                if st.status in ("", "EN COURS"):
                    st.status = ABORTED if err else diagnostic.SKIP
                    st.value = "arrêté" if err else ""
                    self._show_step(st)
            v = diagnostic.verdict(steps)
            n_ok = sum(st.status == OK for st in steps)
            if err:
                self.verdict.configure(text=f"  ■  {err}", fg_color="#3a2a12", text_color="#fcd34d")
                self.app.log(err, "warn")
            elif v == OK:
                self.verdict.configure(text=f"  ✓  Liaison OK — {n_ok}/{len(steps)} vérifications réussies",
                                       fg_color=GREEN_D, text_color="#ffffff")
            elif v == WARN:
                self.verdict.configure(text="  !  Liaison fonctionnelle, avec une remarque (voir en orange)",
                                       fg_color="#92400e", text_color="#ffffff")
            else:
                first = next(st for st in steps if st.status in (NOK, ERROR))
                self.verdict.configure(text=f"  ✗  Problème détecté : {first.title}", fg_color=RED_D,
                                       text_color="#ffffff")
            self.app.log(f"═══ Verdict : {'arrêté' if err else v} ═══", "ok" if v == OK and not err else "err")
            self.results = diagnostic.to_results(steps, sdesc, t0, time.time() - t0)
            self.btn.configure(text="▶   Relancer le diagnostic", state="normal", fg_color=ACCENT,
                               hover_color=ACCENT_D)
            self.btn_report.configure(state="normal")
            self.app.progress.set(0)
            self.app.release("diag")

    def _report(self):
        if self.results:
            self.app.save_report(self.results, "Diagnostic")


# =====================================================================
# Page 2 : Envoi continu (oscilloscope)
# =====================================================================
PATTERNS = [
    ("square", "Signal carré 0x55", "Pour mesurer la vitesse : fréquence affichée = vitesse ÷ 2."),
    ("modbus", "Trame Modbus type", "01 03 00 00 00 0A + CRC : une vraie trame de lecture de 10 registres."),
    ("all", "Les 256 valeurs (00 → FF)", "Pour vérifier que chaque valeur d'octet passe."),
    ("custom", "Ma trame", "Saisie libre en hexadécimal ou en texte."),
]
PAUSES = {"Aucune": 0.0, "1 ms": 0.001, "10 ms": 0.010, "100 ms": 0.100, "1 s": 1.0}


class StreamPage(ctk.CTkScrollableFrame):
    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        c = app.cfg.get("stream", {})
        self.grid_columnconfigure((0, 1), weight=1, uniform="c")
        page_title(self, "Envoi continu pour l'oscilloscope",
                   "Le PC répète la même trame sans arrêt : le signal reste stable à l'écran. Chaque trame qui "
                   "arrive de l'autre côté est comparée à celle envoyée.")
        # ---- colonne réglages
        left = card(self)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        left.grid_columnconfigure(0, weight=1)
        self.v_pat = tk.StringVar(value=c.get("pat", "square"))
        self.v_mode = tk.StringVar(value=c.get("mode", "HEX"))
        self.v_text = tk.StringVar(value=c.get("text", "01 06 00 10 0A 0D"))
        self.v_crc = tk.BooleanVar(value=c.get("crc", True))
        self.v_side = tk.StringVar(value=c.get("side", "A → B"))
        self.v_pause = tk.StringVar(value=c.get("pause", "10 ms") if c.get("pause") in PAUSES else "10 ms")
        ctk.CTkLabel(left, text="1.  Quoi envoyer", font=F(15, True)).grid(row=0, column=0, sticky="w",
                                                                          padx=16, pady=(14, 4))
        r = 1
        for key, name, desc in PATTERNS:
            ctk.CTkRadioButton(left, text=name, value=key, variable=self.v_pat, font=F(13, True),
                               command=self.refresh).grid(row=r, column=0, sticky="w", padx=20, pady=(6, 0))
            ctk.CTkLabel(left, text=desc, font=F(12), text_color=MUTED).grid(row=r + 1, column=0, sticky="w",
                                                                             padx=50)
            r += 2
        cu = ctk.CTkFrame(left, fg_color="transparent")
        cu.grid(row=r, column=0, sticky="ew", padx=48, pady=(4, 0))
        cu.grid_columnconfigure(1, weight=1)
        ctk.CTkSegmentedButton(cu, values=["HEX", "Texte"], variable=self.v_mode, width=110,
                               command=lambda _v: self.refresh()).grid(row=0, column=0, padx=(0, 6))
        self.ent = ctk.CTkEntry(cu, textvariable=self.v_text, font=MONO)
        self.ent.grid(row=0, column=1, sticky="ew")
        self.ent.bind("<KeyRelease>", lambda _e: self.refresh())
        ctk.CTkCheckBox(cu, text="+ CRC Modbus", variable=self.v_crc, command=self.refresh).grid(
            row=0, column=2, padx=(8, 0))
        self.lab_err = ctk.CTkLabel(left, text="", text_color=RED, font=F(12))
        self.lab_err.grid(row=r + 1, column=0, sticky="w", padx=50)
        ctk.CTkLabel(left, text="2.  Sens", font=F(15, True)).grid(row=r + 2, column=0, sticky="w", padx=16,
                                                                  pady=(14, 4))
        ctk.CTkSegmentedButton(left, values=["A → B", "B → A"], variable=self.v_side, height=34,
                               command=lambda _v: self.refresh()).grid(row=r + 3, column=0, sticky="w", padx=20)
        ctk.CTkLabel(left, text="3.  Pause entre deux trames", font=F(15, True)).grid(
            row=r + 4, column=0, sticky="w", padx=16, pady=(14, 4))
        ctk.CTkSegmentedButton(left, values=list(PAUSES), variable=self.v_pause, height=34,
                               command=lambda _v: self.refresh()).grid(row=r + 5, column=0, sticky="w", padx=20)
        self.btn = ctk.CTkButton(left, text="▶   Démarrer l'envoi", font=F(16, True), height=52, corner_radius=10,
                                 fg_color=ACCENT, hover_color=ACCENT_D, command=self.toggle)
        self.btn.grid(row=r + 6, column=0, sticky="ew", padx=16, pady=(22, 16))

        # ---- colonne oscilloscope
        right = card(self)
        right.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        right.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(right, text="Ce que vous devez voir à l'oscilloscope", font=F(15, True)).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 0))
        self.wave = Redrawable(right, 170, self._draw_wave)
        self.wave.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        self.tiles = {}
        for i, (k, lab) in enumerate([("bit", "Durée d'un bit"), ("frame", "Durée d'une trame"),
                                      ("freq", "Fréquence"), ("rate", "Répétition")]):
            t = ctk.CTkFrame(right, fg_color=CARD2, corner_radius=10)
            t.grid(row=2 + i // 2, column=i % 2, sticky="ew", padx=(16 if i % 2 == 0 else 6, 16 if i % 2 else 6),
                   pady=5)
            ctk.CTkLabel(t, text=lab, font=F(11), text_color=MUTED).pack(anchor="w", padx=12, pady=(8, 0))
            v = ctk.CTkLabel(t, text="—", font=F(18, True))
            v.pack(anchor="w", padx=12)
            n = ctk.CTkLabel(t, text="", font=F(11), text_color=MUTED)
            n.pack(anchor="w", padx=12, pady=(0, 8))
            self.tiles[k] = (v, n)
        ctk.CTkLabel(right, justify="left", anchor="w", font=F(12), text_color="#cbd5e1", wraplength=520, text=(
            "Réglage conseillé : déclenchement sur front DESCENDANT (le bit de start), mode Normal.\n"
            "Sonde 1 à l'entrée du module 1, sonde 2 à la sortie du module 2 : les deux signaux doivent être "
            "identiques, simplement décalés dans le temps.")).grid(row=4, column=0, columnspan=2, sticky="ew",
                                                                  padx=16, pady=(8, 4))
        cnt = ctk.CTkFrame(right, fg_color="transparent")
        cnt.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 4))
        cnt.grid_columnconfigure((0, 1, 2), weight=1)
        self.cnt = {}
        for i, (k, lab) in enumerate([("sent", "Trames envoyées"), ("ok", "Reçues identiques"),
                                      ("bad", "Erreurs")]):
            t = ctk.CTkFrame(cnt, fg_color=CARD2, corner_radius=10)
            t.grid(row=0, column=i, sticky="ew", padx=6)
            ctk.CTkLabel(t, text=lab, font=F(11), text_color=MUTED).pack(pady=(8, 0))
            v = ctk.CTkLabel(t, text="0", font=F(24, True))
            v.pack(pady=(0, 8))
            self.cnt[k] = v
        self.lab_last = ctk.CTkLabel(right, text="", font=F(12), text_color=MUTED, wraplength=520, justify="left")
        self.lab_last.grid(row=6, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 14))
        self.frame = b""
        self.refresh()

    def save(self):
        return {"stream": {"pat": self.v_pat.get(), "mode": self.v_mode.get(), "text": self.v_text.get(),
                           "crc": self.v_crc.get(), "side": self.v_side.get(), "pause": self.v_pause.get()}}

    def on_settings(self):
        self.refresh()

    def get_frame(self) -> bytes:
        k = self.v_pat.get()
        if k == "square":
            return b"\x55" * 16
        if k == "modbus":
            return core.modbus_request(1, 3, 0, 10)
        if k == "all":
            return bytes(range(256))
        data = build_frame(self.v_text.get(), self.v_mode.get(), self.v_crc.get())
        if not data:
            raise ValueError("la trame est vide")
        return data

    def refresh(self):
        custom = self.v_pat.get() == "custom"
        self.ent.configure(state="normal" if custom else "disabled")
        try:
            self.frame = self.get_frame()
            s = self.app.get_settings(quiet=True)
            if s and s.bytesize < 8 and any(b >> s.bytesize for b in self.frame):
                raise ValueError(f"des octets dépassent {s.bytesize} bits de données")
            self.lab_err.configure(text=f"{len(self.frame)} octets : {to_hex(self.frame, 24)}" if custom else "",
                                   text_color=MUTED)
        except ValueError as e:
            self.frame = b""
            self.lab_err.configure(text=f"⚠ {e}", text_color=RED)
        self._tiles()
        self.wave.redraw()

    def _draw_wave(self, c):
        s = self.app.get_settings(quiet=True) or SerialSettings()
        diagrams.draw_uart(c, self.frame or b"\x55", s, max_chars=3, show_diff=False)

    def _tiles(self):
        s = self.app.get_settings(quiet=True) or SerialSettings()
        n = len(self.frame)
        ft = s.wire_time(n)
        pause = PAUSES.get(self.v_pause.get(), 0.0)
        period = ft + pause
        self.tiles["bit"][0].configure(text=fmt_time(s.bit_time()))
        self.tiles["bit"][1].configure(text=f"à {s.baudrate} bauds")
        self.tiles["frame"][0].configure(text=fmt_time(ft) if n else "—")
        self.tiles["frame"][1].configure(text=f"{n} octets × {s.bits_per_char():g} bits ({s.format_str()})")
        if self.v_pat.get() == "square":
            self.tiles["freq"][0].configure(text=f"{s.baudrate / 2:,.0f} Hz".replace(",", " "))
            self.tiles["freq"][1].configure(text="signal carré pendant la trame")
        else:
            self.tiles["freq"][0].configure(text="—")
            self.tiles["freq"][1].configure(text="(motif non périodique)")
        if n:
            self.tiles["rate"][0].configure(text=f"toutes les {fmt_time(period)}")
            self.tiles["rate"][1].configure(text="sans silence entre trames" if pause == 0 else
                                            f"dont {fmt_time(pause)} de silence")

    def toggle(self):
        if self.app.activity == "gen":
            self.stop()
            return
        if not self.frame:
            messagebox.showerror("Envoi continu", "La trame est invalide (voir le message en rouge).")
            return
        a, b = self.app.dev_a(), self.app.dev_b()
        two = a in self.app.port_infos and b in self.app.port_infos and a != b
        ports = self.app.get_ports(need_b=two)
        s = self.app.get_settings()
        if not ports or not s or not self.app.claim("gen"):
            return
        tx, rx = (ports[0], ports[1]) if self.v_side.get().startswith("A") else (ports[1], ports[0])
        if not two:
            if self.v_side.get().startswith("B"):
                self.app.release("gen")
                messagebox.showerror("Envoi continu", "Choisissez le port B.")
                return
            rx = None
        for v in self.cnt.values():
            v.configure(text="0", text_color=TXT)
        self.cnt["ok"].configure(text="0" if rx else "—")
        self.cnt["bad"].configure(text="0" if rx else "—")
        self.lab_last.configure(text="" if rx else "Un seul port choisi : envoi sans vérification.",
                                text_color=MUTED)
        pause = PAUSES.get(self.v_pause.get(), 0.0)
        period = s.wire_time(len(self.frame)) + pause if pause else 0.0
        self.app.generator = engine.Generator(tx, rx, s, self.frame, period, 0, self.app.emit)
        self.app.generator.start()
        self.btn.configure(text="■   Arrêter l'envoi", fg_color=RED_D, hover_color="#991b1b")
        self.app.log(f"Envoi continu {tx} → {rx or '(sans vérification)'} : {len(self.frame)} o, pause "
                     f"{self.v_pause.get()} — {to_hex(self.frame, 24)}", "title")

    def stop(self, wait=False):
        g = self.app.generator
        if g:
            g.stop()
            if wait and g.thread:
                g.thread.join(2)

    def handle(self, kind, p):
        if kind == "gen_stats":
            self.cnt["sent"].configure(text=str(p["sent"]))
            if self.app.generator and self.app.generator.rx_port:
                self.cnt["ok"].configure(text=str(p["ok"]), text_color=GREEN if p["ok"] else TXT)
                self.cnt["bad"].configure(text=str(p["bad"]), text_color=RED if p["bad"] else TXT)
        elif kind in ("gen_bad", "gen_error"):
            self.lab_last.configure(text="Dernière erreur : " + p, text_color="#fca5a5")
            self.app.log("Envoi continu : " + p, "err")
        elif kind == "gen_done":
            self.app.generator = None
            self.btn.configure(text="▶   Démarrer l'envoi", fg_color=ACCENT, hover_color=ACCENT_D)
            self.app.release("gen")
            self.app.log(f"Envoi continu arrêté : {p['sent']} trames"
                         + (f", {p['ok']} identiques, {p['bad']} erreurs" if p["ok"] or p["bad"] else ""),
                         "ok" if not p["bad"] else "err")


# =====================================================================
# Page 3 : Envoi manuel
# =====================================================================
EXAMPLES = [("0x55 × 8", "HEX", "55 55 55 55 55 55 55 55", False),
            ("Modbus lecture", "HEX", "01 03 00 00 00 0A", True),
            ("Texte « Hello »", "Texte", "Hello RS485\\r\\n", False)]


class ManualPage(ctk.CTkFrame):
    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        c = app.cfg.get("manual", {})
        self.grid_columnconfigure((0, 1), weight=1, uniform="m")
        self.grid_rowconfigure(5, weight=1)
        page_title(self, "Envoi manuel",
                   "Envoyez une trame d'un côté et regardez ce qui arrive de l'autre. Les deux ports restent à "
                   "l'écoute tant que cette page est ouverte.")
        top = card(self)
        top.grid(row=2, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        self.v_mode = tk.StringVar(value=c.get("mode", "HEX"))
        self.v_text = tk.StringVar(value=c.get("text", "01 03 00 00 00 0A"))
        self.v_crc = tk.BooleanVar(value=c.get("crc", True))
        ctk.CTkSegmentedButton(top, values=["HEX", "Texte"], variable=self.v_mode, width=120,
                               command=lambda _v: self._preview()).grid(row=0, column=0, padx=(16, 8), pady=(14, 4))
        e = ctk.CTkEntry(top, textvariable=self.v_text, font=MONO, height=36)
        e.grid(row=0, column=1, sticky="ew", pady=(14, 4))
        e.bind("<KeyRelease>", lambda _e: self._preview())
        e.bind("<Return>", lambda _e: self.send("A"))
        ctk.CTkCheckBox(top, text="+ CRC Modbus", variable=self.v_crc, command=self._preview).grid(
            row=0, column=2, padx=12, pady=(14, 4))
        ex = ctk.CTkFrame(top, fg_color="transparent")
        ex.grid(row=1, column=1, columnspan=2, sticky="w")
        ctk.CTkLabel(ex, text="Exemples :", text_color=MUTED, font=F(12)).grid(row=0, column=0, padx=(0, 6))
        for i, (lab, mode, txt, crc) in enumerate(EXAMPLES):
            ctk.CTkButton(ex, text=lab, height=26, width=10, fg_color=CARD2, hover_color=LINE, font=F(12),
                          command=lambda m=mode, t=txt, cr=crc: self._example(m, t, cr)).grid(row=0, column=i + 1,
                                                                                         padx=3)
        self.lab_prev = ctk.CTkLabel(top, text="", font=MONO_S, text_color=MUTED, anchor="w")
        self.lab_prev.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(4, 0))
        bt = ctk.CTkFrame(top, fg_color="transparent")
        bt.grid(row=3, column=0, columnspan=3, sticky="ew", padx=16, pady=(10, 14))
        bt.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(bt, text="Envoyer   A  →  B", font=F(15, True), height=44, fg_color=GREEN_D,
                      hover_color="#166534", command=lambda: self.send("A")).grid(row=0, column=0, sticky="ew",
                                                                                padx=(0, 6))
        ctk.CTkButton(bt, text="Envoyer   B  →  A", font=F(15, True), height=44, fg_color=ACCENT_D,
                      hover_color="#1d4ed8", command=lambda: self.send("B")).grid(row=0, column=1, sticky="ew",
                                                                               padx=(6, 0))
        self.result = ctk.CTkLabel(self, text="  Résultat : envoyez une trame", font=F(14, True), height=46,
                                   corner_radius=10, fg_color=CARD2, text_color=MUTED, anchor="w", justify="left")
        self.result.grid(row=3, column=0, columnspan=2, sticky="ew", pady=12)
        self.lab_state = ctk.CTkLabel(self, text="", font=F(12), text_color=MUTED, anchor="w")
        self.lab_state.grid(row=4, column=0, columnspan=2, sticky="ew", padx=4)
        self.txt = {}
        for i, side in enumerate(("A", "B")):
            f = card(self)
            f.grid(row=5, column=i, sticky="nsew", padx=(0, 6) if i == 0 else (6, 0), pady=(4, 0))
            f.grid_columnconfigure(0, weight=1)
            f.grid_rowconfigure(1, weight=1)
            hdr = ctk.CTkFrame(f, fg_color="transparent")
            hdr.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 0))
            hdr.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(hdr, text=f"Port {side} : envoyé / reçu", font=F(13, True),
                         text_color="#4ade80" if side == "A" else "#60a5fa").grid(row=0, column=0, sticky="w")
            ctk.CTkButton(hdr, text="Effacer", width=70, height=24, fg_color=CARD2, hover_color=LINE,
                          command=lambda s=side: self.txt[s].delete("1.0", "end")).grid(row=0, column=1)
            t = tk.Text(f, bg=CARD, fg=TXT, font=MONO_S, relief="flat", wrap="none", height=10,
                        highlightthickness=0, insertbackground=TXT)
            t.grid(row=1, column=0, sticky="nsew", padx=(12, 0), pady=8)
            sbar = ctk.CTkScrollbar(f, command=t.yview)
            sbar.grid(row=1, column=1, sticky="ns", pady=8, padx=(0, 4))
            t.configure(yscrollcommand=sbar.set)
            t.tag_configure("TX", foreground="#93c5fd")
            t.tag_configure("RX", foreground="#5eead4")
            self.txt[side] = t
        self._preview()

    def save(self):
        return {"manual": {"mode": self.v_mode.get(), "text": self.v_text.get(), "crc": self.v_crc.get()}}

    def _example(self, mode, txt, crc):
        self.v_mode.set(mode)
        self.v_text.set(txt)
        self.v_crc.set(crc)
        self._preview()

    def _frame(self) -> bytes:
        return build_frame(self.v_text.get(), self.v_mode.get(), self.v_crc.get())

    def _preview(self):
        try:
            d = self._frame()
            self.lab_prev.configure(text=f"Sera envoyé ({len(d)} octets) : {to_hex(d, 40)}", text_color=MUTED)
        except ValueError as e:
            self.lab_prev.configure(text=f"⚠ {e}", text_color=RED)

    # -- écoute automatique -----------------------------------------------
    def on_show(self):
        self.open_monitor()

    def on_leave(self):
        self.close_monitor()

    def on_ports(self):
        if self.app.current == "manual":
            self.close_monitor()
            self.open_monitor()

    def on_settings(self):
        self.on_ports()

    def open_monitor(self):
        if self.app.activity is not None or self.app.monitor:
            if self.app.activity != "mon":
                self.lab_state.configure(text="○  Ports occupés par une autre fonction (diagnostic, test ou envoi "
                                              "continu).", text_color=ORANGE)
            return
        a, b = self.app.dev_a(), self.app.dev_b()
        s = self.app.get_settings(quiet=True)
        if a not in self.app.port_infos or not s:
            self.lab_state.configure(text="○  Choisissez au moins le port A en haut de la fenêtre.",
                                     text_color=ORANGE)
            return
        if b not in self.app.port_infos or b == a or ((a in core.SIM_PORTS) != (b in core.SIM_PORTS)):
            b = ""
        self.app.activity = "mon"
        self.app.apply_sim()
        mon = engine.Monitor(a, b, s, self.app.emit, 10.0)
        try:
            mon.open()
        except Exception as e:   # noqa
            self.app.activity = None
            self.lab_state.configure(text=f"○  Ouverture impossible : {e}", text_color=RED)
            return
        self.app.monitor = mon
        self.app._set_status("mon")
        self.lab_state.configure(text=f"●  À l'écoute : A = {a}" + (f", B = {b}" if b else " (B non choisi)")
                                      + f" — {s.describe()}", text_color=GREEN)

    def close_monitor(self):
        if self.app.monitor:
            self.app.monitor.close()
            self.app.monitor = None
        if self.app.activity == "mon":
            self.app.release("mon")
        self.lab_state.configure(text="○  Ports fermés", text_color=MUTED)

    def send(self, side: str):
        if not self.app.monitor:
            self.open_monitor()
            if not self.app.monitor:
                return
        if side not in self.app.monitor.ser:
            messagebox.showerror("Envoi", f"Le port {side} n'est pas ouvert : choisissez-le en haut.")
            return
        try:
            data = self._frame()
            if not data:
                raise ValueError("trame vide")
        except ValueError as e:
            messagebox.showerror("Envoi", str(e))
            return
        try:
            self.app.monitor.send(side, data, verify=True)
            self.result.configure(text="  Envoyé… en attente de la réception", fg_color=CARD2, text_color=ORANGE)
        except Exception as e:   # noqa
            messagebox.showerror("Envoi", str(e))

    def handle(self, kind, p):
        if kind == "mon_frame":
            side, k, data, t = p
            t_ = self.txt[side]
            arrow = "→ envoyé" if k == "TX" else "← reçu  "
            t_.insert("end", f"{time.strftime('%H:%M:%S')}  {arrow} {len(data):4d} o  {to_hex(data, 48)}"
                             f"   {to_ascii(data[:32])}\n", k)
            if int(t_.index("end-1c").split(".")[0]) > 2000:
                t_.delete("1.0", "500.0")
            t_.see("end")
        elif kind == "mon_verify":
            side, data, got, cmp = p
            if cmp.ok:
                self.result.configure(text=f"  ✓  IDENTIQUE — les {len(data)} octets reçus sur {side} sont exactement "
                                           "ceux envoyés", fg_color=GREEN_D, text_color="#ffffff")
            else:
                diff = "   ".join(f"octet {i} : attendu {e:02X}, reçu {r:02X}" for i, e, r in cmp.diffs[:4])
                self.result.configure(text=f"  ✗  {cmp.summary()} (reçu sur {side})" + (f"\n      {diff}" if diff
                                                                                       else ""),
                                      fg_color=RED_D, text_color="#ffffff")
            self.app.log(f"Envoi manuel, reçu sur {side} : {cmp.summary()}", "ok" if cmp.ok else "err")
        elif kind == "mon_error":
            self.lab_state.configure(text="○  " + p, text_color=RED)


# =====================================================================
# Page 4 : Tests avancés
# =====================================================================
ADV = {
    "Retournement": ("turnaround",
                     "En RS485 2 fils, un seul côté parle à la fois. Après une réception, le module doit libérer la "
                     "ligne avant que l'autre côté réponde. Le côté B répond de plus en plus vite : le test donne le "
                     "plus petit délai qui marche à 100 %.",
                     "À l'oscilloscope (côté B) : mesurez le silence entre la fin de la trame reçue et le début de "
                     "la réponse."),
    "Vitesses": ("sweep",
                 "Refait un échange complet (256 valeurs + motifs) à chaque vitesse de la liste, dans les deux "
                 "sens. Le tableau montre jusqu'à quelle vitesse la liaison fonctionne.",
                 "Pour confirmer une vitesse : envoi continu « Signal carré 0x55 », fréquence = vitesse ÷ 2."),
    "Latence": ("latency",
                "Mesure N fois le temps entre l'envoi et la réception complète, avec statistiques et histogramme. "
                "Ce temps inclut l'USB des deux adaptateurs (souvent 1 à 16 ms).",
                "La latence propre des modules (µs) se mesure à l'oscilloscope : même front, sonde 1 en entrée "
                "et sonde 2 en sortie."),
    "Endurance": ("endurance",
                  "Envoie des paquets numérotés en continu pendant la durée choisie et compte chaque bit faux et "
                  "chaque paquet perdu. Sans erreur, on obtient le taux d'erreur maximal garanti (BER).",
                  "Laissez tourner pendant que vous perturbez la liaison (CEM, courbure de fibre, alimentation) : "
                  "le compteur d'erreurs réagit en direct."),
}


class AdvancedPage(ctk.CTkScrollableFrame):
    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.raw: Dict[str, Dict[str, object]] = app.cfg.get("adv", {})
        self.grid_columnconfigure(0, weight=1)
        page_title(self, "Tests avancés",
                   "Pour caractériser la liaison plus finement. Pour un contrôle courant, le Diagnostic suffit.")
        self.v_test = tk.StringVar(value="Retournement")
        ctk.CTkSegmentedButton(self, values=list(ADV), variable=self.v_test, height=38, font=F(14),
                               command=lambda _v: self.select()).grid(row=2, column=0, sticky="w", pady=(0, 12))
        body = card(self)
        body.grid(row=3, column=0, sticky="ew")
        body.grid_columnconfigure(0, weight=1)
        self.lab_desc = ctk.CTkLabel(body, text="", font=F(13), justify="left", anchor="w", wraplength=1000)
        self.lab_desc.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 2))
        self.lab_osc = ctk.CTkLabel(body, text="", font=F(12), text_color="#fcd34d", justify="left", anchor="w",
                                    wraplength=1000)
        self.lab_osc.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.diag = Redrawable(body, 150, self._draw)
        self.diag.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        self.params = ctk.CTkFrame(body, fg_color="transparent")
        self.params.grid(row=3, column=0, sticky="ew", padx=12, pady=6)
        self.pw: Dict[str, tuple] = {}
        act = ctk.CTkFrame(body, fg_color="transparent")
        act.grid(row=4, column=0, sticky="ew", padx=16, pady=(6, 14))
        act.grid_columnconfigure(1, weight=1)
        self.btn = ctk.CTkButton(act, text="▶   Lancer", font=F(15, True), height=44, width=200, fg_color=ACCENT,
                                 hover_color=ACCENT_D, command=self.start)
        self.btn.grid(row=0, column=0)
        self.res = ctk.CTkLabel(act, text="", font=F(13, True), height=44, corner_radius=10, fg_color=CARD2,
                                text_color=MUTED, anchor="w", justify="left", wraplength=760)
        self.res.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        out = ctk.CTkFrame(self, fg_color="transparent")
        out.grid(row=4, column=0, sticky="ew", pady=12)
        out.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(out, style="Dark.Treeview", show="headings", height=6)
        self.tree.grid(row=0, column=0, sticky="ew")
        self.tree.tag_configure("ko", background="#4a1f22")
        self.tree.tag_configure("warn", background="#4a3b12")
        self.hist = Redrawable(out, 170, self._draw_hist)
        self.hist_data = None
        self.live = ctk.CTkFrame(out, fg_color="transparent")
        self.live_lab = {}
        for i, (k, lab) in enumerate([("elapsed", "Durée"), ("ok", "Paquets OK"), ("corrupt", "Corrompus"),
                                      ("lost", "Perdus / en vol"), ("bit_errors", "Bits faux"), ("ber", "BER")]):
            self.live.grid_columnconfigure(i, weight=1)
            t = ctk.CTkFrame(self.live, fg_color=CARD2, corner_radius=10)
            t.grid(row=0, column=i, sticky="ew", padx=4)
            ctk.CTkLabel(t, text=lab, font=F(11), text_color=MUTED).pack(pady=(8, 0))
            v = ctk.CTkLabel(t, text="—", font=F(18, True))
            v.pack(pady=(0, 8))
            self.live_lab[k] = v
        self.results: Dict[str, engine.TestResult] = {}
        self.select()

    def save(self):
        self._store()
        return {"adv": self.raw}

    def tid(self) -> str:
        return ADV[self.v_test.get()][0]

    def _draw(self, c):
        t = self.tid()
        if t == "turnaround":
            diagrams.draw_turnaround(c)
        elif t == "endurance":
            diagrams.draw_packet(c, 32)
        else:
            diagrams.draw_topology(c, "path_one" if t == "latency" else "path_both",
                                   f"Port A : {self.app.dev_a() or '?'}", f"Port B : {self.app.dev_b() or '?'}")

    def _draw_hist(self, c):
        if self.hist_data:
            d = self.hist_data
            diagrams.draw_histogram(c, d["values_ms"], d["wire_ms"], d["title"])

    def _store(self):
        if getattr(self, "cur", None):
            d = self.raw.setdefault(self.cur, {})
            for k, (p, var) in self.pw.items():
                d[k] = var.get()

    def select(self):
        self._store()
        tid = self.tid()
        self.cur = tid
        name = self.v_test.get()
        self.lab_desc.configure(text=ADV[name][1])
        self.lab_osc.configure(text=ADV[name][2])
        self.diag.redraw()
        for w in self.params.winfo_children():
            w.destroy()
        self.pw = {}
        raw = self.raw.get(tid, {})
        for i, p in enumerate(TESTS_BY_ID[tid].params):
            ctk.CTkLabel(self.params, text=p.label, font=F(12)).grid(row=i // 2, column=(i % 2) * 2, sticky="w",
                                                                    padx=(4, 8), pady=4)
            val = raw.get(p.key, p.default)
            if p.kind == "choice":
                var = tk.StringVar(value=val if val in p.choices else p.default)
                w = ctk.CTkSegmentedButton(self.params, values=list(p.choices), variable=var)
            else:
                var = tk.StringVar(value=str(val))
                w = ctk.CTkEntry(self.params, textvariable=var,
                                 width=380 if p.kind in ("intlist", "floatlist") else 110)
            w.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 24), pady=4)
            self.pw[p.key] = (p, var)
        self._show(self.results.get(tid))
        self.hist.grid_remove()
        self.live.grid_remove()
        if tid == "latency":
            self.hist.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            self.hist.redraw()
        if tid == "endurance":
            self.live.grid(row=1, column=0, sticky="ew", pady=(10, 0))

    def _show(self, r: Optional[engine.TestResult]):
        self.tree.delete(*self.tree.get_children())
        if r is None:
            self.res.configure(text="  Pas encore lancé", fg_color=CARD2, text_color=MUTED)
            self.tree["columns"] = ()
            return
        col = {OK: GREEN_D, NOK: RED_D, WARN: "#92400e", ERROR: RED_D}.get(r.status, CARD2)
        icon = {OK: "✓", NOK: "✗", WARN: "!", ERROR: "✗", ABORTED: "■"}.get(r.status, "")
        self.res.configure(text=f"  {icon}  {r.summary}", fg_color=col, text_color="#ffffff")
        cols = r.columns or ["Info"]
        self.tree["columns"] = [f"c{i}" for i in range(len(cols))]
        for i, name in enumerate(cols):
            self.tree.heading(f"c{i}", text=name)
            self.tree.column(f"c{i}", width=max(90, min(240, 9 * len(name) + 30)), anchor="w")
        for row in r.rows:
            tag = ("ko",) if any(str(x) in ("NOK", "ERREUR", "NON", "INVALIDE") for x in row) else (
                ("warn",) if "NON SUPPORTÉ" in row else ())
            self.tree.insert("", "end", values=row, tags=tag)
        self.tree.configure(height=min(14, max(3, len(r.rows))))

    def start(self):
        if self.app.activity == "adv":
            self.app.stop_event.set()
            self.btn.configure(text="Arrêt…", state="disabled")
            return
        self._store()
        tid = self.tid()
        tdef = TESTS_BY_ID[tid]
        try:
            params = {p.key: engine.parse_param(p, self.raw.get(tid, {}).get(p.key, p.default)) for p in tdef.params}
        except ValueError as e:
            messagebox.showerror("Paramètres", str(e))
            return
        ports = self.app.get_ports()
        s = self.app.get_settings()
        if not ports or not s or not self.app.claim("adv"):
            return
        ctx = self.app.make_ctx(ports, s)
        self.btn.configure(text="■   Arrêter", fg_color=RED_D, hover_color="#991b1b")
        self.res.configure(text="  En cours…", fg_color=CARD2, text_color=ORANGE)
        self.tree.delete(*self.tree.get_children())
        if tid == "latency":
            self.hist_data = None
        self.app.log("")
        self.app.log(f"═══ {tdef.title} — A = {ports[0]}, B = {ports[1]}, {s.describe()} ═══", "title")
        emit = self.app.emit

        def work():
            res = engine.run_test(tdef, ctx, params)
            emit("test_done", res)
            emit("adv_done", None)

        self.app.run_worker("adv", work)

    def handle(self, kind, p):
        if kind == "test_done":
            self.results[p.test_id] = p
            if p.test_id == self.tid():
                self._show(p)
            self.app.log(f"⇒ {p.title} : {p.status} — {p.summary}", "ok" if p.status == OK else "err")
        elif kind == "adv_done":
            self.btn.configure(text="▶   Lancer", state="normal", fg_color=ACCENT, hover_color=ACCENT_D)
            self.app.progress.set(0)
            self.app.release("adv")
        elif kind == "histogram":
            self.hist_data = p
            self.hist.redraw()
        elif kind == "stream":
            L = self.live_lab
            L["elapsed"].configure(text=f"{p['elapsed']:.0f} s  {p['dir']}")
            L["ok"].configure(text=str(p["ok"]))
            L["corrupt"].configure(text=str(p["corrupt"]), text_color=RED if p["corrupt"] else GREEN)
            L["lost"].configure(text=str(p["pending"]))
            L["bit_errors"].configure(text=str(p["bit_errors"]), text_color=RED if p["bit_errors"] else GREEN)
            if p["bit_errors"]:
                L["ber"].configure(text=f"{p['bit_errors'] / p['bits']:.1e}", text_color=RED)
            else:
                ub = core.ber_upper_bound(p["bits"], 0)
                L["ber"].configure(text=f"< {ub:.0e}" if ub else "—", text_color=GREEN)


# =====================================================================
# Page 5 : Aide
# =====================================================================
TROUBLE = [
    ("Rien n'est reçu", "Alimentation des modules · fibres non croisées (OUT1 → IN2, OUT2 → IN1) · mauvais port COM · "
                        "câblage A/B/GND"),
    ("Octets reçus mais faux", "Vitesse ou format différents (8N1…) · fils A et B inversés · pas de masse commune"),
    ("Quelques erreurs isolées", "Parasites · câble non torsadé ou trop long · terminaison 120 Ω absente · fibre abîmée"),
    ("Erreurs seulement à haute vitesse", "Limite d'un élément de la chaîne : comparez 0x55 en entrée et en sortie "
                                          "à l'oscilloscope (largeur des impulsions)"),
    ("Réponse perdue en aller-retour", "Le module libère la ligne trop lentement : test « Retournement »"),
    ("Temps de transfert ≈ 16 ms", "Adaptateur FTDI : Latency Timer à régler sur 1 ms (Gestionnaire de périphériques)"),
    ("Port refusé / occupé", "Fermez les autres logiciels qui utilisent le port (terminal, ancienne version…)"),
]


class HelpPage(ctk.CTkScrollableFrame):
    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        page_title(self, "Aide", "Ce que fait le logiciel, comment lire les résultats et comment utiliser "
                                 "l'oscilloscope.")
        r = [2]

        def section(title, text=None, canvas=None, h=150):
            c = card(self)
            c.grid(row=r[0], column=0, sticky="ew", pady=6)
            c.grid_columnconfigure(0, weight=1)
            r[0] += 1
            ctk.CTkLabel(c, text=title, font=F(16, True), anchor="w").grid(row=0, column=0, sticky="w", padx=16,
                                                                           pady=(12, 2))
            row = 1
            if canvas:
                Redrawable(c, h, canvas).grid(row=row, column=0, sticky="ew", padx=10, pady=4)
                row += 1
            if text:
                ctk.CTkLabel(c, text=text, font=F(13), justify="left", anchor="w", wraplength=1000,
                             text_color="#d1d5db").grid(row=row, column=0, sticky="ew", padx=16, pady=(2, 14))
            return c

        section("1.  Le montage",
                "Le PC a deux ports série, c'est-à-dire deux adaptateurs USB ↔ RS485. Le port A est relié au "
                "module 1, le port B au module 2, et les modules sont reliés par les fibres. Tout ce que le PC "
                "envoie sur un port doit ressortir à l'identique sur l'autre. Le logiciel compare l'envoyé et le "
                "reçu octet par octet.",
                lambda c: diagrams.draw_topology(c, "path_both", "Port A", "Port B"), 160)
        section("2.  Ce que veut dire « OK »",
                "« OK » signifie que toutes les données envoyées sont arrivées, identiques, dans le sens testé. "
                "Le logiciel ne mesure pas les tensions ni la forme des signaux, car il voit les données à "
                "travers l'USB. Pour cela, utilisez l'oscilloscope avec la page « Envoi continu ».")
        section("3.  Une trame série (ce que montre l'oscilloscope)",
                "Au repos, la ligne est au niveau haut. Chaque octet commence par un bit de start (bas), puis les "
                "8 bits de données (poids faible en premier), puis un bit de stop (haut). En 8N1, un octet fait "
                "donc 10 bits : à 9600 bauds, un bit dure 104 µs et un octet 1,04 ms. En RS485, le niveau haut "
                "correspond à VA − VB > +200 mV.",
                lambda c: diagrams.draw_uart(c, b"\x55\x00\xFF", self.app.get_settings(quiet=True) or SerialSettings(),
                                             show_diff=False), 160)
        section("4.  Utiliser l'oscilloscope",
                "1.  Page « Envoi continu » : choisissez « Signal carré 0x55 », puis Démarrer.\n"
                "2.  Sonde 1 à l'entrée du module 1, sonde 2 à la sortie du module 2 (en différentiel A − B, "
                "ou sur la sortie logique RO).\n"
                "3.  Déclenchement sur front descendant, mode Normal. Les durées attendues sont affichées sur la "
                "page.\n"
                "4.  Vérifiez : fréquence = vitesse ÷ 2 (à ±2 % près), niveaux haut et bas de même largeur, même "
                "signal en entrée et en sortie.")
        c = section("5.  Problèmes fréquents")
        for i, (sym, fix) in enumerate(TROUBLE):
            ctk.CTkLabel(c, text=sym, font=F(13, True), anchor="w").grid(row=1 + i, column=0, sticky="w", padx=16,
                                                                         pady=3)
            ctk.CTkLabel(c, text=fix, font=F(12), text_color="#d1d5db", anchor="w", justify="left",
                         wraplength=650).grid(row=1 + i, column=1, sticky="w", padx=(10, 16), pady=3)
        c.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(c, height=8, fg_color="transparent").grid(row=20, column=0)
        section("6.  Essayer sans matériel",
                "Choisissez SIM-A et SIM-B comme ports : c'est une liaison simulée. Dans Réglages, vous pouvez "
                "lui ajouter des erreurs, puis vérifier que le diagnostic les détecte bien.")


# =====================================================================
# Dialogues
# =====================================================================
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, app: App):
        super().__init__(app, fg_color=BG)
        self.app = app
        self.title("Réglages")
        self.geometry("640x560")
        self.resizable(False, False)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        c = card(self)
        c.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        c.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(c, text="Contrôle du sens d'émission", font=F(15, True)).grid(row=0, column=0, sticky="w",
                                                                                    padx=16, pady=(12, 4))
        ctk.CTkSegmentedButton(c, values=["Automatique", "RTS actif haut", "RTS actif bas"],
                               variable=app.v_dir).grid(row=1, column=0, sticky="w", padx=16)
        ctk.CTkLabel(c, font=F(12), text_color=MUTED, justify="left", wraplength=560, text=(
            "Laissez « Automatique » pour la plupart des adaptateurs USB-RS485. Choisissez RTS seulement si votre "
            "adaptateur demande de piloter l'émission par la broche RTS.")).grid(row=2, column=0, sticky="w",
                                                                                 padx=16, pady=(4, 8))
        ctk.CTkCheckBox(c, text="Écho local : l'adaptateur reçoit ce qu'il envoie lui-même",
                        variable=app.v_echo).grid(row=3, column=0, sticky="w", padx=16, pady=(4, 14))
        d = card(self)
        d.grid(row=1, column=0, sticky="ew", padx=16, pady=8)
        d.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(d, text="Liaison simulée (essai sans matériel)", font=F(15, True)).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 4))
        ctk.CTkCheckBox(d, text="Proposer les ports SIM-A / SIM-B", variable=app.v_show_sim,
                        command=lambda: app._refresh_ports(force=True)).grid(row=1, column=0, columnspan=2,
                                                                             sticky="w", padx=16, pady=4)
        ctk.CTkLabel(d, text="Taux d'erreur injecté (ex. 0.001)").grid(row=2, column=0, sticky="w", padx=16, pady=4)
        ctk.CTkEntry(d, textvariable=app.v_sim_ber, width=100).grid(row=2, column=1, sticky="w", pady=4)
        ctk.CTkLabel(d, text="Retournement des modules (ms)").grid(row=3, column=0, sticky="w", padx=16,
                                                                   pady=(4, 14))
        ctk.CTkEntry(d, textvariable=app.v_sim_turn, width=100).grid(row=3, column=1, sticky="w", pady=(4, 14))
        ctk.CTkLabel(self, text=f"Réglages enregistrés dans : {config_path()}", font=F(11), text_color=MUTED,
                     wraplength=600).grid(row=2, column=0, sticky="w", padx=20, pady=4)
        ctk.CTkButton(self, text="Fermer", width=140, height=36, command=self.destroy).grid(row=3, column=0,
                                                                                         pady=12)
        self.after(150, self._grab)

    def _grab(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass


class ReportDialog(ctk.CTkToplevel):
    def __init__(self, app: App, results: List[engine.TestResult], kind: str):
        super().__init__(app, fg_color=BG)
        self.app, self.results, self.kind = app, results, kind
        self.title("Enregistrer le rapport")
        self.geometry("520x380")
        self.resizable(False, False)
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text="Informations du rapport (facultatives)", font=F(15, True)).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(18, 10))
        self.vars = {}
        for i, (k, lab) in enumerate([("operator", "Opérateur"), ("sn1", "N° série module 1"),
                                      ("sn2", "N° série module 2"), ("comment", "Commentaire")]):
            v = tk.StringVar(value=app.trace.get(k, ""))
            self.vars[k] = v
            ctk.CTkLabel(self, text=lab).grid(row=1 + i, column=0, sticky="w", padx=20, pady=6)
            ctk.CTkEntry(self, textvariable=v).grid(row=1 + i, column=1, sticky="ew", padx=(0, 20), pady=6)
        ctk.CTkLabel(self, text="Le rapport HTML s'ouvre dans le navigateur et peut être imprimé en PDF. "
                                "Un fichier CSV est créé à côté.", font=F(11), text_color=MUTED,
                     wraplength=470, justify="left").grid(row=5, column=0, columnspan=2, sticky="w", padx=20, pady=8)
        ctk.CTkButton(self, text="Enregistrer…", height=38, fg_color=GREEN_D, hover_color="#166534",
                      command=self._save).grid(row=6, column=0, columnspan=2, pady=10)
        self.after(150, lambda: SettingsDialog._grab(self))

    def _save(self):
        self.app.trace = {k: v.get() for k, v in self.vars.items()}
        sn = self.app.trace.get("sn1", "").strip().replace(" ", "_") or "module"
        path = filedialog.asksaveasfilename(parent=self, title="Enregistrer le rapport",
                                            initialfile=f"Rapport_RS485_{sn}_{time.strftime('%Y%m%d_%H%M')}.html",
                                            defaultextension=".html", filetypes=[("Rapport HTML", "*.html")])
        if not path:
            return
        try:
            meta = self.app.report_meta()
            report.write_html(path, self.results, meta)
            report.write_csv(os.path.splitext(path)[0] + ".csv", self.results, meta)
        except OSError as e:
            messagebox.showerror("Rapport", str(e), parent=self)
            return
        self.app.log(f"Rapport enregistré : {path}", "ok")
        webbrowser.open("file:///" + os.path.abspath(path).replace("\\", "/"))
        self.destroy()


def main():
    app = App()
    app.mainloop()


# =====================================================================
# Auto-vérification (build / CI) : parcourt l'appli sur la liaison simulée
# =====================================================================
def smoke(out_json: str, shots_dir: Optional[str] = None, keep_cfg: bool = False) -> None:
    os.environ.setdefault("APPDATA", os.path.dirname(os.path.abspath(out_json)))
    app = App()
    res = {"pages": [], "errors": []}
    app.report_callback_exception = lambda et, ev, tb: res["errors"].append(repr(ev))
    app.v_show_sim.set(True)
    app._refresh_ports(force=True)
    app.cb_a.set(app._label(app.port_infos["SIM-A"]))
    app.cb_b.set(app._label(app.port_infos["SIM-B"]))
    app.v_baud.set("9600")
    app._ports_changed()
    P = app.pages

    def shot(name):
        res["pages"].append(name)
        if not shots_dir:
            return
        try:
            from PIL import ImageGrab
            app.lift()
            app.attributes("-topmost", True)
            app.update_idletasks()
            x, y = app.winfo_rootx(), app.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height())).save(
                os.path.join(shots_dir, f"{len(res['pages']):02d}_{name}.png"))
        except Exception as e:   # noqa
            res["errors"].append(f"capture {name}: {e!r}")

    seq = []

    def nxt():
        if not seq:
            finish()
            return
        fn, delay = seq.pop(0)
        try:
            fn()
        except Exception as e:   # noqa
            res["errors"].append(repr(e))
        app.after(delay, nxt)

    def when_idle(fn):
        def chk():
            if app.activity in ("diag", "adv", "gen"):
                app.after(200, chk)
            else:
                fn()
        return chk

    def finish():
        res["diag_verdict"] = P["diag"].verdict.cget("text").strip()
        res["diag_steps"] = {k: v[1].cget("text") for k, v in P["diag"].rows.items()}
        res["stream"] = {k: v.cget("text") for k, v in P["stream"].cnt.items()}
        res["manual"] = P["manual"].result.cget("text").strip()
        res["ok"] = ("Liaison OK" in res["diag_verdict"] and "IDENTIQUE" in res["manual"]
                     and int(res["stream"]["ok"]) > 0 and res["stream"]["bad"] == "0" and not res["errors"])
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        if not keep_cfg:
            app.stop_activity()
            app.destroy()
        else:
            app._on_close()

    def start():
        shot("accueil")
        P["diag"].start()
        app.after(300, when_idle(after_diag))

    def after_diag():
        seq.extend([
            (lambda: shot("diagnostic"), 300),
            (lambda: app.show("stream"), 700),
            (lambda: P["stream"].toggle(), 2500),
            (lambda: shot("envoi_continu"), 300),
            (lambda: P["stream"].toggle(), 1200),
            (lambda: app.show("manual"), 900),
            (lambda: P["manual"].send("A"), 1500),
            (lambda: shot("envoi_manuel"), 300),
            (lambda: app.show("adv"), 700),
            (lambda: shot("tests_avances"), 300),
            (lambda: (P["adv"].v_test.set("Endurance"), P["adv"].select()), 600),
            (lambda: shot("endurance"), 300),
            (lambda: app.show("help"), 900),
            (lambda: shot("aide"), 300),
            (lambda: (app.show("diag"), app.toggle_log()), 900),
            (lambda: shot("journal"), 300),
            (lambda: app.open_settings(), 1200),
            (lambda: shot("reglages"), 300),
        ])
        nxt()

    app.after(1500, start)
    app.after(120000, lambda: (res["errors"].append("timeout"), finish()))
    app.mainloop()
