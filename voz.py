"""La voz de la recepcionista: OmniVoice cargado una vez y una cola de frases.

Se ejecuta con el python de OmniVoice, que es el que tiene el modelo:

    ~/omni_voice_project/venv/bin/python ...

Dos hilos. Uno sintetiza frases en cuanto llegan; otro reproduce. Asi la
segunda frase se genera mientras suena la primera, y como OmniVoice va 4-8
veces mas rapido que el tiempo real, la cola no se vacia a media respuesta.

Callarse es cambiar de "epoca": todo lo que esta en cola, sintetizandose o a
medio sonar pertenece a la epoca anterior y se descarta. El altavoz se vacia
en el acto, y el hilo que reproduce mira la epoca en cada trozo de 40 ms.
"""

import queue
import threading
import time
from pathlib import Path

import numpy as np
import torch

RAIZ = Path(__file__).resolve().parent
RATE = 24000
TROZO = int(RATE * 0.04)      # lo que se manda al altavoz de cada vez
PAUSA = 0.10                  # silencio entre frases de una misma respuesta


class Voz:
    """OmniVoice con la voz de la recepcionista ya clonada."""

    def __init__(self, cfg):
        from omnivoice import OmniVoice
        ajustes = cfg.get("voz", {})
        prompt = RAIZ / ajustes.get("prompt", "voces/recepcionista.pt")
        if not prompt.is_file():
            raise SystemExit(f"Falta {prompt}: corre antes preparar_voz.py")
        self.modelo = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda",
                                                dtype=torch.float16)
        self.prompt = torch.load(prompt, weights_only=False)
        self.pasos = ajustes.get("num_step", 16)
        self.velocidad = ajustes.get("velocidad", 1.0)
        self._cerrojo = threading.Lock()      # una GPU: una sintesis a la vez
        self._fijas = {}
        self.sintetizar("Hola.")              # la primera siempre tarda mas

    def sintetizar(self, texto):
        if texto in self._fijas:
            return self._fijas[texto]
        with self._cerrojo:
            audio = self.modelo.generate(text=texto, language="Spanish",
                                         voice_clone_prompt=self.prompt,
                                         num_step=self.pasos, speed=self.velocidad)[0]
        return np.asarray(audio, dtype=np.float32).squeeze()

    def fijar(self, *textos):
        """Deja sintetizadas de antemano frases que no pueden esperar: el saludo."""
        for t in textos:
            if t not in self._fijas:
                self._fijas[t] = self.sintetizar(t)


class Hablante:
    """Cola de frases hacia una salida con escribir(audio), vaciar() y esperar().

    al_terminar se llama cuando lo encolado termino de sonar entero, y no si
    se corto con callar(). linea guarda, por frase, cuando se encolo, cuando
    estuvo sintetizada y cuando empezo y acabo de sonar, para medir huecos.
    """

    def __init__(self, voz, salida, al_terminar=None):
        self.voz, self.salida, self.al_terminar = voz, salida, al_terminar
        self.linea = []
        self._epoca = 0
        self._pendientes = 0
        self._cerrojo = threading.Lock()
        self._por_sintetizar = queue.Queue()
        self._por_sonar = queue.Queue()
        for destino in (self._sintetizador, self._reproductor):
            threading.Thread(target=destino, daemon=True).start()

    def decir(self, texto):
        with self._cerrojo:
            self._pendientes += 1
            marca = {"texto": texto, "encolada": time.perf_counter()}
            self.linea.append(marca)
            self._por_sintetizar.put((self._epoca, texto, marca))

    def callar(self):
        with self._cerrojo:
            self._epoca += 1
            self._pendientes = 0
        for cola in (self._por_sintetizar, self._por_sonar):
            while True:
                try:
                    cola.get_nowait()
                except queue.Empty:
                    break
        self.salida.vaciar()

    @property
    def hablando(self):
        return self._pendientes > 0

    def _vigente(self, epoca):
        return epoca == self._epoca

    def _sintetizador(self):
        while True:
            epoca, texto, marca = self._por_sintetizar.get()
            if not self._vigente(epoca):
                continue
            audio = self.voz.sintetizar(texto)
            marca["sintetizada"] = time.perf_counter()
            marca["duracion"] = len(audio) / RATE
            marca["audio"] = audio
            if self._vigente(epoca):
                self._por_sonar.put((epoca, audio, marca))

    def _reproductor(self):
        while True:
            epoca, audio, marca = self._por_sonar.get()
            if not self._vigente(epoca):
                continue
            audio = np.concatenate([audio, np.zeros(int(RATE * PAUSA), dtype=np.float32)])
            marca["empieza"] = time.perf_counter()
            for i in range(0, len(audio), TROZO):
                if not self._vigente(epoca):
                    break
                self.salida.escribir(audio[i:i + TROZO])
            else:
                marca["termina"] = time.perf_counter()
                with self._cerrojo:
                    if not self._vigente(epoca):
                        continue
                    self._pendientes -= 1
                    ultima = self._pendientes == 0
                if ultima:
                    self.salida.esperar()
                    if self.al_terminar and self._vigente(epoca):
                        self.al_terminar()


class Archivo:
    """Salida que no suena: acumula el audio a ritmo real, para medir y escuchar.

    Los ratos en que nadie escribe quedan como silencio, asi que el resultado
    se oye con las esperas de verdad, que es lo que hay que juzgar.
    """

    def __init__(self):
        self.partes = []
        self._reloj = None
        self._cerrojo = threading.Lock()

    def escribir(self, audio):
        with self._cerrojo:
            ahora = time.perf_counter()
            if self._reloj is not None and ahora > self._reloj + 0.02:
                self.partes.append(np.zeros(int((ahora - self._reloj) * RATE), dtype=np.float32))
            self.partes.append(np.asarray(audio, dtype=np.float32).copy())
            self._reloj = max(ahora, self._reloj or 0) + len(audio) / RATE
        time.sleep(max(0.0, self._reloj - time.perf_counter()))

    def vaciar(self):
        pass

    def esperar(self):
        pass

    def audio(self):
        return np.concatenate(self.partes) if self.partes else np.zeros(0, dtype=np.float32)
