# AGENTS.md — Monitor Boletim Oficial Angra

Contexto para qualquer agente de IA (Claude Code, Copilot, Cursor etc.) que for
mexer neste repositório. Leia antes de propor mudanças, principalmente a
seção "Decisões de design" — várias coisas que parecem estranhas à primeira
vista são propositais.

## O que este projeto faz

Bot em Python que roda 2x/dia via GitHub Actions, verifica
`https://angra.rj.gov.br/boletim-oficial` em busca de novas edições do
Boletim Oficial, baixa os PDFs novos, procura por termos configurados
(o cargo "agente de inclusão digital" e o nome completo do usuário) e manda
alerta no Telegram quando encontra. O usuário quer saber de qualquer menção
ao cargo/concurso e ao próprio nome, caso seja convocado.

## Fluxo de execução (monitor.py)

1. `carregar_config()` — lê `config.json` (termos não sensíveis) e concatena
   o termo vindo da env var `NOME_BUSCA` (nome completo, só via secret).
2. `listar_edicoes()` — faz GET na página de listagem, extrai todos os
   `<a href=*.pdf>`, sobe até a `<tr>` pai de cada um para achar data e
   número da edição. Retorna na ordem da página (mais recente primeiro).
3. Compara com o SQLite (`state/boletins.db`) para saber quais URLs ainda
   não foram processadas (ou usa `--backfill N` pra forçar reprocessamento
   das N mais recentes, sem gravar nada se também tiver `--test`).
4. Para cada edição nova: `baixar_pdf()` (valida cabeçalho `%PDF-`, retry
   com backoff exponencial) → `extrair_texto_por_pagina()` (pdfplumber,
   texto nativo, sem OCR) → `buscar_termos()` (normaliza acento/caixa/
   hifenização e procura cada termo por página).
5. Grava resultado no SQLite, manda alerta no Telegram se achou algo, ou
   manda um heartbeat verde no fim do dia se não achou nada.
6. Compara o número da edição mais recente vista com o maior já salvo no
   banco; se a diferença for maior que a quantidade de novas edições
   processadas no lote (`diferenca > len(alvo)`), dispara aviso de possível
   lacuna (pode indicar que o bot ficou fora do ar ou uma edição foi pulada).

## Estrutura de arquivos

```
monitor.py                       # tudo (parsing, extração, busca, notificação, CLI)
requirements.txt                 # requests, beautifulsoup4, pdfplumber
config.json                      # termos de busca NÃO sensíveis (cargo)
state/boletins.db                # SQLite, versionado no git (ver seção Deploy)
.github/workflows/monitor.yml    # agendamento + commit do estado
README.md                        # passo a passo de setup pro usuário (humano)
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

`url_pdf` é a chave de deduplicação principal; `hash_arquivo` (SHA-256) fica
guardado para eventualmente detectar republicação do mesmo conteúdo sob URL
diferente, mas hoje não é usado ativamente pra decisão de pular ou não.

## Configuração / segredos

| Nome | Onde vive | Sensível? |
|---|---|---|
| `termos_busca` (cargo) | `config.json`, commitado | Não |
| `TELEGRAM_BOT_TOKEN` | GitHub Secret | Sim |
| `TELEGRAM_CHAT_ID` | GitHub Secret | Sim |
| `NOME_BUSCA` (nome completo) | GitHub Secret | Sim |

## Deploy — GitHub Actions

- Cron 2x/dia: `0 15 * * *` e `0 22 * * *` (UTC) = 12h e 19h em Brasília
  (BRT = UTC-3, Brasil não observa horário de verão desde 2019).
- `workflow_dispatch` com inputs `backfill` (int) e `test` (bool) pra rodar
  manualmente.
- **Importante:** o estado (state/boletins.db) NÃO é commitado (repo público, *.db no .gitignore). No Actions ele persiste via `actions/cache` (restore + save). Para persistência confiável, prefira Docker/daemon com volume `./state`. O workflow só precisa de `contents: read`.
- Nunca commitar `.env`; `log()` mascara tokens do Telegram via `ocultar_segredos()`.

## Decisões de design (não "corrigir" sem entender o motivo)

- **Nome completo nunca é commitado.** Vem só da secret `NOME_BUSCA` e é
  concatenado em `carregar_config()` em tempo de execução. Se alguém propor
  colocar o nome direto no `config.json`, é regressão de privacidade.
- **Parsing sem índice fixo de coluna.** `listar_edicoes()` procura qualquer
  `<a href=*.pdf>` e sobe até a `<tr>` pai, extraindo data por regex
  (`\d{2}/\d{2}/\d{4}`) e número pela primeira célula puramente numérica de
  3-5 dígitos — em vez de `celulas[2]`. Isso foi escolhido porque o parsing
  só foi validado contra uma versão do HTML já convertida em markdown, não
  o DOM bruto real. Se `numero`/`data` começarem a vir `None` nos logs, o
  HTML do site mudou — não é bug de lógica, é o seletor que precisa de
  ajuste, e o primeiro passo é inspecionar o HTML real da página.
- **Snippet do match usa o texto já normalizado** (minúsculo, sem acento),
  não o texto original. Foi assim de propósito pra simplificar o
  mapeamento de offset (normalização preserva o comprimento do texto).
  Não trocar para o texto original sem reimplementar o mapeamento de
  índice.
- **`INSERT OR IGNORE`** na tabela `boletins_processados`: segurança extra
  contra `UNIQUE constraint` caso a mesma URL apareça duas vezes na mesma
  listagem (já é deduplicada em `listar_edicoes()`, mas mantido como
  cinto de segurança).
- **Modo `--test`** não escreve no banco nem marca nada como processado —
  é o mecanismo oficial de validar mudanças (parsing, extração, alerta)
  sem afetar o estado de produção. Qualquer alteração no pipeline deveria
  ser testável com `--backfill N --test` antes de rodar de verdade.

## Como testar mudanças

```bash
python monitor.py --backfill 3 --test    # não grava estado, mostra logs
```

Para testar sem depender da rede real (o site da prefeitura não está
acessível de todo ambiente), simular com HTML fake + PDF gerado localmente
(ex.: `reportlab`) cobre `listar_edicoes()`, `normalizar()`,
`extrair_texto_por_pagina()` e `buscar_termos()` isoladamente.

## Limitações conhecidas

- Parsing do HTML da listagem não foi validado contra o DOM real da página
  (só contra uma renderização em markdown) — confirmar no primeiro
  `--test` em produção antes de confiar cegamente.
- Só processa a página 1 da listagem (edições mais recentes). Como o site
  mostra o mais novo primeiro e o bot roda 2x/dia, não deveria faltar
  paginação — mas se o volume de publicações da prefeitura aumentar muito,
  isso pode precisar mudar.
- Assume que todo PDF publicado tem texto nativo extraível (confirmado
  pelo usuário) — não há fallback de OCR.
