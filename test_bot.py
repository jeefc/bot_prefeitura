#!/usr/bin/env python3
"""
Script de testes e diagnóstico para o Monitor do Boletim Oficial de Angra.
Permite testar configurações, envio de alertas, conexão com o portal e
teste interativo de envio/recebimento de mensagens pelo Telegram no servidor.

Uso:
    python test_bot.py                 # Menu interativo
    python test_bot.py --interativo    # Aguarda mensagem no Telegram e responde
    python test_bot.py --env           # Valida ambiente e dependências
    python test_bot.py --telegram      # Envia mensagem de teste para o Telegram
    python test_bot.py --portal        # Testa listagem no portal da Prefeitura
    python test_bot.py --tudo          # Executa todos os testes automatizados
"""

import argparse
import html
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("❌ Dependência 'requests' não encontrada. Execute: pip install -r requirements.txt")
    sys.exit(1)

import monitor

monitor.carregar_env()



def banner():
    print("=" * 65)
    print("  🔧 DIAGNÓSTICO & TESTES - MONITOR BOLETIM OFICIAL ANGRA")
    print("=" * 65)


def test_ambiente():
    print("\n[1/5] 🔍 Verificando variáveis de ambiente e arquivos...")
    cfg = monitor.carregar_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    nome_busca = os.environ.get("NOME_BUSCA")

    erros = 0
    if not token:
        print("  ❌ TELEGRAM_BOT_TOKEN não encontrado em .env ou variáveis de sistema.")
        erros += 1
    else:
        print("  ✅ TELEGRAM_BOT_TOKEN configurado: ********")

    if not chat_id:
        print("  ❌ TELEGRAM_CHAT_ID não encontrado.")
        erros += 1
    else:
        print("  ✅ TELEGRAM_CHAT_ID configurado: ********")

    if not nome_busca:
        print("  ⚠️ NOME_BUSCA não configurado. Apenas os termos de config.json serão monitorados.")
    else:
        print("  ✅ NOME_BUSCA configurado: ********")

    print(f"  ✅ Quantidade de termos ativos para busca: {len(cfg.get('termos_busca', []))}")

    # Valida banco SQLite
    try:
        conn = monitor.conectar_db()
        total = conn.execute("SELECT count(*) FROM boletins_processados").fetchone()[0]
        conn.close()
        print(f"  ✅ Banco de dados SQLite acessível ({total} edições cadastradas).")
    except Exception as e:
        print(f"  ❌ Erro ao acessar SQLite: {e}")
        erros += 1

    return erros == 0


def test_telegram_envio():
    print("\n[2/5] 📤 Testando envio de mensagem do Servidor -> Telegram...")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ❌ Impossível testar Telegram: configure TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
        return False

    agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M:%S")
    msg = (
        "🚀 <b>Teste de Conexão com o Servidor</b>\n\n"
        "Seu bot de monitoramento do Boletim Oficial de Angra dos Reis conseguiu "
        "enviar mensagens com sucesso a partir do servidor!\n\n"
        f"🕒 <b>Data/Hora do envio:</b> {agora} (BRT)\n"
        "🟢 <b>Status:</b> Comunicação de saída operacional."
    )
    sucesso = monitor.enviar_telegram(token, chat_id, msg)
    if sucesso:
        print("  ✅ Mensagem enviada com sucesso! Verifique seu Telegram.")
        return True
    else:
        print("  ❌ Falha no envio para o Telegram. Verifique se o bot token e chat_id estão corretos.")
        return False


def test_telegram_interativo(tempo_limite_segundos=60):
    print("\n[3/5] 💬 Teste Interativo: Envie uma mensagem para o bot!")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_esperado = str(os.environ.get("TELEGRAM_CHAT_ID") or "")

    if not token:
        print("  ❌ TELEGRAM_BOT_TOKEN não configurado.")
        return False

    # Obtém info do bot
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        bot_info = r.json().get("result", {})
        bot_username = bot_info.get("username", "seu_bot")
        print(f"  🤖 Bot identificado: @{bot_username} ({bot_info.get('first_name')})")
    except Exception as e:
        print(f"  ⚠️ Não foi possível obter info do bot: {monitor.ocultar_segredos(e)}")
        bot_username = "seu_bot"

    # Limpa mensagens pendentes anteriores
    offset = None
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates?offset=-1", timeout=10)
        if r.ok and r.json().get("result"):
            offset = r.json()["result"][-1]["update_id"] + 1
    except Exception:
        pass

    print("-" * 65)
    print(f"  👉 Abra o Telegram e envie uma mensagem para @{bot_username}")
    print("     Exemplos: /ping, /status, oi, teste")
    print(f"  ⏳ Aguardando sua mensagem por até {tempo_limite_segundos} segundos...")
    print("-" * 65)

    inicio = time.time()
    while time.time() - inicio < tempo_limite_segundos:
        restante = int(tempo_limite_segundos - (time.time() - inicio))
        sys.stdout.write(f"\r  ⏱️ Aguardando... ({restante}s restantes) ")
        sys.stdout.flush()

        try:
            params = {"timeout": 3}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=5)
            if not r.ok:
                time.sleep(1)
                continue

            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                remetente_id = str(msg.get("chat", {}).get("id", ""))
                texto = msg.get("text", "").strip()
                usuario = msg.get("from", {}).get("first_name", "Usuário")

                print(f"\n\n  📩 Mensagem recebida de {usuario} (ID {remetente_id})!")
                print(f"  💬 Conteúdo: \"{texto}\"")

                if chat_id_esperado and remetente_id != chat_id_esperado:
                    print(f"  ⚠️ Atenção: Chat ID recebido ({remetente_id}) é diferente do configurado ({chat_id_esperado}).")

                # Responde de volta para o usuário no Telegram
                resposta = monitor.responder_comando(texto, monitor.carregar_config())
                monitor.enviar_telegram(token, remetente_id, resposta)
                print(f"  📤 Resposta enviada de volta pelo bot para o Telegram:")
                print("  " + "\n  ".join(resposta.splitlines()[:5]) + " ...")
                print("\n  🎉 SUCESSO! A comunicação bidirecional com o bot está 100% funcionando!")
                return True

        except requests.RequestException:
            pass
        except KeyboardInterrupt:
            print("\n  ⏹️ Teste interrompido pelo usuário.")
            return False

        time.sleep(1)

    print("\n  ⚠️ Tempo limite atingido sem receber mensagem.")
    print(f"     Certifique-se de que iniciou a conversa com @{bot_username} no Telegram.")
    return False


def test_portal():
    print("\n[4/5] 🌐 Testando consulta ao portal da Prefeitura de Angra...")
    try:
        edicoes = monitor.listar_edicoes()
        if not edicoes:
            print("  ❌ Nenhuma edição encontrada no portal. O layout do site pode ter mudado.")
            return False
        mais_recente = edicoes[0]
        print(f"  ✅ Conexão estabelecida com sucesso!")
        print(f"  📄 Total de edições listadas na página inicial: {len(edicoes)}")
        print(f"  🆕 Mais recente: Edição nº {mais_recente['numero']} ({mais_recente['data']})")
        print(f"  🔗 URL: {mais_recente['url_pdf']}")
        return True
    except Exception as e:
        print(f"  ❌ Erro ao acessar o portal da Prefeitura: {e}")
        return False


def test_extracao_segura():
    print("\n[5/5] 📄 Testando download e busca em 1 PDF (modo teste seguro)...")
    try:
        class ArgsMock:
            backfill = 1
            test = True
            daemon = False

        print("  📥 Baixando e processando a edição mais recente (sem gravar no banco)...")
        monitor.executar_uma_vez(ArgsMock())
        print("  ✅ Processamento do PDF e rotina concluídos com êxito!")
        return True
    except Exception as e:
        print(f"  ❌ Erro ao processar PDF: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Ferramenta de testes do Monitor Boletim Oficial Angra")
    parser.add_argument("--env", action="store_true", help="testa variáveis de ambiente e banco")
    parser.add_argument("--telegram", action="store_true", help="testa envio de mensagem para o Telegram")
    parser.add_argument("--interativo", action="store_true", help="aguarda mensagem no Telegram e responde de volta")
    parser.add_argument("--portal", action="store_true", help="testa leitura do portal de Angra")
    parser.add_argument("--extracao", action="store_true", help="testa extração de 1 PDF em modo seguro")
    parser.add_argument("--tudo", action="store_true", help="executa todos os testes em sequência")
    args = parser.parse_args()

    banner()

    monitor.carregar_env()

    if args.env:
        test_ambiente()
    elif args.telegram:
        test_telegram_envio()
    elif args.interativo:
        test_telegram_interativo()
    elif args.portal:
        test_portal()
    elif args.extracao:
        test_extracao_segura()
    elif args.tudo:
        test_ambiente()
        test_telegram_envio()
        test_portal()
        test_extracao_segura()
        test_telegram_interativo(tempo_limite_segundos=45)
    else:
        # Modo interativo CLI no terminal
        while True:
            print("\nSelecione o teste que deseja executar:")
            print("  1. Validar ambiente, tokens e banco SQLite")
            print("  2. Testar envio de mensagem para o Telegram (Push)")
            print("  3. Teste Interativo: Enviar mensagem no Telegram e receber resposta")
            print("  4. Testar conexão e listagem do portal da Prefeitura")
            print("  5. Testar download e leitura de 1 PDF (modo seguro)")
            print("  6. Executar todos os testes")
            print("  0. Sair")
            escolha = input("\nDigite a opção [0-6]: ").strip()

            if escolha == "1":
                test_ambiente()
            elif escolha == "2":
                test_telegram_envio()
            elif escolha == "3":
                test_telegram_interativo()
            elif escolha == "4":
                test_portal()
            elif escolha == "5":
                test_extracao_segura()
            elif escolha == "6":
                test_ambiente()
                test_telegram_envio()
                test_portal()
                test_extracao_segura()
                test_telegram_interativo()
            elif escolha == "0":
                print("Saindo...")
                break
            else:
                print("Opção inválida.")


if __name__ == "__main__":
    main()
