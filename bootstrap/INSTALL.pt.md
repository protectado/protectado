[🇬🇧 English](INSTALL.md) | [🇫🇷 Français](INSTALL.fr.md) | [🇪🇸 Español](INSTALL.es.md) | [🇵🇹 Português](INSTALL.pt.md)

# Protectado — Guia de instalação

Este guia cobre a instalação completa do Protectado em casa de uma nova família, desde o cartão SD em branco até ao painel operativo.

---

## Instalação em Linux existente (NAS, PC antigo...)

Se já tem uma máquina Linux na rede familiar — um NAS, mini-PC ou PC antigo com Ubuntu — o bootstrap funciona diretamente nela.

**Requisitos:**
- Debian / Ubuntu (o script usa `apt`)
- A máquina deve estar na **mesma rede local** que os dispositivos dos filhos
- Pi-hole v6 já instalado, **ou** não instalado (o bootstrap instala-o)
- Python 3.10 mínimo (`python3 --version`)
- systemd ativo

> **VPS / servidor remoto: não compatível.** O Pi-hole deve ver o tráfego DNS local. Um servidor cloud não pode desempenhar este papel sem VPN.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

Se o Pi-hole já estiver instalado e configurado, o bootstrap deteta-o e deixa-o intacto — instala apenas o Protectado por cima. Se o Pi-hole estiver ausente, instala-o.

Continuar a partir do **Passo 4** (configuração via assistente).

---

## Instalação em Raspberry Pi (via nominal)

---

## O que preparar ANTES de ir a casa da família

### Hardware

| Artigo | Notas |
|--------|-------|
| Raspberry Pi | Pi 3B+, Pi 4 ou Pi 5 recomendado (Ethernet integrado). Pi 2W funciona por WiFi. |
| Cartão SD | 16 GB mínimo, classe 10 |
| Alimentação | USB-C (Pi 4/5) ou micro-USB (Pi 2W/3) |
| Cabo Ethernet | Opcional mas recomendado — liga o Pi diretamente ao router |

### Contas / chaves a criar antecipadamente

**Chave API OpenRouter** (indispensável — a IA não funcionará sem ela)
1. Criar uma conta em [openrouter.ai](https://openrouter.ai)
2. Adicionar crédito (alguns euros chegam para vários meses)
3. Gerar uma chave API → copiar a chave (começa por `sk-or-`)

---

## Passo 1 — Preparar o cartão SD (no seu PC)

1. Descarregar **Raspberry Pi Imager**: [raspberrypi.com/software](https://www.raspberrypi.com/software/)
2. Inserir o cartão SD no PC
3. No Raspberry Pi Imager:
   - **Dispositivo** → escolher o modelo de Pi
   - **Sistema operativo** → `Ubuntu Server (64-bit)` (necessário para a sandbox do agente de IA)
   - **Armazenamento** → o seu cartão SD
4. Clicar em **⚙️ Editar definições** (antes de gravar!)

Nas definições avançadas, configurar:

```
✅ Nome do host      → protectado
✅ Ativar SSH        → Usar palavra-passe
   Nome de utilizador → pi
   Palavra-passe     → [escolher uma palavra-passe SSH]
✅ Configurar WiFi   → [SSID e palavra-passe do lar]
   País WiFi         → [o seu país]
```

> **Se usar cabo Ethernet**: pode deixar o WiFi sem configurar.

5. Gravar o cartão → inserir no Pi

---

## Passo 2 — Primeiro arranque

1. Ligar o cabo Ethernet **ou** deixar o WiFi ligar automaticamente
2. Ligar a alimentação
3. Aguardar ~60 segundos (o Pi arranca e entra na rede)

**Encontrar o endereço IP do Pi:**

```bash
# Opção A — a partir do seu PC na mesma rede
ping protectado.local

# Opção B — interface de administração do router (normalmente 192.168.1.1)
```

---

## Passo 3 — Ligação SSH e instalação

```bash
ssh pi@protectado.local
```

Uma vez ligado, executar a instalação com um único comando:

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

A instalação demora **5 a 10 minutos**. Instala automaticamente:
- Pi-hole (filtragem DNS)
- Protectado (agente IA + painel)
- Atualizações automáticas

No final, o script mostra:

```
╔══════════════════════════════════════════════════╗
║       Protectado instalado com sucesso!         ║
╚══════════════════════════════════════════════════╝

  Painel  →  http://192.168.x.x

  ┌─ Informações de configuração ───────────────────
  │  PIHOLE_PASSWORD :  xxxxxxxxxxxxxxxx
  │  PAIRING_CODE    :  XXXXXXXX
  └──────────────────────────────────────────────────
```

**Guarde a palavra-passe do Pi-hole** — para a interface de admin do Pi-hole, em `http://<ip>:81`.

**Guarde o código de emparelhamento** — em modo DNS, o assistente pede-o antes de aceitar a
palavra-passe dos pais. Enquanto o equipamento não estiver configurado, responde a toda a rede da
casa: sem este código, qualquer aparelho — incluindo o de uma criança — poderia definir a
palavra-passe antes de si. Em modo gateway o assistente só é acessível a partir da rede isolada
`Protectado-Setup` e o código não é pedido.

---

## Passo 4 — Assistente de primeiro arranque

No primeiro arranque, o Protectado escolhe o seu **modo automaticamente** e abre um assistente.
Não escolhe o modo — depende do hardware
(ver [modos de funcionamento](../README.pt.md#dois-modos-de-funcionamento)).

### Modo DNS (predefinido)

A partir de qualquer dispositivo na rede, abra `http://protectado.local` (ou o IP do passo 3).
O assistente pede a **palavra-passe de pai/mãe** do painel e depois mostra um **passo
indispensável**.

> ⚠️ **Em modo DNS nada é filtrado enquanto o seu router não encaminhar os aparelhos para o
> equipamento.** O Pi não é router: só vê os aparelhos que lhe pedem para resolver os nomes
> dos sites.

Na interface do seu router (muitas vezes `http://192.168.1.1`), procure **DNS** nas
definições de rede ou DHCP e substitua o servidor DNS pelo endereço do equipamento — o
assistente mostra-o. Depois reinicie o Wi-Fi dos aparelhos das crianças para que o apliquem.

Se o seu router não permitir alterar o DNS, configure-o aparelho a aparelho nas definições
de Wi-Fi. Os perfis e horários são adicionados depois no painel.

### Modo gateway (hardware compatível detetado)

O equipamento emite um Wi-Fi aberto temporário chamado **`Protectado-Setup`**. Ligue um
telemóvel a ele — abre-se sozinho um portal cativo — e siga os passos:

| Passo | O que introduzir |
|-------|-----------------|
| 1 | O seu router de internet — escolha a sua rede Wi-Fi e introduza **a** chave dele |
| 2 | Wi-Fi das crianças — um nome e uma chave fácil de 3 palavras (WPA2) |
| 3 | Palavra-passe de pai/mãe do painel |
| 4 | Volte a ligar o telemóvel ao seu router, abra o endereço indicado, clique **Terminar** |

O equipamento reinicia então em modo gateway: o Wi-Fi das crianças ativa-se e é filtrado,
e a rede de configuração desaparece. O painel continua acessível em `http://<ip-do-equipamento>`.

> A **chave API OpenRouter** introduz-se depois, a partir do painel de conversa do painel —
> não neste assistente.

---

## Passo 5 — Atribuir dispositivos a perfis

No painel → separador **Dispositivos**:

1. Clicar **Analisar rede**
2. Para cada dispositivo detetado: selecionar o perfil
3. Clicar **Atribuir**

---

## Passo 6 — Configurar franjas horárias

No painel → separador **Perfis**:

1. Clicar **Editar** num perfil
2. Adicionar franjas horárias para cada dia da semana (o formato antigo
   Semana/Fim de semana continua a ser lido por compatibilidade)
3. Modos disponíveis: `blocked`, `work`, `permissive`
4. Clicar **Guardar** → **⚙️ Reconfigurar Pi-hole**

---

## Cópia de segurança e restauro

No painel → separador **Gestão** → cartão **Cópia de segurança e restauro**.

> ⚠️ **O ZIP contém segredos EM TEXTO SIMPLES**: a palavra-passe dos pais, a chave da API de IA e, em modo gateway, a chave Wi-Fi do seu router e a da rede das crianças. Por isso, tanto a transferência como a restauração exigem introduzir novamente a palavra-passe dos pais. Guarde o ficheiro como guardaria essas palavras-passe: nunca num espaço partilhado, nunca por email.

---

## Resolução de problemas

```bash
sudo systemctl status protectado-agent
sudo journalctl -u protectado-agent -n 30
pihole status
sudo bash /opt/protectado/update.sh
```

---

## Reiniciar para reconfigurar

Para voltar a lançar o assistente (ex. entregar o equipamento a outra família):

```bash
# Reset simples — mantém os valores, apenas volta a mostrar o assistente no próximo arranque
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot

# Reset total — estado de fábrica: apaga config, Wi-Fi guardado e estado detetado
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

Após um reset total, o equipamento volta como novo e re-escolhe o seu modo (DNS ou
gateway) automaticamente no arranque.

---

## Atualizações automáticas

O Protectado atualiza-se sozinho todas as noites às 3h a partir do ramo **`stable`**.

`main` é o ramo de desenvolvimento; `stable` é promovido **manualmente**. Assim, uma
regressão enviada à noite não pode partir todos os equipamentos de manhã. O ramo
escolhido na instalação é guardado em `data/branch` e reutilizado pelo atualizador: um
equipamento nunca muda de ramo sozinho.

```bash
# Promover o estado atual de main para stable (a partir do repositório de desenvolvimento)
git checkout stable && git merge --ff-only main && git push origin stable

# Instalar uma máquina de testes em main em vez de stable
curl -sSL .../bootstrap.sh | sudo PROTECTADO_BRANCH=main bash
```

A versão instalada (commit curto, ramo, data) é apresentada no painel → separador
**Gestão** → cartão **Atualização**, e na página de início de sessão.
O Pi-hole atualiza-se todos os domingos às 4h.
Os patches de segurança do SO instalam-se automaticamente via `unattended-upgrades`.

---

## Atualizar uma instalação existente

O script bootstrap deteta automaticamente uma instalação existente e passa para o modo de atualização em vez de reinstalar.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

O que a atualização faz:
1. Guarda `config.json` e `protectado.db` numa pasta com data/hora em `/opt/`
2. Descarrega o último código do ramo seguido pelo equipamento
3. Restaura `config.json` (os seus perfis e configuração são preservados)
4. Executa as migrações da base de dados (`database.init_db()`)
5. Reinicia os serviços

Se o agente não arrancar após a atualização, o script reverte automaticamente para a cópia de segurança.
