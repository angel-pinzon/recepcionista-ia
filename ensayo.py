"""Una llamada simulada con voz, de punta a punta salvo el micro.

    ~/omni_voice_project/venv/bin/python ensayo.py prisa

El llamante es el de simular.py: otro modelo escribe lo que dice, OmniVoice
le pone una voz diseñada, se degrada a calidad telefonica (mu-law 8 kHz) y
Whisper lo transcribe. Lo transcrito entra al conductor y la recepcionista
contesta con su voz por la cola de frases, como en una llamada.

Deja en llamadas/ el audio de la llamada entera, con los silencios reales de
la recepcionista, y mide por turno lo que tarda en sonar. Lo que piensa el
llamante simulado no cuenta como silencio: en la grabacion se sustituye por
una pausa fija.
"""

import json
import subprocess
import sys
import tempfile
import time
import warnings
import logging
from statistics import median

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import librosa
import numpy as np
import soundfile as sf
import torch

import modelos
import simular
from cerebro import saludo
from conductor import Conductor
from util import cargar_config, cargar_env, llamadas_dir
from voz import RATE, Archivo, Hablante, Voz

PAUSA_LLAMANTE = 0.4
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


def cola_cortada(audio):
    """Energia de los ultimos 50 ms con voz. Alta = la frase acaba en seco."""
    v = int(RATE * 0.05)
    bloques = [audio[i:i + v] for i in range(0, len(audio) - v, v)]
    rms = [float(np.sqrt((b ** 2).mean())) for b in bloques]
    return next((r for r in reversed(rms) if r > 0.003), 0.0)


def main():
    nombre = sys.argv[1] if len(sys.argv) > 1 else "prisa"
    esc = simular.ESCENARIOS[nombre]
    cargar_env()
    cfg = cargar_config()

    print("cargando modelos...", flush=True)
    voz = Voz(cfg)
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                   torch_dtype=torch.float16, device="cuda")
    # Calentar con habla de verdad: con silencio, la primera frase real
    # tardaba 2,5 s en vez de 0,5 s.
    calor, _ = librosa.load("voces/recepcionista.wav", sr=16000)
    asr({"raw": calor, "sampling_rate": 16000}, generate_kwargs={"language": "es"})
    print(f"VRAM {torch.cuda.memory_allocated() / 1e9:.2f} GB", flush=True)

    archivo = Archivo()
    hablante = Hablante(voz, archivo)
    conductor = Conductor(cfg)
    voz.fijar(saludo(cfg))

    sis = simular.LLAMANTE.format(dueno=cfg["recepcionista"]["dueno"],
                                  personaje=esc["personaje"], colgar=simular.COLGAR)
    vista, turnos = [], []

    def escuchar_hasta_que_calle():
        while hablante.hablando:
            time.sleep(0.02)

    def recibir(evento):
        dicho = []
        for orden in conductor.recibir({"t": time.time(), **evento}):
            if orden["orden"] == "decir":
                dicho.append(orden["texto"])
                hablante.decir(orden["texto"])
        if dicho:
            vista.append({"role": "user", "content": " ".join(dicho)})

    recibir({"evento": "conecta", "llamante": esc.get("llamante")})
    escuchar_hasta_que_calle()

    for _ in range(12):
        if conductor.terminada:
            break
        texto = modelos.completo(cfg, cfg["simulador"], sis, vista)
        cuelga = simular.COLGAR in texto
        texto = texto.replace(simular.COLGAR, "").strip()
        if texto:
            vista.append({"role": "assistant", "content": texto})
            with voz._cerrojo:
                a = voz.modelo.generate(text=texto, language="Spanish",
                                        instruct=VOCES.get(nombre, "male, middle-aged"),
                                        num_step=16)[0]
            a = telefono(np.asarray(a, dtype=np.float32).squeeze(), RATE)
            # Lo que tarda el simulador en pensar no es silencio de la llamada.
            archivo._reloj = None
            archivo.escribir(np.zeros(int(RATE * PAUSA_LLAMANTE), dtype=np.float32))
            archivo.escribir(a)
            fin_habla = time.perf_counter()

            x16 = librosa.resample(a, orig_sr=RATE, target_sr=16000)
            oido = asr({"raw": x16, "sampling_rate": 16000},
                       generate_kwargs={"language": "es", "task": "transcribe"})["text"].strip()
            t_asr = time.perf_counter() - fin_habla
            n = len(hablante.linea)
            recibir({"evento": "habla", "texto": oido, "final": True})
            escuchar_hasta_que_calle()
            propias = hablante.linea[n:]
            if propias:
                huecos = [b["empieza"] - a_["termina"] for a_, b in zip(propias, propias[1:])
                          if "termina" in a_ and "empieza" in b]
                turnos.append({"dijo": texto, "oyo": oido, "asr": t_asr,
                               "silencio": propias[0]["empieza"] - fin_habla,
                               "huecos": huecos,
                               "sintesis": [m["sintetizada"] - m["encolada"] for m in propias],
                               "respuesta": " ".join(m["texto"] for m in propias)})
        if cuelga and not conductor.terminada:
            list(conductor.recibir({"t": time.time(), "evento": "cuelga"}))
            break

    salida = llamadas_dir() / f"ensayo_{nombre}.wav"
    sf.write(salida, telefono(archivo.audio(), RATE), RATE)

    print(f"\n=== ensayo {nombre} ===")
    for t in turnos:
        print(f"L dijo: {t['dijo']}")
        if t["oyo"].lower().strip(" .,¿?¡!") != t["dijo"].lower().strip(" .,¿?¡!"):
            print(f"  oyó:  {t['oyo']}")
        print(f"R: {t['respuesta']}")
        hue = ", ".join(f"{h:.2f}" for h in t["huecos"]) or "-"
        sin = ", ".join(f"{s:.2f}" for s in t["sintesis"])
        print(f"   silencio {t['silencio']:.2f}s (Whisper {t['asr']:.2f}s) | "
              f"huecos entre frases [{hue}] | síntesis por frase [{sin}]")
    if turnos:
        print(f"\nsilencio: mediana {median(t['silencio'] for t in turnos):.2f}s, "
              f"máx {max(t['silencio'] for t in turnos):.2f}s")
    colas = [cola_cortada(m["audio"]) for m in hablante.linea if "audio" in m]
    print("energía final por frase (alta = corte en seco):",
          ", ".join(f"{c:.3f}" for c in colas))
    print(f"audio en {salida}")
    (llamadas_dir() / f"ensayo_{nombre}.json").write_text(
        json.dumps({"turnos": turnos, "registro": conductor.registro}, ensure_ascii=False,
                   indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
