"""Rapport de test HTML autonome (traçabilité) et export CSV."""
from __future__ import annotations

import csv
import html
import platform
import time
from typing import Dict, List

from . import APP_NAME, APP_VERSION
from .engine import ABORTED, ERROR, NOK, OK, WARN, TestResult

_COLORS = {OK: "#1f9d55", NOK: "#d64545", WARN: "#d98a00", ERROR: "#8a2be2", ABORTED: "#6b7280"}


def _e(x) -> str:
    return html.escape(str(x))


def global_status(results: List[TestResult]) -> str:
    sts = [r.status for r in results]
    if not sts:
        return "—"
    if any(s in (NOK, ERROR) for s in sts):
        return NOK
    if any(s in (WARN, ABORTED) for s in sts):
        return WARN
    return OK


def write_html(path: str, results: List[TestResult], meta: Dict[str, str]) -> None:
    gs = global_status(results)
    rows = []
    for r in results:
        rows.append(f"<tr><td>{_e(r.title)}</td><td><span class='b' style='background:{_COLORS.get(r.status, '#555')}'>"
                    f"{_e(r.status)}</span></td><td>{_e(r.summary)}</td><td>{r.duration:.1f} s</td></tr>")
    details = []
    for r in results:
        tbl = ""
        if r.columns and r.rows:
            head = "".join(f"<th>{_e(c)}</th>" for c in r.columns)
            body = ""
            for row in r.rows:
                cls = ""
                if any(str(x) in ("NOK", "ERREUR", "NON", "INVALIDE") for x in row):
                    cls = " class='ko'"
                body += f"<tr{cls}>" + "".join(f"<td>{_e(x)}</td>" for x in row) + "</tr>"
            tbl = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        notes = "".join(f"<p class='note'>{_e(d)}</p>" for d in r.details if not d.startswith("Traceback"))
        details.append(
            f"<section><h3>{_e(r.title)} <span class='b' style='background:{_COLORS.get(r.status, '#555')}'>"
            f"{_e(r.status)}</span></h3><p>{_e(r.summary)}</p>"
            f"<p class='meta'>Réglages : {_e(r.settings)} · début {time.strftime('%H:%M:%S', time.localtime(r.started))}"
            f" · durée {r.duration:.1f} s</p>{tbl}{notes}</section>")
    meta_rows = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in meta.items() if v)
    doc = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Rapport de test RS485</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1d1d1f;background:#fff;max-width:1100px}}
h1{{margin:0 0 4px}} h3{{margin:18px 0 6px}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}}
th,td{{border:1px solid #d0d4da;padding:4px 8px;text-align:left;vertical-align:top}}
th{{background:#f0f2f5}} tr.ko td{{background:#fdecec}}
.b{{color:#fff;border-radius:10px;padding:2px 10px;font-size:12px;font-weight:600}}
.big{{font-size:20px;padding:4px 16px}}
.meta{{color:#666;font-size:12px}} .note{{color:#555;font-size:12px;font-style:italic}}
section{{border-top:1px solid #e3e5e8;padding-top:6px}}
@media print{{section{{page-break-inside:avoid}}}}
</style></head><body>
<h1>Rapport de test RS485</h1>
<p class="meta">{_e(APP_NAME)} v{_e(APP_VERSION)} · {_e(time.strftime('%d/%m/%Y %H:%M:%S'))} · {_e(platform.node())}</p>
<p>Verdict global : <span class="b big" style="background:{_COLORS.get(gs, '#555')}">{_e(gs)}</span></p>
<h2>Identification</h2><table>{meta_rows}</table>
<h2>Synthèse</h2>
<table><thead><tr><th>Test</th><th>Statut</th><th>Résultat</th><th>Durée</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Détail</h2>{''.join(details)}
<p class="meta">Signature opérateur : ______________________ &nbsp;&nbsp; Date : ____________</p>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def write_csv(path: str, results: List[TestResult], meta: Dict[str, str]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        for k, v in meta.items():
            w.writerow([k, v])
        w.writerow([])
        w.writerow(["Test", "Statut", "Résultat", "Durée (s)", "Réglages"])
        for r in results:
            w.writerow([r.title, r.status, r.summary, f"{r.duration:.1f}", r.settings])
        for r in results:
            if r.columns and r.rows:
                w.writerow([])
                w.writerow([r.title])
                w.writerow(r.columns)
                w.writerows(r.rows)
