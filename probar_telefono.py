"""Simula a Twilio contra telefono.py, para probar el adaptador sin numero.

    # en una terminal, con un token cualquiera:
    TWILIO_AUTH_TOKEN=prueba ~/omni_voice_project/venv/bin/python telefono.py
    # en otra:
    TWILIO_AUTH_TOKEN=prueba ~/omni_voice_project/venv/bin/python probar_telefono.py

Hace lo que haria Twilio: firma la peticion a /voz, abre el Media Stream con
el pase que le devuelven, manda la voz del llamante en mu-law 8 kHz a trozos
de 20 ms al ritmo del reloj (y silencio cuando no habla), reproduce lo que le
llega con su propio reloj, devuelve cada marca cuando ha sonado lo anterior a
ella y obedece 'clear'. Deja el audio de la llamada en llamadas/ y mide el
silencio de cada turno visto desde el lado de quien llama.
"""

import asyncio
import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
import warnings
import logging

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np
import soundfile as sf

from telefono import a_mulaw, de_mulaw, firma_valida  # noqa: F401  (misma firma)
from util import cargar_env, llamadas_dir

PUERTO = 8080
LINEAS = [
    "Buenas tardes, habla Patricia León, la contadora. Es por un soporte que falta para la renta.",
    "Mi número es tres uno ocho, seis cuatro cero, dos dos siete cinco.",
    "Sí, es correcto. Muchas gracias, hasta luego.",
]


def voz_llamante(carpeta):
    """Las lineas del llamante, sintetizadas una vez y guardadas a 8 kHz."""
    rutas = [carpeta / f"l{i}.wav" for i in range(len(LINEAS))]
    if all(r.exists() for r in rutas):
        return [sf.read(r, dtype="float32")[0] for r in rutas]
    import librosa
    import torch
    from omnivoice import OmniVoice
    m = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda", dtype=torch.float16)
    salida = []
    for texto, ruta in zip(LINEAS, rutas):
        a = np.asarray(m.generate(text=texto, language="Spanish",
                                  instruct="female, middle-aged", num_step=16)[0],
                       dtype=np.float32).reshape(-1)
        a = librosa.effects.trim(librosa.resample(a, orig_sr=24000, target_sr=8000),
                                 top_db=35)[0]
        sf.write(ruta, a, 8000)
        salida.append(a)
    del m
    torch.cuda.empty_cache()
    return salida


def firmar(url, params, token):
    import hashlib, hmac
    datos = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), datos.encode(), hashlib.sha1).digest()).decode()


async def llamar(lineas):
    import websockets
    token = os.environ["TWILIO_AUTH_TOKEN"]
    params = {"From": "+573186402275", "To": "+15550000000", "CallSid": "CAprueba"}
    # Sin cabeceras de tunel, el servidor reconstruye la URL como https://host/voz.
    firma = firmar(f"https://127.0.0.1:{PUERTO}/voz", params, token)
    req = urllib.request.Request(f"http://127.0.0.1:{PUERTO}/voz",
                                 data=urllib.parse.urlencode(params).encode(),
                                 headers={"X-Twilio-Signature": firma})
    twiml = urllib.request.urlopen(req, timeout=10).read().decode()
    pase = re.search(r'name="pase" value="([^"]+)"', twiml).group(1)

    sonando = []                  # (inicio, audio 8 kHz) de lo que dijo ella
    del_llamante = []             # (inicio, audio 8 kHz) de lo que dijo el llamante
    estado = {"fin": 0.0, "ultimo_media": 0.0, "cerrado": False}
    cola = []                     # trozos de 20 ms del llamante por mandar

    async with websockets.connect(f"ws://127.0.0.1:{PUERTO}/medios") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({"event": "start", "start": {
            "streamSid": "MZprueba", "callSid": "CAprueba",
            "customParameters": {"pase": pase},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))

        async def recibir():
            try:
                async for crudo in ws:
                    m = json.loads(crudo)
                    ahora = time.perf_counter()
                    if m["event"] == "media":
                        a = de_mulaw(m["media"]["payload"])
                        inicio = max(ahora, estado["fin"])
                        sonando.append((inicio, a))
                        estado["fin"] = inicio + len(a) / 8000
                        estado["ultimo_media"] = ahora
                    elif m["event"] == "clear":
                        sonando[:] = [(t, a[:max(0, int((ahora - t) * 8000))])
                                      for t, a in sonando if t < ahora]
                        estado["fin"] = ahora
                    elif m["event"] == "mark":
                        espera = max(0.0, estado["fin"] - ahora)
                        asyncio.get_running_loop().call_later(
                            espera, lambda n=m["mark"]["name"]: asyncio.ensure_future(
                                ws.send(json.dumps({"event": "mark", "streamSid": "MZprueba",
                                                    "mark": {"name": n}}))))
            except websockets.ConnectionClosed:
                pass
            estado["cerrado"] = True

        async def mandar():
            t0, n = time.perf_counter(), 0
            silencio = a_mulaw(np.zeros(160, dtype=np.float32))
            while not estado["cerrado"]:
                carga = cola.pop(0) if cola else silencio
                try:
                    await ws.send(json.dumps({"event": "media", "streamSid": "MZprueba",
                                              "media": {"track": "inbound", "payload": carga}}))
                except websockets.ConnectionClosed:
                    break
                n += 1
                await asyncio.sleep(max(0.0, t0 + n * 0.02 - time.perf_counter()))

        tareas = [asyncio.create_task(recibir()), asyncio.create_task(mandar())]

        async def esperar_que_calle(minimo):
            """Hasta que ella hablo algo despues de 'minimo' y lleva 1,2 s callada."""
            while not estado["cerrado"]:
                ahora = time.perf_counter()
                if (estado["ultimo_media"] > minimo and ahora > estado["fin"] + 1.2):
                    return
                await asyncio.sleep(0.05)

        await esperar_que_calle(0)
        for a in lineas:
            if estado["cerrado"]:
                break
            await asyncio.sleep(0.4)
            inicio = time.perf_counter()
            del_llamante.append((inicio, a))
            cola.extend(a_mulaw(a[i:i + 160]) for i in range(0, len(a) - 159, 160))
            await asyncio.sleep(len(a) / 8000)
            await esperar_que_calle(inicio + len(a) / 8000)
        limite = time.perf_counter() + 15
        while not estado["cerrado"] and time.perf_counter() < limite:
            await asyncio.sleep(0.1)
        for t in tareas:
            t.cancel()

    return sonando, del_llamante


def main():
    cargar_env()
    carpeta = llamadas_dir() / "prueba_telefono"
    carpeta.mkdir(exist_ok=True)
    lineas = voz_llamante(carpeta)
    sonando, del_llamante = asyncio.run(llamar(lineas))

    # Silencio visto desde quien llama: de que termina de hablar a que ella suena.
    print("\nturnos, desde el lado de quien llama:")
    for t, a in del_llamante:
        fin = t + len(a) / 8000
        siguiente = next((s for s, x in sonando if s > fin and len(x)), None)
        if siguiente:
            print(f"  silencio {siguiente - fin:.2f}s")
    todo = sonando + del_llamante
    if todo:
        t0 = min(t for t, _ in todo)
        n = max(int((t - t0) * 8000) + len(a) for t, a in todo)
        mezcla = np.zeros(n, dtype=np.float32)
        for t, a in todo:
            i = int((t - t0) * 8000)
            mezcla[i:i + len(a)] += a
        ruta = llamadas_dir() / "prueba_telefono.wav"
        sf.write(ruta, np.clip(mezcla, -1, 1), 8000)
        print(f"audio en {ruta}")


if __name__ == "__main__":
    main()
