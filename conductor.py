"""El conductor de la llamada: recibe eventos y emite ordenes.

No sabe si la llamada entra por el micro del PC o por un telefono: eso es
cosa de los adaptadores. Si algo de aqui empieza a saber de audio, sockets o
dispositivos, es un bug aunque funcione.

ENTRA   {"t": float, "evento": "conecta", "llamante": str | None}
        {"t": float, "evento": "habla",   "texto": str, "final": bool}
        {"t": float, "evento": "fin_voz"}      # el adaptador termino de sonar
        {"t": float, "evento": "cuelga"}

SALE    {"orden": "decir",  "texto": str}
        {"orden": "callar"}
        {"orden": "colgar"}
"""

import time

import filtro
from cerebro import Turno, saludo


class Conductor:
    def __init__(self, cfg, turno=Turno):
        self.cfg = cfg
        self.dueno = cfg["recepcionista"]["dueno"]
        self._turno = turno            # inyectable, para probar sin red
        self.mensajes = []             # lo que ve el modelo
        self.registro = []             # lo que se guarda de la llamada
        self.llamante = None
        self.hablando = False
        self.terminada = False
        self._cortar = False

    def recibir(self, evento):
        """Generador de ordenes para un evento."""
        tipo = evento["evento"]
        if tipo == "conecta":
            yield from self._conecta(evento)
        elif tipo == "habla":
            if not evento.get("final"):
                if self.hablando:
                    self._interrumpir()
                    yield {"orden": "callar"}
                return
            yield from self._responder(evento)
        elif tipo == "fin_voz":
            self.hablando = False
        elif tipo == "cuelga":
            self.terminada = True
            self._apuntar("sistema", "(quien llama colgó)")

    def _conecta(self, evento):
        self.llamante = evento.get("llamante")
        texto = saludo(self.cfg)
        # Gemini quiere que la conversacion empiece por el usuario.
        self.mensajes = [{"role": "user", "content": "(Entra la llamada.)"},
                         {"role": "assistant", "content": texto}]
        self._apuntar("recepcionista", texto, fijo=True)
        self.hablando = True
        yield {"orden": "decir", "texto": texto}

    def _responder(self, evento):
        texto = evento["texto"].strip()
        if not texto or self.terminada:
            return
        self._apuntar("llamante", texto)
        self.mensajes.append({"role": "user", "content": texto})

        turno = self._turno(self.cfg, self.mensajes, self.llamante)
        self._cortar = False
        # Pensando cuenta como hablando: si la interrumpen mientras el modelo
        # escribe, tiene que poder cortarse antes de decir nada.
        self.hablando = True
        dichas, cambios = [], []
        for frase in turno:
            if self._cortar:
                break
            dicha, motivo = filtro.revisar(frase, self.dueno)
            if motivo:
                cambios.append(motivo)
            dichas.append(dicha)
            self.hablando = True
            yield {"orden": "decir", "texto": dicha}

        dicho = " ".join(dichas)
        if self._cortar:
            dicho += " (la interrumpieron aquí)"
        self.mensajes.append({"role": "assistant", "content": dicho or "..."})
        self._apuntar("recepcionista", dicho, primera=turno.primera,
                      proveedor=turno.proveedor, fallo=turno.fallo, filtro=cambios or None)
        if not dichas:
            self.hablando = False
        if turno.fin and not self._cortar:
            self.terminada = True
            yield {"orden": "colgar"}

    def _interrumpir(self):
        self._cortar = True
        self.hablando = False
        if self.registro and self.registro[-1]["quien"] == "recepcionista":
            self.registro[-1]["interrumpida"] = True

    def _apuntar(self, quien, texto, **datos):
        self.registro.append({"t": round(time.time(), 2), "quien": quien, "texto": texto,
                              **{k: v for k, v in datos.items() if v is not None}})
