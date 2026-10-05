#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VIGIA DO CHECKOUT — centerpayhub.com

Verifica o checkout como um cliente anonimo de verdade: busca a pagina E
cada arquivo CSS/JS critico pelo mesmo caminho que o navegador do cliente
percorre (passando pelo Cloudflare). Foi desenhado a partir da falha de
23-24/09/2026, em que o servidor entregava HTML 200 perfeito enquanto o
checkout estava inutilizavel.

Nao instala nada no WordPress. Nao precisa de senha do site.
Roda com o Python que ja vem no macOS. Sem dependencias externas.
"""

import gzip
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# Horario de Brasilia (UTC-3, fixo — o Brasil nao usa mais horario de verao).
# Garante o mesmo horario no Mac e na nuvem (GitHub Actions roda em UTC).
BRT = timezone(timedelta(hours=-3))


def agora():
    return datetime.now(BRT)

# ------------------------------------------------------------------ AJUSTES
URL_CHECKOUT = os.environ.get(
    "VIGIA_URL", "https://centerpayhub.com/secure-prosperity-code/"
)
TIMEOUT = int(os.environ.get("VIGIA_TIMEOUT", "25"))
# Avisa so depois de N falhas seguidas (evita alarme por instabilidade de rede)
FALHAS_PARA_ALERTAR = int(os.environ.get("VIGIA_FALHAS", "2"))

# Telegram (opcional). Se vazio, o alerta sai so no terminal/log/notificacao.
TG_TOKEN = os.environ.get("VIGIA_TG_TOKEN", "")
TG_CHAT = os.environ.get("VIGIA_TG_CHAT", "")

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_ESTADO = os.path.join(PASTA, "estado.json")
ARQ_LOG = os.path.join(PASTA, "vigia.log")

UA_CLIENTE = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)

# Marcas que PRECISAM estar no HTML do checkout.
MARCAS_HTML = {
    "formulario do checkout": r"woocommerce-checkout|form[^>]+class=\"[^\"]*checkout",
    "bloco de pagamento": r"wc-stripe-upe-element|wcstripe-payment-element",
    "botao de compra": r"place_order",
    "config da Stripe": r"wc_stripe_upe_params|wcStripeUPEParams",
    "chave publica live": r"pk_live_[A-Za-z0-9]{10,}",
}

# Arquivos que o navegador PRECISA conseguir baixar. Detectados no HTML.
ASSETS_CRITICOS = [
    ("JS do checkout WooCommerce", r"[^\"']+/woocommerce/assets/js/frontend/checkout[^\"']*\.js[^\"']*"),
    ("JS da Stripe (UPE)", r"[^\"']+woocommerce-gateway-stripe[^\"']*(?:upe|blocks|index)[^\"']*\.js[^\"']*"),
    ("jQuery", r"[^\"']+/wp-includes/js/jquery/jquery(?:\.min)?\.js[^\"']*"),
    ("CSS do checkout (Elementor)", r"[^\"']+/uploads/elementor/css/post-2312\.css[^\"']*"),
]


# ------------------------------------------------------------------ REDE
def _abrir(url, metodo="GET"):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, method=metodo)
    req.add_header("User-Agent", UA_CLIENTE)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Encoding", "gzip, deflate")
    req.add_header("Cache-Control", "no-cache")
    return urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)


def buscar(url, metodo="GET"):
    """Retorna (status, corpo_texto, tamanho_bytes, cabecalhos). NUNCA levanta."""
    try:
        r = _abrir(url, metodo)
    except urllib.error.HTTPError as e:
        try:
            bruto = e.read() or b""
        except Exception:
            bruto = b""
        return e.code, _texto(bruto, e.headers), len(bruto), dict(e.headers)
    except Exception as e:
        return 0, "ERRO DE CONEXAO: %s" % e, 0, {}
    # A leitura do corpo tambem falha: o servidor aceita a conexao e trava no
    # meio do envio. Era aqui que o vigia morria sem alertar (bug de 05/10/2026).
    try:
        bruto = r.read()
    except Exception as e:
        return 0, "ERRO DE LEITURA: %s" % e, 0, {}
    try:
        return r.status, _texto(bruto, r.headers), len(bruto), dict(r.headers)
    except Exception as e:
        return 0, "ERRO AO DECODIFICAR: %s" % e, 0, {}


def _texto(bruto, headers):
    try:
        if (headers.get("Content-Encoding") or "").lower() == "gzip":
            bruto = gzip.GzipFile(fileobj=io.BytesIO(bruto)).read()
    except Exception:
        pass
    return bruto.decode("utf-8", "replace")


def absolutizar(u):
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        m = re.match(r"(https?://[^/]+)", URL_CHECKOUT)
        return (m.group(1) if m else "") + u
    return u


# ------------------------------------------------------------------ CHECAGEM
def checar():
    """Executa a bateria. Retorna (ok:bool, problemas:list, detalhes:dict)."""
    problemas = []
    det = {"quando": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    t0 = time.time()

    status, html, tam, hdr = buscar(URL_CHECKOUT)
    det["status_pagina"] = status
    det["tamanho_html"] = tam
    det["ms_pagina"] = int((time.time() - t0) * 1000)

    if status != 200:
        problemas.append("A pagina do checkout respondeu %s (esperado 200)." % status)
        return False, problemas, det
    if tam < 20000:
        problemas.append(
            "O HTML veio pequeno demais (%s bytes) — pagina provavelmente incompleta." % tam
        )

    # 1) marcas obrigatorias no HTML
    faltando = [nome for nome, rx in MARCAS_HTML.items()
                if not re.search(rx, html, re.I)]
    det["marcas_faltando"] = faltando
    for nome in faltando:
        problemas.append("Sumiu do HTML: %s." % nome)

    # 2) os arquivos criticos baixam mesmo? (este e o teste que faltava)
    resultados = []
    for rotulo, rx in ASSETS_CRITICOS:
        m = re.search(rx, html)
        if not m:
            resultados.append({"arquivo": rotulo, "situacao": "nao referenciado no HTML"})
            problemas.append("O HTML nao referencia mais: %s." % rotulo)
            continue
        url = absolutizar(m.group(0).replace("&amp;", "&"))
        st, corpo, n, h = buscar(url)
        item = {"arquivo": rotulo, "status": st, "bytes": n,
                "url": url[:160],
                "cf": h.get("cf-cache-status", "-")}
        # um asset saudavel: 200 e com conteudo
        if st != 200:
            problemas.append("%s nao carrega (HTTP %s)." % (rotulo, st))
        elif n < 200:
            problemas.append("%s carregou vazio (%s bytes)." % (rotulo, n))
        elif re.search(r"<html|<!doctype", corpo[:400], re.I):
            # veio pagina de erro no lugar do arquivo
            problemas.append("%s veio como pagina HTML em vez do arquivo." % rotulo)
        resultados.append(item)
    det["assets"] = resultados

    return (len(problemas) == 0), problemas, det


# ------------------------------------------------------------------ ESTADO
def ler_estado():
    try:
        with open(ARQ_ESTADO) as f:
            return json.load(f)
    except Exception:
        return {"falhas_seguidas": 0, "alertado": False}


def gravar_estado(e):
    try:
        with open(ARQ_ESTADO, "w") as f:
            json.dump(e, f)
    except Exception:
        pass


def registrar(linha):
    carimbo = agora().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(ARQ_LOG, "a") as f:
            f.write("[%s] %s\n" % (carimbo, linha))
    except Exception:
        pass
    print("[%s] %s" % (carimbo, linha))


# ------------------------------------------------------------------ ALERTA
def avisar(titulo, corpo):
    """Dispara por VARIOS canais. Nenhum deles pode falhar em silencio."""
    entregues = []

    # 1) arquivo de alertas — sempre funciona, serve de prova do que foi disparado
    try:
        with open(os.path.join(PASTA, "ALERTAS.log"), "a") as f:
            f.write("[%s] %s\n%s\n%s\n" % (
                agora().strftime("%Y-%m-%d %H:%M:%S"), titulo, corpo, "-" * 60))
        entregues.append("arquivo")
    except Exception:
        pass

    # 2) som — independe de permissao de notificacao do macOS
    try:
        if os.path.exists("/System/Library/Sounds/Sosumi.aiff"):
            os.system("(afplay /System/Library/Sounds/Sosumi.aiff &) >/dev/null 2>&1")
            entregues.append("som")
    except Exception:
        pass

    # 3) notificacao do macOS — pode ser bloqueada pelo Foco/permissoes
    try:
        seguro = corpo.replace('"', "'").replace("\\", "")[:300]
        rc = os.system(
            'osascript -e \'display notification "%s" with title "%s" sound name "Sosumi"\' >/dev/null 2>&1'
            % (seguro, titulo.replace('"', "'"))
        )
        entregues.append("notificacao" if rc == 0 else "notificacao(FALHOU rc=%s)" % rc)
    except Exception:
        pass

    registrar("ALERTA DISPARADO [%s] %s" % (", ".join(entregues) or "nenhum canal", titulo))
    # telegram (se configurado)
    if TG_TOKEN and TG_CHAT:
        try:
            dados = json.dumps({
                "chat_id": TG_CHAT,
                "text": "%s\n\n%s" % (titulo, corpo),
                "disable_web_page_preview": True,
            }).encode()
            req = urllib.request.Request(
                "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
                data=dados, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            registrar("alerta enviado no Telegram")
        except Exception as e:
            registrar("FALHA ao enviar Telegram: %s" % e)


# ------------------------------------------------------------------ MAIN
def main():
    modo_teste = "--teste" in sys.argv

    ok, problemas, det = checar()
    estado = ler_estado()

    resumo_assets = " | ".join(
        "%s:%s" % (a["arquivo"].split()[0], a.get("status", "?")) for a in det.get("assets", [])
    )

    if ok:
        if estado.get("alertado"):
            avisar("✅ Checkout voltou ao normal",
                   "%s respondendo e todos os arquivos carregando." % URL_CHECKOUT)
            registrar("RECUPERADO — checkout normal de novo")
        registrar("ok — pagina %s, %s bytes, %sms | %s"
                  % (det["status_pagina"], det["tamanho_html"], det["ms_pagina"], resumo_assets))
        gravar_estado({"falhas_seguidas": 0, "alertado": False})
        if modo_teste:
            print(json.dumps(det, indent=1, ensure_ascii=False))
        return 0

    n = estado.get("falhas_seguidas", 0) + 1
    texto = "\n".join("• " + p for p in problemas)
    registrar("FALHA %s/%s — %s" % (n, FALHAS_PARA_ALERTAR, "; ".join(problemas)))

    if n >= FALHAS_PARA_ALERTAR and not estado.get("alertado"):
        avisar("🚨 CHECKOUT COM PROBLEMA",
               "%s\n\n%s\n\nConfira: %s" % (
                   agora().strftime("%d/%m %H:%M"), texto, URL_CHECKOUT))
        estado["alertado"] = True

    estado["falhas_seguidas"] = n
    gravar_estado(estado)
    if modo_teste:
        print(json.dumps(det, indent=1, ensure_ascii=False))
    return 1


def _blindado():
    """Qualquer exceção inesperada vira alerta, nunca morte silenciosa."""
    try:
        return main()
    except Exception as e:
        import traceback
        try:
            registrar("ERRO INTERNO DO VIGIA: %s: %s" % (type(e).__name__, e))
            with open(os.path.join(PASTA, "ALERTAS.log"), "a") as f:
                f.write("[%s] ERRO INTERNO\n%s\n%s\n" % (
                    agora().strftime("%Y-%m-%d %H:%M:%S"),
                    traceback.format_exc(), "-" * 60))
        except Exception:
            pass
        try:
            avisar("⚠️ VIGIA FALHOU",
                   "O monitor quebrou e NAO esta vigiando: %s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    sys.exit(_blindado())
