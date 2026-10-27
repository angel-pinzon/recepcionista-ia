"""Hablar con la recepcionista escribiendo, haciendo de quien llama.

    python3 charla.py
    python3 charla.py --llamante 3154287719      # como si llegara el numero

/colgar termina la llamada. Al acabar se guarda en llamadas/.
"""

import argparse
import json
import time
from datetime import datetime

from conductor import Conductor
from util import cargar_config, cargar_env, llamadas_dir


def ejecutar(conductor, evento):
    for orden in conductor.recibir({"t": time.time(), **evento}):
        if orden["orden"] == "decir":
            print(f"  recepcionista: {orden['texto']}", flush=True)
        elif orden["orden"] == "colgar":
            print("  (cuelga)")
    ultimo = conductor.registro[-1]
    if ultimo["quien"] == "recepcionista" and "primera" in ultimo:
        extra = f" — {ultimo['fallo']}" if ultimo.get("fallo") else ""
        print(f"  [{ultimo['primera']:.2f}s hasta la primera frase, {ultimo['proveedor']}{extra}]")


def guardar(conductor, nombre):
    ruta = llamadas_dir() / f"{datetime.now():%Y%m%d_%H%M%S}_{nombre}.json"
    ruta.write_text(json.dumps({"llamante": conductor.llamante, "registro": conductor.registro},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return ruta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llamante")
    args = ap.parse_args()
    cargar_env()
    conductor = Conductor(cargar_config())
    ejecutar(conductor, {"evento": "conecta", "llamante": args.llamante})
    while not conductor.terminada:
        try:
            texto = input("tú: ").strip()
        except EOFError:
            texto = "/colgar"
        if texto == "/colgar":
            list(conductor.recibir({"t": time.time(), "evento": "cuelga"}))
            break
        ejecutar(conductor, {"evento": "habla", "texto": texto, "final": True})
    print(f"guardada en {guardar(conductor, 'charla')}")


if __name__ == "__main__":
    main()
