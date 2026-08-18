[🇬🇧 English](README.md) | [🇫🇷 Français](README.fr.md) | [🇪🇸 Español](README.es.md) | [🇵🇹 Português](README.pt.md)

# Protectado

**Supervisão inteligente da rede familiar — automática, adaptativa, cuidadosa.**

Protectado é um sistema de controlo parental de rede, de código aberto,
pensado para pais de adolescentes. Corre num Raspberry Pi ligado à sua rede
doméstica e gere automaticamente o acesso à internet dos seus filhos — sem
que tenha de vigiar manualmente cada novo site ou aplicação.

---

## Porquê o Protectado?

Os adolescentes navegam por centenas de domínios por dia. As ferramentas de
controlo parental tradicionais assentam em listas estáticas que os filhos
contornam em minutos. Os pais não têm tempo para acompanhar a evolução
constante da web.

O Protectado resolve isto de forma diferente: **aprende, categoriza e
bloqueia dinamicamente**, sem intervenção manual. Cada novo domínio visitado
é analisado automaticamente e classificado pelo seu conteúdo. As regras
aplicam-se em tempo real, adaptam-se a novas plataformas e mantêm-no
informado do que se passa — para que possa ter as conversas certas com o
seu filho em vez de perseguir contornos.

---

## O que o Protectado faz

- **Bloqueio dinâmico** — Os domínios visitados são categorizados automaticamente,
  sem lista nenhuma a manter manualmente. A categorização corre em contínuo e as novas
  regras entram em vigor na mudança de horário seguinte
- **Horários** — Acesso restrito à noite, modo trabalho durante os
  deveres, modo livre ao fim de semana — configurados uma vez, aplicados
  automaticamente
- **Relatórios diários** — Resumo inteligente em linguagem natural do dia
  digital do seu filho
- **Alertas contextuais** — Deteção de tentativas de contornar o DNS,
  padrões invulgares, conteúdos preocupantes
- **Agente de IA** — Faça perguntas em linguagem natural e obtenha
  respostas claras. Dê instruções: "bloqueia o TikTok à Alice", "permite o
  Signal esta noite"
- **Visibilidade total** — Painel em tempo real por dispositivo e por filho

---

## O que o Protectado não é

O Protectado observa os padrões de navegação de rede — não o conteúdo das
mensagens privadas nem as conversas dos seus filhos. Atua ao nível do DNS:
sabe que o seu filho visitou o YouTube, não o que viu lá.

O objetivo não é a vigilância total, mas sim **um ambiente digital saudável
e previsível** — regras claras, aplicadas automaticamente, que deixam espaço
para a confiança e o diálogo.

---

## Arquitetura

O Protectado assenta no [Pi-hole](https://pi-hole.net) como motor DNS,
enriquecido com uma camada de inteligência artificial para classificação e
análise.

### Dois modos de funcionamento

O Protectado funciona num de dois modos, escolhido **automaticamente** no primeiro
arranque — não escolhe, ele adapta-se àquilo em que é instalado:

- **Modo DNS** (predefinido) — o Protectado filtra o DNS na sua rede existente. Instale-o
  num Raspberry Pi ou em qualquer máquina Linux que já tenha ligada em permanência (um
  NAS, um mini-PC). O seu router encaminha os dispositivos para ele para a resolução de
  nomes. Funciona em todo o lado, sem hardware adicional.
- **Modo gateway** (avançado) — o Protectado torna-se o router das crianças: emite um
  Wi-Fi dedicado para elas e filtra **cada** ligação, não apenas o DNS — muito mais
  difícil de contornar. Este modo requer **hardware compatível**.

Por predefinição, o equipamento instala-se em **modo DNS**, e só muda para modo gateway
por si próprio quando deteta hardware compatível.

### Hardware

| Modo | Hardware | Capacidades |
|------|----------|-------------|
| **DNS** (predefinido) | Qualquer Raspberry Pi (2W / 3 / 4 / 5) ou uma máquina Linux ligada em permanência | Bloqueio DNS dinâmico, relatórios de IA, horários |
| **Gateway** (avançado) | Raspberry Pi 4 / 5 com hardware Wi-Fi compatível | O acima **+** filtragem ao nível do pacote e um Wi-Fi dedicado e filtrado para as crianças |

> O equipamento instala-se em modo DNS por predefinição e muda para modo gateway por si
> próprio quando deteta hardware compatível.

### Componentes de software
```
protectado-client    Este repositório — corre no seu Raspberry Pi
protectado-server    Servidor central (classificação partilhada, anónima) — previsto
protectado.com       Site e documentação
```

---

## Como começar

Três passos, como em [protectado.com](https://protectado.com): ligar, configurar, esquecer.
O caminho até lá depende do seu perfil.

### Community (grátis, autoalojado)

1. **Ligar** — grave um cartão SD com Ubuntu Server (64 bits) e ligue o Pi
   à sua rede doméstica. Guia completo passo a passo: [bootstrap/INSTALL.pt.md](bootstrap/INSTALL.pt.md)
2. **Instalar** — ligue-se por SSH e execute:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
   ```
   Instalação automática do Pi-hole e do Protectado (5 a 10 minutos).
3. **Configurar** — um assistente abre-se sozinho no primeiro arranque. Em **modo DNS**,
   abra `http://protectado.local` e defina a sua palavra-passe de pai/mãe; em **modo
   gateway**, o equipamento emite um Wi-Fi temporário `Protectado-Setup` que o orienta
   para o ligar ao seu router de internet e dar nome ao Wi-Fi das crianças. Os perfis e
   horários criam-se depois no painel.

> **Requisitos**: Raspberry Pi (2W, 3, 4 ou 5) ou qualquer máquina Linux ligada em
> permanência · Ubuntu Server recomendado (o agente de IA corre numa sandbox Landlock) ·
> Ligação à sua rede doméstica

---

## Privacidade

A filtragem é feita **ao nível do DNS**: o Protectado vê os *nomes* dos sites pedidos,
nunca o seu conteúdo, nunca as mensagens, nunca as palavras-passe. Nada é enviado para um
servidor central do Protectado — não existe nenhum.

Duas coisas saem do equipamento, ambas opcionais:

| O que sai | Para onde | Quando | O que contém |
|---|---|---|---|
| Nomes de domínio | Resolvedores Cloudflare DoH (`1.1.1.1`, `1.1.1.2`, `1.1.1.3`) | Ao classificar um domínio desconhecido | Apenas o nome de domínio — sem perfil, sem equipamento, sem horário |
| Dados de utilização pseudonimizados | OpenRouter (o modelo de IA que escolher) | Relatórios e conversa com os pais, **apenas se configurar uma chave de API** | `Criança 1`, um *escalão* etário (`13-15`), domínios e contadores. Nunca um nome, uma idade exata, um endereço IP ou MAC, nem uma mensagem de evento em texto simples |

**A IA é totalmente opcional.** Sem chave de API, o Protectado bloqueia, planeia, alerta e
acompanha a atividade exatamente da mesma forma — simplesmente não terá relatórios
redigidos nem conversa. Também pode manter a chave e desligar a partilha a qualquer
momento em **Gestão → Privacidade**.

**Conservação.** O histórico é guardado 90 dias por predefinição e depois apagado
automaticamente — configurável, incluindo «ilimitado» se o escolher deliberadamente. O
histórico de cada criança pode ser apagado individualmente a qualquer momento, e cada
perfil tem um *nível de privacidade* que limita o detalhe com que a sua atividade passada
pode ser reconstituída. O bloqueio, os horários e os alertas de segurança nunca são
afetados por esse nível.

**A criança também pode ver.** A partir da rede das crianças, `protectado.admin` mostra-lhe
o seu modo de acesso atual, o horário do dia, o que é registado e durante quanto tempo — e
informa-a quando um adulto consultou o detalhe do seu histórico.

---

## Documentação

Para o uso diário (comandos de chat, modos de acesso, cópia de
segurança/restauro) e a referência técnica (segurança do sandbox, estrutura
de ficheiros), consulte [docs/USAGE.pt.md](docs/USAGE.pt.md).

---

## Licença

O Protectado está disponível sob duas licenças:

- **Uso pessoal / código aberto**: GNU AGPL v3 — ver [LICENSE](LICENSE)
- **Uso comercial**: Licença Comercial do Protectado —
  ver [LICENSE-COMMERCIAL](LICENSE-COMMERCIAL) · arnaud@barbed.fr

Copyright (C) 2026 Arnaud Ortais

O Protectado usa [Pi-hole](https://pi-hole.net), licenciado sob EUPL v1.2.
