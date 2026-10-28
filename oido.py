"""El oido: detecta cuando habla quien llama y lo transcribe.

Recibe audio en bloques (del micro o del telefono) y emite eventos del
contrato del conductor:

    {"evento": "habla", "texto": "", "final": False}   empezo a hablar
    {"evento": "habla", "texto": str, "final": True}   termino, y esto dijo

El que empezo a hablar sirve para callar a la recepcionista si la
interrumpen. El final, para contestarle.

Silero decide que es voz. El turno se da por terminado tras FIN_MS de
silencio; esperar menos parte a la gente que dicta un numero por grupos.

Para no sumar Whisper a esa espera se transcribe de forma especulativa: en
cuanto hay ESPECULAR_MS de silencio se transcribe todo lo dicho hasta ahi.
Si la persona no sigue, cuando se cumple FIN_MS el texto ya esta o le falta
poco.

Si sigue hablando, lo ya transcrito no se tira: queda como prefijo, y la
siguiente vez solo se transcribe lo nuevo, pasandole a Whisper el prefijo
como contexto (prompt_ids). Medido: Whisper no repite el contexto en la
salida, y el tramo final sale en 0,39 s en vez de 0,61 s el entero. Sin el
contexto se perderia lo que hace acertar los numeros dictados por grupos.
"""

import queue
import re
import threading
import time
from collections import deque

import numpy as np
import torch

RATE = 16000
VENTANA = 512                   # lo que pide Silero a 16 kHz: 32 ms
MS = VENTANA * 1000 // RATE
PREFIJO_MIN = 2000 // MS        # por debajo de 2 s de prefijo no compensa
CONTEXTO = 200                  # caracteres del prefijo que se le pasan a Whisper

# Lo que Whisper "oye" en el ruido o en audio muy corto, a veces repetido en
# bucle: "¡Gracias por ver el video!" cuarenta veces tras un "Anote, por favor".
FANTASMAS = re.compile(r"^\W*(gracias por ver( el v[ií]deo| el video)?|subt[ií]tulos.*|"
                       r"amara\.org.*|suscr[ií]bete.*|m[uú]sica|\.+)\W*$", re.IGNORECASE)


def limpiar(texto):
    """Quita frases fantasma y repeticiones en bucle. Vacio = no se dijo nada."""
    # Se corta solo donde al signo le sigue un espacio: "3.18" no es un final.
    frases = re.split(r"(?<=[.!?])\s+", texto)
    limpias = []
    for f in (f.strip() for f in frases):
        if not f or FANTASMAS.match(f):
            continue
        if limpias and f.lower() == limpias[-1].lower():
            continue
        limpias.append(f)
    if len(limpias) == 1 and re.fullmatch(r"\W*gracias\W*", limpias[0], re.IGNORECASE):
        return ""       # un "gracias" suelto y nada mas: el fantasma mas comun
    return " ".join(limpias)


class Oido:
    def __init__(self, cfg, asr, emitir):
        o = cfg.get("oido", {})
        self.umbral = o.get("umbral", 0.5)
        self.inicio = o.get("inicio_ms", 96) // MS          # voz para empezar a escuchar
        self.interrumpir = o.get("interrumpir_ms", 400) // MS  # voz para cortarla a ella
        self.especular = o.get("especular_ms", 250) // MS
        self.fin = o.get("fin_ms", 600) // MS
        self.minimo = o.get("minimo_voz_ms", 250) // MS     # menos que esto es ruido
        self.previo = o.get("previo_ms", 300) // MS         # audio de antes de detectar voz
        self.asr, self.emitir = asr, emitir
        from silero_vad import load_silero_vad
        self.vad = load_silero_vad()

        self.ella_habla = False     # lo pone el adaptador: cambia el umbral para cortarla
        self.registro = []          # por intervencion: tiempos y texto, para medir
        self._resto = np.zeros(0, dtype=np.float32)
        self._antes = deque(maxlen=self.previo)
        self._seguidas = 0
        self._en_habla = False
        self._segmento, self._voz, self._silencio = [], 0, 0
        self._version = 0           # sube cada vez que la intervencion crece
        self._especulada = -1       # version ya mandada a transcribir
        self._listo = None          # (version, texto) del ultimo trabajo hecho
        self._esperando = {}        # version cerrada -> su entrada, esperando texto
        self._intervencion = 0
        self._prefijo = None        # (ventanas, texto) ya transcrito de esta intervencion
        self._cerrojo = threading.Lock()
        self._trabajos = queue.Queue()
        threading.Thread(target=self._transcriptor, daemon=True).start()

    # --- audio entrante -------------------------------------------------

    def alimentar(self, bloque):
        """Audio float32 mono a 16 kHz, en bloques de cualquier tamaño."""
        datos = np.concatenate([self._resto, np.asarray(bloque, dtype=np.float32)])
        n = len(datos) // VENTANA * VENTANA
        for i in range(0, n, VENTANA):
            self._ventana(datos[i:i + VENTANA])
        self._resto = datos[n:]

    def _ventana(self, v):
        p = float(self.vad(torch.from_numpy(v), RATE))
        if not self._en_habla:
            self._antes.append(v)
            self._seguidas = self._seguidas + 1 if p >= self.umbral else 0
            if self._seguidas >= (self.interrumpir if self.ella_habla else self.inicio):
                self._empieza()
            return

        self._segmento.append(v)
        # Histeresis: una vez hablando, cuesta mas dar algo por silencio.
        if p >= self.umbral - 0.15:
            self._voz += 1
            if self._silencio:
                self._silencio = 0
                self._version += 1          # lo especulado ya no vale
            return
        self._silencio += 1
        if self._silencio == self.especular and self._voz >= self.minimo:
            self._mandar(self._version)
        if self._silencio >= self.fin:
            self._termina()

    def _empieza(self):
        self._en_habla = True
        self._segmento = list(self._antes)
        self._antes.clear()
        self._voz, self._silencio, self._seguidas = self._seguidas, 0, 0
        self._version += 1
        self._intervencion += 1
        self._prefijo = None
        self._t_inicio = time.perf_counter()
        self.emitir({"t": time.time(), "evento": "habla", "texto": "", "final": False})

    def _termina(self):
        self._en_habla = False
        t_fin_voz = time.perf_counter() - self._silencio * MS / 1000
        if self._voz < self.minimo:
            return                          # un golpe o una tos: no es un turno
        entrada = {"inicio": self._t_inicio, "fin_voz": t_fin_voz,
                   "cierre": time.perf_counter(), "voz_s": self._voz * MS / 1000}
        self.registro.append(entrada)
        version = self._version
        with self._cerrojo:
            if self._listo and self._listo[0] == version:
                self._entregar(entrada, self._listo[1])
                return
            self._esperando[version] = entrada
        if self._especulada != version:
            self._mandar(version)

    # --- transcripcion --------------------------------------------------

    def _mandar(self, version):
        self._especulada = version
        n = len(self._segmento)
        with self._cerrojo:
            base = self._prefijo
        if not base or base[0] < PREFIJO_MIN or base[0] >= n:
            base = (0, "")
        self._trabajos.put((version, self._intervencion, n,
                            np.concatenate(self._segmento[base[0]:]), base[1]))

    def _transcriptor(self):
        while True:
            version, intervencion, n, audio, previo = self._trabajos.get()
            with self._cerrojo:
                vigente = version == self._version or version in self._esperando
            if not vigente:
                continue                    # la persona siguio hablando
            t = time.perf_counter()
            # Tope de tokens segun lo que duro: quien habla 3 s no dice 200
            # palabras, y sin tope un bucle de Whisper se lleva segundos.
            ajustes = {"language": "es", "task": "transcribe",
                       "max_new_tokens": int(len(audio) / RATE * 8) + 16}
            if previo:
                ajustes["prompt_ids"] = self.asr.tokenizer.get_prompt_ids(
                    previo[-CONTEXTO:], return_tensors="pt").to(self.asr.device)
            nuevo = self.asr({"raw": audio, "sampling_rate": RATE},
                             generate_kwargs=ajustes)["text"].strip()
            texto = f"{previo} {nuevo}".strip()
            duracion = time.perf_counter() - t
            with self._cerrojo:
                if intervencion == self._intervencion and (
                        not self._prefijo or n > self._prefijo[0]):
                    self._prefijo = (n, texto)
                self._listo = (version, texto)
                pendiente = self._esperando.pop(version, None)
            if pendiente:
                pendiente["whisper_s"] = duracion
                pendiente["con_prefijo"] = bool(previo)
                self._entregar(pendiente, texto)

    def _entregar(self, entrada, texto):
        entrada["entregado"] = time.perf_counter()
        entrada["crudo"] = texto
        texto = entrada["texto"] = limpiar(texto)
        if not texto:
            entrada["descartado"] = True
            return
        self.emitir({"t": time.time(), "evento": "habla", "texto": texto, "final": True})
