#!/usr/bin/env python3
"""
Monitor do Boletim Oficial de Angra dos Reis.

Busca, nos PDFs publicados em https://angra.rj.gov.br/boletim-oficial,
os termos configurados (cargo + nome) e envia alertas via Telegram.

Uso:
    python monitor.py                    # execução normal (só edições novas)
    python monitor.py --backfill 5       # reprocessa as 5 edições mais recentes
    python monitor.py --test             # modo teste: não grava no banco
    python monitor.py --backfill 3 --test  # testa a extração sem sujar o estado
"""

import argparse
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time

import unicodedata
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
import pdfplumber

BASE_URL = "https://angra.rj.gov.br/boletim-oficial"
DB_PATH = os.path.join(os.path.dirname(__file__), "state", "boletins.db")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

TIMEOUT_LISTAGEM = 20
TIMEOUT_DOWNLOAD = 60
MAX_TENTATIVAS = 4
EXECUCAO_LOCK = threading.Lock()


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_RE_TOKEN_TELEGRAM = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def ocultar_segredos(texto):
    """Mascara tokens do Telegram (ex.: em URLs de exceções do requests) e o valor atual do token/chat."""
    texto = _RE_TOKEN_TELEGRAM.sub("bot<TOKEN_OCULTO>", str(texto))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        texto = texto.replace(token, "<TOKEN_OCULTO>")
    return texto


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {ocultar_segredos(msg)}", flush=True)


def normalizar(texto):
    """minúsculas, sem acento, e reconecta hifenização de quebra de linha."""
    texto = texto.lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.replace("\xad", "")   # remove soft hyphens invisíveis usados em diagramação de PDFs
    texto = re.sub(r"[\u2010\u2011\u2012\u2013\u2014]", "-", texto)  # unifica traços tipográficos
    texto = re.sub(r"-\s*\n\s*", "", texto)   # "inclu-\nsao" -> "inclusao"
    texto = re.sub(r"\s+", " ", texto)
    return texto


def carregar_env():
    for fname in (".env", "env"):
        env_file = os.path.join(os.path.dirname(__file__), fname)
        if os.path.exists(env_file):
            try:
                with open(env_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                        elif ":" in line:
                            k, v = line.split(":", 1)
                        else:
                            continue
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
            except Exception as e:
                log(f"aviso: nao foi possivel ler {fname}: {e}")



def carregar_config():
    carregar_env()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # o nome completo NUNCA fica no repositório: vem só da secret NOME_BUSCA
    nome_extra = os.environ.get("NOME_BUSCA")
    if nome_extra and nome_extra.strip():
        cfg["termos_busca"].append(nome_extra.strip())
    # Deduplica termos preservando a ordem
    cfg["termos_busca"] = list(dict.fromkeys(t.strip() for t in cfg["termos_busca"] if t and t.strip()))
    cfg["termos_normalizados"] = [normalizar(t) for t in cfg["termos_busca"]]
    return cfg


def request_com_retry(url, timeout, metodo="get", headers=None, **kwargs):
    espera = 2
    ultimo_erro = None
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = requests.request(metodo, url, timeout=timeout, headers=hdrs, **kwargs)
            if resp.status_code in (500, 502, 503, 429):
                raise requests.HTTPError(f"status {resp.status_code}")
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            ultimo_erro = e
            if tentativa == MAX_TENTATIVAS:
                raise
            log(f"tentativa {tentativa}/{MAX_TENTATIVAS} falhou ({e}); aguardando {espera}s")
            time.sleep(espera)
            espera *= 2
    raise ultimo_erro


def listar_edicoes(pagina_url=BASE_URL):
    """
    Retorna lista de dicts {numero, data, url_pdf}, na ordem em que aparecem
    na página (mais recente primeiro, conforme o site).

    OBS: o parsing procura qualquer <a href=*.pdf> e sobe até a <tr> pai para
    achar data (regex dd/mm/aaaa) e número (célula puramente numérica de 3-5
    dígitos), em vez de depender de um índice fixo de coluna — mais resistente
    a mudanças no HTML.
    """
    resp = request_com_retry(pagina_url, TIMEOUT_LISTAGEM)
    soup = BeautifulSoup(resp.text, "html.parser")

    edicoes = []
    vistos = set()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href.lower().endswith(".pdf"):
            continue
        url_pdf = urllib.parse.urljoin(pagina_url, href)
        if url_pdf in vistos:
            continue
        vistos.add(url_pdf)

        linha = link.find_parent("tr")
        if not linha:
            continue
        texto_linha = linha.get_text(" ", strip=True)

        data_match = re.search(r"\d{2}/\d{2}/\d{4}", texto_linha)
        data_pub = data_match.group(0) if data_match else None

        numero = None
        for celula in linha.find_all("td"):
            txt = celula.get_text(strip=True)
            if txt.isdigit() and 3 <= len(txt) <= 5:
                numero = int(txt)
                break

        # Ignora links de PDFs avulsos do site (ex: lista telefônica, manuais) que não são boletins
        if numero is None:
            continue

        edicoes.append({"numero": numero, "data": data_pub, "url_pdf": url_pdf})
    return edicoes


def baixar_pdf(url):
    resp = request_com_retry(url, TIMEOUT_DOWNLOAD)
    conteudo = resp.content
    if b"%PDF-" not in conteudo[:1024]:
        raise ValueError("conteudo baixado nao parece ser um PDF valido (cabecalho %PDF- ausente nos primeiros 1024 bytes)")
    return conteudo


def extrair_texto_por_pagina(conteudo_pdf):
    paginas = []
    with pdfplumber.open(io.BytesIO(conteudo_pdf)) as pdf:
        for i, pagina in enumerate(pdf.pages, start=1):
            try:
                texto = pagina.extract_text() or ""
            except Exception as e:
                log(f"aviso: falha ao extrair texto da pagina {i}: {e}")
                texto = ""
            paginas.append((i, texto))
    return paginas


def buscar_termos(paginas, termos_normalizados, termos_originais):
    ocorrencias = []
    for num_pagina, texto in paginas:
        texto_norm = normalizar(texto)
        for termo_norm, termo_original in zip(termos_normalizados, termos_originais):
            if not termo_norm or not termo_norm.strip():
                continue
            idx = texto_norm.find(termo_norm)
            if idx == -1:
                continue
            inicio = max(0, idx - 100)
            fim = min(len(texto_norm), idx + len(termo_norm) + 100)
            trecho = texto_norm[inicio:fim].strip()
            ocorrencias.append({"termo": termo_original, "pagina": num_pagina, "trecho": trecho})
    return ocorrencias


def enviar_telegram(token, chat_id, mensagem, parse_mode="HTML"):
    if not token or not chat_id:
        log("Telegram nao configurado (faltam secrets); mensagem que seria enviada:\n" + mensagem)
        return False
    if len(mensagem) > 4000:
        mensagem = mensagem[:3950] + "\n\n<i>[...texto truncado pelo limite do Telegram...]</i>"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensagem}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code == 400 and parse_mode:
            log(f"aviso: erro de parse HTML no Telegram ({resp.text}). Reenviando em texto puro...")
            payload.pop("parse_mode", None)
            payload["text"] = re.sub(r"<[^>]+>", "", mensagem)
            resp = requests.post(url, data=payload, timeout=15)
        if not resp.ok:
            log(f"ERRO Telegram ({resp.status_code}): {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        log(f"falha ao enviar mensagem no telegram: {e}")
        return False



def conectar_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS boletins_processados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero INTEGER,
            data_publicacao TEXT,
            url_pdf TEXT UNIQUE,
            hash_arquivo TEXT,
            status TEXT,
            data_processamento TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boletim_id INTEGER,
            termo TEXT,
            pagina INTEGER,
            trecho TEXT,
            FOREIGN KEY(boletim_id) REFERENCES boletins_processados(id)
        )
    """)
    conn.commit()
    return conn


def ja_processado(conn, url_pdf):
    cur = conn.execute(
        "SELECT id FROM boletins_processados WHERE url_pdf = ? AND status IN ('PROCESSADO', 'COM_ALERTA')",
        (url_pdf,)
    )
    return cur.fetchone() is not None


def maior_numero_registrado(conn):
    cur = conn.execute("SELECT MAX(numero) FROM boletins_processados WHERE status IN ('PROCESSADO', 'COM_ALERTA')")
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def registrar_boletim(conn, edicao, hash_arquivo, status, teste):
    if teste:
        return None
    cur = conn.execute(
        """INSERT INTO boletins_processados
           (numero, data_publicacao, url_pdf, hash_arquivo, status, data_processamento)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(url_pdf) DO UPDATE SET
           numero=excluded.numero,
           data_publicacao=excluded.data_publicacao,
           hash_arquivo=excluded.hash_arquivo,
           status=excluded.status,
           data_processamento=excluded.data_processamento""",
        (edicao["numero"], edicao["data"], edicao["url_pdf"], hash_arquivo, status,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    boletim_id = cur.lastrowid
    if not boletim_id:
        cur_id = conn.execute("SELECT id FROM boletins_processados WHERE url_pdf = ?", (edicao["url_pdf"],)).fetchone()
        boletim_id = cur_id[0] if cur_id else None
    return boletim_id


def registrar_matches(conn, boletim_id, ocorrencias, teste):
    if teste or not boletim_id:
        return
    conn.execute("DELETE FROM matches WHERE boletim_id = ?", (boletim_id,))
    for oc in ocorrencias:
        conn.execute(
            "INSERT INTO matches (boletim_id, termo, pagina, trecho) VALUES (?, ?, ?, ?)",
            (boletim_id, oc["termo"], oc["pagina"], oc["trecho"]),
        )
    conn.commit()


def processar_edicao(conn, edicao, cfg, teste):
    log(f"processando edicao nº {edicao['numero']} ({edicao['data']}) — {edicao['url_pdf']}")
    prefixo_teste = "[TESTE] " if teste else ""
    num_txt = html.escape(str(edicao['numero'] or 'desconhecido'))
    data_txt = html.escape(str(edicao['data'] or 'desconhecida'))

    try:
        conteudo = baixar_pdf(edicao["url_pdf"])
    except Exception as e:
        log(f"ERRO ao baixar: {e}")
        enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                         f"{prefixo_teste}⚠️ Erro ao baixar boletim nº {num_txt}: {html.escape(str(e))}")
        return False, False

    hash_arquivo = hashlib.sha256(conteudo).hexdigest()

    try:
        paginas = extrair_texto_por_pagina(conteudo)
    except Exception as e:
        log(f"ERRO ao extrair texto: {e}")
        enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                         f"{prefixo_teste}⚠️ Erro ao ler PDF do boletim nº {num_txt}: {html.escape(str(e))}")
        return False, False

    ocorrencias = buscar_termos(paginas, cfg["termos_normalizados"], cfg["termos_busca"])
    status = "COM_ALERTA" if ocorrencias else "PROCESSADO"
    boletim_id = registrar_boletim(conn, edicao, hash_arquivo, status, teste)
    registrar_matches(conn, boletim_id, ocorrencias, teste)

    for oc in ocorrencias:
        termo_esc = html.escape(str(oc['termo']))
        trecho_esc = html.escape(str(oc['trecho']))
        url_esc = html.escape(str(edicao['url_pdf']))
        msg = (f"{prefixo_teste}🚨 <b>Termo encontrado!</b>\n"
               f"Boletim nº {num_txt} ({data_txt})\n"
               f"Termo: <code>{termo_esc}</code>\n"
               f"Página: {oc['pagina']}\n"
               f"Trecho: <i>...{trecho_esc}...</i>\n"
               f"Link: {url_esc}")
        enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg)
        log(f"MATCH: termo='{oc['termo']}' pagina={oc['pagina']}")

    return True, bool(ocorrencias)


def executar_uma_vez(args):
    if not EXECUCAO_LOCK.acquire(blocking=False):
        log("Uma verificacao ja esta em andamento; ignorando execucao concorrente.")
        return {"status": "ocupado", "houve_alerta": False}
    try:
        houve_alerta = _executar_uma_vez_interno(args)
        return {"status": "sucesso", "houve_alerta": bool(houve_alerta)}
    except Exception as e:
        log(f"Erro inesperado durante execucao: {e}")
        return {"status": "erro", "erro": str(e), "houve_alerta": False}
    finally:
        EXECUCAO_LOCK.release()


def _executar_uma_vez_interno(args):
    cfg = carregar_config()
    cfg["telegram_token"] = os.environ.get("TELEGRAM_BOT_TOKEN")
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID")

    log(f"termos configurados: {cfg['termos_busca']}")

    conn = conectar_db()
    try:
        try:
            edicoes = listar_edicoes()
        except Exception as e:
            log(f"ERRO ao listar edicoes no portal: {e}")
            enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                            f"⚠️ Erro ao consultar o portal da Prefeitura de Angra: {html.escape(str(e))}")
            if not getattr(args, "daemon", False):
                sys.exit(1)
            return False

        if not edicoes:
            log("ERRO: nenhuma edicao encontrada na pagina — o parsing pode ter quebrado")
            enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                             "⚠️ Falha ao consultar o portal da Prefeitura de Angra: nenhuma edição encontrada na listagem.")
            if not getattr(args, "daemon", False):
                sys.exit(1)
            return False

        mais_recente = edicoes[0]
        maior_numero_no_banco = maior_numero_registrado(conn)

        log(f"edicao mais recente na pagina: nº {mais_recente['numero']} ({mais_recente['data']})")
        log(f"maior numero ja registrado no banco: {maior_numero_no_banco}")

        if args.backfill:
            alvo = edicoes[:args.backfill]
        else:
            alvo = [e for e in edicoes if not ja_processado(conn, e["url_pdf"])]

        log(f"{len(alvo)} edicao(oes) selecionada(s) para processar")

        houve_alerta = False
        houve_falha = False
        # Processa na ordem cronológica (mais antiga para mais recente)
        for edicao in reversed(alvo):
            ok, achou = processar_edicao(conn, edicao, cfg, args.test)
            if not ok:
                houve_falha = True
            if achou:
                houve_alerta = True

        aviso_lacuna = ""
        # Só checa lacuna se não for backfill e se houver salto não coberto pelas edições do lote
        if maior_numero_no_banco and mais_recente["numero"] and not getattr(args, "backfill", 0):
            diferenca = mais_recente["numero"] - maior_numero_no_banco
            if diferenca > max(1, len(alvo)):
                puladas = diferenca - len(alvo)
                aviso_lacuna = (f"\n⚠️ Possível lacuna: último número no banco era {maior_numero_no_banco}, "
                                 f"página mostra {mais_recente['numero']} ({puladas} edição(ões) intermediária(s) não encontrada(s)). "
                                 f"Confira se alguma edição foi pulada.")

        if not houve_alerta:
            prefixo_teste = "[TESTE] " if args.test else ""
            num_txt = html.escape(str(mais_recente['numero'] or 'desconhecido'))
            data_txt = html.escape(str(mais_recente['data'] or 'desconhecida'))
            if houve_falha:
                msg = (f"{prefixo_teste}⚠️ Verificação concluída com falhas. "
                       f"Última edição vista: nº {num_txt} ({data_txt}). "
                       f"Ocorreu erro ao baixar/ler uma ou mais edições (ver logs acima).")
            else:
                msg = (f"{prefixo_teste}🟢 Verificação concluída. "
                       f"Última edição vista: nº {num_txt} ({data_txt}). "
                       f"{len(alvo)} edição(ões) verificada(s), nenhum termo novo encontrado."
                       f"{aviso_lacuna}")
            enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg)
            log(msg)
        elif aviso_lacuna:
            enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], aviso_lacuna.strip())

        return houve_alerta
    finally:
        conn.close()


def responder_comando(texto, cfg):
    partes = texto.strip().split()
    cmd = partes[0].lower().split("@")[0] if partes else ""
    brt_tz = timezone(timedelta(hours=-3))
    agora = datetime.now(brt_tz).strftime("%d/%m/%Y %H:%M:%S")

    if cmd in ("/ping", "/teste"):
        return (f"🏓 <b>Pong!</b>\n\n"
                f"O monitor do Boletim Oficial de Angra está ativo e operacional no servidor!\n"
                f"⏱️ <b>Horário no servidor:</b> {agora} (BRT)")

    elif cmd == "/status":
        conn = conectar_db()
        try:
            cur = conn.execute("SELECT count(*), MAX(numero) FROM boletins_processados WHERE status IN ('PROCESSADO', 'COM_ALERTA')")
            total, maior = cur.fetchone()
            cur_alertas = conn.execute("SELECT count(*) FROM matches")
            total_matches = cur_alertas.fetchone()[0]
        finally:
            conn.close()

        termos = ", ".join(f"<code>{html.escape(t)}</code>" for t in cfg.get("termos_busca", []))
        return (f"📊 <b>Status do Monitor de Boletins</b>\n\n"
                f"🟢 <b>Status:</b> Operacional (servidor ativo)\n"
                f"🕒 <b>Horário BRT:</b> {agora}\n"
                f"📚 <b>Boletins processados no banco:</b> {total or 0}\n"
                f"📄 <b>Última edição registrada:</b> nº {maior or 'Nenhuma'}\n"
                f"🎯 <b>Total de ocorrências salvas:</b> {total_matches}\n"
                f"🔍 <b>Termos monitorados:</b>\n{termos}\n\n"
                f"⏰ <b>Rotinas diárias automáticas:</b> 12:00 e 19:00 BRT")

    elif cmd in ("/verificar", "/check", "/checar"):
        enviar_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                        "🔎 <i>Iniciando verificação sob demanda no portal da Prefeitura...</i>")
        resultado = executar_uma_vez(argparse.Namespace(backfill=0, test=False, daemon=True))
        if resultado.get("status") == "ocupado":
            return "⏳ Já havia uma verificação em andamento. Aguarde o término."
        elif resultado.get("status") == "erro":
            return f"⚠️ Erro ao realizar verificação sob demanda: {html.escape(str(resultado.get('erro', 'desconhecido')))}"
        return "✅ Verificação sob demanda finalizada."

    elif cmd in ("/ajuda", "/help", "/start"):
        return ("🤖 <b>Comandos do Monitor Boletim Oficial Angra:</b>\n\n"
                "• /status — Exibe dados do banco, termos e saúde do serviço\n"
                "• /verificar — Força uma checagem imediata no portal da Prefeitura\n"
                "• /ping — Testa se o bot está respondendo no servidor\n"
                "• /ajuda — Exibe esta lista de comandos\n\n"
                "💡 <i>Você também recebe alertas automáticos diários às 12:00 e 19:00 quando novas edições forem publicadas.</i>")

    else:
        return (f"Olá! Recebi sua mensagem: <i>{html.escape(texto)}</i>\n\n"
                f"Envie /ajuda para ver os comandos disponíveis.")


def ouvir_comandos_telegram(cfg=None, parar_evento=None):
    if cfg is None:
        cfg = carregar_config()
        cfg["telegram_token"] = os.environ.get("TELEGRAM_BOT_TOKEN")
        cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID")
    token = cfg.get("telegram_token")
    chat_id_autorizado = str(cfg.get("telegram_chat_id") or "")
    if not token:
        log("Telegram nao configurado. Escuta de comandos desativada.")
        return

    log("Iniciando escuta de comandos no Telegram (long-polling)...")
    offset = None
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates?offset=-1", timeout=10)
        if r.ok and r.json().get("result"):
            offset = r.json()["result"][-1]["update_id"] + 1
    except Exception as e:
        log(f"aviso ao verificar updates pendentes: {e}")

    while parar_evento is None or not parar_evento.is_set():
        try:
            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=25)
            if not r.ok:
                time.sleep(3)
                continue
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                remetente_id = str(msg.get("chat", {}).get("id", ""))
                texto_msg = msg.get("text", "").strip()
                nome_usuario = msg.get("from", {}).get("first_name", "Usuario")

                if chat_id_autorizado and remetente_id != chat_id_autorizado:
                    log(f"Comando ignorado: remetente {remetente_id} ({nome_usuario}) nao autorizado.")
                    continue

                if not texto_msg:
                    continue

                log(f"Comando recebido de {nome_usuario} ({remetente_id}): {texto_msg}")
                resposta = responder_comando(texto_msg, cfg)
                enviar_telegram(token, remetente_id, resposta)
        except requests.RequestException:
            time.sleep(5)
        except Exception as e:
            log(f"Erro no loop de escuta Telegram: {e}")
            time.sleep(3)


def rodar_daemon():
    brt_tz = timezone(timedelta(hours=-3))
    cfg = carregar_config()
    cfg["telegram_token"] = os.environ.get("TELEGRAM_BOT_TOKEN")
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID")

    log("Modo daemon ativado: verificações agendadas diariamente às 12:00 e 19:00 (BRT)")

    # Inicia a escuta de comandos interativos do Telegram em thread de fundo
    t_telegram = threading.Thread(target=ouvir_comandos_telegram, args=(cfg,), daemon=True)
    t_telegram.start()
    log("Thread de comandos interativos do Telegram iniciada com sucesso.")

    # Executa a primeira checagem imediatamente ao subir o container/serviço
    try:
        executar_uma_vez(argparse.Namespace(backfill=0, test=False, daemon=True))
    except Exception as e:
        log(f"Erro na checagem inicial: {e}")

    slots_executados = set()
    while True:
        time.sleep(30)
        agora_brt = datetime.now(brt_tz)
        dia_atual = agora_brt.strftime("%Y-%m-%d")
        hora = agora_brt.hour
        minuto = agora_brt.minute

        # Janela das 12h (12:00 a 12:30)
        slot_12 = f"{dia_atual}_12"
        if hora == 12 and 0 <= minuto <= 30 and slot_12 not in slots_executados:
            slots_executados.add(slot_12)
            log("Executando verificação agendada das 12:00 (BRT)...")
            try:
                executar_uma_vez(argparse.Namespace(backfill=0, test=False, daemon=True))
            except Exception as e:
                log(f"Erro na execução agendada das 12h: {e}")

        # Janela das 19h (19:00 a 19:30)
        slot_19 = f"{dia_atual}_19"
        if hora == 19 and 0 <= minuto <= 30 and slot_19 not in slots_executados:
            slots_executados.add(slot_19)
            log("Executando verificação agendada das 19:00 (BRT)...")
            try:
                executar_uma_vez(argparse.Namespace(backfill=0, test=False, daemon=True))
            except Exception as e:
                log(f"Erro na execução agendada das 19h: {e}")

        if len(slots_executados) > 20:
            slots_executados = {s for s in slots_executados if s.startswith(dia_atual)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, default=0,
                         help="reprocessa as N edições mais recentes (ignora checagem de já-processado)")
    parser.add_argument("--test", action="store_true",
                         help="modo teste: não grava no banco (não marca como processado)")
    parser.add_argument("--daemon", action="store_true",
                         help="executa em background contínuo às 12:00 e 19:00 BRT com comandos interativos")
    parser.add_argument("--listen", action="store_true",
                         help="inicia apenas a escuta interativa de comandos no Telegram")
    args = parser.parse_args()

    if args.daemon:
        rodar_daemon()
    elif args.listen:
        ouvir_comandos_telegram()
    else:
        executar_uma_vez(args)


if __name__ == "__main__":
    main()

