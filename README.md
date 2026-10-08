# Monitor do Boletim Oficial de Angra dos Reis

Um bot que fica de olho no [Boletim Oficial de Angra](https://angra.rj.gov.br/boletim-oficial) pra você. Ele baixa as edições novas, procura pelos termos que você definiu (um cargo, seu nome, o que quiser) e manda um alerta no Telegram quando acha alguma coisa.

Ele checa sozinho às 12h e às 19h (horário de Brasília) e ainda responde a uns comandos no chat, tipo `/status` e `/verificar`.

Nada sensível mora no repositório: token, chat id e nome ficam só no seu `.env`, que o git ignora.

## 1. Criando o bot no Telegram

1. Abra o Telegram e procure por **@BotFather** (o oficial, com selo azul).
2. Mande `/newbot`.
3. Escolha um nome pro bot (ex.: `Monitor Boletim Angra`).
4. Escolha um username que termine com `bot` (ex.: `meu_boletim_angra_bot`).
5. O BotFather vai te devolver o **token**, parecido com `123456789:ABCdefGhIJKlmNoPQRstuvWXyz`. Guarde bem, ele é a senha do bot. Se vazar, mande `/revoke` pro BotFather e gere outro.

### Descobrindo seu chat id

1. Abra a conversa com o seu bot novo e mande qualquer mensagem (ex.: `oi`).
2. No navegador, abra `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` (troque pelo seu token).
3. No JSON que aparecer, pegue o número em `"chat": {"id": ...}`. Esse é o seu `TELEGRAM_CHAT_ID`.

Se preferir, o bot `@userinfobot` também te mostra seu id.

## 2. Configurando

Copie o arquivo de exemplo e preencha:

```bash
cp .env.example .env
```

No Windows (PowerShell): `Copy-Item .env.example .env`

```ini
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuvWXyz
TELEGRAM_CHAT_ID=123456789
NOME_BUSCA=Seu Nome Completo
```

| Variável | O que é |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token que o BotFather te deu |
| `TELEGRAM_CHAT_ID` | Seu chat id. Só esse chat recebe alertas e pode mandar comandos |
| `NOME_BUSCA` | Seu nome completo, do jeito que sai em publicação oficial |

Os termos que não são sensíveis (como o cargo) ficam no [config.json](config.json). Quer monitorar mais alguma coisa? É só adicionar lá.

## 3. Rodando com Docker (recomendado)

Precisa de Docker e Docker Compose.

```bash
git clone https://github.com/jeefc/bot_prefeitura.git
cd bot_prefeitura
cp .env.example .env    # preencha o .env
docker compose up -d --build
docker compose logs -f
```

O container roda em modo daemon: faz uma checagem ao subir, outras duas por dia e fica ouvindo seus comandos. O banco de dados fica na pasta `./state`, então reiniciar o container não faz o bot reler tudo do zero.

Pra parar: `docker compose down`. Pra atualizar: `git pull && docker compose up -d --build`.

## 4. Rodando direto com Python

Precisa de Python 3.10 ou mais novo.

```bash
git clone https://github.com/jeefc/bot_prefeitura.git
cd bot_prefeitura
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # preencha o .env
```

Formas de rodar:

```bash
python monitor.py --daemon              # fica rodando: 12h, 19h e comandos no Telegram
python monitor.py                       # faz uma checagem e sai (bom pra cron)
python monitor.py --backfill 3 --test   # reprocessa as 3 últimas edições sem gravar nada
```

### Deixando rodando numa VM Linux (systemd)

Pra o bot subir sozinho e voltar se cair, crie `/etc/systemd/system/bot-angra.service`:

```ini
[Unit]
Description=Monitor Boletim Oficial Angra
After=network.target

[Service]
Type=simple
WorkingDirectory=/caminho/para/bot_prefeitura
ExecStart=/caminho/para/bot_prefeitura/venv/bin/python monitor.py --daemon
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Depois:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bot-angra
sudo journalctl -u bot-angra -f
```

## 5. Testando

O `test_bot.py` ajuda a conferir se está tudo certo antes de deixar rodando:

```bash
python test_bot.py --env         # confere o .env e o banco (valores sensíveis saem mascarados)
python test_bot.py --telegram    # manda uma mensagem de teste
python test_bot.py --portal      # testa o acesso ao site da prefeitura
python test_bot.py --extracao    # baixa e lê 1 PDF sem gravar nada
python test_bot.py --interativo  # você manda mensagem e o bot responde
python test_bot.py --tudo        # roda tudo
```

## 6. Comandos no Telegram

| Comando | O que faz |
|---|---|
| `/ping` | Confirma que o bot está vivo |
| `/status` | Mostra banco, termos monitorados e horários |
| `/verificar` | Força uma checagem agora |
| `/ajuda` | Lista os comandos |

Mensagens de qualquer outro chat são ignoradas.

## 7. Como saber se continua funcionando

Quando não tem novidade, o bot manda uma mensagem verde de "verificação concluída" com a última edição vista. Se der erro de download ou leitura, ele avisa. Se a numeração das edições pular, ele também avisa de uma possível lacuna.

Se os logs começarem a mostrar número ou data como `None`, o site da prefeitura provavelmente mudou o HTML e o parsing de `listar_edicoes()` precisa de um ajuste.

## 8. Cuidados com segredos

- Nunca coloque token, chat id ou nome em arquivo versionado.
- Os logs mascaram o token do Telegram automaticamente.
- Antes de commitar, olhe o `git status`: `.env` e `*.db` não podem aparecer.
- Se um token já foi commitado um dia, revogue no BotFather. Apagar o arquivo não tira do histórico.
