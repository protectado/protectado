[🇬🇧 English](INSTALL.md) | [🇫🇷 Français](INSTALL.fr.md) | [🇪🇸 Español](INSTALL.es.md) | [🇵🇹 Português](INSTALL.pt.md)

# Protectado — Guía de instalación

Esta guía cubre la instalación completa de Protectado en casa de una nueva familia, desde la tarjeta SD en blanco hasta el panel operativo.

---

## Instalación en Linux existente (NAS, PC antiguo...)

Si ya tienes una máquina Linux en la red familiar — un NAS, mini-PC o PC antiguo con Ubuntu — el bootstrap funciona directamente en ella.

**Requisitos:**
- Debian / Ubuntu (el script usa `apt`)
- La máquina debe estar en la **misma red local** que los dispositivos de los hijos
- Pi-hole v6 ya instalado, **o** no instalado (el bootstrap lo instala)
- Python 3.10 mínimo (`python3 --version`)
- systemd activo

> **VPS / servidor remoto: no compatible.** Pi-hole debe ver el tráfico DNS local. Un servidor en la nube no puede desempeñar este rol sin VPN.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

Si Pi-hole ya está instalado y configurado, el bootstrap lo detecta y lo deja intacto — solo instala Protectado encima. Si Pi-hole no está, lo instala.

Continuar desde el **Paso 4** (configuración via el asistente).

---

## Instalación en Raspberry Pi (vía nominal)

---

## Qué preparar ANTES de ir a casa de la familia

### Hardware

| Artículo | Notas |
|---------|-------|
| Raspberry Pi | Pi 3B+, Pi 4 o Pi 5 recomendado (Ethernet integrado). Pi 2W funciona por WiFi. |
| Tarjeta SD | 16 GB mínimo, clase 10 |
| Alimentación | USB-C (Pi 4/5) o micro-USB (Pi 2W/3) |
| Cable Ethernet | Opcional pero recomendado — conecta el Pi directamente al router |

### Cuentas / claves a crear de antemano

**Clave API OpenRouter** (imprescindible — la IA no funcionará sin ella)
1. Crear una cuenta en [openrouter.ai](https://openrouter.ai)
2. Añadir crédito (unos pocos euros duran varios meses)
3. Generar una clave API → copiar la clave (empieza por `sk-or-`)

---

## Paso 1 — Preparar la tarjeta SD (en tu PC)

1. Descargar **Raspberry Pi Imager**: [raspberrypi.com/software](https://www.raspberrypi.com/software/)
2. Insertar la tarjeta SD en tu PC
3. En Raspberry Pi Imager:
   - **Dispositivo** → elegir tu modelo de Pi
   - **Sistema operativo** → `Ubuntu Server (64-bit)` (necesario para la sandbox del agente de IA)
   - **Almacenamiento** → tu tarjeta SD
4. Clicar en **⚙️ Editar ajustes** (¡antes de grabar!)

En los ajustes avanzados, configurar:

```
✅ Nombre de host    → protectado
✅ Activar SSH       → Usar contraseña
   Nombre de usuario → pi
   Contraseña        → [elegir una contraseña SSH]
✅ Configurar WiFi   → [SSID y contraseña del hogar]
   País WiFi         → [tu país]
```

> **Si usas cable Ethernet**: puedes dejar el WiFi sin configurar.

5. Grabar la tarjeta → insertar en el Pi

---

## Paso 2 — Primer arranque

1. Conectar el cable Ethernet **o** dejar que el WiFi se conecte automáticamente
2. Conectar la alimentación
3. Esperar ~60 segundos (el Pi arranca y se une a la red)

**Encontrar la IP del Pi:**

```bash
# Opción A — desde tu PC en la misma red
ping protectado.local

# Opción B — interfaz de administración del router (normalmente 192.168.1.1)
```

---

## Paso 3 — Conexión SSH e instalación

```bash
ssh pi@protectado.local
```

Una vez conectado, ejecutar la instalación con un único comando:

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

La instalación tarda **5 a 10 minutos**. Instala automáticamente:
- Pi-hole (filtrado DNS)
- Protectado (agente IA + panel)
- Actualizaciones automáticas

Al final, el script muestra:

```
╔══════════════════════════════════════════════════╗
║        ¡Protectado instalado con éxito!         ║
╚══════════════════════════════════════════════════╝

  Panel  →  http://192.168.x.x

  ┌─ Información de configuración ──────────────────
  │  PIHOLE_PASSWORD :  xxxxxxxxxxxxxxxx
  │  PAIRING_CODE    :  XXXXXXXX
  └──────────────────────────────────────────────────
```

**Conserva la contraseña de Pi-hole** — para la interfaz de admin de Pi-hole, en `http://<ip>:81`.

**Guarda el código de emparejamiento**: en modo DNS, el asistente lo pide antes de aceptar la
contraseña de los padres. Mientras el dispositivo no esté configurado responde a toda la red de
casa: sin este código, cualquier aparato —incluido el de un menor— podría fijar la contraseña
antes que tú. En modo pasarela el asistente solo es accesible desde la red aislada
`Protectado-Setup` y no se pide el código.

---

## Paso 4 — Asistente de primer arranque

En el primer arranque, Protectado elige su **modo automáticamente** y abre un asistente.
Tú no eliges el modo — depende del hardware
(ver [modos de funcionamiento](../README.es.md#dos-modos-de-funcionamiento)).

### Modo DNS (por defecto)

Desde cualquier dispositivo de la red, abre `http://protectado.local` (o la IP del paso 3).
El asistente pide la **contraseña de padre** del panel y luego muestra un **paso
imprescindible**.

> ⚠️ **En modo DNS no se filtra nada mientras tu router no envíe los dispositivos al
> equipo.** La Pi no es un router: solo ve los dispositivos que le piden resolver los
> nombres de los sitios.

En la interfaz de tu router (a menudo `http://192.168.1.1`), busca **DNS** en los ajustes de
red o DHCP y sustituye el servidor DNS por la dirección del equipo — el asistente la
muestra. Después reinicia el Wi-Fi de los dispositivos de tus hijos para que la apliquen.

Si tu router no permite cambiar el DNS, configúralo dispositivo por dispositivo en sus
ajustes de Wi-Fi. Los perfiles y horarios se añaden después desde el panel.

### Modo pasarela (hardware compatible detectado)

El equipo emite un Wi-Fi abierto temporal llamado **`Protectado-Setup`**. Conecta un
teléfono a él — se abre solo un portal cautivo — y sigue los pasos:

| Paso | Qué introducir |
|------|----------------|
| 1 | Tu router de internet — elige tu red Wi-Fi e introduce **su** clave |
| 2 | Wi-Fi de los niños — un nombre y una clave fácil de 3 palabras (WPA2) |
| 3 | Contraseña de padre del panel |
| 4 | Reconecta el teléfono a tu router, abre la dirección indicada, clica **Finalizar** |

El equipo reinicia entonces en modo pasarela: el Wi-Fi de los niños se activa y se filtra,
y la red de configuración desaparece. El panel sigue accesible en `http://<ip-del-equipo>`.

> La **clave API OpenRouter** se introduce después, desde el panel de chat del panel — no
> en este asistente.

---

## Paso 5 — Asignar dispositivos a perfiles

En el panel → pestaña **Dispositivos**:

1. Clicar **Escanear red**
2. Para cada dispositivo detectado: seleccionar el perfil
3. Clicar **Asignar**

---

## Paso 6 — Configurar franjas horarias

En el panel → pestaña **Perfiles**:

1. Clicar **Editar** en un perfil
2. Añadir franjas horarias para cada día de la semana (el formato antiguo
   Semana/Fin de semana se sigue leyendo por compatibilidad)
3. Modos disponibles: `blocked`, `work`, `permissive`
4. Clicar **Guardar** → **⚙️ Reconfigurar Pi-hole**

---

## Copia de seguridad y restauración

En el panel → pestaña **Gestión** → tarjeta **Copia de seguridad y restauración**.

> ⚠️ **El ZIP contiene secretos SIN CIFRAR**: la contraseña de los padres, la clave de la API de IA y, en modo pasarela, la clave Wi-Fi de tu router y la de la red infantil. Por eso, tanto la descarga como la restauración exigen volver a introducir la contraseña de los padres. Guarda el archivo como guardarías esas contraseñas: nunca en un espacio compartido, nunca por correo electrónico.

---

## Resolución de problemas

```bash
sudo systemctl status protectado-agent
sudo journalctl -u protectado-agent -n 30
pihole status
sudo bash /opt/protectado/update.sh
```

---

## Reiniciar para reconfigurar

Para volver a lanzar el asistente (p. ej. entregar el equipo a otra familia):

```bash
# Reset simple — mantiene los valores, solo vuelve a mostrar el asistente en el próximo arranque
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot

# Reset total — estado de fábrica: borra config, Wi-Fi guardado y estado detectado
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

Tras un reset total, el equipo vuelve como nuevo y re-elige su modo (DNS o pasarela)
automáticamente en el arranque.

---

## Actualizaciones automáticas

Protectado se actualiza solo cada noche a las 3h desde la rama **`stable`**.

`main` es la rama de desarrollo; `stable` se promueve **manualmente**. Así, una regresión
subida por la tarde no puede romper todos los equipos por la mañana. La rama elegida en
la instalación se guarda en `data/branch` y la reutiliza el actualizador: un equipo nunca
cambia de rama por su cuenta.

```bash
# Promover el estado actual de main a stable (desde el repositorio de desarrollo)
git checkout stable && git merge --ff-only main && git push origin stable

# Instalar una máquina de pruebas en main en lugar de stable
curl -sSL .../bootstrap.sh | sudo PROTECTADO_BRANCH=main bash
```

La versión instalada (commit corto, rama, fecha) se muestra en el panel → pestaña
**Gestión** → tarjeta **Actualización**, y en la página de inicio de sesión.
Pi-hole se actualiza cada domingo a las 4h.
Los parches de seguridad del SO se instalan automáticamente via `unattended-upgrades`.

---

## Actualizar una instalación existente

El script bootstrap detecta automáticamente una instalación existente y cambia al modo actualización en lugar de reinstalar.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

Qué hace la actualización:
1. Guarda `config.json` y `protectado.db` en un directorio con marca de tiempo en `/opt/`
2. Descarga el último código desde la rama seguida por el equipo
3. Restaura `config.json` (tus perfiles y configuración se conservan)
4. Ejecuta las migraciones de la base de datos (`database.init_db()`)
5. Reinicia los servicios

Si el agente no arranca tras la actualización, el script vuelve automáticamente a la copia de seguridad.
