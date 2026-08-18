[🇬🇧 English](USAGE.md) | [🇫🇷 Français](USAGE.fr.md) | [🇪🇸 Español](USAGE.es.md) | [🇵🇹 Português](USAGE.pt.md)

# Protectado — Guía de uso y referencia técnica

Para la instalación, consulta el [README](../README.es.md#puesta-en-marcha) y la
[guía de instalación detallada](../bootstrap/INSTALL.es.md).

---

## Cómo funciona

```
WiFi (router)
    ↓ todo el tráfico DNS pasa por →
Pi-hole  (instalado y configurado por el bootstrap)
    ↓ logs + API →
Protectado  (panel :80 + supervisión automática)
    ↓ bloqueo DNS →
grupos Pi-hole por perfil y modo

Cada noche a las 23h:
  informe diario generado via OpenRouter
```

> Este es el **modo DNS** (por defecto). En **modo pasarela**, el equipo es también el
> router de los niños y filtra a nivel de paquete, no solo el DNS — ver
> [modos de funcionamiento](../README.es.md#dos-modos-de-funcionamiento). El panel está en
> el puerto **80** (la interfaz de admin de Pi-hole pasa al **81**).

**Sin intervención de los padres**, Protectado aplica automáticamente el horario configurado: cortar el acceso de noche, pasar a modo trabajo después del colegio, reabrir por la noche.

**Bajo demanda**, el padre escribe en el chat del panel en lenguaje natural — la IA interpreta y actúa.

---

## Primer arranque

En el primer arranque, Protectado elige su modo automáticamente y abre un asistente
(ver [modos de funcionamiento](../README.es.md#dos-modos-de-funcionamiento)):

- **Modo DNS** (por defecto) — abre `http://protectado.local` y define la **contraseña de
  padre**. Es el único paso; el equipo queda listo.
- **Modo pasarela** (hardware compatible) — el equipo emite un Wi-Fi temporal
  `Protectado-Setup` con un portal cautivo que te guía para conectarlo a tu router de
  internet, nombrar el Wi-Fi de los niños y definir la contraseña de padre.

Los perfiles, los horarios y la clave API OpenRouter no se introducen en el asistente —
se añaden después desde el panel (pestaña Perfiles, y el panel de chat para la clave). Una
breve visita guiada explica cada pestaña en el primer inicio de sesión.

---

## Uso diario

### Panel de control

`http://protectado.local`  (interfaz de admin de Pi-hole: `http://protectado.local:81`)

- Estado en tiempo real de cada perfil (dispositivos activos, modo actual, siguiente franja)
- Historial de eventos (bloqueos, alertas, cambios de modo)
- Catálogo de dominios visitados y su categoría

### Chat para padres

La función principal: escribir lo que se quiere hacer, la IA se ocupa del resto.

| Lo que escribes | Lo que hace |
|---|---|
| "Corta internet a Alicia, tiene que dormir" | Bloquea inmediatamente todos sus dispositivos |
| "Autoriza YouTube a Alicia durante 30 minutos" | Desbloquea youtube.com 30 min y vuelve a bloquear |
| "Dale 45 minutos más a Alicia esta noche" | Retrasa el fin de la franja actual |
| "Mañana Alicia está de vacaciones, modo libre" | Día completo sin restricciones (excepto contenido adulto) |
| "Bloquea todo a Alicia el sábado" | Día completo bloqueado |
| "khanacademy.org es educativo" | Recategoriza el dominio — nunca bloqueado en modo trabajo |
| "Bloquea twitch.tv incluso en modo permisivo" | Lista negra permanente |
| "¿Por qué YouTube estaba accesible ayer por la tarde?" | Explica qué regla se aplicaba en ese momento. El detalle de la respuesta depende del *nivel de privacidad* del perfil (ver más abajo) |

### Modos de acceso

| Modo | Qué es accesible |
|---|---|
| **Bloqueado** | Nada — corte de red completo |
| **Trabajo** | Educación, herramientas escolares. YouTube, redes sociales y contenido adulto bloqueados |
| **Libre** | Todo excepto contenido adulto |

El cambio de modo es automático según el horario. Se puede anular en cualquier momento desde el chat o el panel.

---

## Perfiles

Cada hijo tiene su propio perfil con:
- sus dispositivos (IPs fijas recomendadas)
- su horario **día a día**, de lunes a domingo (franjas `blocked`, `work`, `permissive`)
- anulaciones puntuales (vacaciones, excepción de noche…)

El perfil **monitoring** es especial: observa sin bloquear. Útil para supervisar un dispositivo compartido sin aplicarle reglas.

### Zona horaria

Todos los horarios del producto siguen la hora local del equipo: franjas, hora de dormir,
excepciones temporales, informe de la tarde. La zona horaria es por tanto determinante, y
se detecta **desde el navegador del adulto** durante el asistente de primer arranque, y
luego se aplica al sistema. Sin geolocalización y sin llamadas a un servicio externo.

Se puede cambiar después en **Gestión → Modo actual por perfil**, línea «Hora del
equipo». Conviene revisarla tras una mudanza, o si el equipo se configuró desde un
teléfono que estaba de viaje: una zona errónea desplaza en silencio todas las reglas.

---

## Modo adulto en dispositivo compartido

Si un hijo usa un dispositivo compartido (TV, tablet familiar), el padre puede cambiar temporalmente el dispositivo a modo adulto sin tocar el perfil del hijo.

Desde el panel: botón **Modo adulto** → contraseña del padre → duración. El dispositivo vuelve automáticamente al perfil del hijo al expirar.

---

## Informe diario

Cada noche a las 23h, Protectado envía automáticamente via OpenRouter:
- la categorización de los nuevos dominios desconocidos
- un resumen del día: tiempo por dominio, alertas, bloqueos

El informe aparece en el panel (sección Eventos) y en los logs.

Para activarlo manualmente:
```bash
cd /opt/protectado && .venv/bin/python daily_report.py
```

---

## Copia de seguridad y restauración

El panel permite guardar y restaurar la configuración con un clic.

- **Copia de seguridad**: botón en el panel → descarga un ZIP (`config.json` + base de datos)
- **Restaurar**: subir el ZIP → configuración recargada en caliente, sin reinicio

> ⚠️ El ZIP contiene **secretos sin cifrar**: contraseña de los padres, clave de la API de IA y, en modo pasarela, las claves Wi-Fi. Tanto la descarga como la restauración exigen volver a introducir la contraseña de los padres.

---

## Actualización

```bash
cd /opt/protectado
sudo bash update.sh
```

El script obtiene la última versión, migra la base de datos y reinicia los servicios. La configuración (`config.json`) nunca se sobreescribe. Se realiza un rollback automático si el agente no reinicia correctamente.

---

## Resolución de problemas

### Reiniciar los servicios
```bash
sudo systemctl restart protectado-runner protectado-agent
```

### Ver lo que ocurre en directo
```bash
sudo journalctl -fu protectado-agent   # panel + supervisión
sudo journalctl -fu protectado-runner  # bloqueos Pi-hole
```

### Estado de los servicios
```bash
sudo systemctl status protectado-runner protectado-agent
```

## Privacidad

Ajustes en **Gestión → Privacidad**, y por perfil en **Perfiles**.

### Conservación

El historial (uso diario, registro de eventos, informes de IA, catálogo de dominios no
revisados a mano) se conserva **90 días por defecto** y luego se borra automáticamente en
la purga semanal. Configurable, incluido «ilimitado» — en cuyo caso no se borra nunca
nada, algo que la interfaz señala explícitamente.

> Por debajo de 31 días, la revisión mensual se queda sin materia y lo indica claramente
> en lugar de producir un informe vacío; por debajo de 8 días, la semanal hace lo mismo.

### Borrar el historial de un menor

**Perfiles → (editar) → Borrar el historial** elimina todo lo relativo a ese menor —uso,
línea temporal, eventos, excepciones— conservando su configuración y sus horarios. Se
pide de nuevo la contraseña. Al eliminar un perfil también se ofrece borrar su historial,
en vez de dejar datos sin forma de alcanzarlos.

### Nivel de privacidad

Cada perfil tiene un nivel, del que la edad solo es el **valor por defecto**:

| Nivel | Por defecto | Lo que el adulto puede reconstruir | Informes |
|---|---|---|---|
| Detallado | menos de 13 | Actividad en ventanas de 5 minutos | diario, semanal, mensual |
| Resumen | 13–15 | Agregados por media jornada | diario, semanal |
| Mínimo | 16+ | Totales del día, sin horarios | semanal |

**El nivel no cambia el bloqueo, ni los horarios, ni las alertas.** Solo cambia lo que se
puede consultar después. Un adulto preocupado conserva el acceso al detalle por horas de
un día concreto: se pide de nuevo la contraseña y la consulta queda inscrita en el
registro de eventos — visible para el adulto y para el menor en su propia página.

### Lo que el menor puede ver

Desde la red infantil, `protectado.admin` le muestra su modo de acceso actual, el horario
del día, qué se registra y durante cuánto tiempo, y si un adulto ha consultado el detalle
de su historial. Esa página **nunca** muestra el historial de navegación: un hermano puede
acceder desde la misma red.

### Compartir con la IA

**Gestión → Privacidad → Compartir datos con la IA.** Desactivado, ya no sale nada hacia
OpenRouter: ni chat, ni informes, ni clasificación por el modelo. El bloqueo, los horarios
y las alertas siguen igual. Lo que sale cuando está activado está seudonimizado —«Niño 1»,
una franja de edad, dominios y contadores; nunca un nombre, una edad exacta ni una IP.

---

### Reinicializar la base de datos
```bash
sudo systemctl stop protectado-agent protectado-runner
cd /opt/protectado && source .venv/bin/activate
rm data/protectado.db
python -c "import database; database.init_db(); print('OK')"
sudo systemctl start protectado-runner protectado-agent
```

### Reiniciar para reconfigurar
```bash
# Volver a mostrar el asistente (mantiene los valores)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot
# Reset total de fábrica (borra config, Wi-Fi guardado, estado detectado)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

---

## Referencia técnica

### Arquitectura detallada

```
[sandbox nono — Landlock]
  dashboard.py  (FastAPI :8080 interno — publicado en :80 por la capa root)
    ├── monitor.py     → hilo 60s, reglas deterministas sin IA
    └── claude_agent.py→ IA via OpenRouter, solo bajo demanda
    ↓ cola de acciones →
/tmp/fw-queue/
    ↓
action_runner.py (root, fuera del sandbox)
    → API Pi-hole (grupos, listas negras por modo)

[cron 23h — fuera del sandbox]
  daily_report.py → clasificación (hasta 10 pasadas de 60 dominios)
                  + informe diario (2 llamadas: informe y luego resumen)
```

**Volumen real**: hasta 12 llamadas a OpenRouter en un día normal, 13 los lunes (revisión
semanal) y 14 el día 1 de cada mes (revisión mensual). Las pasadas de clasificación se
detienen en cuanto no queda ningún dominio desconocido — en una red estabilizada suele
haber solo una o dos. Unas pocas llamadas al día con un modelo barato: el coste diario
sigue siendo bajo, pero no es nulo.

La supervisión rutinaria también puede llamar a la IA, en contadas ocasiones:
`monitor.py` registra un evento cuando un dominio desconocido se ve al menos 50 veces en
5 minutos (`UNUSUAL_QUERY_THRESHOLD`) y escala al modelo tras 3 eventos
(`ESCALATE_AFTER`). Sin clave de API, o con el envío a la IA desactivado, nada de esto
sale del equipo: el bloqueo y los horarios no dependen de ello.

### Seguridad (sandbox)

El agente corre en un sandbox Landlock (por eso el equipo usa Ubuntu Server — su núcleo
incluye Landlock). Solo puede acceder a:

| Recurso | Acceso |
|---|---|
| `/opt/protectado` | Lectura (`nono run --read`) |
| `/opt/protectado/data` | Lectura + escritura (configuración, base de datos, ficheros de estado) |
| `/tmp/fw-queue` | Escritura (cola de acciones al runner root) |
| Red — salida | `openrouter.ai` (informes y chat) · `cloudflare-dns.com`, `security.cloudflare-dns.com`, `family.cloudflare-dns.com` (clasificación gratuita de dominios desconocidos) |
| Red — puertos | 80 (panel), 81 (Pi-hole), 8080 (portal de configuración) |
| Todo lo demás | Bloqueado por el kernel |

El agente no accede ni a `/var/log/pihole` ni a `/etc/pihole`: pasa exclusivamente por la
API de Pi-hole, nunca por sus ficheros. El perfil se despliega en
`/etc/protectado/agent.json` — fuera del directorio de trabajo y, por tanto, fuera del
alcance del propio agente.

El detalle de lo que sale del equipo, y por qué, está en la sección
[Privacidad del README](../README.es.md#privacidad).

### Cambiar el modelo IA
En `config.json`:
```json
"openrouter": {
    "model": "anthropic/claude-sonnet-4-5"
}
```
Alternativas económicas: `mistralai/mistral-7b-instruct`, `meta-llama/llama-3-8b-instruct`

### Estructura de archivos

```
/opt/protectado/
├── data/                     ← Datos locales, nunca versionados
│   ├── config.json           ← Configuración (claves, perfiles, dispositivos)
│   ├── protectado.db         ← Base SQLite (eventos, dominios, uso)
│   ├── posture.json          ← Postura elegida en el arranque (gateway | dns_only)
│   ├── arp_scan.json         ← Último inventario ARP (dns_only)
│   ├── pairing_code          ← Código de emparejamiento del asistente (modo DNS)
│   └── update.trigger/.log   ← Disparador y registro de actualización
├── dashboard.py              ← Servidor web + supervisión (punto de entrada)
├── monitor.py                ← Hilo de supervisión DNS (60s)
├── claude_agent.py           ← IA bajo demanda via OpenRouter
├── scheduler.py              ← Horario por perfil
├── action_runner.py          ← Ejecutor root fuera del sandbox
├── domain_classifier.py      ← Categorización de dominios DNS
├── daily_report.py           ← Informe diario (cron)
├── access_control.py         ← Punto único de los derechos de acceso
├── device_grace.py           ← Periodo de gracia de nuevos dispositivos
├── pihole_api.py             ← Cliente API Pi-hole v6
├── arp_scanner.py            ← Inventario de red: Pi-hole FTL, completado en dns_only
│                                por el escaneo ARP del runner root (data/arp_scan.json)
├── privacy.py                ← Seudonimización de salidas, retención, niveles
├── database.py               ← Acceso SQLite
├── i18n/                     ← Traducciones (fr, en, es, pt)
├── protectado-agent.json     ← Perfil sandbox nono
├── bootstrap/bootstrap.sh    ← Instalación Y actualizaciones
├── bootstrap/net-common.sh   ← País Wi-Fi y detección de hardware compartidos
├── update.sh                 ← Actualización manual
└── templates/
    ├── index.html            ← Panel de control
    ├── devices.html          ← Dispositivos
    ├── admin_info.html       ← Recordatorio de dirección (red infantil)
    ├── login.html            ← Inicio de sesión
    └── onboarding.html       ← Asistente de primer arranque (DNS y pasarela)
```
