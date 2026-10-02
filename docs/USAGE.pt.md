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
> das crianças: veja abaixo o que fecha a mais, e os
> [modos de funcionamento](../README.pt.md#dois-modos-de-funcionamento). O painel está na
> porta **80** (a interface de admin do Pi-hole passa para a **81**).

**Sem intervenção dos pais**, o Protectado aplica automaticamente o horário configurado: cortar o acesso de noite, passar para modo trabalho após a escola, reabrir à noite.

**Sob pedido**, o pai escreve no chat do painel em linguagem natural — a IA interpreta e age.

### O que o modo gateway fecha

Em modo gateway, além da filtragem pelo nome:

- **Corte ao nível do pacote**: um dispositivo cujo acesso está fechado deixa de sair, e as
  ligações já abertas caem. Um dispositivo que não se ligou com a chave de nenhum perfil
  não sai de todo.
- **DNS forçado**: um pedido DNS enviado a outro servidor (8.8.8.8 configurado à mão) é
  reencaminhado para o equipamento.
- **Resolvedores cifrados recusados**: DNS-over-TLS e DNS-over-QUIC (porta 853) para
  qualquer servidor, e DNS-over-HTTPS para os resolvedores públicos conhecidos, pelo nome
  e pelo endereço. A lista (`catalog/doh_resolvers.json`) é atualizada com o catálogo.
- **Domínios de contorno**: `use-application-dns.net` (Firefox), `mask.icloud.com` e
  `mask-h2.icloud.com` (iCloud Private Relay) respondem «domínio inexistente».
- **IPv6**: nenhum tráfego IPv6 passa pela rede das crianças.
- **Registo**: cada tentativa recusada é inscrita no registo, uma vez por dispositivo, por
  tipo e por dia. Uma aplicação pode fazê-lo por si própria: não é necessariamente um
  gesto da criança.

O que continua possível: uma **VPN**, ou um resolvedor DNS-over-HTTPS ausente da lista,
leva tráfego para fora da filtragem pelo nome. Os horários e os cortes continuam a
aplicar-se. Um dispositivo que envia quase todo o seu tráfego para um único endereço
durante um quarto de hora, sem pedir nomes ao equipamento, é assinalado no registo como túnel
provável (apenas aviso, sem corte).

### O que o equipamento não consegue ver

O equipamento só filtra o que passa por ele. Não vê:

- os **dados móveis** (4G/5G) do telemóvel;
- a **partilha de ligação** de outro telemóvel;
- o **Wi-Fi do router da família**, se a criança souber a chave: o dispositivo dela pode
  ligar-se diretamente. Em modo gateway, reserve essa chave aos adultos, ou mude-a;
- em modo gateway, uma **VPN** ou um resolvedor cifrado ausente da lista conhecida
  continua a levar tráfego para fora da filtragem pelo nome (os horários e os cortes
  continuam a aplicar-se). Em modo DNS, um dispositivo configurado com outro DNS escapa à
  filtragem: o equipamento deteta-o e assinala-o.

Para estes casos, combine o Protectado com os controlos do próprio telemóvel: **Tempo de
ecrã** no iPhone, **Family Link** no Android.

---

## Primeiro arranque

No primeiro arranque, o Protectado escolhe o seu modo automaticamente e abre um assistente
(ver [modos de funcionamento](../README.pt.md#dois-modos-de-funcionamento)):

- **Modo DNS** (predefinido) — abra `http://protectado.local` e defina a **palavra-passe
  de pai/mãe**. É o único passo; o equipamento fica pronto.
- **Modo gateway** (hardware compatível) — o equipamento emite um Wi-Fi temporário
  `Protectado-Setup` com um portal cativo que o orienta para o ligar ao seu router de
  internet, dar nome ao Wi-Fi das crianças e definir a palavra-passe de pai/mãe. Depois
  pede-lhe para voltar ao Wi-Fi de casa e abrir `http://protectado.local` para terminar:
  a rede temporária desaparece e esse endereço passa a ser o do painel.

Os perfis, os horários e a chave API OpenRouter não são inseridos no assistente —
adicionam-se depois no painel (separador Perfis, e o painel de conversa para a chave). Uma
breve visita guiada explica cada separador no primeiro início de sessão.

---

## Utilização diária

### Painel de controlo

`http://protectado.local`  (interface de admin do Pi-hole: `http://protectado.local:81`)

Um ecrã por pergunta. Rede é uma página à parte, os outros cinco são separadores:

| Separador | A pergunta a que responde |
|---|---|
| **Estado atual** | O que se passa agora, e o que posso fazer imediatamente? |
| **Crianças** | Quais são as regras desta criança? |
| **Exceções** | O que se afasta dessas regras neste momento, e até quando? |
| **Rede** | Quem está ligado? |
| **Domínios** | O que faz a caixa com este site? |
| **Configurações** | A caixa em si, e o registo do que se passou. |

O **registo de decisões** está nas Configurações. Só mostra decisões: exceções,
extensões, bloqueios manuais, alterações de perfil e de chave, histórico apagado, modo
adulto, tomadas no ecrã ou através do assistente. As tentativas bloqueadas (muitas vezes
verificações automáticas dos próprios dispositivos) e as mudanças de franja estão no
**Histórico** de cada criança.

Duas das suas linhas são a exceção e ficam no **Estado atual**, num cartão «A ver» que só
aparece se houver algo: um aparelho que contorna o DNS da caixa, e um aparelho associado
com uma chave que não pertence a nenhum perfil. São os dois únicos casos em que a
filtragem não se aplica de forma alguma a alguém. Uma tentativa bloqueada não consta ali:
é a caixa a fazer o seu trabalho, e chegam dezenas por dia. Um alerta cuja causa
desapareceu apaga-se sozinho ao cabo de dois dias, sem nenhum botão para premir.

### Exceções

A caixa concede cinco tipos de exceção temporária, e estão todos aqui, com o seu prazo e um
botão para os retirar antes da hora:

- uma **exceção temporária**, de alguns minutos a algumas horas;
- um **dia inteiro** num modo dado;
- um **prolongamento** da franja em curso («mais 20 minutos»);
- um **domínio aberto** temporariamente, em geral concedido pelo assistente;
- um **aparelho fora da filtragem** (modo adulto num dispositivo partilhado).

Os três do meio não eram visíveis em parte alguma: concedidos, aplicados, e depois
expirados sem que o adulto os pudesse ver nem retirar.

As exceções **por domínio** não estão aqui: são permanentes e vivem no separador Domínios,
com o catálogo que corrigem. Este separador só fala do que tem um fim.

### Abrir a interface do Pi-hole

A sua palavra-passe é gerada na instalação e não está escrita em mais nenhum lado. Lê-se em
**Configurações → Interface do Pi-hole**, oculta por omissão e revelada por um botão, como
a chave Wi-Fi de uma criança.

### Quando o acesso está cortado, a caixa diz quando reabre

Uma franja fechada não diz «até às 23:59»: anuncia a hora de reabertura, no mesmo dia, no
dia seguinte ou no dia da semana em questão. Uma exceção que corta também não promete o
regresso do acesso no seu prazo, se o horário estiver fechado nesse momento. Quando o
horário não abre na semana que vem, a caixa di-lo em vez de inventar uma hora.

### Tema claro ou escuro

O painel, o assistente de instalação, a página de entrada e a página servida às crianças
seguem a definição do sistema: claro por omissão, escuro para quem tenha o telemóvel ou o
computador assim. O botão ☀️/🌙 do cabeçalho impõe um tema e guarda-o nesse aparelho, onde
prevalece sobre o sistema nos dois sentidos.

A escolha pertence ao navegador que a faz: não muda nada para o resto da família. Todas as
cores passam por fichas declaradas uma única vez em `templates/_theme.html`; um ecrã que
escrevesse uma cor à mão sairia errado num dos dois temas.

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
- o seu horário **dia a dia**, de segunda a domingo (franjas `off`, `homework`, `free`)
- substituições pontuais (férias, exceção de noite…)

O perfil **monitoring** é especial: observa sem bloquear. Útil para supervisionar um dispositivo partilhado sem lhe aplicar regras.

### Fuso horário

Todos os horários do produto seguem a hora local do equipamento: períodos, hora de
deitar, exceções temporárias, relatório da noite. O fuso é por isso determinante, e é
detetado **a partir do navegador do adulto** durante o assistente de primeiro arranque, e
depois aplicado ao sistema. Sem geolocalização e sem chamadas a um serviço externo.

Pode ser alterado depois em **Definições → Hora do equipamento**, linha
equipamento». Vale a pena verificar após uma mudança de casa, ou se o equipamento foi
configurado a partir de um telemóvel em viagem: um fuso errado desloca silenciosamente
todas as regras.

---

### Uma chave Wi-Fi por criança

Em modo gateway o equipamento emite um único Wi-Fi para as crianças, mas **cada perfil tem
a sua própria chave**. Não é um detalhe de conforto: é ela que identifica a criança. O
hostapd indica ao equipamento qual a chave usada na associação, portanto o aparelho
continua ligado ao seu perfil **mesmo quando muda de endereço MAC**, coisa que os
telemóveis recentes fazem. A identificação por endereço deixava um aparelho com MAC nova
passar por desconhecido, ou seja sem filtragem nem horários.

A chave é criada com o perfil e lê-se em **Crianças → Modificar**, tantas vezes quantas
forem precisas: é uma chave para ditar, não um segredo de uso único. Substitui-se no mesmo
sítio, por uma chave gerada ou pela sua.

Alterar a chave de uma criança desliga **apenas os aparelhos dela**, não os das outras. O
Wi-Fi do router, o dos adultos, nunca é afetado.

Duas consequências a conhecer:

- um aparelho que não conhece nenhuma chave **não entra na rede de todo**. Já não há
  aparelho desconhecido com acesso livre: a recusa acontece ao nível do rádio;
- **enquanto não existir nenhum perfil de criança, o Wi-Fi das crianças não é emitido**.
  Sem perfil não há chave nenhuma, e uma rede visível em que ninguém pode entrar seria pior
  do que rede nenhuma. Aparece quando o primeiro perfil é criado.

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

### O catálogo de serviços atualiza-se sozinho

O catálogo que liga um domínio a um serviço — `googlevideo.com` é o YouTube,
`nflxvideo.net` é a Netflix — vive em `catalog/services.json`. São dados, não código: muda
porque um serviço acrescenta um domínio de distribuição, não porque o produto evolui.

Uma publicação que toque apenas nesse ficheiro é por isso aplicada **sem reinício**: sem
reinstalação de dependências, sem migração, sem corte da rede das crianças. O agente volta
a ler o catálogo no ciclo seguinte, dentro do minuto. A caixa não vai buscar nada a outro
lado: o catálogo viaja dentro da atualização que a caixa já consulta, e não é emitida
nenhuma chamada de saída adicional.

Para corrigir ou acrescentar uma correspondência na **sua** caixa sem esperar por uma
publicação, crie `data/services.local.json`:

```json
{
  "services": {
    "arte": {"label": "Arte", "category": "education",
             "domains": ["arte.tv", "artecdn.net"]}
  }
}
```

Este ficheiro aplica-se **por cima** do catálogo entregue, serviço por serviço: redefinir
`youtube` substitui a sua lista de domínios inteira, o que é também a forma de remover um.
Sobrevive às atualizações, e um erro de sintaxe é registado e ignorado sem impedir a caixa
de arrancar. As categorias admitidas são as da grelha de acesso: `education`, `work`,
`other`, `entertainment`, `social`, `adult`, `extremism`, `cdn`.

---


### Ramo seguido

Um equipamento segue o ramo indicado em `data/branch` (fora do controlo de versões, por
isso as atualizações mantêm-no). Sem esse ficheiro usa-se o ramo extraído localmente e, em
último recurso, `stable` — o que os equipamentos em funcionamento consomem. Mudar de ramo
exige escrever o ficheiro **e** realinhar o repositório:

```bash
cd /opt/protectado
SVC=$(stat -c %U /opt/protectado)
echo main | sudo -u "$SVC" tee data/branch
sudo -u "$SVC" git remote set-branches origin '*'
sudo -u "$SVC" git fetch origin --depth 1 main
sudo -u "$SVC" git checkout -B main origin/main
sudo -u "$SVC" git reset --hard origin/main
```

O `remote set-branches` só é preciso uma vez: a instalação clona um único ramo e sem ele o
repositório local não conhece nenhuma outra referência remota.
`PROTECTADO_BRANCH=main sudo -E bash update.sh` força um ramo para uma única atualização,
sem fixar nada.

### Autorreparação no arranque (aparelho por configurar)

Uma falha que impede o assistente de abrir impede também chegar à interface a partir da qual se teria lançado a atualização. Por isso, enquanto o aparelho não estiver configurado, ele põe-se em dia sozinho: no arranque, se houver um cabo Ethernet ativo, compara a sua versão com a publicada no ramo que segue e volta a executar `bootstrap.sh` em dois casos, existe uma versão mais recente, ou o assistente não responde apesar de o código já estar atualizado.

É `bootstrap.sh` que é executado, e não `update.sh`: só o primeiro regenera `/etc/protectado/agent.json` e as unidades systemd, que um simples alinhamento do código deixaria dessincronizados.

Ao fim de três tentativas falhadas sobre a mesma versão publicada, o aparelho deixa de tentar, para não insistir em cada arranque. Qualquer publicação nova rearma a reparação. Sem cabo Ethernet a verificação é abandonada de imediato e o arranque não fica mais lento nem um segundo.

Num aparelho já configurado, nada disto é executado.

---

## Resolução de problemas

### O navegador abre uma página HTTPS em vez do painel

O painel é servido por **HTTP**, em `http://protectado.local`. Se for lá enquanto a caixa
está a arrancar, o `:80` ainda não responde: o navegador tenta então o HTTPS por si
próprio. Até esta versão aterrava na interface de administração do Pi-hole, que escutava o
443 sem motivo, com um certificado autoassinado.

Está corrigido: o Pi-hole só conserva o seu `:81` em claro, e a configuração é reaplicada
a cada arranque da caixa, incluindo numa caixa já instalada.

Se o seu navegador continuar a insistir no HTTPS para esse nome, é porque o memorizou.
Escreva o endereço completo, `http://protectado.local`, ou apague os dados do site nas suas
configurações.


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

Definições em **Definições → Privacidade**, e por criança em **Crianças**.

### Conservação

O histórico (utilização diária, volume de Internet diário e túneis prováveis, registo de eventos, relatórios de IA, catálogo de
domínios não revistos à mão) é guardado **90 dias por predefinição** e depois apagado
automaticamente pela limpeza semanal. Configurável, incluindo «ilimitado» — caso em que
nada é jamais apagado, o que a interface assinala explicitamente.

> Abaixo de 31 dias, a revisão mensal fica sem matéria e di-lo claramente em vez de
> produzir um relatório vazio; abaixo de 8 dias, a semanal faz o mesmo.

### Apagar o histórico de uma criança

**Crianças → Modificar → Apagar o histórico** elimina tudo o que diz respeito a essa criança
— utilização, linha temporal, eventos, exceções — mantendo a configuração e os horários. A
palavra-passe é pedida novamente. Ao eliminar um perfil também é proposto apagar o seu
histórico, em vez de deixar dados sem forma de lhes chegar.

### Nível de privacidade

Cada perfil tem um nível, do qual o escalão etário é apenas a **predefinição**. Os
quatro escalões do produto são **6-9, 10-12, 13-15 e 16+**, e são os únicos: servem
tanto aqui como para calibrar o tom dos relatórios.

| Nível | Predefinição | O que o adulto pode reconstituir | Relatórios |
|---|---|---|---|
| Detalhado | 6-9 e 10-12 | Atividade em janelas de 5 minutos | diário, semanal, mensal |
| Resumo | 13-15 | Agregados por meio-dia | diário, semanal |
| Mínimo | 16+ | Totais do dia, sem horários | semanal |

**O nível não altera o bloqueio, nem os horários, nem os alertas.** Altera apenas o que
pode ser consultado depois. Um adulto preocupado mantém o acesso ao detalhe hora a hora de
um dia concreto: **Crianças → Modificar → Ver o detalhe de um dia**. A palavra-passe é pedida
de novo, o alcance limita-se à data escolhida e a consulta fica inscrita no registo de
eventos do adulto. O ecrã agrupa as janelas de 5 minutos em períodos contínuos, para
responder à pergunta que é realmente feita: de que hora a que hora.

O assistente conversacional continua sujeito ao nível do perfil: indica a granularidade de
que dispõe e não deduz nenhum horário do planeamento, que diz o que era permitido e não o
que foi usado.

### O que a criança pode ver

A partir da rede das crianças, `protectado.admin` mostra-lhe o modo de acesso atual, o
horário do dia, e o que é registado e durante quanto tempo. Essa página **nunca** mostra
o histórico de navegação: um irmão pode aceder-lhe a partir da mesma rede.

### Partilhar com a IA

**Definições → Privacidade → Partilhar dados com a IA.** Desativado, deixa de sair o que quer
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
    ├── claude_agent.py→ IA via OpenRouter, apenas sob pedido
    └── API Pi-hole :81 → consultas DNS, dispositivos, grupos, listas de bloqueio
    ↓ fila de ações →
/tmp/fw-queue/
    ↓
action_runner.py (root, fora do sandbox)
    → API Pi-hole (grupos, listas negras por modo)

[cron 23h — fora do sandbox]
  daily_report.py → classificação (até 10 passagens de 60 domínios)
                  + relatório diário (2 chamadas: relatório e depois resumo)
```

O processo no sandbox também fala com o Pi-hole, através da sua API na porta 81: lê as
consultas DNS e a lista de dispositivos, muda o grupo de um dispositivo e sincroniza as
listas de bloqueio. O perfil do nono autoriza essa porta de forma explícita. O runner
root trata do que o sandbox proíbe: firewall, ponto de acesso Wi-Fi, serviços do
sistema.

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

A política de rede é aplicada pelo **próprio Landlock** (`nono run --sandbox-policy
landlock`), e não pelo modo `auto` do nono. Em `auto`, o nono complementa o Landlock com
uma base seccomp estática para a rede: incapaz de exprimir uma regra por porta, deixa
passar o proxy e recusa o resto, incluindo as portas que o perfil autoriza. Ao agente era
então negado tanto escutar na 8080 como chamar a API do Pi-hole. Este modo exige um
kernel com ABI Landlock V4 ou posterior e recusa arrancar caso contrário: mais vale um
serviço que para e o diz do que um aparelho a funcionar sem sandbox.

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
├── modes.py                  ← O vocabulário dos modos de acesso, declarado uma vez
├── access_grid.py            ← O que cada modo permite, por escalão etário e por criança
├── services.py               ← A lógica de agrupamento dos serviços (a lista está em
│                                catalog/, não aqui)
├── catalog/services.json     ← O catálogo entregue: serviços, rótulos, domínios. São
│                                DADOS, editáveis sem saber Python. Um complemento local
│                                opcional (data/services.local.json) aplica-se por cima
├── action_runner.py          ← Executor root fora do sandbox
├── domain_classifier.py      ← Categorização de domínios DNS
├── daily_report.py           ← Relatório diário (cron)
├── access_control.py         ← Ponto único dos direitos de acesso
├── wifi_keys.py              ← Uma chave Wi-Fi por perfil, servida ao hostapd
├── station_identity.py       ← Quem está por trás de um endereço: a chave, não a MAC
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
    ├── admin_info.html       ← Lembrete de endereço (rede das crianças)
    ├── login.html            ← Início de sessão
    └── onboarding.html       ← Assistente de primeiro arranque (DNS e gateway)
```
