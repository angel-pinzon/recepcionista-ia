"""Prueba el oido sin micro: intervenciones conocidas a ritmo real.

    ~/omni_voice_project/venv/bin/python probar_oido.py
    ~/omni_voice_project/venv/bin/python probar_oido.py --fin 500

Sintetiza con una voz diseñada unas intervenciones de llamante, entre ellas un
numero dictado por grupos con pausas como las de verdad, y un ruido corto que
no debe contar. Lo pasa por mu-law 8 kHz, se lo da al oido en bloques de 32 ms
al ritmo del reloj, y mide por intervencion cuanto tarda el texto desde que
la voz termina, y si el numero llego en una sola pieza.
"""

import argparse
import logging
import time
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import librosa
import numpy as np
import torch

from ensayo import telefono
from oido import RATE, Oido
from util import cargar_config

PAUSA_DICTADO = 0.45
ENTRE = 2.5

INTERVENCIONES = [
    ["Buenas tardes, habla Patricia León, la contadora, es por un soporte de la renta."],
    ["Anote, por favor:", "tres dieciocho,", "seis cuarenta,", "dos dos siete cinco."],
    ["Sí, ese es. Muchas gracias."],
    "ruido",
    ["Es urgente, se vence la próxima semana, dígale que me llame hoy."],
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fin", type=int, help="ms de silencio para cerrar el turno")
    args = ap.parse_args()
    cfg = cargar_config()
    if args.fin:
        cfg.setdefault("oido", {})["fin_ms"] = args.fin

    from omnivoice import OmniVoice
    from transformers import pipeline
    tts = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda", dtype=torch.float16)
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                   torch_dtype=torch.float16, device="cuda")
    calor, _ = librosa.load("voces/recepcionista.wav", sr=RATE)
    asr({"raw": calor, "sampling_rate": RATE}, generate_kwargs={"language": "es"})

    partes, fines, textos = [np.zeros(int(RATE * 1.0), dtype=np.float32)], [], []
    rng = np.random.default_rng(0)
    for inter in INTERVENCIONES:
        if inter == "ruido":
            golpe = (rng.standard_normal(int(RATE * 0.15)) * 0.08).astype(np.float32)
            partes += [golpe, np.zeros(int(RATE * ENTRE), dtype=np.float32)]
            continue
        for k, trozo in enumerate(inter):
            a = tts.generate(text=trozo, language="Spanish", instruct="female, middle-aged",
                             num_step=16)[0]
            a = telefono(np.asarray(a, dtype=np.float32).squeeze(), 24000)
            a = librosa.resample(a, orig_sr=24000, target_sr=RATE)
            # OmniVoice deja silencio al principio y al final de cada trozo:
            # sin recortarlo, la pausa del dictado es mas larga de lo que dice.
            a, _ = librosa.effects.trim(a, top_db=35)
            partes.append(a)
            if k < len(inter) - 1:
                partes.append(np.zeros(int(RATE * PAUSA_DICTADO), dtype=np.float32))
        fines.append(sum(len(p) for p in partes) / RATE)
        textos.append(" ".join(inter))
        partes.append(np.zeros(int(RATE * ENTRE), dtype=np.float32))
    audio = np.concatenate(partes)
    del tts
    torch.cuda.empty_cache()

    eventos = []
    oido = Oido(cfg, asr, lambda e: eventos.append((time.perf_counter(), e)))
    bloque = int(RATE * 0.032)
    t0 = time.perf_counter()
    for i in range(0, len(audio), bloque):
        oido.alimentar(audio[i:i + bloque])
        espera = t0 + (i + bloque) / RATE - time.perf_counter()
        if espera > 0:
            time.sleep(espera)
    time.sleep(2)

    finales = [(t - t0, e["texto"]) for t, e in eventos if e["final"]]
    inicios = sum(1 for _, e in eventos if not e["final"])
    print(f"fin de turno tras {oido.fin * 32} ms de silencio")
    print(f"{inicios} inicios de voz detectados, {len(finales)} intervenciones entregadas "
          f"(esperadas {len(textos)})\n")
    for fin, esperado in zip(fines, textos):
        # La entrega que corresponde: la primera despues de que acabo de hablar.
        t, texto = next(((t, x) for t, x in finales if t >= fin), (None, "(nada)"))
        cuando = f"{t - fin:.2f}s" if t is not None else "-"
        print(f"  dicho:   {esperado}\n  oído:    {texto}\n"
              f"  entregado {cuando} después de terminar de hablar\n")
    for t, texto in finales:
        if not any(0 <= t - f < 3 for f in fines):
            print(f"  entrega de más a los {t:.1f}s: «{texto}»")
    for r in oido.registro:
        if r.get("descartado"):
            print(f"  descartado: «{r.get('texto')}»")
    especuladas = sum(1 for r in oido.registro if "whisper_s" not in r and "entregado" in r)
    print(f"{especuladas} de {len(oido.registro)} cerradas con el texto ya especulado")


if __name__ == "__main__":
    main()
