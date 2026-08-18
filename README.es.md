[🇬🇧 English](README.md) | [🇫🇷 Français](README.fr.md) | [🇪🇸 Español](README.es.md) | [🇵🇹 Português](README.pt.md)

# Protectado

**Supervisión inteligente de la red familiar — automática, adaptativa, respetuosa.**

Protectado es un sistema de control parental de red, de código abierto,
pensado para padres de adolescentes. Funciona en una Raspberry Pi conectada
a tu red doméstica y gestiona automáticamente el acceso a internet de tus
hijos — sin que tengas que vigilar manualmente cada sitio o aplicación nueva.

---

## ¿Por qué Protectado?

Los adolescentes navegan por cientos de dominios al día. Las herramientas de
control parental tradicionales dependen de listas estáticas que los niños
esquivan en minutos. Los padres no tienen tiempo de seguir la evolución
constante de la web.

Protectado resuelve esto de otra manera: **aprende, categoriza y bloquea de
forma dinámica**, sin intervención manual. Cada dominio nuevo visitado se
analiza automáticamente y se clasifica según su contenido. Las reglas se
aplican en tiempo real, se adaptan a nuevas plataformas y te mantienen
informado de lo que ocurre — para que puedas tener las conversaciones
adecuadas con tu hijo en vez de perseguir formas de evadir el control.

---

## Qué hace Protectado

- **Bloqueo dinámico** — Los dominios visitados se categorizan automáticamente,
  sin listas que mantener a mano. La categorización funciona en continuo y las nuevas
  reglas se aplican en el siguiente cambio de franja horaria
- **Horarios** — Acceso restringido de noche, modo trabajo durante los
  deberes, modo libre el fin de semana — se configuran una vez y se aplican
  automáticamente
- **Informes diarios** — Resumen inteligente en lenguaje natural del día
  digital de tu hijo
- **Alertas contextuales** — Detección de intentos de evadir el DNS,
  patrones inusuales, contenidos preocupantes
- **Agente IA** — Haz preguntas en lenguaje natural y obtén respuestas
  claras. Da instrucciones: "bloquea TikTok a Alicia", "permite Signal esta
  noche"
- **Visibilidad total** — Panel en tiempo real por dispositivo y por hijo

---

## Lo que Protectado no es

Protectado observa los patrones de navegación de red — no el contenido de
los mensajes privados ni las conversaciones de tus hijos. Actúa a nivel de
DNS: sabe que tu hijo visitó YouTube, no qué vio allí.

El objetivo no es la vigilancia total sino **un entorno digital sano y
predecible** — reglas claras, aplicadas automáticamente, que dejan espacio
para la confianza y el diálogo.

---

## Arquitectura

Protectado se apoya en [Pi-hole](https://pi-hole.net) como motor DNS,
enriquecido con una capa de inteligencia artificial para la clasificación
y el análisis.

### Dos modos de funcionamiento

Protectado funciona en uno de dos modos, elegido **automáticamente** en el primer
arranque — tú no eliges, se adapta a aquello en lo que se instala:

- **Modo DNS** (por defecto) — Protectado filtra el DNS en tu red existente. Instálalo en
  una Raspberry Pi o en cualquier máquina Linux que ya tengas encendida de forma continua
  (un NAS, un mini-PC). Tu router dirige los dispositivos hacia él para la resolución de
  nombres. Funciona en todas partes, sin hardware adicional.
- **Modo pasarela** (avanzado) — Protectado se convierte en el router de los niños:
  emite un Wi-Fi dedicado para ellos y filtra **cada** conexión, no solo el DNS — mucho
  más difícil de eludir. Este modo requiere **hardware compatible**.

Por defecto, el equipo se instala en **modo DNS**, y solo cambia a modo pasarela por sí
mismo cuando detecta hardware compatible.

### Hardware

| Modo | Hardware | Capacidades |
|------|----------|-------------|
| **DNS** (por defecto) | Cualquier Raspberry Pi (2W / 3 / 4 / 5) o una máquina Linux encendida de forma continua | Bloqueo DNS dinámico, informes IA, horarios |
| **Pasarela** (avanzado) | Raspberry Pi 4 / 5 con hardware Wi-Fi compatible | Lo anterior **+** filtrado a nivel de paquete y un Wi-Fi dedicado y filtrado para los niños |

> El equipo se instala en modo DNS por defecto y cambia a modo pasarela por sí mismo
> cuando detecta hardware compatible.

### Componentes de software
```
protectado-client    Este repositorio — funciona en tu Raspberry Pi
protectado-server    Servidor central (clasificación compartida, anónima) — previsto
protectado.com       Sitio web y documentación
```

---

## Puesta en marcha

Tres pasos, como en [protectado.com](https://protectado.com): enchufar, configurar, olvidar.
El camino para llegar depende de tu perfil.

### Community (gratis, autoalojado)

1. **Enchufar** — flashea una tarjeta SD con Ubuntu Server (64 bits) y conecta el Pi
   a tu red doméstica. Guía paso a paso completa: [bootstrap/INSTALL.es.md](bootstrap/INSTALL.es.md)
2. **Instalar** — conéctate por SSH y ejecuta:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
   ```
   Instalación automática de Pi-hole y Protectado (5 a 10 minutos).
3. **Configurar** — un asistente se abre solo en el primer arranque. En **modo DNS**,
   abre `http://protectado.local` y define tu contraseña de padre; en **modo pasarela**,
   el equipo emite un Wi-Fi temporal `Protectado-Setup` que te guía para conectarlo a tu
   router de internet y nombrar el Wi-Fi de los niños. Los perfiles y horarios se crean
   después desde el panel.

> **Requisitos**: Raspberry Pi (2W, 3, 4 o 5) o cualquier máquina Linux encendida de
> forma continua · Ubuntu Server recomendado (el agente de IA se ejecuta en una sandbox
> Landlock) · Conexión a tu red doméstica

---

## Privacidad

El filtrado se hace **a nivel de DNS**: Protectado ve los *nombres* de los sitios
solicitados, nunca su contenido, nunca los mensajes, nunca las contraseñas. Nada se
envía a un servidor central de Protectado — no existe ninguno.

Dos cosas salen del dispositivo, ambas opcionales:

| Qué sale | Hacia dónde | Cuándo | Qué contiene |
|---|---|---|---|
| Nombres de dominio | Resolutores Cloudflare DoH (`1.1.1.1`, `1.1.1.2`, `1.1.1.3`) | Al clasificar un dominio desconocido | Solo el nombre de dominio — sin perfil, sin dispositivo, sin horario |
| Datos de uso seudonimizados | OpenRouter (el modelo de IA que elijas) | Informes y chat con los padres, **solo si configuras una clave de API** | `Niño 1`, una *franja* de edad (`13-15`), dominios y contadores. Nunca un nombre, una edad exacta, una dirección IP o MAC, ni un mensaje de evento en claro |

**La IA es totalmente opcional.** Sin clave de API, Protectado bloquea, planifica, alerta
y sigue la actividad exactamente igual — simplemente no tendrás informes redactados ni
chat. También puedes conservar la clave y desactivar el envío en cualquier momento en
**Gestión → Privacidad**.

**Conservación.** El historial se guarda 90 días por defecto y luego se borra
automáticamente — configurable, incluido «ilimitado» si lo eliges deliberadamente. El
historial de cada menor puede borrarse individualmente en cualquier momento, y cada
perfil tiene un *nivel de privacidad* que limita con qué detalle se puede reconstruir su
actividad pasada. El bloqueo, los horarios y las alertas de seguridad nunca se ven
afectados por ese nivel.

**El menor también puede verlo.** Desde la red infantil, `protectado.admin` le muestra su
modo de acceso actual, el horario del día, qué se registra y durante cuánto tiempo — y le
avisa cuando un adulto ha consultado el detalle de su historial.

---

## Documentación

Para el uso diario (comandos de chat, modos de acceso, copia de
seguridad/restauración) y la referencia técnica (seguridad del sandbox,
estructura de archivos), consulta [docs/USAGE.es.md](docs/USAGE.es.md).

---

## Licencia

Protectado está disponible bajo dos licencias:

- **Uso personal / código abierto**: GNU AGPL v3 — ver [LICENSE](LICENSE)
- **Uso comercial**: Licencia Comercial de Protectado —
  ver [LICENSE-COMMERCIAL](LICENSE-COMMERCIAL) · arnaud@barbed.fr

Copyright (C) 2026 Arnaud Ortais

Protectado usa [Pi-hole](https://pi-hole.net), licenciado bajo EUPL v1.2.
