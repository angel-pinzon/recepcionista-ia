"""Altavoz y micro del PC a traves del PulseAudio de WSLg, con ctypes.

Se usa libpulse-simple directamente en vez de sounddevice o pyaudio porque ya
esta en el sistema y no hace falta instalar PortAudio con sudo. Y porque trae
lo que importa para callarse: pa_simple_flush tira el audio que ya estaba en
el buffer, asi que cortar no espera a que suene lo encolado.

El buffer se pide corto (~60 ms) por la misma razon: todo lo que esta en el
buffer es audio que sonara aunque la recepcionista ya deberia estar callada.
"""

import ctypes
import ctypes.util

import numpy as np

PA_STREAM_PLAYBACK, PA_STREAM_RECORD = 1, 2
PA_SAMPLE_FLOAT32LE = 5


class _Spec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]


class _Buffer(ctypes.Structure):
    _fields_ = [("maxlength", ctypes.c_uint32), ("tlength", ctypes.c_uint32),
                ("prebuf", ctypes.c_uint32), ("minreq", ctypes.c_uint32),
                ("fragsize", ctypes.c_uint32)]


_lib = None


def _pa():
    global _lib
    if _lib is None:
        _lib = ctypes.CDLL(ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0")
        _lib.pa_simple_new.restype = ctypes.c_void_p
        _lib.pa_simple_new.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_char_p,
                                       ctypes.POINTER(_Spec), ctypes.c_void_p,
                                       ctypes.POINTER(_Buffer), ctypes.POINTER(ctypes.c_int)]
        for nombre in ("pa_simple_write", "pa_simple_read"):
            f = getattr(_lib, nombre)
            f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.POINTER(ctypes.c_int)]
        for nombre in ("pa_simple_drain", "pa_simple_flush"):
            getattr(_lib, nombre).argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        _lib.pa_simple_get_latency.restype = ctypes.c_uint64
        _lib.pa_simple_get_latency.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        _lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    return _lib


class _Flujo:
    def __init__(self, direccion, rate, nombre, ms):
        pa, err = _pa(), ctypes.c_int(0)
        spec = _Spec(PA_SAMPLE_FLOAT32LE, rate, 1)
        bytes_ms = rate * 4 // 1000
        menos1 = 0xFFFFFFFF
        if direccion == PA_STREAM_PLAYBACK:
            attr = _Buffer(menos1, bytes_ms * ms, bytes_ms * 20, menos1, menos1)
        else:
            attr = _Buffer(menos1, menos1, menos1, menos1, bytes_ms * ms)
        self._s = pa.pa_simple_new(None, b"recepcionista-ia", direccion, None, nombre.encode(),
                                   ctypes.byref(spec), None, ctypes.byref(attr), ctypes.byref(err))
        if not self._s:
            raise OSError(f"PulseAudio no abrio el flujo {nombre} (error {err.value})")
        self.rate = rate

    def cerrar(self):
        if self._s:
            _pa().pa_simple_free(self._s)
            self._s = None


class Altavoz(_Flujo):
    def __init__(self, rate=24000, ms=60):
        super().__init__(PA_STREAM_PLAYBACK, rate, "voz", ms)

    def escribir(self, audio):
        """Bloquea hasta que el trozo cabe en el buffer. Mandar trozos cortos."""
        datos = np.ascontiguousarray(audio, dtype=np.float32)
        err = ctypes.c_int(0)
        if _pa().pa_simple_write(self._s, datos.ctypes.data, datos.nbytes, ctypes.byref(err)) < 0:
            raise OSError(f"PulseAudio no acepto el audio (error {err.value})")

    def vaciar(self):
        _pa().pa_simple_flush(self._s, ctypes.byref(ctypes.c_int(0)))

    def esperar(self):
        _pa().pa_simple_drain(self._s, ctypes.byref(ctypes.c_int(0)))

    def latencia(self):
        return _pa().pa_simple_get_latency(self._s, ctypes.byref(ctypes.c_int(0))) / 1e6


class Micro(_Flujo):
    def __init__(self, rate=16000, ms=30):
        super().__init__(PA_STREAM_RECORD, rate, "micro", ms)
        self.bloque = rate * ms // 1000

    def leer(self):
        """Un bloque de audio, float32 mono. Bloquea hasta tenerlo."""
        datos = np.empty(self.bloque, dtype=np.float32)
        err = ctypes.c_int(0)
        if _pa().pa_simple_read(self._s, datos.ctypes.data, datos.nbytes, ctypes.byref(err)) < 0:
            raise OSError(f"PulseAudio no entrego audio del micro (error {err.value})")
        return datos
