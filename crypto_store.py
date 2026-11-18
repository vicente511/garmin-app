"""Cifra / descifra los datos de la app con la clave SITE_PASSWORD (AES-256-GCM + PBKDF2-SHA256).

Uso (la clave va en la variable de entorno SITE_PASSWORD, nunca en el código):
  python crypto_store.py decrypt data.enc _work/data.json   # si data.enc no existe, no hace nada
  python crypto_store.py encrypt _work/data.json data.enc

Formato: base64( salt[16] | iv[12] | texto cifrado + tag ). La app (index.html) usa el mismo
formato con WebCrypto.
"""
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERACIONES = 250_000


def _clave(pw: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERACIONES).derive(pw.encode("utf-8"))


def cifrar(pw: str, datos: bytes) -> str:
    salt, iv = os.urandom(16), os.urandom(12)
    return base64.b64encode(salt + iv + AESGCM(_clave(pw, salt)).encrypt(iv, datos, None)).decode()


def descifrar(pw: str, texto: str) -> bytes:
    b = base64.b64decode(texto)
    return AESGCM(_clave(pw, b[:16])).decrypt(b[16:28], b[28:], None)


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in ("encrypt", "decrypt"):
        sys.exit(__doc__)
    pw = os.environ.get("SITE_PASSWORD", "")
    if not pw:
        sys.exit("Falta SITE_PASSWORD")
    cmd, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    dst.parent.mkdir(parents=True, exist_ok=True)
    if cmd == "decrypt":
        if not src.exists():
            return
        dst.write_bytes(descifrar(pw, src.read_text().strip()))
    else:
        nuevo = src.read_bytes()
        # Si los datos no cambiaron (salvo la hora), no se reescribe: evita commits innecesarios.
        if dst.exists():
            import json
            try:
                viejo = json.loads(descifrar(pw, dst.read_text().strip()))
                actual = json.loads(nuevo)
                viejo.pop("updated", None); actual.pop("updated", None)
                if viejo == actual:
                    print("Sin cambios en los datos")
                    return
            except Exception:
                pass
        dst.write_text(cifrar(pw, nuevo))


if __name__ == "__main__":
    main()
