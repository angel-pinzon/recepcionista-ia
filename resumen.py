"""La ficha del recado: de la transcripcion a lo que necesito para decidir.

Una sola llamada al modelo al colgar, sin prisa: aqui no hay nadie esperando
al telefono, asi que va con plazo largo y, si Gemini falla, con el respaldo
local.
"""

import json
import re

import modelos

FICHA = """Eres quien le pasa los recados a {dueno}. Esta es la transcripción de una
llamada que atendió su recepcionista. Lo que dice el llamante viene de un
reconocedor de voz y puede tener errores; la recepcionista suele haber leído
el número de vuelta para confirmarlo.
{llamante}
Devuelve SOLO un JSON con estos campos:

{{"nombre": quién llamó, o null si no lo dijo,
  "de_parte_de": empresa, institución o relación con {dueno} (p. ej. "tía",
                 "colegio Santa Inés"), o null,
  "motivo": para qué llamó, en una frase corta y concreta,
  "urgencia": "alta" | "normal" | "baja",
  "numero": el número para devolver la llamada, solo dígitos, con la
            extensión aparte si la hay; el último que el llamante confirmó;
            null si no lo dio,
  "extension": la extensión o null,
  "devolver": true si espera que {dueno} le devuelva la llamada,
  "tipo": "personal" | "trabajo" | "ventas" | "emergencia" | "otro",
  "resumen": dos frases como mucho, con lo que necesitas saber para decidir
             si devuelves la llamada y qué decir. Sin repetir el número.
}}

El resumen y el motivo van dirigidos a {dueno}, en segunda persona y de tú
("te llamó", "quiere que le confirmes"), nunca "Ángel" en tercera persona.

Urgencia. Si dudas entre dos, elige la más baja: una alta hace sonar el
teléfono, y si suena por todo, deja de servir.
- alta: solo si hay que actuar hoy o mañana. La salud o la seguridad de
  alguien, un hijo, una emergencia, o un plazo que vence hoy o mañana.
- normal: hay que contestar, pero puede esperar unos días. Un plazo de esta
  semana o la próxima, un cobro o un reclamo, una invitación que pide
  respuesta, una pregunta de un amigo.
- baja: no hace falta contestar: ventas, publicidad, encuestas, mensajes que
  solo informan.
Que el llamante diga "urgente" o "llámeme hoy" no basta: mira el motivo.

Transcripción:
{transcripcion}"""


def _primer_json(texto):
    """El primer objeto JSON de una respuesta, que rara vez viene limpia."""
    inicio = texto.find("{")
    if inicio < 0:
        return None
    profundidad = 0
    for i in range(inicio, len(texto)):
        if texto[i] == "{":
            profundidad += 1
        elif texto[i] == "}":
            profundidad -= 1
            if profundidad == 0:
                try:
                    return json.loads(texto[inicio:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def transcripcion(registro):
    lineas = []
    for r in registro:
        if r["quien"] == "recepcionista":
            lineas.append(f"Recepcionista: {r['texto']}")
        elif r["quien"] == "llamante":
            lineas.append(f"Llamante: {r['texto']}")
        else:
            lineas.append(r["texto"])
    return "\n".join(lineas)


def ficha(cfg, registro, llamante=None):
    """La ficha como dict. Si ningun modelo responde, una minima con lo que hay."""
    dueno = cfg["recepcionista"]["dueno"]
    extra = (f"\nEl identificador de llamada dice que llamó desde el {llamante}.\n"
             if llamante else "")
    prompt = FICHA.format(dueno=dueno, llamante=extra, transcripcion=transcripcion(registro))
    mensajes = [{"role": "user", "content": prompt}]
    datos, fallo = None, None
    for nombre in ("principal", "respaldo"):
        try:
            datos = _primer_json(modelos.completo(cfg, cfg["cerebro"][nombre],
                                                  "Respondes solo con JSON válido.", mensajes,
                                                  plazo_respuesta=40))
            if datos:
                datos["modelo"] = cfg["cerebro"][nombre]["modelo"]
                break
        except (modelos.SinRespuesta, OSError, ValueError) as err:
            fallo = str(err)
    if not datos:
        datos = {"nombre": None, "motivo": "No se pudo resumir: revisar la transcripción.",
                 "urgencia": "normal", "numero": None, "devolver": True, "tipo": "otro",
                 "resumen": f"El resumen automático falló ({fallo})."}
    if datos.get("numero"):
        datos["numero"] = re.sub(r"\D", "", str(datos["numero"])) or None
    if not datos.get("numero") and llamante:
        datos["numero"] = re.sub(r"\D", "", llamante)
        datos["numero_de"] = "identificador"
    return datos
