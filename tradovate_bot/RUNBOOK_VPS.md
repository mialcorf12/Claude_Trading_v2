# Guía de Despliegue: Bot de Ejecución NT8 ATI en VPS Windows

## Requisitos Previos

* **VPS Windows:** Instancia provisionada con 2 vCPU y 8 GB de RAM.
* **NinjaTrader 8 (v8.1.3+ / v8.0.x):** Instalado y conectado a la cuenta de trading (ej. Lucid Tradovate).
* **Nivel de Ejecución:** Manual por el usuario a través de RDP (Remote Desktop Protocol) en el VPS.

---

## 1. Clonar o Actualizar el Repositorio

Asegurar que Git esté instalado en el VPS:

```powershell
git --version

```

*Si el comando no se reconoce, descargar e instalar Git for Windows.*

Navegar a la raíz del disco e inicializar el proyecto:

```powershell
cd C:\
git clone https://github.com/mialcorf12/Claude_Trading_v2.git
cd Claude_Trading_v2

```

Cambiar a la rama de desarrollo de ATI/OIF:

```powershell
git fetch origin
git checkout feature/tradovate-oif-execution

```

*Nota: Para actualizaciones posteriores ejecutar `git pull`. Al fusionar a la rama principal, regresar mediante `git checkout main && git pull`.*

---

## 2. Configurar el Entorno Python y Dependencias

Verificar que Python 3.9+ esté registrado en el sistema:

```powershell
python --version

```

*Si no está instalado, instalar la versión adecuada desde python.org asegurando marcar la opción **"Add python.exe to PATH"**.*

Instalar las librerías necesarias:

```powershell
cd C:\TradingPlatform_v2\Claude_Trading_v2
python -m pip install --user pytest requests

```

Ejecutar la suite de pruebas unitarias para validar la integridad del módulo:

```powershell
python -m pytest tests\tradovate_bot\ -q

```

*Resultado esperado:* **`69 passed`**.

---

## 3. Configurar la Interfaz de Trading Automatizado (ATI) en NT8

> **Especificación NT8 (v8.1.3 / v8.0+):** La configuración del ATI global y las carpetas de intercambio de archivos DLL/ATI se encuentran en el panel central de opciones.

1. En el **Control Center** de NinjaTrader 8, navegar a:
`Tools` $\rightarrow$ `Options` $\rightarrow$ sección **Automated Trading Interface (ATI)**.
2. Activar la opción **Enable ATI**.
3. Verificar o definir las rutas locales para la recepción y envío de órdenes (carpetas por defecto):
* **Incoming Directory:** `C:\Users\Administrator\Documents\NinjaTrader 8\incoming\`
* **Outgoing Directory:** `C:\Users\Administrator\Documents\NinjaTrader 8\outgoing\`


4. Confirmar en la pestaña **Accounts** del Control Center el nombre exacto de la cuenta activa (ej. `Sim101` o el identificador de la cuenta Tradovate).
5. Hacer clic en **OK** y reiniciar NinjaTrader 8 para aplicar los cambios de red y de interfaz.

---

## 4. Compilar e Instalar el AddOn de Telemetría (Read-Only)

El AddOn `AccountTelemetryAddOn.cs` compila métricas de la cuenta (Cash Value, Net Liquidation) en tiempo real y las exporta en formato JSON.

### Paso 4.1: Copiar el script C# al directorio NinjaScript

Ejecutar en PowerShell:

```powershell
copy ninjatrader_addon\AccountTelemetryAddOn.cs "C:\Users\Administrator\Documents\NinjaTrader 8\bin\Custom\AddOns\"

```

### Paso 4.2: Compilar el AddOn en NinjaTrader 8

1. Ir al **Control Center** $\rightarrow$ `Tools` $\rightarrow$ `Edit NinjaScript` $\rightarrow$ `AddOn`.
2. Seleccionar `AccountTelemetryAddOn.cs` y presionar **F5** (o hacer clic en el icono de **Compile**).
3. Confirmar en el panel inferior que finalice sin errores de sintaxis (*0 errors*).

### Paso 4.3: Validación de ejecución en segundo plano

Este AddOn funciona de forma headless (sin interfaz gráfica). Para validar su funcionamiento:

1. Asegurar que las variables de entorno del **Paso 5** estén configuradas antes de reiniciar NT8.
2. Reiniciar NinjaTrader 8.
3. Abrir la ventana de salida: **Control Center** $\rightarrow$ `New` $\rightarrow$ `NinjaScript Output`.
4. Verificar la presencia del log:
```text
AccountTelemetryAddOn: started.

```


*Si se despliega `account '...' not found`, validar que la variable `NT8_ACCOUNT_ID` coincida exactamente con el nombre asignado en la pestaña Accounts.*
5. Inspeccionar el archivo JSON generado en la ruta configurada (`account_telemetry.json`) mediante el Notepad para confirmar la paridad con el **Cash Value** de NT8.

---

## 5. Registrar Variables de Entorno del Sistema

Configurar las variables a nivel de máquina (`Machine`) para garantizar la persistencia tras reiniciar el VPS. Ejecutar en PowerShell como **Administrador**:

```powershell
[Environment]::SetEnvironmentVariable("NT8_INCOMING_DIR", "C:\Users\Administrator\Documents\NinjaTrader 8\incoming", "Machine")
[Environment]::SetEnvironmentVariable("NT8_OUTGOING_DIR", "C:\Users\Administrator\Documents\NinjaTrader 8\outgoing", "Machine")
[Environment]::SetEnvironmentVariable("NT8_ACCOUNT_ID", "Sim101", "Machine")
[Environment]::SetEnvironmentVariable("ACCOUNT_TELEMETRY_JSON_PATH", "C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\account_telemetry.json", "Machine")
[Environment]::SetEnvironmentVariable("ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS", "5", "Machine")

```

> **Importante:** Cerrar la consola actual y abrir una nueva ventana de PowerShell para que la sesión cargue las nuevas variables de entorno a nivel de sistema.

---

## 6. Prueba Manual de Ejecución (Foreground Test)

Antes de delegar el proceso a un servicio desatendido, validar la lógica de lectura y la conexión del cliente Python:

```powershell
cd C:\TradingPlatform_v2\Claude_Trading_v2
python -m tradovate_bot.executor

```

* **Criterio de éxito:** Inicialización limpia en `stderr`, sin excepciones críticas durante ciclos de sondeo en vacío (cuando `trader/orders.json` esté ausente o vacío).
* Presionar `Ctrl + C` para detener el proceso una vez verificado.

---

## 7. Despliegue como Servicio Desatendido (vía NSSM)

Se recomienda utilizar **NSSM (Non-Sucking Service Manager)** para gestionar el ejecutor Python como un servicio de Windows con reinicio automático y redirección nativa de logs.

### Paso 7.1: Instalación de NSSM

1. Descargar NSSM desde [nssm.cc](https://nssm.cc) y extraer el binario `nssm.exe` de 64 bits en la ruta `C:\nssm\`.

 Agregar NSSM a las Variables de Entorno (Para usarlo desde cualquier parte)
Si quieres poder escribir nssm desde cualquier ventana de comandos sin importar dónde estés ubicado:
1. Copia la ruta de la carpeta donde está nssm.exe (por ejemplo, C:\nssm).
2. Abre el menú de inicio de Windows y escribe Variables de entorno ("Editar las variables de entorno del sistema").
3. Haz clic en el botón Variables de entorno...
4. En el recuadro inferior ("Variables del sistema"), busca y selecciona la variable Path, luego haz clic en Editar.
5. Haz clic en Nuevo y pega la ruta de la carpeta (C:\nssm).
6. Haz clic en Aceptar en todas las ventanas.
7. Cierra y vuelve a abrir tu consola (cmd o PowerShell) para que los cambios surtan efecto y prueba escribir nssm


### Paso 7.2: Configurar el Servicio

Ejecutar en PowerShell como **Administrador**:

```powershell
C:\nssm\nssm.exe install TradovateExecutor

```

Completar los parámetros en la interfaz gráfica:

* **Tab Application:**
* **Path:** Ruta absoluta a `python.exe` (obtenerla ejecutando `where python`, ej. `C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`).
* **Startup directory:** `C:\TradingPlatform_v2\Claude_Trading_v2`
* **Arguments:** `-m tradovate_bot.executor`


* **Tab I/O (Redirección de Logs):**
* **Input (stdin):** `C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\executor.stdin.log`
* **Output (stdout):** `C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\executor.stdout.log`
* **Error (stderr):** `C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\executor.stderr.log`


* **Tab Environment:**
* Si las variables de entorno del **Paso 5** no se registraron a nivel de máquina, añadir cada par `CLAVE=VALOR` en líneas individuales dentro de este panel.



Hacer clic en **Install service**.

### Paso 7.3: Control y Monitoreo del Servicio

Iniciar el servicio:

```powershell
nssm start TradovateExecutor

```

Monitorear registros de ejecución e inspeccionar la salida en tiempo real:

```powershell
Get-Content C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\decisions.log.jsonl -Tail 5
Get-Content C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\executor.stderr.log -Tail 10 -Wait

```

Comandos de gestión básica:

```powershell
# Detener el servicio
nssm stop TradovateExecutor

# Reiniciar el servicio
nssm restart TradovateExecutor

```

---

## Consideraciones de Validación y Monitoreo

* **Auditoría de Rollover Diario:** Confirmar durante el primer cambio de sesión bursátil que el archivo `account_telemetry.json` mantenga sincronización con el Net Liquidation de NT8 sin interrupciones en la lectura C#.
* **Verificación de Confirmaciones ATI:** Auditar la generación y consumo de archivos de confirmación en el directorio `NT8_OUTGOING_DIR` al enviar la primera orden de prueba.