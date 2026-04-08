# utils/id_generator.py
import os
import time
import threading

class ULID:
    """Thread-safe, monotonic ULID generator using Crockford Base32."""
    ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    _LOCK = threading.Lock()
    _LAST_TIMESTAMP_MS = -1
    _LAST_RANDOM = 0

    @classmethod
    def generate(cls) -> str:
        with cls._LOCK:
            timestamp_ms = int(time.time() * 1000)
            if timestamp_ms > cls._LAST_TIMESTAMP_MS:
                cls._LAST_TIMESTAMP_MS = timestamp_ms
                cls._LAST_RANDOM = int.from_bytes(os.urandom(10), "big")
            else:
                cls._LAST_RANDOM = (cls._LAST_RANDOM + 1) & ((1 << 80) - 1)
                if cls._LAST_RANDOM == 0:
                    while timestamp_ms <= cls._LAST_TIMESTAMP_MS:
                        time.sleep(0.001)
                        timestamp_ms = int(time.time() * 1000)
                    cls._LAST_TIMESTAMP_MS = timestamp_ms
                    cls._LAST_RANDOM = int.from_bytes(os.urandom(10), "big")
            value = (cls._LAST_TIMESTAMP_MS << 80) | cls._LAST_RANDOM
            return cls._encode_128bit(value)

    @classmethod
    def _encode_128bit(cls, value: int) -> str:
        chars = []
        for _ in range(26):
            chars.append(cls.ENCODING[value & 0x1F])
            value >>= 5
        return "".join(reversed(chars))
