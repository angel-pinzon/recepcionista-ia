"""Lo que no puede llegar a la voz, y lo que hay que dejar listo para ella.

Se aplica frase a frase, sobre texto, antes del TTS: es el ultimo punto donde
algo se puede parar. El prompt ya pide no prometer nada; esto es para cuando
el modelo lo hace igual.
"""

import re

UNIDADES = "cero uno dos tres cuatro cinco seis siete ocho nueve".split()

# Promesas en nombre del dueno. "Le confirmo" no esta: es como se lee de
# vuelta un numero dictado, y bloquearlo romperia la confirmacion.
PROMESAS = [
    r"\b(le\s+)?(garantizo|prometo)\b",
    r"\b(va|van)\s+a\s+(pagar|consignar|transferir|devolver\s+(el|su)\s+dinero)\b",
    r"\b(le\s+)?(paga|pagará|consigna|consignará|transfiere|transferirá)\b",
    r"\b(lo|la|le)\s+(llama|llamará|va\s+a\s+llamar)\s+(hoy|mañana|esta\s+(tarde|noche|semana)|"
    r"en\s+\S+\s+(minutos?|horas?)|a\s+las)\b",
    r"\b(queda|quedó|está)\s+(agendad|confirmad|programad|reservad)[oa]s?\b",
]
PROMESA = re.compile("|".join(PROMESAS), re.IGNORECASE)


def _digitos(m):
    """Un numero en cifras, dicho digito a digito y por grupos de tres o cuatro.

    El prompt pide los numeros en palabras; esto cubre cuando no lo hace.
    OmniVoice leeria "3154287719" como una cifra de miles de millones.
    """
    digitos = re.sub(r"\D", "", m.group(0))
    if len(digitos) < 7:        # un monto o una hora, no un telefono
        return m.group(0)
    grupos, i = [], 0
    while i < len(digitos):
        resto = len(digitos) - i
        n = 4 if resto == 4 else 3
        grupos.append(" ".join(UNIDADES[int(d)] for d in digitos[i:i + n]))
        i += n
    return ", ".join(grupos)


def para_voz(frase):
    frase = re.sub(r"[*_#`]+", "", frase)
    frase = re.sub(r"\d[\d\s\-.]{1,}\d", _digitos, frase)
    return re.sub(r"\s{2,}", " ", frase).strip()


def revisar(frase, dueno):
    """Devuelve (frase_para_decir, motivo_si_se_cambio)."""
    m = PROMESA.search(frase)
    if m:
        return (f"Eso se lo confirma {dueno} directamente; yo le paso el mensaje.",
                f"promesa: «{m.group(0)}»")
    return para_voz(frase), None
