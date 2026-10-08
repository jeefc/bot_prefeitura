# AGENTS.md: Monitor Boletim Oficial Angra

Contexto para qualquer agente de IA (Claude Code, Copilot, Cursor etc.) que for
mexer neste repositório. Leia antes de propor mudanças, principalmente a
seção "Decisões de design": várias coisas que parecem estranhas à primeira
vista são propositais.

## O que este projeto faz

Bot em Python que verifica `https://angra.rj.gov.br/boletim-oficial` em busca
de novas edições do Boletim Oficial, baixa os PDFs novos, procura por termos
configurados (o cargo "agente de inclusão digital" e o nome completo do
usuário) e manda alerta no Telegram quando encontra.

Roda em uma VM do próprio usuário, via Docker (`docker compose`) ou como
serviço systemd, em modo `--daemon`. O plano original de rodar via GitHub
Actions foi abandonado por instabilidade e o workflow foi removido. Não
reintroduzir sem o usuário pedir.

## Fluxo de execução (monitor.py)

1. `carregar_config()`: lê `config.json` (termos não sensíveis) e concatena o
   termo vindo da env var `NOME_BUSCA` (nome completo, só via `.env`).
2. `listar_edicoes()`: GET na página de listagem, extrai todos os
   `<a href=*.pdf>`, sobe até a `<tr>` pai de cada um para achar data e
   número da edição. Retorna na ordem da página (mais recente primeiro).
3. Compara com o SQLite (`state/boletins.db`) para saber quais URLs ainda não
   foram processadas (ou usa `--backfill N` para forçar reprocessamento das N
   mais recentes; com `--test` nada é gravado).
4. Para cada edição nova: `baixar_pdf()` (valida cabeçalho `%PDF-`, retry com
   backoff exponencial), `extrair_texto_por_pagina()` (pdfplumber, sem OCR) e
   `buscar_termos()` (normaliza acento/caixa/hifenização).
5. Grava no SQLite, manda alerta no Telegram se achou algo, ou um aviso verde
   de "verificação concluída" se não achou nada.
6. Se o número da edição mais recente excede o maior já salvo por mais do que
   a quantidade de edições processadas no lote (`diferenca > len(alvo)`),
   avisa de possível lacuna.
7. No modo `--daemon`, roda às 12h e 19h (BRT), faz uma checagem ao iniciar e
   mantém uma thread de long-polling atendendo comandos do Telegram
   (`/ping`, `/status`, `/verificar`, `/ajuda`), só para o `TELEGRAM_CHAT_ID`.

## Estrutura de arquivos

```
monitor.py            # tudo: parsing, extração, busca, notificação, daemon, CLI
test_bot.py           # diagnóstico manual (--env, --telegram, --portal, ...)
config.json           # termos de busca NÃO sensíveis (cargo)
requirements.txt      # requests, beautifulsoup4, pdfplumber
Dockerfile
docker-compose.yml    # volume ./state e env_file .env
.dockerignore         # mantém .env e state fora da imagem
.env.example          # modelo das variáveis (sem valores reais)
state/.gitkeep        # garante a pasta; o .db é ignorado pelo git
README.md             # tutorial para humanos
```

## Esquema do banco (SQLite)

```sql
boletins_processados (
    id, numero, data_publicacao, url_pdf UNIQUE,
    hash_arquivo, status,           -- PROCESSADO / COM_ALERTA / ERRO
    data_processamento
)

matches (
    id, boletim_id -> boletins_processados.id,
    termo, pagina, trecho
)
```

`url_pdf` é a chave de deduplicação. `hash_arquivo` (SHA-256) fica guardado
mas hoje não é usado para decidir nada.

## Configuração e segredos

| Nome | Onde vive | Sensível? |
|---|---|---|
| `termos_busca` (cargo) | `config.json`, commitado | Não |
| `TELEGRAM_BOT_TOKEN` | `.env` local | Sim |
| `TELEGRAM_CHAT_ID` | `.env` local | Sim |
| `NOME_BUSCA` (nome completo) | `.env` local | Sim |

O repositório é **público**. Regras:

- Nunca commitar `.env`, `*.db`, token, chat id, nome completo ou username do
  bot. Conferir `git status` e `git diff --staged` antes de commitar.
- `log()` passa tudo por `ocultar_segredos()`, que mascara tokens do Telegram
  (exceções do `requests` incluem a URL com o token). Manter isso.
- `test_bot.py` imprime valores sensíveis mascarados. Manter.
- O histórico do git foi recriado do zero após um vazamento de token. Se algum
  segredo aparecer de novo, o token deve ser revogado no BotFather.

## Decisões de design (não "corrigir" sem entender o motivo)

- **Nome completo nunca é commitado.** Vem só de `NOME_BUSCA` e é concatenado
  em `carregar_config()` em tempo de execução. Colocar o nome no
  `config.json` é regressão de privacidade.
- **Parsing sem índice fixo de coluna.** `listar_edicoes()` procura qualquer
  `<a href=*.pdf>` e sobe até a `<tr>` pai, extraindo data por regex
  (`\d{2}/\d{2}/\d{4}`) e número pela primeira célula puramente numérica de
  3-5 dígitos. Links sem número (PDFs avulsos do site) são ignorados. Se
  `numero`/`data` vierem `None` nos logs, o HTML do site mudou e o seletor
  precisa de ajuste.
- **Snippet do match usa o texto já normalizado** (minúsculo, sem acento).
  Foi proposital para simplificar o mapeamento de offset. Não trocar pelo
  texto original sem reimplementar o mapeamento de índice.
- **Upsert em `boletins_processados`** (`ON CONFLICT(url_pdf) DO UPDATE`) e
  limpeza de `matches` antes de regravar tornam o reprocessamento idempotente.
- **Modo `--test`** não escreve no banco. É o jeito oficial de validar
  mudanças sem afetar o estado de produção.
- **Lock de execução** (`EXECUCAO_LOCK`) evita que `/verificar` e o agendador
  rodem ao mesmo tempo.

## Como testar mudanças

```bash
python monitor.py --backfill 3 --test    # não grava estado, mostra logs
python test_bot.py --tudo                # diagnóstico completo
```

Para testar sem rede, simular HTML fake + PDF gerado localmente (ex.:
`reportlab`) cobre `listar_edicoes()`, `normalizar()`,
`extrair_texto_por_pagina()` e `buscar_termos()` isoladamente.

## Limitações conhecidas

- Só processa a página 1 da listagem. Como o site mostra o mais novo primeiro
  e o bot roda 2x/dia, não deveria faltar edição.
- Assume que todo PDF tem texto nativo extraível. Não há fallback de OCR.
