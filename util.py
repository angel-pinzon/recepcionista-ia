"""Configuracion y .env, compartidos por todos los modulos."""

import json
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


def cargar_env():
    """Vuelca las claves de .env en el entorno sin pisar las ya definidas."""
    ruta = RAIZ / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip("\"'"))


def cargar_config():
    with open(RAIZ / "config.json", encoding="utf-8") as fh:
        return json.load(fh)


def llamadas_dir():
    ruta = RAIZ / "llamadas"
    ruta.mkdir(exist_ok=True)
    return ruta
