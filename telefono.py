"""El telefono: llamadas de Twilio conectadas a la recepcionista.

    ~/omni_voice_project/venv/bin/python telefono.py --tunel --numero +1XXXXXXXXXX

Twilio llama a /voz cuando entra una llamada; aqui se comprueba la firma y se
le contesta que abra un Media Stream contra /medios. Por ese WebSocket va el
audio en las dos direcciones, en mu-law a 8 kHz y trozos de 20 ms. Es TCP, asi
que entra por el tunel de cloudflared sin abrir puertos en Windows.

Con --tunel arranca un tunel rapido de cloudflared (sin cuenta ni dominio; la
URL cambia cada vez) y con --numero apunta ese numero de Twilio a la URL nueva
usando su API, para no tener que pegarla a mano en la consola.

Una llamada a la vez: la GPU es una. Si entra otra mientras hay una en curso,
Twilio la rechaza como ocupado.

El .env necesita TWILIO_ACCOUNT_SID y TWILIO_AUTH_TOKEN. Sin el token no se
puede comprobar que las peticiones vienen de Twilio, y no se arranca.
"""

import argparse
import asyncio
import audioop
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import warnings
from datetime import datetime
from xml.sax.saxutils import escape

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi

from cerebro import saludo
from util import cargar_config, cargar_env

RATE_VOZ = 24000
RATE_LINEA = 8000
RATE_OIDO = 16000
ADELANTO = 0.25          # segundos de audio que se le mandan a Twilio por delante

# --- audio -----------------------------------------------------------------


class Bajada:
    """24 kHz -> 8 kHz con filtro antialias que guarda su estado entre trozos.

    Remuestrear cada trozo de 40 ms por separado deja un chasquido en cada
    junta: el filtro arranca de cero en cada uno. Aqui el estado pasa de un
    trozo al siguiente, y solo se reinicia al callarse.
    """

    def __init__(self):
        self.b = firwin(63, 3400, fs=RATE_VOZ)
        self.reiniciar()

    def reiniciar(self):
        self.zi = lfilter_zi(self.b, 1.0) * 0.0
        self.fase = 0

    def __call__(self, audio):
        y, self.zi = lfilter(self.b, 1.0, audio, zi=self.zi)
        salida = y[self.fase::3]
        self.fase = (self.fase - len(audio)) % 3
        return salida


class Subida:
    """8 kHz -> 16 kHz por interpolacion lineal, enlazando con el trozo anterior."""

    def __init__(self):
        self.ultimo = 0.0

    def __call__(self, x):
        previo = np.concatenate([[self.ultimo], x])
        medios = (previo[:-1] + previo[1:]) / 2
        salida = np.empty(len(x) * 2, dtype=np.float32)
        salida[0::2], salida[1::2] = medios, x
        self.ultimo = float(x[-1]) if len(x) else self.ultimo
        return salida


def de_mulaw(payload):
    pcm = audioop.ulaw2lin(base64.b64decode(payload), 2)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def a_mulaw(audio):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    return base64.b64encode(audioop.lin2ulaw(pcm, 2)).decode()


class SalidaTwilio:
    """La salida de la recepcionista hacia la llamada: escribir, vaciar, esperar.

    El Hablante escribe desde su hilo; el WebSocket vive en el bucle de asyncio,
    asi que cada envio se cruza con run_coroutine_threadsafe. Se manda a ritmo
    real con ADELANTO de margen: lo que Twilio tiene en cola es lo que sonaria
    aunque ella ya se hubiera callado, y por eso callar es pedirle 'clear'.
    """

    def __init__(self, ws, bucle, stream_sid):
        self.ws, self.bucle, self.sid = ws, bucle, stream_sid
        self.bajada = Bajada()
        self._reloj = None
        self._marcas = {}
        self._n = 0

    def _mandar(self, mensaje):
        futuro = asyncio.run_coroutine_threadsafe(self.ws.send_text(json.dumps(mensaje)),
                                                  self.bucle)
        try:
            futuro.result(timeout=2)
        except Exception:
            pass            # la llamada se corto: lo que quede ya no importa

    def escribir(self, audio):
        ahora = time.perf_counter()
        if self._reloj is None or self._reloj < ahora:
            self._reloj = ahora
        linea = self.bajada(np.asarray(audio, dtype=np.float32))
        self._mandar({"event": "media", "streamSid": self.sid,
                      "media": {"payload": a_mulaw(linea)}})
        self._reloj += len(audio) / RATE_VOZ
        espera = self._reloj - ADELANTO - time.perf_counter()
        if espera > 0:
            time.sleep(espera)

    def vaciar(self):
        self._mandar({"event": "clear", "streamSid": self.sid})
        self._reloj = None
        self.bajada.reiniciar()

    def esperar(self):
        """Hasta que Twilio diga que sono todo lo mandado."""
        self._n += 1
        nombre = f"m{self._n}"
        evento = threading.Event()
        self._marcas[nombre] = evento
        self._mandar({"event": "mark", "streamSid": self.sid, "mark": {"name": nombre}})
        falta = max(0.0, (self._reloj or 0) - time.perf_counter())
        evento.wait(timeout=falta + 2.0)
        self._marcas.pop(nombre, None)

    def marca_recibida(self, nombre):
        evento = self._marcas.get(nombre)
        if evento:
            evento.set()


# --- firma de Twilio ---------------------------------------------------------


def firma_valida(url, params, firma, token):
    """X-Twilio-Signature: HMAC-SHA1 de la URL y los parametros POST ordenados."""
    datos = url + "".join(k + params[k] for k in sorted(params))
    calculada = base64.b64encode(hmac.new(token.encode(), datos.encode(),
                                          hashlib.sha1).digest()).decode()
    return hmac.compare_digest(calculada, firma or "")


def url_publica(request):
    """La URL tal como la pidio Twilio: el tunel cambia el esquema, no el host."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    esquema = request.headers.get("x-forwarded-proto", "https")
    consulta = f"?{request.url.query}" if request.url.query else ""
    return f"{esquema}://{host}{request.url.path}{consulta}"


def numero_local(desde):
    """'+573154287719' -> '3154287719'. Oculto o raro -> None."""
    digitos = re.sub(r"\D", "", desde or "")
    if desde and desde.startswith("+57") and len(digitos) == 12:
        return digitos[2:]
    return digitos if len(digitos) >= 7 else None


# --- servidor ----------------------------------------------------------------


def crear_app(cfg, voz, asr):
    from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

    import recado
    from llamada import Llamada

    app = FastAPI()
    token = os.environ["TWILIO_AUTH_TOKEN"]
    estado = {"ocupada": False, "pases": {}}

    def twiml(cuerpo):
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{cuerpo}</Response>',
                        media_type="application/xml")

    @app.post("/voz")
    async def voz_entrante(request: Request):
        params = {k: str(v) for k, v in (await request.form()).items()}
        if not firma_valida(url_publica(request), params,
                            request.headers.get("x-twilio-signature"), token):
            return Response(status_code=403)
        if estado["ocupada"]:
            return twiml('<Reject reason="busy"/>')
        # Un pase de un solo uso por llamada: el WebSocket no lleva firma de
        # Twilio, y sin esto cualquiera que diera con la URL podria hablarle.
        pase = secrets.token_urlsafe(24)
        estado["pases"][pase] = (time.time(), params.get("From", ""))
        host = urllib.parse.urlsplit(url_publica(request)).netloc
        return twiml(f'<Connect><Stream url="wss://{escape(host)}/medios">'
                     f'<Parameter name="pase" value="{pase}"/></Stream></Connect>')

    @app.websocket("/medios")
    async def medios(ws: WebSocket):
        await ws.accept()
        bucle = asyncio.get_running_loop()
        llamada = salida = None
        subida = Subida()
        inicio = datetime.now()
        try:
            while True:
                mensaje = json.loads(await ws.receive_text())
                evento = mensaje.get("event")
                if evento == "start":
                    pase = mensaje["start"].get("customParameters", {}).get("pase", "")
                    creado, desde = estado["pases"].pop(pase, (0, ""))
                    if time.time() - creado > 30 or estado["ocupada"]:
                        await ws.close()
                        return
                    estado["ocupada"] = True
                    llamante = numero_local(desde)
                    salida = SalidaTwilio(ws, bucle, mensaje["start"]["streamSid"])
                    llamada = await bucle.run_in_executor(
                        None, lambda: Llamada(cfg, voz, asr, salida, llamante=llamante))
                    print(f"{inicio:%H:%M:%S} entra llamada de {desde or 'número oculto'}",
                          flush=True)
                    asyncio.create_task(_vigilar(ws, llamada))
                elif evento == "media" and llamada:
                    if mensaje["media"].get("track", "inbound") == "inbound":
                        llamada.alimentar(subida(de_mulaw(mensaje["media"]["payload"])))
                elif evento == "mark" and salida:
                    salida.marca_recibida(mensaje["mark"]["name"])
                elif evento == "stop":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if llamada:
                await bucle.run_in_executor(None, _terminar, cfg, llamada, inicio, recado)
            estado["ocupada"] = False

    return app


async def _vigilar(ws, llamada):
    """Cuando ella se despide y termina de sonar, se cuelga cerrando el stream."""
    while not llamada.terminada:
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    try:
        await ws.close()
    except Exception:
        pass


def _terminar(cfg, llamada, inicio, recado):
    if not llamada.terminada:
        llamada.colgar()
        time.sleep(0.3)
    ruta = llamada.guardar("telefono")
    registro, llamante = llamada.conductor.registro, llamada.conductor.llamante
    llamada.cerrar()
    s = llamada.silencios()
    resumen = (f"silencio mediana {sorted(x['silencio'] for x in s)[len(s) // 2]:.2f}s"
               if s else "sin turnos")
    print(f"{datetime.now():%H:%M:%S} colgó · {resumen} · {ruta.name}", flush=True)
    try:
        ficha = recado.despachar(cfg, registro, llamante, inicio)
        print(f"   recado {ficha.get('urgencia')}: {ficha.get('motivo')}", flush=True)
    except Exception as err:            # el recado no puede tumbar el servidor
        print(f"   !! el recado falló: {err}", flush=True)


# --- tunel y numero ------------------------------------------------------------


def abrir_tunel(puerto):
    """Tunel rapido de cloudflared. Devuelve (proceso, url publica)."""
    proc = subprocess.Popen([os.path.expanduser("~/bin/cloudflared"), "tunnel", "--url",
                             f"http://127.0.0.1:{puerto}", "--no-autoupdate"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    limite = time.time() + 30
    for linea in proc.stderr:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", linea)
        if m:
            threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()
            return proc, m.group(0)
        if time.time() > limite:
            break
    proc.kill()
    raise SystemExit("cloudflared no dio una URL en 30 s")


def apuntar_numero(numero, url):
    """Cambia la URL de voz del numero en Twilio. Solo toca ese campo."""
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers"
    credencial = base64.b64encode(f"{sid}:{token}".encode()).decode()
    cabeceras = {"Authorization": f"Basic {credencial}"}
    consulta = urllib.parse.urlencode({"PhoneNumber": numero})
    with urllib.request.urlopen(urllib.request.Request(f"{base}.json?{consulta}",
                                                       headers=cabeceras), timeout=15) as r:
        encontrados = json.load(r)["incoming_phone_numbers"]
    if not encontrados:
        raise SystemExit(f"El número {numero} no está en esta cuenta de Twilio")
    datos = urllib.parse.urlencode({"VoiceUrl": url, "VoiceMethod": "POST"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"{base}/{encontrados[0]['sid']}.json", data=datos, headers=cabeceras), timeout=15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--puerto", type=int, default=8080)
    ap.add_argument("--tunel", action="store_true", help="abrir un tunel rapido de cloudflared")
    ap.add_argument("--numero", help="numero de Twilio a apuntar al tunel, p. ej. +15551234567")
    args = ap.parse_args()
    cargar_env()
    if not os.environ.get("TWILIO_AUTH_TOKEN"):
        raise SystemExit("Falta TWILIO_AUTH_TOKEN en el .env")
    cfg = cargar_config()

    import telegram
    from micro import cargar_oido
    from voz import Voz
    enviados, quedan = telegram.vaciar_buzon()
    if enviados or quedan:
        print(f"buzón: {enviados} recados pendientes enviados, {quedan} esperando")
    print("cargando voz y oído...", flush=True)
    voz, asr = Voz(cfg), cargar_oido()
    # Los tres saludos (dias, tardes, noches) listos: es lo primero que se oye
    # y no puede esperar dos segundos de sintesis.
    voz.fijar(*{saludo(cfg, datetime(2026, 1, 1, h)) for h in (9, 15, 20)})

    tunel = None
    if args.tunel:
        tunel, url = abrir_tunel(args.puerto)
        print(f"túnel: {url}", flush=True)
        if args.numero:
            apuntar_numero(args.numero, f"{url}/voz")
            print(f"{args.numero} apunta a {url}/voz", flush=True)
        else:
            print(f"pon esta URL como webhook de voz (POST) del número: {url}/voz")

    import uvicorn
    print("esperando llamadas. Ctrl+C para parar.", flush=True)
    try:
        uvicorn.run(crear_app(cfg, voz, asr), host="127.0.0.1", port=args.puerto,
                    log_level="warning")
    finally:
        if tunel:
            tunel.terminate()


if __name__ == "__main__":
    main()
