# Monitor do Boletim Oficial de Angra dos Reis

Bot em Python que verifica [angra.rj.gov.br/boletim-oficial](https://angra.rj.gov.br/boletim-oficial),
baixa as edições novas do Boletim Oficial, procura por termos configurados
(cargo + seu nome) e avisa no **Telegram** quando encontra.

- Roda 2x/dia (12h e 19h, horário de Brasília) e responde a comandos no Telegram (`/status`, `/verificar`, `/ping`).
- **Nenhum segredo fica no repositório**: token, chat id e seu nome vêm de variáveis de ambiente (`.env` local ou GitHub Secrets). O repositório pode ser público.

---

## 1. Criar o bot no Telegram (BotFather)

1. No Telegram, procure por **@BotFather** (conta oficial, com selo azul) e abra a conversa.
2. Envie `/newbot`.
3. Escolha um **nome de exibição** (ex.: `Monitor Boletim Angra`).
4. Escolha um **username** terminado em `bot` (ex.: `meu_boletim_angra_bot`).
5. O BotFather responde com o **token**, no formato `123456789:ABCdefGhIJKlmNoPQRstuvWXyz`.
   Guarde-o: ele é a senha do seu bot. **Nunca publique nem commite.**
   Se vazar, envie `/revoke` ao BotFather para gerar outro.

### Descobrir o seu `chat_id`

1. Abra a conversa com o **seu bot** e envie qualquer mensagem (ex.: `oi`).
2. No navegador, acesse (trocando pelo seu token):
   `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
3. No JSON, copie o número de `"chat": {"id": 123456789, ...}`. Esse é o `TELEGRAM_CHAT_ID`.

   > Alternativa: converse com `@userinfobot`, que mostra o seu ID.

---

## 2. Configurar as variáveis

```bash
cp .env.example .env      # Windows (PowerShell): Copy-Item .env.example .env
```

Edite o `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuvWXyz
TELEGRAM_CHAT_ID=123456789
NOME_BUSCA=Seu Nome Completo
```

| Variável | Descrição |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token do BotFather |
| `TELEGRAM_CHAT_ID` | Seu chat id (só ele recebe alertas e pode dar comandos) |
| `NOME_BUSCA` | Seu nome completo, como aparece em publicações oficiais |

O `.env` está no `.gitignore` e no `.dockerignore`. Os termos **não sensíveis** (ex.: cargo) ficam em [config.json](config.json); edite para adicionar outros.

---

## 3A. Rodar com Docker (recomendado)

Requisitos: Docker + Docker Compose.

```bash
git clone https://github.com/jeefc/bot_prefeitura.git
cd bot_prefeitura
cp .env.example .env     # e preencha
docker compose up -d --build
docker compose logs -f
```

- O container roda em modo `--daemon`: checa ao iniciar, às 12h e às 19h (BRT) e escuta comandos do Telegram.
- O banco (`state/boletins.db`) fica no volume `./state`, então sobrevive a reinícios.
- Parar: `docker compose down`. Atualizar: `git pull && docker compose up -d --build`.

---

## 3B. Rodar direto com Python

Requisitos: Python 3.10+.

```bash
git clone https://github.com/jeefc/bot_prefeitura.git
cd bot_prefeitura
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # e preencha
```

Modos de execução:

```bash
python monitor.py --daemon             # fica rodando: 12h/19h + comandos no Telegram
python monitor.py                      # uma verificação e sai (use com cron/Agendador de Tarefas)
python monitor.py --backfill 3 --test  # reprocessa 3 edições SEM gravar no banco (teste)
```

Para manter no Linux após fechar o terminal, veja o exemplo de serviço systemd em [GUIA_SERVIDOR.md](GUIA_SERVIDOR.md).

---

## 3C. Rodar no GitHub Actions (opcional)

1. No seu fork/repositório: **Settings → Secrets and variables → Actions → New repository secret** e crie `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` e `NOME_BUSCA`.
2. **Actions → Monitor Boletim Oficial Angra → Run workflow**. Teste primeiro com `backfill = 3` e `test = true`.
3. O cron (12h e 19h BRT) cuida do resto. O estado do banco é guardado via `actions/cache` (nada de dados vai para o git).

> Atenção: workflows agendados são pausados após 60 dias sem atividade no repositório e o cache pode expirar (7 dias sem uso). Para uso contínuo, prefira Docker/Python em uma máquina sua.

---

## 4. Testar

```bash
python test_bot.py --env         # valida .env e banco (valores sensíveis aparecem mascarados)
python test_bot.py --telegram    # envia mensagem de teste
python test_bot.py --portal      # testa acesso ao site da prefeitura
python test_bot.py --extracao    # baixa e lê 1 PDF sem gravar nada
python test_bot.py --interativo  # você manda mensagem e o bot responde
python test_bot.py --tudo
```

## 5. Comandos no Telegram

| Comando | Descrição |
|---|---|
| `/ping` | Confirma que o bot está ativo |
| `/status` | Resumo do banco, termos ativos e horários |
| `/verificar` | Força checagem imediata |
| `/ajuda` | Lista de comandos |

O bot ignora mensagens de qualquer chat diferente de `TELEGRAM_CHAT_ID`.

## 6. Segurança (repositório público)

- Nunca coloque token, chat id ou nome em arquivos versionados (`config.json`, README, código).
- Logs mascaram automaticamente tokens do Telegram.
- Antes de commitar, confira: `git status` não deve listar `.env` nem `*.db`.
- Se um token já foi commitado alguma vez: **revogue-o no BotFather** (`/revoke`) — apagar o arquivo não remove do histórico do git.

## 7. Como saber se continua funcionando

Sem novidades, o bot manda uma mensagem verde (`🟢 Verificação concluída...`). Falhas de download/leitura e possíveis lacunas na numeração das edições geram avisos específicos. Se `numero`/`data` aparecerem como `None` nos logs, o HTML do site mudou e o parsing em `listar_edicoes()` precisa de ajuste.
