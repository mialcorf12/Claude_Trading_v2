> **SUPERSEDED (2026-11-09).** Este runbook completo describe el camino de
> API directa de Tradovate, confirmado como un dead end para cuentas
> prop/evaluation como Lucid (no pueden registrar API Access). Tampoco
> aplica ya el entorno Mac: el deployment ahora es un VPS Windows. No sigas
> estos pasos contra la cuenta real. El camino vigente es NinjaTrader 8 ATI
> (OIF) + AddOn de telemetría -- ver `tradovate_bot/README.md` y
> `odd/tasks/tradovate-execution-bot.md`. Este archivo queda como
> referencia histórica únicamente.

# Runbook: probar tradovate_bot contra la cuenta DEMO de Tradovate (macOS)

Pasos y comandos exactos. Corré cada bloque en Terminal, desde la raíz del
proyecto (`/Users/albertocordero/Documents/PI/Claude_Trading_v2`).

## 0. Verificar que estás en la carpeta correcta

```sh
cd /Users/albertocordero/Documents/PI/Claude_Trading_v2
pwd
```

Debe imprimir esa misma ruta.

## 1. Conseguir las credenciales de API de Tradovate (demo)

En el portal de Tradovate (web), con tu cuenta demo activa:

1. Entrá a tu perfil → **API Access** (o "Developer"/"App Registration",
   según el layout actual del sitio).
2. Generá una app/API key nueva. Te va a dar un **API Key (app id)** y un
   **API Secret (client secret)**. Guardalos, no se vuelven a mostrar igual.
3. Anotá el **Account ID** numérico de tu cuenta demo (aparece en la lista
   de cuentas de tu perfil, no es el username).

Nadie fuera de tu máquina necesita ver estos valores — nunca los pegues en
este chat ni en ningún otro.

## 2. Crear el archivo de credenciales local

Este archivo vive solo en tu disco, fuera de control de versiones.

```sh
cat > .env.tradovate << 'EOF'
TRADOVATE_USERNAME=
TRADOVATE_PASSWORD=
TRADOVATE_API_KEY=
TRADOVATE_API_SECRET=
TRADOVATE_ACCOUNT_ID=
TRADOVATE_DEVICE_ID=mac-alberto-01
TRADOVATE_ENV=demo
EOF
```

Después abrilo y completá los valores vos mismo:

```sh
open -e .env.tradovate
```

(o `nano .env.tradovate` si preferís terminal). Guardá y cerrá.

## 3. Bloquear permisos del archivo y excluirlo de git

```sh
chmod 600 .env.tradovate
echo ".env.tradovate" >> .gitignore
```

## 4. Instalar dependencias (si no están)

```sh
python3 -m pip install --user pytest requests websocket-client
```

## 5. Cargar las variables en la sesión de terminal actual

Tenés que repetir este paso cada vez que abrís una Terminal nueva para
correr el bot.

```sh
set -a
source .env.tradovate
set +a
```

Verificación rápida (no imprime el password, solo confirma que cargó):

```sh
echo "Account ID cargado: $TRADOVATE_ACCOUNT_ID — Env: $TRADOVATE_ENV"
```

## 6. Correr los tests (sanity check, no toca la red)

```sh
python3 -m pytest tests/tradovate_bot/ -v
```

Esperable: 45 passed.

## 7. Probar la conexión real contra demo

Abrí una consola Python interactiva desde el proyecto:

```sh
python3
```

Dentro de Python:

```python
from tradovate_bot.client import TradovateClient

client = TradovateClient()
client.authenticate()
print("Login OK, access token obtenido")

state = client.fetch_account_state()
print(state)
```

Si `authenticate()` falla o `fetch_account_state()` devuelve campos en
`None`/distintos a lo esperado, es la señal de que hay que ajustar el
mapeo de campos en `client.py` contra la respuesta real — avisame con el
error o el `print(state)` (sin pegar tokens ni passwords) y lo corregimos.

Salir de Python: `exit()`.

## 8. Una vez que el login y fetch_account_state confirman datos reales

Recién ahí probamos `place_order` con una orden mínima en demo (1 contrato
MNQ) antes de considerar correr `executor.py` en loop.
