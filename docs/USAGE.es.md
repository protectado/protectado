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
> router de los niños: vea más abajo lo que cierra además, y los
> [modos de funcionamiento](../README.es.md#dos-modos-de-funcionamiento). El panel está en
> el puerto **80** (la interfaz de admin de Pi-hole pasa al **81**).

**Sin intervención de los padres**, Protectado aplica automáticamente el horario configurado: cortar el acceso de noche, pasar a modo trabajo después del colegio, reabrir por la noche.

**Bajo demanda**, el padre escribe en el chat del panel en lenguaje natural — la IA interpreta y actúa.

### Lo que cierra el modo pasarela

En modo pasarela, además del filtrado por nombre:

- **Corte a nivel de paquete**: un dispositivo cuyo acceso está cerrado ya no sale, y sus
  conexiones ya abiertas se cortan. Un dispositivo que no se conectó con la clave de
  ningún perfil no sale en absoluto.
- **DNS forzado**: una consulta DNS enviada a otro servidor (8.8.8.8 configurado a mano)
  se devuelve al equipo.
- **Resolutores cifrados rechazados**: DNS-over-TLS y DNS-over-QUIC (puerto 853) hacia
  cualquier servidor, y DNS-over-HTTPS hacia los resolutores públicos conocidos, por
  nombre y por dirección. La lista (`catalog/doh_resolvers.json`) se actualiza con el
  catálogo.
- **Dominios de elusión**: `use-application-dns.net` (Firefox), `mask.icloud.com` y
  `mask-h2.icloud.com` (iCloud Private Relay) responden «dominio inexistente».
- **IPv6**: ningún tráfico IPv6 pasa por la red de los niños.
- **Aviso**: cada intento rechazado se anota en el registro, una vez por dispositivo, por
  tipo y por día. Una aplicación puede hacerlo por sí sola: no es necesariamente un
  gesto del niño.

Lo que sigue siendo posible: una **VPN**, o un resolutor DNS-over-HTTPS ausente de la
lista, saca tráfico fuera del filtrado por nombre. Los horarios y los cortes siguen
aplicándose. Un dispositivo que envía casi todo su tráfico a una sola dirección durante
un cuarto de hora, sin pedir nombres al equipo, se indica en el registro como túnel probable
(solo aviso, sin corte).

### Lo que el equipo no puede ver

El equipo solo filtra lo que pasa por él. No ve:

- los **datos móviles** (4G/5G) del teléfono;
- el **punto de acceso** compartido desde otro teléfono;
- el **Wi-Fi del router familiar**, si el niño conoce su clave: su dispositivo puede
  conectarse directamente. En modo pasarela, reserve esa clave a los adultos, o cámbiela;
- en modo pasarela, una **VPN** o un resolutor cifrado ausente de la lista conocida sigue
  sacando tráfico fuera del filtrado por nombre (los horarios y los cortes siguen
  aplicándose). En modo DNS, un dispositivo configurado con otro DNS escapa al filtrado:
  el equipo lo detecta y lo indica.

Para estos casos, combine Protectado con los controles del propio teléfono: **Tiempo de
uso** en iPhone, **Family Link** en Android.

---

## Primer arranque

En el primer arranque, Protectado elige su modo automáticamente y abre un asistente
(ver [modos de funcionamiento](../README.es.md#dos-modos-de-funcionamiento)):

- **Modo DNS** (por defecto) — abre `http://protectado.local` y define la **contraseña de
  padre**. Es el único paso; el equipo queda listo.
- **Modo pasarela** (hardware compatible) — el equipo emite un Wi-Fi temporal
  `Protectado-Setup` con un portal cautivo que te guía para conectarlo a tu router de
  internet, nombrar el Wi-Fi de los niños y definir la contraseña de padre. Después te
  pide volver al Wi-Fi de casa y abrir `http://protectado.local` para terminar: la red
  temporal desaparece y esa dirección pasa a ser la del panel.

Los perfiles, los horarios y la clave API OpenRouter no se introducen en el asistente —
se añaden después desde el panel (pestaña Perfiles, y el panel de chat para la clave). Una
breve visita guiada explica cada pestaña en el primer inicio de sesión.

---

## Uso diario

### Panel de control

`http://protectado.local`  (interfaz de admin de Pi-hole: `http://protectado.local:81`)

Una pantalla por pregunta. Red es una página aparte, las otras cinco son pestañas:

| Pestaña | La pregunta que responde |
|---|---|
| **Estado actual** | ¿Qué está pasando ahora, y qué puedo hacer de inmediato? |
| **Niños** | ¿Cuáles son las reglas de este menor? |
| **Excepciones** | ¿Qué se aparta de esas reglas ahora mismo, y hasta cuándo? |
| **Red** | ¿Quién está conectado? |
| **Dominios** | ¿Qué hace la caja con este sitio? |
| **Ajustes** | La caja en sí, y el registro de lo que ha pasado. |

El **registro de decisiones** está en Ajustes. Solo muestra decisiones: excepciones,
ampliaciones, bloqueos manuales, cambios de perfil y de clave, historial borrado, modo
adulto, tomadas en pantalla o mediante el asistente. Los intentos bloqueados (a menudo
comprobaciones automáticas de los propios dispositivos) y los cambios de franja están en
el **Historial** de cada menor.

Dos de sus líneas son la excepción y permanecen en **Estado actual**, en una tarjeta
«Para mirar» que solo aparece si hay algo: un aparato que evita el DNS de la caja, y un
aparato asociado con una clave que no pertenece a ningún perfil. Son los dos únicos casos
en los que el filtrado no se aplica en absoluto a alguien. Un intento bloqueado no figura
ahí: es la caja haciendo su trabajo, y llegan docenas cada día. Una alerta cuya causa ha
desaparecido se borra sola al cabo de dos días, sin ningún botón que pulsar.

### Excepciones

La caja concede cinco clases de excepción temporal, y todas están aquí, con su vencimiento
y un botón para retirarlas antes de la hora:

- una **excepción temporal**, de unos minutos a unas horas;
- un **día completo** en un modo dado;
- una **prórroga** de la franja en curso («20 minutos más»);
- un **dominio abierto** temporalmente, normalmente concedido por el asistente;
- un **aparato fuera del filtrado** (modo adulto en un dispositivo compartido).

Las tres del medio no eran visibles en ninguna parte: concedidas, aplicadas y luego
vencidas sin que el adulto pudiera verlas ni retirarlas.

Las excepciones **por dominio** no están aquí: son permanentes y viven en la pestaña
Dominios, con el catálogo que corrigen. Esta pestaña solo habla de lo que tiene un final.

### Abrir la interfaz de Pi-hole

Su contraseña se genera durante la instalación y no está escrita en ningún otro sitio. Se
lee en **Ajustes → Interfaz de Pi-hole**, oculta por defecto y revelada por un botón, como
la clave Wi-Fi de un menor.

### Cuando el acceso está cortado, la caja dice cuándo se reabre

Una franja cerrada no dice «hasta las 23:59»: anuncia la hora de reapertura, el mismo día,
al día siguiente o el día de la semana correspondiente. Una excepción que corta tampoco
promete el regreso del acceso a su vencimiento si el horario está cerrado en ese momento.
Cuando el horario no abre en la semana que viene, la caja lo dice en lugar de inventar una
hora.

### Tema claro u oscuro

El panel, el asistente de instalación, la página de acceso y la página que se sirve a los
menores siguen el ajuste del sistema: claro por defecto, oscuro para quien tenga así su
teléfono o su ordenador. El botón ☀️/🌙 de la cabecera impone un tema y lo recuerda en ese
aparato, donde manda sobre el sistema en ambos sentidos.

La elección pertenece al navegador que la hace: no cambia nada para el resto de la
familia. Todos los colores pasan por fichas declaradas una sola vez en
`templates/_theme.html`; una pantalla que escribiera un color a mano saldría mal en uno de los
dos temas.

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
- su horario **día a día**, de lunes a domingo (franjas `off`, `homework`, `free`)
- anulaciones puntuales (vacaciones, excepción de noche…)

El perfil **monitoring** es especial: observa sin bloquear. Útil para supervisar un dispositivo compartido sin aplicarle reglas.

### Zona horaria

Todos los horarios del producto siguen la hora local del equipo: franjas, hora de dormir,
excepciones temporales, informe de la tarde. La zona horaria es por tanto determinante, y
se detecta **desde el navegador del adulto** durante el asistente de primer arranque, y
luego se aplica al sistema. Sin geolocalización y sin llamadas a un servicio externo.

Se puede cambiar después en **Ajustes → Hora del equipo**, línea
equipo». Conviene revisarla tras una mudanza, o si el equipo se configuró desde un
teléfono que estaba de viaje: una zona errónea desplaza en silencio todas las reglas.

---

### Una clave Wi-Fi por menor

En modo pasarela el equipo emite un solo Wi-Fi para los menores, pero **cada perfil tiene
su propia clave**. No es un detalle de comodidad: es ella la que identifica al menor.
hostapd indica al equipo qué clave se ha usado en la asociación, así que el aparato sigue
vinculado a su perfil **aunque cambie de dirección MAC**, cosa que hacen los teléfonos
recientes. La identificación por dirección dejaba pasar un aparato con MAC nueva como si
fuera un desconocido, es decir sin filtrado ni horarios.

La clave se crea con el perfil y se lee en **Menores → Modificar**, tantas veces como
haga falta: es una clave para dictar, no un secreto de un solo uso. Se sustituye en el
mismo sitio, por una clave generada o por la tuya.

Cambiar la clave de un menor desconecta **solo sus aparatos**, no los de los demás. El
Wi-Fi del router, el de los adultos, no se toca nunca.

Dos consecuencias a tener en cuenta:

- un aparato que no conoce ninguna clave **no entra en la red en absoluto**. Ya no hay
  aparato desconocido con acceso libre: el rechazo ocurre a nivel de radio;
- **mientras no exista ningún perfil de menor, el Wi-Fi de los menores no se emite**. Sin
  perfil no hay ninguna clave, y una red visible a la que nadie puede entrar sería peor
  que ninguna red. Aparece al crear el primer perfil.

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

### El catálogo de servicios se actualiza solo

El catálogo que vincula un dominio con un servicio — `googlevideo.com` es YouTube,
`nflxvideo.net` es Netflix — vive en `catalog/services.json`. Son datos, no código: cambia
porque un servicio añade un dominio de distribución, no porque el producto evolucione.

Una publicación que solo toca ese archivo se aplica por tanto **sin reinicio**: ni
reinstalación de dependencias, ni migración, ni corte de la red de los niños. El agente
vuelve a leer el catálogo en el ciclo siguiente, dentro del minuto. La caja no va a buscar
nada a ninguna otra parte: el catálogo viaja en la actualización que la caja ya consulta, y
no se emite ninguna llamada saliente adicional.

Para corregir o añadir una correspondencia en **su** caja sin esperar una publicación, cree
`data/services.local.json`:

```json
{
  "services": {
    "arte": {"label": "Arte", "category": "education",
             "domains": ["arte.tv", "artecdn.net"]}
  }
}
```

Este archivo se aplica **por encima** del catálogo entregado, servicio por servicio:
redefinir `youtube` reemplaza su lista de dominios completa, que es también la forma de
quitar uno. Sobrevive a las actualizaciones, y un error de sintaxis se registra y se ignora
sin impedir que la caja arranque. Las categorías admitidas son las de la rejilla de acceso:
`education`, `work`, `other`, `entertainment`, `social`, `adult`, `extremism`, `cdn`.

---


### Rama seguida

Un equipo sigue la rama anotada en `data/branch` (fuera del control de versiones, así que
las actualizaciones la conservan). Sin ese fichero se usa la rama extraída localmente y, en
último recurso, `stable` — la que consumen los equipos en funcionamiento. Cambiar de rama
exige escribir el fichero **y** realinear el repositorio:

```bash
cd /opt/protectado
SVC=$(stat -c %U /opt/protectado)
echo main | sudo -u "$SVC" tee data/branch
sudo -u "$SVC" git remote set-branches origin '*'
sudo -u "$SVC" git fetch origin --depth 1 main
sudo -u "$SVC" git checkout -B main origin/main
sudo -u "$SVC" git reset --hard origin/main
```

El `remote set-branches` solo hace falta una vez: la instalación clona una única rama y sin
él el repositorio local no conoce ninguna otra referencia remota.
`PROTECTADO_BRANCH=main sudo -E bash update.sh` fuerza una rama para una sola
actualización, sin fijar nada.

### Autorreparación al arrancar (dispositivo sin configurar)

Un fallo que impide abrir el asistente impide igualmente llegar a la interfaz desde la que se habría lanzado la actualización. Por eso, mientras el dispositivo no esté configurado, se pone al día por sí mismo: al arrancar, si hay un cable Ethernet activo, compara su versión con la publicada en la rama que sigue y vuelve a ejecutar `bootstrap.sh` en dos casos, existe una versión más reciente, o el asistente no responde aunque el código ya esté al día.

Se ejecuta `bootstrap.sh`, no `update.sh`: solo el primero regenera `/etc/protectado/agent.json` y las unidades systemd, que una simple alineación del código dejaría desincronizadas.

Tras tres intentos fallidos sobre una misma versión publicada, el dispositivo deja de reintentar para no insistir en cada arranque. Cualquier publicación nueva rearma la reparación. Sin cable Ethernet la comprobación se abandona de inmediato y el arranque no se ralentiza ni un segundo.

En un dispositivo ya configurado, nada de esto se ejecuta.

---

## Resolución de problemas

### El navegador abre una página HTTPS en lugar del panel

El panel se sirve por **HTTP**, en `http://protectado.local`. Si entra mientras la caja
está arrancando, el `:80` todavía no responde: el navegador prueba entonces el HTTPS por su
cuenta. Hasta esta versión aterrizaba en la interfaz de administración de Pi-hole, que
escuchaba el 443 sin motivo, con un certificado autofirmado.

Está corregido: Pi-hole solo conserva su `:81` en claro, y el ajuste se vuelve a aplicar
cada vez que la caja arranca, incluso en una caja ya instalada.

Si su navegador sigue insistiendo en el HTTPS para ese nombre, es que lo ha memorizado.
Escriba la dirección completa, `http://protectado.local`, o borre los datos del sitio en
sus ajustes.


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

Ajustes en **Ajustes → Privacidad**, y por menor en **Menores**.

### Conservación

El historial (uso diario, volumen de Internet diario y túneles probables, registro de eventos, informes de IA, catálogo de dominios no
revisados a mano) se conserva **90 días por defecto** y luego se borra automáticamente en
la purga semanal. Configurable, incluido «ilimitado» — en cuyo caso no se borra nunca
nada, algo que la interfaz señala explícitamente.

> Por debajo de 31 días, la revisión mensual se queda sin materia y lo indica claramente
> en lugar de producir un informe vacío; por debajo de 8 días, la semanal hace lo mismo.

### Borrar el historial de un menor

**Menores → Modificar → Borrar el historial** elimina todo lo relativo a ese menor —uso,
línea temporal, eventos, excepciones— conservando su configuración y sus horarios. Se
pide de nuevo la contraseña. Al eliminar un perfil también se ofrece borrar su historial,
en vez de dejar datos sin forma de alcanzarlos.

### Nivel de privacidad

Cada perfil tiene un nivel, del que la franja de edad solo es el **valor por defecto**.
Las cuatro franjas del producto son **6-9, 10-12, 13-15 y 16+**, y son las únicas:
sirven tanto aquí como para calibrar el tono de los informes.

| Nivel | Por defecto | Lo que el adulto puede reconstruir | Informes |
|---|---|---|---|
| Detallado | 6-9 y 10-12 | Actividad en ventanas de 5 minutos | diario, semanal, mensual |
| Resumen | 13-15 | Agregados por media jornada | diario, semanal |
| Mínimo | 16+ | Totales del día, sin horarios | semanal |

**El nivel no cambia el bloqueo, ni los horarios, ni las alertas.** Solo cambia lo que se
puede consultar después. Un adulto preocupado conserva el acceso al detalle por horas de
un día concreto: **Menores → Modificar → Ver el detalle de un día**. Se pide de nuevo la
contraseña, el alcance se limita a la fecha elegida y la consulta queda inscrita en el
registro de eventos del adulto. La pantalla agrupa las ventanas de 5 minutos en tramos
continuos, para responder a la pregunta que se hace de verdad: de qué hora a qué hora.

El asistente conversacional sigue sujeto al nivel del perfil: indica la granularidad de
la que dispone y no deduce ningún horario de la planificación, que dice lo que estaba
permitido y no lo que se usó.

### Lo que el menor puede ver

Desde la red infantil, `protectado.admin` le muestra su modo de acceso actual, el horario
del día, y qué se registra y durante cuánto tiempo. Esa página **nunca** muestra el
historial de navegación: un hermano puede acceder desde la misma red.

### Compartir con la IA

**Ajustes → Privacidad → Compartir datos con la IA.** Desactivado, ya no sale nada hacia
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
    ├── claude_agent.py→ IA via OpenRouter, solo bajo demanda
    └── API Pi-hole :81 → consultas DNS, dispositivos, grupos, listas de bloqueo
    ↓ cola de acciones →
/tmp/fw-queue/
    ↓
action_runner.py (root, fuera del sandbox)
    → API Pi-hole (grupos, listas negras por modo)

[cron 23h — fuera del sandbox]
  daily_report.py → clasificación (hasta 10 pasadas de 60 dominios)
                  + informe diario (2 llamadas: informe y luego resumen)
```

El proceso dentro del sandbox también habla con Pi-hole, a través de su API en el puerto
81: lee las consultas DNS y la lista de dispositivos, cambia el grupo de un dispositivo y
sincroniza las listas de bloqueo. El perfil de nono autoriza ese puerto de forma
explícita. El runner root se encarga de lo que el sandbox prohíbe: cortafuegos, punto de
acceso Wi-Fi, servicios del sistema.

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

La política de red la aplica **el propio Landlock** (`nono run --sandbox-policy
landlock`), no el modo `auto` de nono. En `auto`, nono complementa Landlock con una base
seccomp estática para la red: incapaz de expresar una regla por puerto, deja pasar el
proxy y rechaza el resto, incluidos los puertos que el perfil autoriza. Al agente se le
negaba entonces tanto escuchar en el 8080 como llamar a la API de Pi-hole. Este modo
exige un kernel con ABI Landlock V4 o posterior, y se niega a arrancar si no la hay: más
vale un servicio que se detiene y lo dice que un dispositivo funcionando sin sandbox.

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
├── modes.py                  ← El vocabulario de los modos de acceso, declarado una vez
├── access_grid.py            ← Lo que permite cada modo, por franja de edad y por menor
├── services.py               ← La lógica de agrupación de servicios (la lista está en
│                                catalog/, no aquí)
├── catalog/services.json     ← El catálogo entregado: servicios, etiquetas, dominios. Son
│                                DATOS, editables sin saber Python. Un complemento local
│                                opcional (data/services.local.json) se aplica encima
├── action_runner.py          ← Ejecutor root fuera del sandbox
├── domain_classifier.py      ← Categorización de dominios DNS
├── daily_report.py           ← Informe diario (cron)
├── access_control.py         ← Punto único de los derechos de acceso
├── wifi_keys.py              ← Una clave Wi-Fi por perfil, servida a hostapd
├── station_identity.py       ← Quién está detrás de una dirección: la clave, no la MAC
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
    ├── admin_info.html       ← Recordatorio de dirección (red infantil)
    ├── login.html            ← Inicio de sesión
    └── onboarding.html       ← Asistente de primer arranque (DNS y pasarela)
```
