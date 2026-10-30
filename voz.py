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
import re
import threading
import time
from pathlib import Path

import numpy as np
import torch

RAIZ = Path(__file__).resolve().parent
RATE = 24000
TROZO = int(RATE * 0.04)      # lo que se manda al altavoz de cada vez
PAUSA = 0.10                  # silencio entre frases de una misma respuesta


COLETILLA = " ¿Eh?"


def quitar_coletilla(audio):
    """Corta en el ultimo silencio (>= 90 ms) antes de la coletilla.

    Desde el final: silencio de cola, la coletilla, y el hueco que la separa
    de la frase. Si ese hueco no existe, devuelve None y no se toca nada:
    cortar a ciegas se llevaria la ultima palabra de verdad.
    """
    ventana = int(RATE * 0.01)
    env = np.array([np.abs(audio[i:i + ventana]).max()
                    for i in range(0, len(audio) - ventana + 1, ventana)])
    callado = env < 0.02
    i = len(env) - 1
    while i > 0 and callado[i]:
        i -= 1
    while i > 0 and not callado[i]:
        i -= 1
    fin_hueco = i
    while i > 0 and callado[i]:
        i -= 1
    if fin_hueco - i < 9:
        return None
    corte = (i + 1) * ventana + int(RATE * 0.06)
    salida = audio[:corte].copy()
    n = int(RATE * 0.04)
    salida[-n:] *= np.linspace(1, 0, n, dtype=np.float32)
    return salida


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
        self.repeticiones = 0                 # tomas rehechas por implausibles
        self.coletillas_fallidas = 0          # preguntas sin hueco antes de la coletilla
        self.sintetizar("Hola.")              # la primera siempre tarda mas

    def sintetizar(self, texto):
        if texto in self._fijas:
            return self._fijas[texto]
        audio = self._generar(texto)
        if not self._plausible(texto, audio):
            # OmniVoice a veces devuelve audio vacio o desbocado. Medido: con
            # velocidad 0,9 devolvio una frase sin una sola muestra. Una
            # segunda toma casi siempre sale bien, y medirlo es aritmetica.
            self.repeticiones += 1
            audio = self._generar(texto)
        return audio

    def _generar(self, texto):
        """Sintetiza sin que la ultima palabra acabe cortada en seco.

        OmniVoice se come el final de la ultima palabra (el pipeline de TikTok
        lo tapa con una coletilla audible). Medido con la vara del pipeline
        (energia final > 250): tal cual, 11 de 30 frases acababan en seco.
        - Afirmaciones: el punto final pasa a puntos suspensivos -> 1 de 30.
        - Preguntas: "?..." no basta (4 de 24 frente a 5), y quitar la
          interrogacion arriesga la entonacion. Se les pega una coletilla
          ("¿Eh?") para que OmniVoice se coma esa, y se recorta en el
          silencio que queda antes de ella -> 0 de 30.
        """
        pregunta = texto.rstrip().endswith("?")
        a_decir = texto + COLETILLA if pregunta else re.sub(r"[.!]+$", "…", texto.rstrip())
        with self._cerrojo:
            audio = self.modelo.generate(text=a_decir, language="Spanish",
                                         voice_clone_prompt=self.prompt,
                                         num_step=self.pasos, speed=self.velocidad)[0]
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if pregunta:
            sin_coletilla = quitar_coletilla(audio)
            if sin_coletilla is not None:
                return sin_coletilla
            self.coletillas_fallidas += 1      # sin hueco donde cortar: va entera
        return audio

    @staticmethod
    def _plausible(texto, audio):
        """Entre 60 y 300 palabras por minuto: fuera de eso algo se rompio."""
        palabras = max(len(texto.split()), 1)
        segundos = len(audio) / RATE
        return segundos > 0.2 and 60 <= palabras / segundos * 60 <= 300

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

    def cerrar(self):
        """Para los dos hilos. Lo pendiente se descarta."""
        self.callar()
        self._por_sintetizar.put(None)
        self._por_sonar.put(None)

    def _sintetizador(self):
        while True:
            item = self._por_sintetizar.get()
            if item is None:
                return
            epoca, texto, marca = item
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
            item = self._por_sonar.get()
            if item is None:
                return
            epoca, audio, marca = item
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
    """Salida que no suena: guarda cada trozo con su hora, a ritmo real.

    Al mezclar, los ratos sin audio quedan como silencio, asi que se oye con
    las esperas de verdad. vaciar() recorta lo que aun no habia "sonado", como
    hace el altavoz. agregar() mete audio ajeno, la voz de quien llama, para
    oir la conversacion entera.
    """

    def __init__(self):
        self.segmentos = []
        self._reloj = None
        self._cerrojo = threading.Lock()

    def escribir(self, audio):
        with self._cerrojo:
            inicio = max(time.perf_counter(), self._reloj or 0)
            self.segmentos.append((inicio, np.asarray(audio, dtype=np.float32).copy()))
            self._reloj = inicio + len(audio) / RATE
        time.sleep(max(0.0, self._reloj - time.perf_counter()))

    def vaciar(self):
        with self._cerrojo:
            ahora = time.perf_counter()
            quedan = []
            for t, a in self.segmentos:
                if t >= ahora:
                    continue
                quedan.append((t, a[:int((ahora - t) * RATE)]))
            self.segmentos = quedan
            self._reloj = ahora

    def esperar(self):
        pass

    def agregar(self, t, audio):
        with self._cerrojo:
            self.segmentos.append((t, np.asarray(audio, dtype=np.float32).copy()))

    def audio(self):
        if not self.segmentos:
            return np.zeros(0, dtype=np.float32)
        t0 = min(t for t, _ in self.segmentos)
        fin = max(int((t - t0) * RATE) + len(a) for t, a in self.segmentos)
        mezcla = np.zeros(fin, dtype=np.float32)
        for t, a in self.segmentos:
            i = int((t - t0) * RATE)
            mezcla[i:i + len(a)] += a
        return np.clip(mezcla, -1, 1)
