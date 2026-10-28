"""Cuanto se le entiende a la recepcionista: sintetiza y vuelve a transcribir.

    ~/omni_voice_project/venv/bin/python probar_voz.py
    ~/omni_voice_project/venv/bin/python probar_voz.py --frases 20 --tomas 2

Toma frases que ella dijo en las llamadas guardadas en llamadas/, las
sintetiza varias veces con cada ajuste y le pasa cada toma a Whisper. Una
toma "falla" si Whisper no reconoce al menos el 85 % de las palabras: es la
misma vara que la tertulia usa para dar una sintesis por buena.

OmniVoice falla de forma aleatoria: la misma frase sale bien en una toma y
atropellada en la siguiente. Por eso se cuentan tomas y no frases, y hacen
falta varias por ajuste para ver diferencias.
"""

import argparse
import difflib
import glob
import json
import logging
import random
import re
import time
import unicodedata
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import librosa
import numpy as np
import torch

UNIDADES = "cero uno dos tres cuatro cinco seis siete ocho nueve".split()
DIFICILES = [   # las dos que no se le entendieron en la prueba por micro
    "Sí, le escucho perfectamente. ¿Con quién tengo el gusto de hablar?",
    "Entiendo perfectamente, don Pedro. ¿Podría contarme brevemente el motivo de su "
    "llamada para que él sepa de qué se trata?",
]


def normalizar(texto):
    """Minusculas, sin tildes ni puntuacion, y los numeros como digitos seguidos."""
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    for i, u in enumerate(UNIDADES):
        t = re.sub(rf"\b{u}\b", str(i), t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"(?<=\d)\s+(?=\d)", "", t)
    return t.split()


def cobertura(esperado, oido):
    a, b = normalizar(esperado), normalizar(oido)
    m = difflib.SequenceMatcher(None, a, b)
    return sum(x.size for x in m.get_matching_blocks()) / max(len(a), 1)


def frases_guardadas():
    vistas = set()
    for f in sorted(glob.glob("llamadas/*.json")):
        for r in json.load(open(f)).get("registro", []):
            if r["quien"] != "recepcionista":
                continue
            for x in re.split(r"(?<=[.!?])\s+", r["texto"].replace(" (la interrumpieron aquí)", "")):
                if len(x.split()) >= 4:
                    vistas.add(x.strip())
    return sorted(vistas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frases", type=int, default=40)
    ap.add_argument("--tomas", type=int, default=3)
    ap.add_argument("--solo", nargs="*", help="solo los ajustes cuyo nombre contenga esto")
    args = ap.parse_args()

    from omnivoice import OmniVoice
    from transformers import pipeline
    modelo = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda", dtype=torch.float16)
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                   torch_dtype=torch.float16, device="cuda")
    prompt = torch.load("voces/recepcionista.pt", weights_only=False)

    random.seed(1)
    # Las que llevan numeros en decenas no se pueden puntuar: Whisper las
    # escribe en digitos y "tres diez y ocho" no se parece a "318".
    guardadas = [f for f in frases_guardadas() if f not in DIFICILES
                 and not re.search(r"\b(diez|once|doce|veinte|treinta|cuarenta|cincuenta|"
                                   r"sesenta|setenta|ochenta|noventa)\b", f)]
    frases = DIFICILES + random.sample(guardadas, min(args.frases, len(guardadas)))

    todos = {
        "actual (16 pasos, vel 1,0)": {"num_step": 16, "speed": 1.0},
        "24 pasos": {"num_step": 24, "speed": 1.0},
        "32 pasos": {"num_step": 32, "speed": 1.0},
        "vel 0,9": {"num_step": 16, "speed": 0.9},
        "24 pasos, vel 0,9": {"num_step": 24, "speed": 0.9},
    }
    ajustes = {k: v for k, v in todos.items() if not args.solo or any(s in k for s in args.solo)}
    modelo.generate(text="Hola.", language="Spanish", voice_clone_prompt=prompt, num_step=16)

    resumen = {}
    for nombre, kw in ajustes.items():
        fallos, tiempos, peores, ppm = 0, [], [], []
        for frase in frases:
            for _ in range(args.tomas):
                t = time.perf_counter()
                a = modelo.generate(text=frase, language="Spanish", voice_clone_prompt=prompt,
                                    **kw)[0]
                tiempos.append(time.perf_counter() - t)
                a = np.asarray(a, dtype=np.float32).squeeze()
                if a.size < 2400:           # menos de 0,1 s: OmniVoice no genero nada
                    fallos += 1
                    peores.append((0.0, frase, "(audio vacío)"))
                    continue
                ppm.append(len(frase.split()) / (len(a) / 24000) * 60)
                oido = asr({"raw": librosa.resample(a, orig_sr=24000, target_sr=16000),
                            "sampling_rate": 16000},
                           generate_kwargs={"language": "es"})["text"].strip()
                c = cobertura(frase, oido)
                if c < 0.85:
                    fallos += 1
                    peores.append((c, frase, oido))
        n = len(frases) * args.tomas
        resumen[nombre] = (fallos, n, np.median(tiempos), np.median(ppm))
        print(f"\n## {nombre}: {fallos}/{n} tomas fallidas ({fallos / n:.0%}), "
              f"síntesis {np.median(tiempos):.2f}s, {np.median(ppm):.0f} palabras/min", flush=True)
        for c, frase, oido in sorted(peores)[:6]:
            print(f"   {c:.2f}  dijo «{frase}»\n         se oyó «{oido}»")

    print("\n## resumen")
    for nombre, (f, n, t, p) in resumen.items():
        print(f"{nombre:28} {f:3}/{n} fallidas ({f / n:4.0%})  síntesis {t:.2f}s  {p:.0f} pal/min")


if __name__ == "__main__":
    main()
