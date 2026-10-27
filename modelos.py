"""Clientes de modelos en streaming: Gemini y Ollama.

En un telefono la respuesta no puede esperar a estar entera. Cada cliente
devuelve un generador de trozos de texto segun llegan, para que la primera
frase empiece a sintetizarse mientras el modelo sigue escribiendo la segunda.

Los mensajes van en el formato de siempre, [{"role": "user"|"assistant",
"content": str}], y cada cliente lo traduce al suyo.
"""

import http.client
import json
import os
import socket
import urllib.parse

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{}:streamGenerateContent?alt=sse"


class FaltaClave(RuntimeError):
    pass


class SinRespuesta(RuntimeError):
    """El modelo no contesto a tiempo o devolvio un error."""


def _clave(nombre):
    clave = os.environ.get(nombre)
    if not clave:
        raise FaltaClave(f"Falta {nombre} en el .env")
    return clave


def _abrir(url, cuerpo, cabeceras, plazo_envio, plazo_respuesta, intentos=3):
    """POST que devuelve la respuesta abierta, para leerla en streaming.

    El envio y la espera tienen plazos aparte. Heredado de la tertulia: desde
    WSL, una peticion a Google se queda a veces atascada a mitad del envio. Con
    un solo plazo para todo, el atasco se comia la espera entera; aqui seria un
    silencio de varios segundos con alguien al telefono. Si el envio se atasca,
    se repite con una conexion nueva. Es seguro: la peticion no llego entera.
    """
    partes = urllib.parse.urlsplit(url)
    ruta = partes.path + (f"?{partes.query}" if partes.query else "")
    datos = json.dumps(cuerpo).encode("utf-8")
    clase = http.client.HTTPSConnection if partes.scheme == "https" else http.client.HTTPConnection
    ultimo = None
    for _ in range(intentos):
        conexion = clase(partes.hostname, partes.port, timeout=plazo_envio)
        try:
            conexion.request("POST", ruta, body=datos,
                             headers={"Content-Type": "application/json", **cabeceras})
        except OSError as err:
            conexion.close()
            ultimo = err
            continue
        conexion.sock.settimeout(plazo_respuesta)
        try:
            resp = conexion.getresponse()
        except OSError as err:
            conexion.close()
            raise SinRespuesta(f"sin respuesta: {err}")
        if resp.status >= 400:
            detalle = resp.read().decode("utf-8", "replace")[:300]
            conexion.close()
            raise SinRespuesta(f"HTTP {resp.status}: {detalle}")
        return conexion, resp
    raise SinRespuesta(f"envio atascado {intentos} veces: {ultimo}")


def _lineas(conexion, resp):
    try:
        while True:
            try:
                linea = resp.readline()
            except (socket.timeout, OSError) as err:
                raise SinRespuesta(f"el streaming se corto: {err}")
            if not linea:
                return
            yield linea.decode("utf-8", "replace").strip()
    finally:
        conexion.close()


def gemini(ficha, sistema, mensajes, tope, plazo_envio, plazo_respuesta):
    contenidos = [{"role": "model" if m["role"] == "assistant" else "user",
                   "parts": [{"text": m["content"]}]} for m in mensajes]
    cuerpo = {
        "systemInstruction": {"parts": [{"text": sistema}]},
        "contents": contenidos,
        "generationConfig": {
            "temperature": ficha.get("temperatura", 0.6),
            "maxOutputTokens": tope,
            # Pensar cuesta segundos que en una llamada no hay.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    conexion, resp = _abrir(GEMINI.format(ficha["modelo"]), cuerpo,
                            {"x-goog-api-key": _clave("GEMINI_API_KEY")},
                            plazo_envio, plazo_respuesta)
    for linea in _lineas(conexion, resp):
        if not linea.startswith("data:"):
            continue
        datos = json.loads(linea[5:])
        candidato = (datos.get("candidates") or [{}])[0]
        for parte in candidato.get("content", {}).get("parts", []):
            if parte.get("text"):
                yield parte["text"]


def ollama(ficha, sistema, mensajes, tope, plazo_envio, plazo_respuesta, host, num_ctx,
           keep_alive):
    """API nativa /api/chat, no la /v1: esa ignora num_ctx y trunca a 4096.

    think=false explicito: en qwen3 el '/no_think' del prompt no lo apaga.
    """
    cuerpo = {
        "model": ficha["modelo"],
        "messages": [{"role": "system", "content": sistema}] + mensajes,
        "stream": True,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"temperature": ficha.get("temperatura", 0.6), "num_ctx": num_ctx,
                    "num_predict": tope},
    }
    conexion, resp = _abrir(f"{host}/api/chat", cuerpo, {}, plazo_envio, plazo_respuesta)
    for linea in _lineas(conexion, resp):
        if not linea:
            continue
        datos = json.loads(linea)
        if datos.get("error"):
            raise SinRespuesta(datos["error"])
        texto = datos.get("message", {}).get("content", "")
        if texto:
            yield texto
        if datos.get("done"):
            return


def stream(cfg, ficha, sistema, mensajes, plazo_respuesta=None):
    """Generador de trozos de texto del proveedor que diga la ficha."""
    c = cfg["cerebro"]
    plazo_respuesta = plazo_respuesta or ficha.get("plazo") or c["plazo_primer_texto"]
    if ficha["proveedor"] == "gemini":
        return gemini(ficha, sistema, mensajes, c["tope_tokens"], c["plazo_envio"],
                      plazo_respuesta)
    if ficha["proveedor"] == "ollama":
        o = cfg["ollama"]
        return ollama(ficha, sistema, mensajes, c["tope_tokens"], c["plazo_envio"],
                      plazo_respuesta, o["host"], o["num_ctx"], o["keep_alive"])
    raise ValueError(f"Proveedor desconocido: {ficha['proveedor']}")


def completo(cfg, ficha, sistema, mensajes, plazo_respuesta=30):
    """La respuesta entera de una vez, para lo que no va a la voz."""
    return "".join(stream(cfg, ficha, sistema, mensajes, plazo_respuesta)).strip()
