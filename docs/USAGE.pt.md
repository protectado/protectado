[🇬🇧 English](USAGE.md) | [🇫🇷 Français](USAGE.fr.md) | [🇪🇸 Español](USAGE.es.md) | [🇵🇹 Português](USAGE.pt.md)

# Protectado — Guia de utilização e referência técnica

Para a instalação, consulte o [README](../README.pt.md#como-começar) e o
[guia de instalação detalhado](../bootstrap/INSTALL.pt.md).

---

## Como funciona

```
WiFi (router)
    ↓ todo o tráfego DNS passa por →
Pi-hole  (instalado e configurado pelo bootstrap)
    ↓ logs + API →
Protectado  (painel :80 + supervisão automática)
    ↓ bloqueio DNS →
grupos Pi-hole por perfil e modo

Todas as noites às 23h:
  relatório diário gerado via OpenRouter
```

> Este é o **modo DNS** (predefinido). Em **modo gateway**, o equipamento é também o router
> das crianças e filtra ao nível do pacote, não apenas o DNS — ver
> [modos de funcionamento](../README.pt.md#dois-modos-de-funcionamento). O painel está na
> porta **80** (a interface de admin do Pi-hole passa para a **81**).

**Sem intervenção dos pais**, o Protectado aplica automaticamente o horário configurado: cortar o acesso de noite, passar para modo trabalho após a escola, reabrir à noite.

**Sob pedido**, o pai escreve no chat do painel em linguagem natural — a IA interpreta e age.

---

## Primeiro arranque

No primeiro arranque, o Protectado escolhe o seu modo automaticamente e abre um assistente
(ver [modos de funcionamento](../README.pt.md#dois-modos-de-funcionamento)):

- **Modo DNS** (predefinido) — abra `http://protectado.local` e defina a **palavra-passe
  de pai/mãe**. É o único passo; o equipamento fica pronto.
- **Modo gateway** (hardware compatível) — o equipamento emite um Wi-Fi temporário
  `Protectado-Setup` com um portal cativo que o orienta para o ligar ao seu router de
  internet, dar nome ao Wi-Fi das crianças e definir a palavra-passe de pai/mãe.

Os perfis, os horários e a chave API OpenRouter não são inseridos no assistente —
adicionam-se depois no painel (separador Perfis, e o painel de conversa para a chave). Uma
breve visita guiada explica cada separador no primeiro início de sessão.

---

## Utilização diária

### Painel de controlo

`http://protectado.local`  (interface de admin do Pi-hole: `http://protectado.local:81`)

- Estado em tempo real de cada perfil (dispositivos ativos, modo atual, próxima franja)
- Histórico de eventos (bloqueios, alertas, mudanças de modo)
- Catálogo de domínios visitados e a sua categoria

### Chat para pais

A funcionalidade principal: escrever o que se quer fazer, a IA trata do resto.

| O que escreve | O que faz |
|---|---|
| "Corta o internet à Alice, ela tem de dormir" | Bloqueia imediatamente todos os seus dispositivos |
| "Autoriza o YouTube à Alice durante 30 minutos" | Desbloqueia youtube.com 30 min e volta a bloquear |
| "Dá mais 45 minutos à Alice esta noite" | Adia o fim da franja atual |
| "Amanhã a Alice está de férias, modo livre" | Dia completo sem restrições (exceto conteúdo adulto) |
| "Bloqueia tudo à Alice no sábado" | Dia completo bloqueado |
| "khanacademy.org é educativo" | Recategoriza o domínio — nunca bloqueado em modo trabalho |
| "Bloqueia twitch.tv mesmo em modo permissivo" | Lista negra permanente |
| "Porque é que o YouTube estava acessível ontem à tarde?" | Explica que regra se aplicava nesse momento. O detalhe da resposta depende do *nível de privacidade* do perfil (ver abaixo) |

### Modos de acesso

| Modo | O que está acessível |
|---|---|
| **Bloqueado** | Nada — corte de rede completo |
| **Trabalho** | Educação, ferramentas escolares. YouTube, redes sociais e conteúdo adulto bloqueados |
| **Livre** | Tudo exceto conteúdo adulto |

A mudança de modo é automática conforme o horário. Pode ser substituída a qualquer momento a partir do chat ou do painel.

---

## Perfis

Cada filho tem o seu próprio perfil com:
- os seus dispositivos (IPs fixos recomendados)
- o seu horário **dia a dia**, de segunda a domingo (franjas `blocked`, `work`, `permissive`)
- substituições pontuais (férias, exceção de noite…)

O perfil **monitoring** é especial: observa sem bloquear. Útil para supervisionar um dispositivo partilhado sem lhe aplicar regras.

### Fuso horário

Todos os horários do produto seguem a hora local do equipamento: períodos, hora de
deitar, exceções temporárias, relatório da noite. O fuso é por isso determinante, e é
detetado **a partir do navegador do adulto** durante o assistente de primeiro arranque, e
depois aplicado ao sistema. Sem geolocalização e sem chamadas a um serviço externo.

Pode ser alterado depois em **Gestão → Modo atual por perfil**, linha «Hora do
equipamento». Vale a pena verificar após uma mudança de casa, ou se o equipamento foi
configurado a partir de um telemóvel em viagem: um fuso errado desloca silenciosamente
todas as regras.

---

## Modo adulto em dispositivo partilhado

Se um filho usa um dispositivo partilhado (TV, tablet familiar), o pai pode mudar temporariamente o dispositivo para modo adulto sem tocar no perfil do filho.

No painel: botão **Modo adulto** → palavra-passe do pai → duração. O dispositivo volta automaticamente ao perfil do filho ao expirar.

---

## Relatório diário

Todas as noites às 23h, o Protectado envia automaticamente via OpenRouter:
- a categorização dos novos domínios desconhecidos
- um resumo do dia: tempo por domínio, alertas, bloqueios

O relatório aparece no painel (secção Eventos) e nos logs.

Para o acionar manualmente:
```bash
cd /opt/protectado && .venv/bin/python daily_report.py
```

---

## Cópia de segurança e restauro

O painel permite guardar e restaurar a configuração com um clique.

- **Cópia de segurança**: botão no painel → descarrega um ZIP (`config.json` + base de dados)
- **Restaurar**: carregar o ZIP → configuração recarregada em tempo real, sem reinício

> ⚠️ O ZIP contém **segredos em texto simples**: palavra-passe dos pais, chave da API de IA e, em modo gateway, as chaves Wi-Fi. Tanto a transferência como o restauro exigem introduzir novamente a palavra-passe dos pais.

---

## Atualização

```bash
cd /opt/protectado
sudo bash update.sh
```

O script obtém a versão mais recente, migra a base de dados e reinicia os serviços. A configuração (`config.json`) nunca é sobrescrita. É feito um rollback automático se o agente não reiniciar corretamente.

---

## Resolução de problemas

### Reiniciar os serviços
```bash
sudo systemctl restart protectado-runner protectado-agent
```

### Ver o que acontece em direto
```bash
sudo journalctl -fu protectado-agent   # painel + supervisão
sudo journalctl -fu protectado-runner  # bloqueios Pi-hole
```

### Estado dos serviços
```bash
sudo systemctl status protectado-runner protectado-agent
```

## Privacidade

Definições em **Gestão → Privacidade**, e por perfil em **Perfis**.

### Conservação

O histórico (utilização diária, registo de eventos, relatórios de IA, catálogo de
domínios não revistos à mão) é guardado **90 dias por predefinição** e depois apagado
automaticamente pela limpeza semanal. Configurável, incluindo «ilimitado» — caso em que
nada é jamais apagado, o que a interface assinala explicitamente.

> Abaixo de 31 dias, a revisão mensal fica sem matéria e di-lo claramente em vez de
> produzir um relatório vazio; abaixo de 8 dias, a semanal faz o mesmo.

### Apagar o histórico de uma criança

**Perfis → (editar) → Apagar o histórico** elimina tudo o que diz respeito a essa criança
— utilização, linha temporal, eventos, exceções — mantendo a configuração e os horários. A
palavra-passe é pedida novamente. Ao eliminar um perfil também é proposto apagar o seu
histórico, em vez de deixar dados sem forma de lhes chegar.

### Nível de privacidade

Cada perfil tem um nível, do qual a idade é apenas a **predefinição**:

| Nível | Predefinição | O que o adulto pode reconstituir | Relatórios |
|---|---|---|---|
| Detalhado | menos de 13 | Atividade em janelas de 5 minutos | diário, semanal, mensal |
| Resumo | 13–15 | Agregados por meio-dia | diário, semanal |
| Mínimo | 16+ | Totais do dia, sem horários | semanal |

**O nível não altera o bloqueio, nem os horários, nem os alertas.** Altera apenas o que
pode ser consultado depois. Um adulto preocupado mantém o acesso ao detalhe hora a hora de
um dia concreto: a palavra-passe é pedida de novo e a consulta fica inscrita no registo de
eventos — visível para o adulto e para a criança na sua própria página.

### O que a criança pode ver

A partir da rede das crianças, `protectado.admin` mostra-lhe o modo de acesso atual, o
horário do dia, o que é registado e durante quanto tempo, e se um adulto consultou o
detalhe do seu histórico. Essa página **nunca** mostra o histórico de navegação: um irmão
pode aceder-lhe a partir da mesma rede.

### Partilhar com a IA

**Gestão → Privacidade → Partilhar dados com a IA.** Desativado, deixa de sair o que quer
que seja para o OpenRouter: nem conversa, nem relatórios, nem classificação pelo modelo. O
bloqueio, os horários e os alertas continuam iguais. O que sai quando está ativo está
pseudonimizado — «Criança 1», um escalão etário, domínios e contadores; nunca um nome, uma
idade exata ou um endereço IP.

---

### Reinicializar a base de dados
```bash
sudo systemctl stop protectado-agent protectado-runner
cd /opt/protectado && source .venv/bin/activate
rm data/protectado.db
python -c "import database; database.init_db(); print('OK')"
sudo systemctl start protectado-runner protectado-agent
```

### Reiniciar para reconfigurar
```bash
# Voltar a mostrar o assistente (mantém os valores)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot
# Reset total de fábrica (apaga config, Wi-Fi guardado, estado detetado)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

---

## Referência técnica

### Arquitetura detalhada

```
[sandbox nono — Landlock]
  dashboard.py  (FastAPI :8080 interno — publicado em :80 pela camada root)
    ├── monitor.py     → thread 60s, regras deterministas sem IA
    └── claude_agent.py→ IA via OpenRouter, apenas sob pedido
    ↓ fila de ações →
/tmp/fw-queue/
    ↓
action_runner.py (root, fora do sandbox)
    → API Pi-hole (grupos, listas negras por modo)

[cron 23h — fora do sandbox]
  daily_report.py → classificação (até 10 passagens de 60 domínios)
                  + relatório diário (2 chamadas: relatório e depois resumo)
```

**Volume real**: até 12 chamadas ao OpenRouter num dia normal, 13 às segundas-feiras
(revisão semanal) e 14 no dia 1 de cada mês (revisão mensal). As passagens de
classificação param assim que não resta nenhum domínio desconhecido — numa rede
estabilizada há muitas vezes só uma ou duas. Algumas chamadas por dia num modelo barato:
o custo diário continua baixo, mas não é nulo.

A supervisão de rotina também pode chamar a IA, raramente: `monitor.py` regista um
evento quando um domínio desconhecido é visto pelo menos 50 vezes em 5 minutos
(`UNUSUAL_QUERY_THRESHOLD`) e escala para o modelo ao fim de 3 eventos
(`ESCALATE_AFTER`). Sem chave de API, ou com a partilha com a IA desativada, nada disto
sai do equipamento: o bloqueio e os horários não dependem disso.

### Segurança (sandbox)

O agente corre num sandbox Landlock (por isso o equipamento usa Ubuntu Server — o seu
núcleo inclui Landlock). Só pode aceder a:

| Recurso | Acesso |
|---|---|
| `/opt/protectado` | Leitura (`nono run --read`) |
| `/opt/protectado/data` | Leitura + escrita (configuração, base de dados, ficheiros de estado) |
| `/tmp/fw-queue` | Escrita (fila de ações para o runner root) |
| Rede — saída | `openrouter.ai` (relatórios e conversa) · `cloudflare-dns.com`, `security.cloudflare-dns.com`, `family.cloudflare-dns.com` (classificação gratuita de domínios desconhecidos) |
| Rede — portas | 80 (painel), 81 (Pi-hole), 8080 (portal de configuração) |
| Todo o resto | Bloqueado pelo kernel |

O agente não acede nem a `/var/log/pihole` nem a `/etc/pihole`: passa exclusivamente pela
API do Pi-hole, nunca pelos seus ficheiros. O perfil é instalado em
`/etc/protectado/agent.json` — fora do diretório de trabalho e, portanto, fora do alcance
do próprio agente.

O detalhe do que sai do equipamento, e porquê, está na secção
[Privacidade do README](../README.pt.md#privacidade).

### Mudar o modelo IA
Em `config.json`:
```json
"openrouter": {
    "model": "anthropic/claude-sonnet-4-5"
}
```
Alternativas económicas: `mistralai/mistral-7b-instruct`, `meta-llama/llama-3-8b-instruct`

### Estrutura de ficheiros

```
/opt/protectado/
├── data/                     ← Dados locais, nunca versionados
│   ├── config.json           ← Configuração (chaves, perfis, dispositivos)
│   ├── protectado.db         ← Base SQLite (eventos, domínios, uso)
│   ├── posture.json          ← Postura escolhida no arranque (gateway | dns_only)
│   ├── arp_scan.json         ← Último inventário ARP (dns_only)
│   ├── pairing_code          ← Código de emparelhamento do assistente (modo DNS)
│   └── update.trigger/.log   ← Acionador e registo de atualização
├── dashboard.py              ← Servidor web + supervisão (ponto de entrada)
├── monitor.py                ← Thread de supervisão DNS (60s)
├── claude_agent.py           ← IA sob pedido via OpenRouter
├── scheduler.py              ← Horário por perfil
├── action_runner.py          ← Executor root fora do sandbox
├── domain_classifier.py      ← Categorização de domínios DNS
├── daily_report.py           ← Relatório diário (cron)
├── access_control.py         ← Ponto único dos direitos de acesso
├── device_grace.py           ← Período de tolerância de novos aparelhos
├── pihole_api.py             ← Cliente API Pi-hole v6
├── arp_scanner.py            ← Inventário de rede: Pi-hole FTL, completado em dns_only
│                                pelo scan ARP do runner root (data/arp_scan.json)
├── privacy.py                ← Pseudonimização das saídas, retenção, níveis
├── database.py               ← Acesso SQLite
├── i18n/                     ← Traduções (fr, en, es, pt)
├── protectado-agent.json     ← Perfil sandbox nono
├── bootstrap/bootstrap.sh    ← Instalação E atualizações
├── bootstrap/net-common.sh   ← País Wi-Fi e deteção de hardware partilhados
├── update.sh                 ← Atualização manual
└── templates/
    ├── index.html            ← Painel de controlo
    ├── devices.html          ← Dispositivos
    ├── admin_info.html       ← Lembrete de endereço (rede das crianças)
    ├── login.html            ← Início de sessão
    └── onboarding.html       ← Assistente de primeiro arranque (DNS e gateway)
```
