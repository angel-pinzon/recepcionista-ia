"""Una llamada simulada con voz, de punta a punta salvo el micro.

    ~/omni_voice_project/venv/bin/python ensayo.py prisa
    ~/omni_voice_project/venv/bin/python ensayo.py corrige --interrumpe

El llamante es el de simular.py: otro modelo escribe lo que dice, OmniVoice
le pone una voz diseñada y se degrada a calidad telefonica (mu-law 8 kHz).
Ese audio entra por el oido en bloques de 32 ms al ritmo del reloj, como si
viniera del micro, y en los ratos en que nadie habla entra silencio: el oido
escucha siempre, como en una llamada.

Con --interrumpe, en el segundo turno el llamante habla encima de la
recepcionista un segundo despues de que ella empieza, y se mide cuanto tarda
en callarse.

Deja en llamadas/ el audio de la conversacion entera, con las dos voces y los
silencios reales, y los tiempos por turno.
"""

import argparse
import logging
import subprocess
import tempfile
import threading
import time
import warnings
from collections import deque
from statistics import median

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import librosa
import numpy as np
import soundfile as sf

import modelos
import simular
from llamada import Llamada
from micro import cargar_oido
from util import cargar_config, cargar_env, llamadas_dir
from voz import RATE, Archivo, Voz

BLOQUE = 512                       # 32 ms a 16 kHz, como el micro
INTERRUPCION = "Perdón, perdón, se me olvidaba decirle que es muy urgente."
VOCES = {"enrollada": "female, elderly", "mismo_numero": "female, young adult",
         "ventas": "female, young adult", "urgente": "female, middle-aged",
         "corrige": "female, middle-aged"}


def telefono(audio, rate):
    """Ida y vuelta por mu-law 8 kHz: lo que deja de la voz una linea telefonica."""
    with tempfile.TemporaryDirectory() as d:
        sf.write(f"{d}/a.wav", audio, rate)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f"{d}/a.wav", "-ar", "8000",
                        "-c:a", "pcm_mulaw", f"{d}/b.wav"], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f"{d}/b.wav", "-ar", str(rate),
                        f"{d}/c.wav"], check=True)
        x, _ = sf.read(f"{d}/c.wav", dtype="float32")
    return x


class Linea:
    """El lado de quien llama: manda a la llamada su voz o silencio, sin parar."""

    def __init__(self, llamada, archivo):
        self.llamada, self.archivo = llamada, archivo
        self._cola = deque()
        self.empiezos = []          # cuando empezo a sonar cada intervencion
        threading.Thread(target=self._bucle, daemon=True).start()

    def hablar(self, a24):
        a16 = librosa.resample(a24, orig_sr=RATE, target_sr=16000)
        bloques = [a16[i:i + BLOQUE] for i in range(0, len(a16) - BLOQUE + 1, BLOQUE)]
        self._cola.append((a24, bloques))

    @property
    def hablando(self):
        return bool(self._cola)

    def _bucle(self):
        t0, n, actual = time.perf_counter(), 0, None
        while not self.llamada.terminada:
            if actual is None and self._cola:
                a24, bloques = self._cola[0]
                actual = iter(bloques)
                ahora = time.perf_counter()
                self.empiezos.append(ahora)
                self.archivo.agregar(ahora, a24)
            bloque = next(actual, None) if actual else None
            if bloque is None:
                if actual is not None:
                    self._cola.popleft()
                    actual = None
                bloque = np.zeros(BLOQUE, dtype=np.float32)
            self.llamada.alimentar(bloque)
            n += 1
            espera = t0 + n * BLOQUE / 16000 - time.perf_counter()
            if espera > 0:
                time.sleep(espera)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("escenario", nargs="?", default="prisa")
    ap.add_argument("--interrumpe", action="store_true")
    args = ap.parse_args()
    esc = simular.ESCENARIOS[args.escenario]
    cargar_env()
    cfg = cargar_config()

    print("cargando voz y oído...", flush=True)
    voz, asr = Voz(cfg), cargar_oido()
    archivo = Archivo()
    llamada = Llamada(cfg, voz, asr, archivo, llamante=esc.get("llamante"))
    linea = Linea(llamada, archivo)
    conductor = llamada.conductor
    instruct = VOCES.get(args.escenario, "male, middle-aged")
    sis = simular.LLAMANTE.format(dueno=cfg["recepcionista"]["dueno"],
                                  personaje=esc["personaje"], colgar=simular.COLGAR)

    def voz_llamante(texto):
        with voz._cerrojo:
            a = voz.modelo.generate(text=texto, language="Spanish", instruct=instruct,
                                    num_step=16)[0]
        a = telefono(np.asarray(a, dtype=np.float32).squeeze(), RATE)
        return librosa.effects.trim(a, top_db=35)[0]

    def respuestas():
        return sum(1 for r in conductor.registro if r["quien"] == "recepcionista")

    def esperar_su_turno(antes):
        """Hasta que ella contesto lo ultimo que se le dijo y termino de hablar."""
        while not llamada.terminada and (respuestas() <= antes or llamada.ocupada
                                         or linea.hablando):
            time.sleep(0.05)
        time.sleep(0.4)

    interrupcion = None
    contestadas = 0                 # el saludo es la primera
    for turno in range(12):
        esperar_su_turno(contestadas)
        contestadas = respuestas()
        if llamada.terminada:
            break
        vista = [{"role": "user" if r["quien"] == "recepcionista" else "assistant",
                  "content": r["texto"]} for r in conductor.registro if r["quien"] != "sistema"]
        texto = modelos.completo(cfg, cfg["simulador"], sis, vista)
        cuelga = simular.COLGAR in texto
        texto = texto.replace(simular.COLGAR, "").strip()
        if texto:
            linea.hablar(voz_llamante(texto))
        if args.interrumpe and turno == 1:
            # Hablarle encima un segundo despues de que empiece a contestar.
            n = len(llamada.hablante.linea)
            while len(llamada.hablante.linea) == n or "empieza" not in llamada.hablante.linea[n]:
                time.sleep(0.02)
            a = voz_llamante(INTERRUPCION)
            espera = llamada.hablante.linea[n]["empieza"] + 1.0 - time.perf_counter()
            time.sleep(max(0.0, espera))
            linea.hablar(a)
            interrupcion = len(linea.empiezos) + 1
        if cuelga:
            while linea.hablando:
                time.sleep(0.05)
            time.sleep(0.5)
            if not llamada.terminada:
                llamada.colgar()
            break
    time.sleep(1.0)

    salida = llamadas_dir() / f"ensayo_{args.escenario}.wav"
    sf.write(salida, telefono(archivo.audio(), RATE), RATE)
    llamada.guardar(f"ensayo_{args.escenario}")

    print(f"\n=== ensayo {args.escenario} ===")
    for r in conductor.registro:
        if r["quien"] != "sistema":
            marca = "R" if r["quien"] == "recepcionista" else "L"
            print(f"{marca}: {r['texto']}")
    s = llamada.silencios()
    huecos = []
    linea_voz = llamada.hablante.linea
    for a_, b_ in zip(linea_voz, linea_voz[1:]):
        if "termina" in a_ and "empieza" in b_ and b_["encolada"] < a_["termina"]:
            huecos.append(b_["empieza"] - a_["termina"])
    print()
    if huecos:
        print(f"huecos dentro de una misma respuesta: máx {max(huecos) * 1000:.0f} ms, "
              f"{sum(h > 0.05 for h in huecos)} de {len(huecos)} por encima de 50 ms")
    for x in s:
        print(f"  silencio {x['silencio']:.2f}s = oído {x['oido']:.2f}s + cerebro y voz "
              f"{x['cerebro_y_voz']:.2f}s   «{x['texto'][:50]}»")
    if s:
        print(f"silencio por turno: mediana {median(x['silencio'] for x in s):.2f}s, "
              f"máx {max(x['silencio'] for x in s):.2f}s")
    if interrupcion and len(linea.empiezos) >= interrupcion:
        habla = linea.empiezos[interrupcion - 1]
        callada = next((c["callada"] for c in llamada.cortes if c["callada"] >= habla), None)
        if callada:
            print(f"interrumpida: se calló {(callada - habla) * 1000:.0f} ms después de que "
                  f"le hablaran encima")
        else:
            print("interrumpida: NO se calló")
    print(f"audio en {salida}")


if __name__ == "__main__":
    main()
