"""El recado por Telegram, y el buzon que evita perderlo.

Cada recado se escribe en llamadas/pendientes/ ANTES de enviarse y se borra
solo cuando Telegram confirma. Si al colgar no hay internet, el recado espera
ahi y sale en el siguiente envio o al arrancar.

El bot solo escribe a TELEGRAM_CHAT_ID: aunque alguien le hable al bot, no
le contesta a nadie mas.
"""

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from util import llamadas_dir

API = "https://api.telegram.org/bot{}/{}"
LIMITE = 4096           # caracteres por mensaje en Telegram
ICONO = {"alta": "🔴", "normal": "🟡", "baja": "⚪"}


def _clave(nombre):
    valor = os.environ.get(nombre)
    if not valor:
        raise RuntimeError(f"Falta {nombre} en el .env")
    return valor


def telefono(digitos):
    """Con el +57 delante y por grupos, Telegram lo vuelve tocable en el movil."""
    if not digitos:
        return None
    if len(digitos) == 10:
        return f"+57 {digitos[:3]} {digitos[3:6]} {digitos[6:]}"
    return digitos


def formato(ficha, registro, cuando, prueba=False):
    e = lambda x: html.escape(str(x)) if x else ""
    urgencia = ficha.get("urgencia", "normal")
    quien = e(ficha.get("nombre")) or "Alguien sin nombre"
    if ficha.get("de_parte_de"):
        quien += f" · {e(ficha['de_parte_de'])}"
    lineas = []
    if prueba:
        lineas.append("<i>[prueba]</i>")
    lineas.append(f"{ICONO.get(urgencia, '🟡')} <b>{quien}</b>")
    lineas.append(e(ficha.get("motivo")))
    numero = telefono(ficha.get("numero"))
    if numero:
        ext = f" ext. {e(ficha['extension'])}" if ficha.get("extension") else ""
        origen = " (del identificador)" if ficha.get("numero_de") == "identificador" else ""
        lineas.append(f"📞 {numero}{ext}{origen}")
    elif ficha.get("devolver"):
        lineas.append("📞 no dejó número")
    if ficha.get("resumen"):
        lineas.append(f"\n{e(ficha['resumen'])}")
    lineas.append(f"\n<i>{cuando:%d/%m %H:%M} · urgencia {e(urgencia)} · {e(ficha.get('tipo'))}</i>")
    cabeza = "\n".join(lineas)

    dialogo = []
    for r in registro:
        if r["quien"] == "recepcionista":
            dialogo.append(f"<b>R:</b> {e(r['texto'])}")
        elif r["quien"] == "llamante":
            dialogo.append(f"<b>L:</b> {e(r['texto'])}")
    cuerpo = "\n".join(dialogo)
    sobra = LIMITE - len(cabeza) - 60
    if len(cuerpo) > sobra:
        cuerpo = cuerpo[:sobra].rsplit("\n", 1)[0] + "\n…"
    return f"{cabeza}\n\n<blockquote expandable>{cuerpo}</blockquote>"


def _enviar(texto, silencioso):
    datos = urllib.parse.urlencode({
        "chat_id": _clave("TELEGRAM_CHAT_ID"), "text": texto, "parse_mode": "HTML",
        "disable_notification": "true" if silencioso else "false",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }).encode()
    url = API.format(_clave("TELEGRAM_BOT_TOKEN"), "sendMessage")
    try:
        with urllib.request.urlopen(url, datos, timeout=15) as r:
            return json.load(r).get("ok", False)
    except urllib.error.HTTPError as err:
        detalle = err.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"Telegram rechazó el mensaje: {detalle}")


def pendientes_dir():
    ruta = llamadas_dir() / "pendientes"
    ruta.mkdir(exist_ok=True)
    return ruta


def encolar(texto, silencioso):
    ruta = pendientes_dir() / f"{datetime.now():%Y%m%d_%H%M%S_%f}.json"
    ruta.write_text(json.dumps({"texto": texto, "silencioso": silencioso}, ensure_ascii=False),
                    encoding="utf-8")
    return ruta


def vaciar_buzon():
    """Envia lo pendiente, en orden. Devuelve (enviados, que_quedan)."""
    enviados = 0
    for ruta in sorted(pendientes_dir().glob("*.json")):
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        try:
            if not _enviar(datos["texto"], datos["silencioso"]):
                break
        except (OSError, RuntimeError):
            break           # sin red: lo demas tambien espera
        ruta.unlink()
        enviados += 1
    return enviados, len(list(pendientes_dir().glob("*.json")))
