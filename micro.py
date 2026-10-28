"""Hablar con la recepcionista por el micro y los altavoces del PC.

    ~/omni_voice_project/venv/bin/python micro.py
    ~/omni_voice_project/venv/bin/python micro.py --sin-auriculares

Con auriculares se la puede interrumpir. Sin ellos el micro la oye a ella por
los altavoces y se interrumpiria sola, asi que --sin-auriculares deja de
escuchar mientras habla: no se la puede cortar, pero no se contesta a si misma.

Ctrl+C cuelga. La llamada se guarda en llamadas/ con los silencios medidos.
"""

import argparse
import logging
import time
import warnings
from statistics import median

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import librosa
import torch

from llamada import Llamada
from pulse import Altavoz, Micro
from util import cargar_config, cargar_env
from voz import Voz


def cargar_oido():
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                   torch_dtype=torch.float16, device="cuda")
    # Calentar con habla de verdad: con silencio, la primera frase real
    # tardaba 2,5 s en vez de 0,5 s.
    calor, _ = librosa.load("voces/recepcionista.wav", sr=16000)
    asr({"raw": calor, "sampling_rate": 16000}, generate_kwargs={"language": "es"})
    return asr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-auriculares", action="store_true")
    ap.add_argument("--llamante", help="numero, como si llegara por el identificador")
    args = ap.parse_args()
    cargar_env()
    cfg = cargar_config()

    print("cargando voz y oído...", flush=True)
    voz, asr = Voz(cfg), cargar_oido()
    micro = Micro(ms=32)
    llamada = Llamada(cfg, voz, asr, Altavoz(), llamante=args.llamante,
                      interrumpible=not args.sin_auriculares)
    vistos = 0
    print("en llamada. Ctrl+C para colgar.\n", flush=True)
    try:
        while not llamada.terminada:
            llamada.alimentar(micro.leer())
            registro = llamada.conductor.registro
            for r in registro[vistos:]:
                if r["quien"] != "sistema":
                    print(f"{'ella' if r['quien'] == 'recepcionista' else 'tú  '}: {r['texto']}",
                          flush=True)
            vistos = len(registro)
    except KeyboardInterrupt:
        llamada.colgar()
        time.sleep(0.3)
    micro.cerrar()

    s = llamada.silencios()
    if s:
        print(f"\nsilencio por turno: mediana {median(x['silencio'] for x in s):.2f}s, "
              f"máx {max(x['silencio'] for x in s):.2f}s")
    if llamada.cortes:
        print(f"se calló en {median(c['callada'] - c['detectado'] for c in llamada.cortes) * 1000:.0f} ms "
              f"al interrumpirla ({len(llamada.cortes)} veces)")
    print(f"guardada en {llamada.guardar('micro')}")


if __name__ == "__main__":
    main()
