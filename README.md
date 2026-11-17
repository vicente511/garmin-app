# Mi recuperación Garmin (app)

App web personal (instalable en el teléfono) con datos de Garmin **cifrados** (AES-256-GCM, clave derivada con PBKDF2).
Sin la clave, `data.enc` y `plan.enc` son ilegibles. GitHub Actions actualiza `data.enc` cada madrugada.

Secretos del repositorio: `GARMIN_TOKEN_B64` (sesión de Garmin) y `SITE_PASSWORD` (clave de la app).
Basado en [python-garminconnect](https://github.com/cyberjunky/python-garminconnect).
