"""Diagnostic en un clic : une suite courte de vérifications essentielles.

Chaque étape dit en une phrase ce qu'elle vérifie, donne un résultat lisible
et, en cas d'échec, ce qu'il faut contrôler. Aucune dépendance à l'interface.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List

from . import core, engine
from .core import fmt_rate, fmt_time
from .engine import ERROR, NOK, OK, WARN, TestContext, TestResult

SKIP = "NON LANCÉ"


@dataclass
class Step:
    key: str
    title: str
    what: str
    status: str = ""
    value: str = ""
    advice: List[str] = field(default_factory=list)


def new_steps() -> List[Step]:
    return [
        Step("ab", "Envoi A → B", "Une trame de contrôle part du port A et doit arriver identique sur le port B."),
        Step("ba", "Envoi B → A", "La même chose dans l'autre sens, ce qui vérifie l'autre fibre."),
        Step("data", "Toutes les valeurs d'octets",
             "Les 256 valeurs possibles et les motifs difficiles (0x00, 0xFF, 0x55) passent sans altération."),
        Step("modbus", "Trames Modbus réelles", "Trames industrielles types avec CRC, dans les deux sens."),
        Step("load", "Charge continue (3 s)", "Flux sans pause à pleine vitesse : aucune perte, aucun débordement."),
        Step("time", "Temps de transfert", "Temps entre l'envoi et la réception complète (pour information)."),
    ]


ADVICE_NOTHING = ["Les modules sont-ils alimentés (voyants) ?",
                  "Les fibres sont-elles croisées : OUT1 → IN2 et OUT2 → IN1 ?",
                  "Les ports A et B correspondent-ils aux bons câbles (étiquettes) ?",
                  "Le câblage RS485 est-il correct : A sur A, B sur B, et masse commune ?"]
ADVICE_GARBLED = ["La vitesse et le format (ex. 8N1) sont-ils identiques des deux côtés et sur les modules ?",
                  "Les fils A et B ne sont-ils pas inversés sur un tronçon ?",
                  "Y a-t-il une résistance de terminaison de 120 Ω et une masse commune ?"]
ADVICE_PARTIAL = ["La vitesse est-elle trop élevée pour un élément de la chaîne ? Essayez plus bas.",
                  "Contrôlez les connexions (faux contact) et l'état des fibres."]


ADVICE_SPARSE = ["Erreurs isolées : parasites (CEM), câble RS485 trop long ou non torsadé, terminaison 120 Ω "
                 "absente, connecteur ou fibre de mauvaise qualité.",
                 "Lancez « Endurance » (Tests avancés) pour mesurer le taux d'erreur sur la durée."]


def _advice_for(cmp: core.CompareResult) -> List[str]:
    if cmp.received == 0:
        return ADVICE_NOTHING
    if cmp.received >= cmp.sent and cmp.byte_errors <= max(2, cmp.sent // 20):
        return ADVICE_SPARSE
    if cmp.received >= cmp.sent:
        return ADVICE_GARBLED
    return ADVICE_PARTIAL


def _exchange_all(ctx: TestContext, items, dirs) -> core.CompareResult:
    """Envoie chaque trame dans chaque sens ; retourne la pire comparaison."""
    worst = None
    a, b = ctx.open_pair()
    try:
        for d in dirs:
            src, dst = (a, b) if d == "AB" else (b, a)
            for name, data in items:
                ctx.check_stop()
                core.precise_sleep(ctx.s.modbus_t35())
                cmp, *_ = engine.exchange(ctx, src, dst, data, ctx.s, f"{name} {'A→B' if d == 'AB' else 'B→A'}")
                if not cmp.ok and worst is None:
                    worst = cmp
    finally:
        a.close()
        b.close()
    return worst


QUICK_REPEAT = 5


def quick(ctx: TestContext, d: str) -> dict:
    """Test rapide d'un sens : la même trame envoyée 5 fois, chaque réception comparée."""
    s = ctx.s
    mask = (1 << s.bytesize) - 1
    frame = bytes(x & mask for x in core.modbus_frame(b"\x55\xAA\x00\xFF\x0F\xF0RS485"))
    arrow = "A → B" if d == "AB" else "B → A"
    src_name, dst_name = ctx.names(d)
    ctx.log(f"── Test rapide {arrow} ({src_name} → {dst_name}) ──", "title")
    a, b = ctx.open_pair()
    ok = 0
    bad = None
    last_rx = b""
    times = []
    try:
        src, dst = (a, b) if d == "AB" else (b, a)
        for _ in range(QUICK_REPEAT):
            ctx.check_stop()
            cmp, rx, t0, tf, tl = engine.exchange(ctx, src, dst, frame, s, arrow)
            last_rx = rx
            if cmp.ok:
                ok += 1
                if tl:
                    times.append(tl - t0)
            elif bad is None:
                bad = cmp
            time.sleep(0.01)
    finally:
        a.close()
        b.close()
    res = {"dir": d, "arrow": arrow, "src": src_name, "dst": dst_name, "ok": ok == QUICK_REPEAT,
           "count": f"{ok}/{QUICK_REPEAT}", "sent": core.to_hex(frame), "received": core.to_hex(last_rx, 24) or "(rien)",
           "summary": "", "advice": []}
    if res["ok"]:
        med = sorted(times)[len(times) // 2] if times else 0
        res["summary"] = f"{QUICK_REPEAT} trames sur {QUICK_REPEAT} reçues identiques · {fmt_time(med)} par trame"
    else:
        res["summary"] = f"{ok}/{QUICK_REPEAT} trames correctes — {bad.summary()}"
        res["advice"] = _advice_for(bad)
    return res


def run(ctx: TestContext, steps: List[Step], on_step: Callable[[Step], None]) -> List[Step]:
    s = ctx.s
    mask = (1 << s.bytesize) - 1

    def mark(st: Step, status: str, value: str = "", advice=None):
        st.status, st.value, st.advice = status, value, list(advice or [])
        on_step(st)

    by = {st.key: st for st in steps}

    # 1-2. connectivité
    for key, d in (("ab", "AB"), ("ba", "BA")):
        st = by[key]
        mark(st, "EN COURS")
        ctx.log(f"── {st.title} ──", "title")
        frame = bytes(x & mask for x in core.modbus_frame(b"\x55\xAA\x00\xFF\x0F\xF0RS485"))
        a, b = ctx.open_pair()
        try:
            src, dst = (a, b) if d == "AB" else (b, a)
            bad = None
            for _ in range(3):
                ctx.check_stop()
                cmp, *_ = engine.exchange(ctx, src, dst, frame, s, st.title)
                if not cmp.ok:
                    bad = cmp
                    break
                time.sleep(0.01)
        finally:
            a.close()
            b.close()
        if bad is None:
            mark(st, OK, f"{len(frame)} octets reçus identiques (3/3)")
        else:
            mark(st, NOK, bad.summary(), _advice_for(bad))
    if by["ab"].status != OK or by["ba"].status != OK:
        for k in ("data", "modbus", "load", "time"):
            mark(by[k], SKIP, "liaison de base en défaut : corrigez d'abord les envois A → B / B → A")
        return steps

    # 3. valeurs d'octets
    st = by["data"]
    mark(st, "EN COURS")
    ctx.log(f"── {st.title} ──", "title")
    items = [("00→FF", bytes(range(256))), ("0x55", b"\x55" * 32), ("0x00", b"\x00" * 32),
             ("0xFF", b"\xFF" * 32), ("PRBS", core.prbs15(96))]
    items = [(n, bytes(x & mask for x in dd)) for n, dd in items]
    worst = _exchange_all(ctx, items, ["AB", "BA"])
    nbits = sum(len(dd) for _, dd in items) * 2 * s.bytesize
    if worst is None:
        mark(st, OK, f"{nbits} bits vérifiés, aucune erreur")
    else:
        mark(st, NOK, worst.summary(), _advice_for(worst) + [
            "Erreurs seulement sur 0x55 ou 0xFF : déformation des impulsions dans la chaîne optique "
            "(à voir à l'oscilloscope)."])

    # 4. Modbus
    st = by["modbus"]
    if s.bytesize != 8:
        mark(st, SKIP, "Modbus RTU impose 8 bits de données")
    else:
        mark(st, "EN COURS")
        ctx.log(f"── {st.title} ──", "title")
        frames = [f for i, f in enumerate(core.typical_modbus_frames()) if i in (0, 1, 3, 8)]
        worst = _exchange_all(ctx, frames, ["AB", "BA"])
        if worst is None:
            mark(st, OK, f"{len(frames) * 2} trames identiques, CRC valides")
        else:
            mark(st, NOK, worst.summary(), _advice_for(worst))

    # 5. charge continue
    st = by["load"]
    if s.bytesize != 8:
        mark(st, SKIP, "nécessite 8 bits de données")
    else:
        mark(st, "EN COURS")
        ctx.log(f"── {st.title} ──", "title")
        r = engine.test_throughput(ctx, {"duration": 3.0, "payload": 32, "min_eff": 85.0, "dirs": "A→B"})
        res = r.measures.get("résultats", [{}])[0] if r.measures else {}
        eff = res.get("rate", 0) / s.max_bytes_per_s() * 100 if res else 0
        val = (f"{res.get('ok', 0)}/{res.get('sent', 0)} paquets OK · {fmt_rate(res.get('rate', 0))} "
               f"({eff:.0f} % du maximum)") if res else r.summary
        if r.status == OK:
            mark(st, OK, val)
        elif r.status == WARN:
            mark(st, WARN, val, ["Aucune erreur, mais le flux a des trous : cela vient en général de l'adaptateur "
                                 "USB ou du PC, pas des modules."])
        else:
            mark(st, NOK, val, ["Des données se perdent à pleine charge : la vitesse est peut-être trop élevée "
                                "pour un élément de la chaîne.", "Réessayez à une vitesse plus basse."])

    # 6. temps de transfert
    st = by["time"]
    mark(st, "EN COURS")
    ctx.log(f"── {st.title} ──", "title")
    r = engine.test_latency(ctx, {"samples": 20, "size": 8, "max_ms": 0.0, "dirs": "A→B"})
    if r.status == OK and r.rows:
        med = r.rows[0][3]
        wire = fmt_time(s.wire_time(8))
        adv = []
        if any("Latency Timer" in d for d in r.details):
            adv = ["Environ 16 ms de plus que prévu : réglez le « Latency Timer » de l'adaptateur FTDI sur 1 ms "
                   "(Gestionnaire de périphériques → port COM → Avancé)."]
        mark(st, WARN if adv else OK, f"médiane {med} pour 8 octets (dont {wire} de transmission sur la ligne)", adv)
    else:
        mark(st, NOK, r.summary, ADVICE_PARTIAL)
    return steps


def verdict(steps: List[Step]) -> str:
    sts = [st.status for st in steps]
    if any(x in (NOK, ERROR) for x in sts):
        return NOK
    if any(x == WARN for x in sts):
        return WARN
    return OK


def to_results(steps: List[Step], settings: str, started: float, duration: float) -> List[TestResult]:
    out = []
    for st in steps:
        r = TestResult(st.key, st.title, st.status or SKIP, st.value)
        r.details = [st.what] + [f"À vérifier : {a}" for a in st.advice]
        r.settings = settings
        r.started = started
        r.duration = duration / max(1, len(steps))
        out.append(r)
    return out
