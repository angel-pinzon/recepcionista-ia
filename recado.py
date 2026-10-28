"""Al colgar: la ficha, el mensaje y el envio a Telegram.

Lo llama micro.py (y en la F2 el telefono) al terminar cada llamada. Tambien
sirve suelto, sobre llamadas ya guardadas en llamadas/:

    python3 recado.py llamadas/sim_*_prisa.json            # ver la ficha
    python3 recado.py llamadas/sim_*.json --enviar          # y mandarla, como [prueba]
    python3 recado.py --buzon                               # reenviar lo pendiente
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import resumen
import telegram
from util import cargar_config, cargar_env


def despachar(cfg, registro, llamante=None, cuando=None, prueba=False, enviar=True):
    """Hace la ficha, la deja en el buzon y vacia el buzon. Devuelve la ficha."""
    cuando = cuando or datetime.now()
    if not any(r["quien"] == "llamante" for r in registro):
        # Colgo tras el saludo: no hay nada que resumir.
        ficha = {"nombre": None, "motivo": "Colgó sin dejar mensaje.", "urgencia": "baja",
                 "numero": "".join(c for c in (llamante or "") if c.isdigit()) or None,
                 "numero_de": "identificador", "devolver": False, "tipo": "otro"}
    else:
        ficha = resumen.ficha(cfg, registro, llamante)
    texto = telegram.formato(ficha, registro, cuando, prueba=prueba)
    ficha["mensaje"] = texto
    if enviar:
        # Solo suena lo urgente: lo demas llega en silencio y se lee al rato.
        telegram.encolar(texto, silencioso=ficha.get("urgencia") != "alta")
        ficha["enviados"], ficha["pendientes"] = telegram.vaciar_buzon()
    return ficha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("llamadas", nargs="*")
    ap.add_argument("--enviar", action="store_true", help="mandarlas a Telegram como [prueba]")
    ap.add_argument("--buzon", action="store_true", help="solo reenviar lo pendiente")
    args = ap.parse_args()
    cargar_env()
    cfg = cargar_config()
    if args.buzon:
        enviados, quedan = telegram.vaciar_buzon()
        print(f"{enviados} enviados, {quedan} siguen pendientes")
        return
    for ruta in args.llamadas:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        registro = datos["registro"]
        cuando = datetime.fromtimestamp(registro[0]["t"]) if registro else datetime.now()
        ficha = despachar(cfg, registro, datos.get("llamante"), cuando, prueba=True,
                          enviar=args.enviar)
        estado = (f" → enviada ({ficha['pendientes']} pendientes)" if args.enviar else "")
        print(f"\n=== {Path(ruta).stem}{estado}")
        print(json.dumps({k: v for k, v in ficha.items()
                          if k not in ("mensaje", "enviados", "pendientes")},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
