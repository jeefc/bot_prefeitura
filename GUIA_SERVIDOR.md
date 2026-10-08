# Guia Completo: Monitor Boletim Oficial Angra no Servidor

Este documento detalha o funcionamento da aplicação, como operá-la no seu servidor (Linux, VPS ou Docker), como utilizar os scripts de teste e como interagir diretamente com o bot pelo Telegram.

---

## 1. Visão Geral e Arquitetura

O bot monitora automaticamente as publicações da Prefeitura de Angra dos Reis em busca de convocações de concursos, menções ao cargo configurado e ao seu nome completo.

```
[ Portal da Prefeitura ]
         │ (GET https://angra.rj.gov.br/boletim-oficial)
         ▼
  [ monitor.py ] ────► [ state/boletins.db ] (SQLite: deduplica edições já lidas)
         │
         ├───► [ pdfplumber ] (Extrai texto nativo de cada página)
         ├───► [ normalização ] (Remove acentos, caixa alta e hifenização)
         │
         ▼
[ Telegram Bot API ] ◄────► [ Usuário no Celular ]
  • Disparo de Alertas        • Recebe ocorrências encontradas
  • Heartbeats diários        • Envia /ping, /status, /verificar
  • Comandos interativos
```

### O que acontece no servidor (Modo `--daemon`):
- **Ao iniciar**: Realiza uma checagem inicial imediata no portal.
- **Agendamentos fixos**: Roda diariamente às **12:00** e às **19:00** (Horário de Brasília - BRT).
- **Escuta interativa em segundo plano**: Uma thread dedicada fica aguardando suas mensagens no Telegram. Você pode enviar comandos a qualquer momento e o bot responderá em tempo real.

---

## 2. Comandos Interativos no Telegram

Você pode conversar com o bot diretamente no seu aplicativo do Telegram:

| Comando | O que ele faz | Resposta do Bot |
|---|---|---|
| `/ping` ou `/teste` | Testa se o bot está ativo no servidor | `🏓 Pong! O monitor está ativo e operacional no servidor!` com data/hora BRT. |
| `/status` | Exibe a saúde do sistema e do banco | Mostra quantidade de boletins no banco, última edição vista, total de termos encontrados e próximos horários. |
| `/verificar` ou `/check` | Força uma checagem imediata no portal | Executa a varredura na hora e te avisa o resultado sem esperar os horários agendados. |
| `/ajuda` ou `/help` | Lista os comandos disponíveis | Mostra o menu de ajuda com instruções rápidas. |

> 🔒 **Segurança**: O bot só responde às mensagens vindas do seu `TELEGRAM_CHAT_ID`. Se qualquer outra pessoa mandar mensagem para o seu bot, o comando será ignorado e registrado nos logs do servidor como tentativa não autorizada.

---

## 3. Relatório de Bugs Encontrados & Melhorias Realizadas

Durante a análise da base de código, foram identificados e corrigidos os seguintes pontos críticos:

1. **Bot estritamente unidirecional (sem resposta a mensagens)**:
   - *Antes*: O bot só enviava alertas (`sendMessage`). Não possuía nenhuma rotina de `getUpdates` para receber comandos.
   - *Correção*: Implementado polling de comandos em segundo plano no modo `--daemon` e `--listen`, permitindo testar e controlar o monitor pelo celular.

2. **Arquivo de segredos exposto no Git (`env` vs `.env`)**:
   - *Antes*: O arquivo contendo o token e o nome real chamava-se `env` (sem ponto) e não estava no `.gitignore`.
   - *Correção*: `env` adicionado ao `.gitignore`, `carregar_env()` atualizado para ler tanto `.env` quanto `env`, e criado `.env.example` para documentar variáveis sem expor credenciais.

3. **Perda potencial de disparos no agendador (`rodar_daemon`)**:
   - *Antes*: O daemon comparava `if hora_atual in ("12:00", "19:00")` com `sleep(30)`. Se uma checagem anterior ou lentidão de rede atrasasse o relógio para 12:01, a rotina diária era perdida.
   - *Correção*: Implementado controle por janela de tolerância e slots diários (`f"{dia_atual}_{hora}"`), garantindo execução robusta.

4. **Persistência do banco SQLite no Docker**:
   - *Antes*: O `Dockerfile` não garantia persistência. Reiniciar o container apagava todo o histórico de edições lidas, gerando alertas duplicados.
   - *Correção*: Criado `docker-compose.yml` com mapeamento explícito do volume `./state:/app/state`, garantindo que o banco de dados sobreviva a qualquer reinício.

5. **Falha de parse HTML no Telegram**:
   - *Antes*: Se uma edição contivesse caracteres especiais que quebrassem as tags HTML, a API do Telegram recusava a mensagem com erro 400 e o alerta era perdido.
   - *Correção*: Implementado fallback automático para texto puro sem formatação caso a API rejeite o HTML, além de truncamento automático para respeitar o limite de 4096 caracteres.

7. **Falso positivo de "Possível lacuna" ao processar múltiplas edições**:
   - *Antes*: Se o bot encontrasse 2 ou mais edições novas de uma vez (ex: após feriado ou fim de semana), comparava `mais_recente - maior_banco > 1` e disparava alarme falso de lacuna, mesmo tendo processado todas com sucesso.
   - *Correção*: Agora verifica se o salto é maior que a quantidade de novas edições do lote (`diferenca > len(alvo)`), alertando apenas quando realmente faltar uma edição intermediária.

8. **Comando `/verificar` com concorrência e retorno inconsistente**:
   - *Antes*: O status de alerta e o lock eram confundidos em `executar_uma_vez`, podendo responder mensagem de sucesso mesmo em falha ou mentir sobre processo em andamento.
   - *Correção*: `executar_uma_vez` agora retorna status estruturado (`sucesso`, `ocupado` ou `erro`), garantindo feedback 100% preciso para o usuário no Telegram.

9. **Detecção de convocações com caracteres invisíveis (*Soft Hyphens* `\xad`)**:
   - *Antes*: Palavras com hifenização invisível (`\xad`) ou traços tipográficos Unicode gerados por exportações de PDF não eram encontradas pelo `find`.
   - *Correção*: Função `normalizar()` higieniza `\xad` e unifica traços tipográficos, impedindo falsos negativos de convocação.

10. **Proteção contra termos vazios**:
    - *Antes*: Se por engano o `config.json` ou `NOME_BUSCA` tivesse string em branco, `"texto".find("") == 0` geraria match em todas as páginas de todos os PDFs.
    - *Correção*: Adicionada guarda defensiva que ignora termos vazios ou compostos apenas por espaços.

11. **Idempotência no reprocessamento (`matches`) e tolerância no cabeçalho PDF**:
    - *Antes*: Reprocessar edições no banco duplicava registros na tabela `matches`, e o download exigia estritamente `%PDF-` nos primeiros 5 bytes (quebrando se o servidor enviasse BOM ou espaços).
    - *Correção*: Limpeza de matches prévios antes de salvar novos e validação de `%PDF-` em qualquer posição dos primeiros 1024 bytes (conforme norma ISO 32000-1).

---

## 4. Como Testar no Servidor (`test_bot.py`)

Foi criado o script `test_bot.py` para você rodar testes no servidor de forma simples e intuitiva.

### A. Teste Rápido das Variáveis e Conexões
Valida se o `.env` foi carregado, se o token é válido e se o banco SQLite está acessível:
```bash
python test_bot.py --env
```

### B. Teste de Envio (Servidor -> Telegram)
Envia uma mensagem de teste para o seu Telegram para certificar que o bot consegue te notificar:
```bash
python test_bot.py --telegram
```

### C. Teste Interativo (Você manda mensagem e o bot responde!)
Este é o teste principal solicitado:
```bash
python test_bot.py --interativo
```
1. O terminal exibirá:
   ```
   🤖 Bot identificado: @seu_bot
   👉 Abra o Telegram e envie uma mensagem para @seu_bot
   ⏳ Aguardando sua mensagem por até 60 segundos...
   ```
2. Abra o Telegram no seu celular e envie `/ping`, `/status` ou qualquer texto (ex.: `oi`).
3. O terminal do servidor mostrará a mensagem que você digitou e enviará uma confirmação de volta para o seu Telegram imediatamente.

### D. Teste de Leitura do Portal de Angra
Testa se o servidor consegue acessar o site da prefeitura e ler a lista de edições:
```bash
python test_bot.py --portal
```

### E. Teste de Extração Segura em 1 PDF
Baixa a edição mais recente e testa a busca de termos sem gravar nada no banco (modo seguro):
```bash
python test_bot.py --extracao
```

### F. Executar Todos os Testes
Roda a bateria completa de uma só vez:
```bash
python test_bot.py --tudo
```

---

## 5. Deploy no Servidor

### Método 1: Usando Docker e Docker Compose (Recomendado)

Esta é a forma mais prática e robusta, pois garante reinício automático caso o servidor reinicie.

1. **Clone ou envie os arquivos para o seu servidor**:
   ```bash
   git clone <url-do-seu-repositorio> bot_prefeitura
   cd bot_prefeitura
   ```

2. **Crie o arquivo `.env`**:
   Copie o modelo e preencha com seus dados reais:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Conteúdo do `.env`:
   ```ini
   TELEGRAM_BOT_TOKEN=cole_o_token_aqui
   TELEGRAM_CHAT_ID=cole_o_chat_id_aqui
   NOME_BUSCA=Seu Nome Completo
   ```

3. **Suba a aplicação com o Docker Compose**:
   ```bash
   docker compose up -d --build
   ```

4. **Verifique os logs**:
   ```bash
   docker compose logs -f
   ```
   Você verá a confirmação do modo daemon e da escuta de comandos no Telegram:
   ```
   [2026-09-09T...] Modo daemon ativado: verificações agendadas diariamente às 12:00 e 19:00 (BRT)
   [2026-09-09T...] Thread de comandos interativos do Telegram iniciada com sucesso.
   [2026-09-09T...] Iniciando escuta de comandos no Telegram (long-polling)...
   ```

5. **Para testar enquanto o container roda**:
   Basta abrir o Telegram e enviar `/ping` ou `/status`!

---

### Método 2: Execução Direta com Python e Systemd (VPS / Linux)

Se preferir rodar direto no sistema operacional sem Docker:

1. **Crie o ambiente virtual e instale as dependências**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure o `.env`**:
   ```bash
   cp .env.example .env
   nano .env
   ```

3. **Crie o arquivo de serviço Systemd**:
   ```bash
   sudo nano /etc/systemd/system/bot-angra.service
   ```
   Insira o seguinte conteúdo (ajuste o caminho da pasta):
   ```ini
   [Unit]
   Description=Monitor Boletim Oficial Angra
   After=network.target

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/caminho/para/bot_prefeitura
   ExecStart=/caminho/para/bot_prefeitura/venv/bin/python monitor.py --daemon
   Restart=always
   RestartSec=10
   Environment=PYTHONUNBUFFERED=1

   [Install]
   WantedBy=multi-user.target
   ```

4. **Ative e inicie o serviço**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable bot-angra
   sudo systemctl start bot-angra
   ```

5. **Acompanhe os logs**:
   ```bash
   sudo journalctl -u bot-angra -f
   ```

---

## 6. Operação e Manutenção

- **Adicionar Novos Cargos ou Palavras-Chave**:
  Basta editar o arquivo `config.json`:
  ```json
  {
    "termos_busca": [
      "agente de inclusao digital",
      "outro termo desejado"
    ]
  }
  ```
  *(O seu nome completo permanece apenas no `.env` sob `NOME_BUSCA` para manter sua privacidade).*

- **Backup do Estado**:
  Todo o histórico de edições lidas fica salvo em `state/boletins.db`. Para fazer backup, basta copiar esse arquivo:
  ```bash
  cp state/boletins.db state/boletins_backup.db
  ```
