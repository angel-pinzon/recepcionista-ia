"""Una llamada en curso: oido, conductor y voz, sobre cualquier audio.

El adaptador (micro del PC, o el telefono en la F2) solo hace dos cosas:
darle audio de quien llama con alimentar(), a 16 kHz, y pasar una salida con
escribir/vaciar/esperar por donde suene la recepcionista. Todo lo demas es
igual venga de donde venga la llamada.

Hay tres hilos que importan:
- el del adaptador, que alimenta el oido bloque a bloque;
- el del conductor, que atiende los eventos en orden (contestar bloquea
  mientras el modelo escribe, por eso va aparte);
- los de la voz, que sintetizan y reproducen.

Cortar a la recepcionista no espera a ninguno: cuando el oido detecta que
alguien empieza a hablar se calla la voz en ese mismo hilo, y el conductor
deja de mandar frases en cuanto mira su marca.
"""

import json
import queue
import threading
import time
from datetime import datetime

from cerebro import saludo
from conductor import Conductor
from oido import Oido
from util import llamadas_dir
from voz import Hablante


class Llamada:
    def __init__(self, cfg, voz, asr, salida, llamante=None, interrumpible=True):
        self.cfg = cfg
        self.conductor = Conductor(cfg)
        self.hablante = Hablante(voz, salida, al_terminar=self._fin_voz)
        self.oido = Oido(cfg, asr, self._oido)
        self.interrumpible = interrumpible
        self.terminada = False
        self.cortes = []                # (cuando empezo la voz ajena, cuando se callo)
        self._eventos = queue.Queue()
        voz.fijar(saludo(cfg))
        threading.Thread(target=self._atender, daemon=True).start()
        self._eventos.put({"t": time.time(), "evento": "conecta", "llamante": llamante})

    @property
    def ocupada(self):
        """La recepcionista esta pensando o hablando."""
        return self.conductor.hablando or self.hablante.hablando

    def alimentar(self, bloque):
        """Audio de quien llama, float32 mono a 16 kHz."""
        if self.terminada:
            return
        if not self.interrumpible and self.ocupada:
            # Sin auriculares el micro oye a la propia recepcionista. Mientras
            # ella habla no se escucha: no se la puede cortar, pero tampoco se
            # contesta a si misma.
            return
        self.oido.ella_habla = self.ocupada
        self.oido.alimentar(bloque)

    # --- eventos ---------------------------------------------------------

    def _oido(self, evento):
        if not evento["final"]:
            if self.ocupada and self.interrumpible:
                t = time.perf_counter()
                for orden in self.conductor.recibir(evento):
                    if orden["orden"] == "callar":
                        self.hablante.callar()
                        self.cortes.append({"detectado": t, "callada": time.perf_counter()})
            return
        self._eventos.put(evento)

    def _fin_voz(self):
        self._eventos.put({"t": time.time(), "evento": "fin_voz"})

    def _atender(self):
        while True:
            evento = self._eventos.get()
            for orden in self.conductor.recibir(evento):
                if orden["orden"] == "decir":
                    self.hablante.decir(orden["texto"])
                elif orden["orden"] == "callar":
                    self.hablante.callar()
                elif orden["orden"] == "colgar":
                    threading.Thread(target=self._colgar, daemon=True).start()
            if evento["evento"] == "cuelga":
                self.terminada = True

    def _colgar(self):
        while self.hablante.hablando:
            time.sleep(0.05)
        self.terminada = True

    def colgar(self):
        """Quien llama colgo, o se corta desde fuera."""
        self._eventos.put({"t": time.time(), "evento": "cuelga"})

    # --- medicion y registro --------------------------------------------

    def silencios(self):
        """Por intervencion: desde que la voz ajena termino hasta que ella suena."""
        salida = []
        for r in self.oido.registro:
            if "entregado" not in r or r.get("descartado"):
                continue
            siguiente = next((m for m in self.hablante.linea
                              if m["encolada"] >= r["entregado"] and "empieza" in m), None)
            if siguiente:
                salida.append({"texto": r["texto"],
                               "silencio": siguiente["empieza"] - r["fin_voz"],
                               "oido": r["entregado"] - r["fin_voz"],
                               "cerebro_y_voz": siguiente["empieza"] - r["entregado"]})
        return salida

    def guardar(self, nombre):
        ruta = llamadas_dir() / f"{datetime.now():%Y%m%d_%H%M%S}_{nombre}.json"
        datos = {"llamante": self.conductor.llamante, "registro": self.conductor.registro,
                 "silencios": self.silencios(),
                 "cortes": [c["callada"] - c["detectado"] for c in self.cortes]}
        ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
        return ruta
