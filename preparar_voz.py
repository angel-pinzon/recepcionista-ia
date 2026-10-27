"""Prepara la voz de la recepcionista a partir de una muestra, una sola vez.

    ~/omni_voice_project/venv/bin/python preparar_voz.py
    ~/omni_voice_project/venv/bin/python preparar_voz.py --muestra otra.wav --max 9

Recorta la muestra en el ultimo final de frase antes de --max segundos,
transcribe el recorte con Whisper y guarda en voces/:

    recepcionista.wav   el recorte, mono 24 kHz
    recepcionista.txt   su transcripcion
    recepcionista.pt    el prompt de clonacion de OmniVoice, ya calculado

Por que recortar: la clonacion arrastra la longitud de la referencia en cada
frase. Medido con la voz de Gemini de la tertulia: con la muestra entera (16 s)
cada frase tardaba 1,6-2,2 s y la VRAM llegaba a 7,1 GB; recortada a 8,8 s,
1,0 s y 2,2 GB.

Por que transcribir y no escribir el texto a mano: OmniVoice deduce el ritmo
de la voz dividiendo palabras entre duracion. Un texto que no cuadre con lo
que de verdad suena lo atropella todo.
"""

import argparse
import logging
import re
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np
import soundfile as sf
import torch
import librosa

RAIZ = Path(__file__).resolve().parent
MUESTRA = Path.home() / "tertulia-artificial/voces/gemini.wav"
COLA = 0.15          # silencio que se deja tras la ultima palabra, con fundido


def recortar(muestra, maximo):
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                   torch_dtype=torch.float16, device="cuda")
    audio, _ = librosa.load(str(muestra), sr=24000, mono=True)
    r = asr({"raw": librosa.resample(audio, orig_sr=24000, target_sr=16000),
             "sampling_rate": 16000},
            return_timestamps="word", generate_kwargs={"language": "spanish"})
    palabras = r["chunks"]
    corte = None
    for i, p in enumerate(palabras):
        fin = p["timestamp"][1]
        if fin is None or fin > maximo:
            break
        if re.search(r"[.!?]$", p["text"].strip()):
            corte = i
    if corte is None:
        raise SystemExit(f"No hay ningun final de frase antes de {maximo} s")
    fin = palabras[corte]["timestamp"][1]
    siguiente = palabras[corte + 1]["timestamp"][0] if corte + 1 < len(palabras) else fin + COLA
    hasta = min(fin + COLA, siguiente)
    recorte = audio[:int(hasta * 24000)].copy()
    n = int(min(COLA, hasta - fin) * 24000)
    if n:
        recorte[-n:] *= np.linspace(1, 0, n, dtype=np.float32)
    texto = "".join(p["text"] for p in palabras[:corte + 1]).strip()
    del asr
    torch.cuda.empty_cache()
    return recorte, texto


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--muestra", default=str(MUESTRA))
    ap.add_argument("--max", type=float, default=9.0, help="segundos maximos del recorte")
    args = ap.parse_args()

    destino = RAIZ / "voces"
    destino.mkdir(exist_ok=True)
    recorte, texto = recortar(args.muestra, args.max)
    wav = destino / "recepcionista.wav"
    sf.write(wav, recorte, 24000)
    (destino / "recepcionista.txt").write_text(texto + "\n", encoding="utf-8")
    dur = len(recorte) / 24000
    print(f"recorte: {dur:.2f} s, {len(texto.split())} palabras "
          f"({len(texto.split()) / dur * 60:.0f} palabras/minuto)\n  «{texto}»")

    from omnivoice import OmniVoice
    modelo = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda",
                                       dtype=torch.float16)
    prompt = modelo.create_voice_clone_prompt(str(wav), ref_text=texto)
    torch.save(prompt, destino / "recepcionista.pt")
    print(f"prompt de clonacion en {destino / 'recepcionista.pt'}")


if __name__ == "__main__":
    main()
