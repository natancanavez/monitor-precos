"""
monitor_precos.py

Colunas da planilha:
  A=SKU, B=Link ML, C=Link Fornecedor, D=Preço ML (número),
  E=Preço Fornecedor (número), F=PMC Máximo (fórmula do usuário),
  G=Status, H=Última Atualização, I=Descontos, J=Melhor Preço Unit.
"""

import re
import json
import time
import logging
import requests
import os
from dataclasses import dataclass
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    COMISSAO_ML, IMPOSTO_DAS, MARGEM_MIN, FRETE_FIXO,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler("/data/monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

DESCONTO = COMISSAO_ML + IMPOSTO_DAS + MARGEM_MIN

ML_ACCESS_TOKEN = os.environ.get("ML_ACCESS_TOKEN", "")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SHEETS_ID       = os.environ.get("SHEETS_ID", "")

COL_SKU        = 0   # A
COL_LINK_ML    = 1   # B
COL_LINK_FORN  = 2   # C
COL_PRECO_ML   = 3   # D
COL_PRECO_FORN = 4   # E  — P&P quando disponível, senão preço normal
COL_PMC        = 5   # F
COL_STATUS     = 6   # G
COL_ATUALIZADO = 7   # H
COL_DESCONTOS  = 8   # I
COL_MELHOR_UN  = 9   # J

# ---------------------------------------------------------------------------
# Credenciais e renovação de token ML
# ---------------------------------------------------------------------------

ML_CLIENT_ID     = "3934461305870964"
ML_CLIENT_SECRET = "TwDkUlKf3nAfKWD1FZUBOEKUSGzpbAZy"
ML_TOKENS_FILE   = "/data/ml_tokens.json"

_ML_INITIAL_ACCESS_TOKEN  = os.environ.get("ML_ACCESS_TOKEN", "")
_ML_INITIAL_REFRESH_TOKEN = os.environ.get("ML_REFRESH_TOKEN", "TG-6a17a3d4bff5d60001aa0b45-643972290")


def _carregar_tokens() -> dict:
    if os.path.exists(ML_TOKENS_FILE):
        try:
            with open(ML_TOKENS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "access_token":  _ML_INITIAL_ACCESS_TOKEN,
        "refresh_token": _ML_INITIAL_REFRESH_TOKEN,
    }


def _salvar_tokens(tokens: dict) -> None:
    os.makedirs(os.path.dirname(ML_TOKENS_FILE), exist_ok=True)
    with open(ML_TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def renovar_token_ml() -> str:
    tokens = _carregar_tokens()
    log.info("Renovando access_token ML...")
    r = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     ML_CLIENT_ID,
            "client_secret": ML_CLIENT_SECRET,
            "refresh_token": tokens["refresh_token"],
        },
        timeout=15,
    )
    if r.status_code == 200:
        data = r.json()
        tokens["access_token"]  = data["access_token"]
        tokens["refresh_token"] = data.get("refresh_token", tokens["refresh_token"])
        _salvar_tokens(tokens)
        log.info("Token ML renovado com sucesso ✅")
        return tokens["access_token"]
    else:
        log.error("Erro ao renovar token ML: %s %s", r.status_code, r.text)
        return tokens["access_token"]


def obter_token_ml() -> str:
    tokens = _carregar_tokens()
    r = requests.get(
        "https://api.mercadolibre.com/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=10,
    )
    if r.status_code == 401:
        return renovar_token_ml()
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# Estrutura de desconto
# ---------------------------------------------------------------------------

@dataclass
class Desconto:
    tipo: str            # 'cupom_pct', 'cupom_fixo', 'mpm'
    codigo: str = ""
    pct: float = 0.0     # fração: 0.10 para 10%
    fixo: float = 0.0    # valor fixo: 50.0 para -R$50
    min_valor: float = 0.0
    min_qtd: int = 1
    prime: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pmc_padrao(preco_ml: float) -> float:
    return round(preco_ml * (1 - DESCONTO) - FRETE_FIXO, 2)


def parse_valor(s: str) -> float | None:
    try:
        v = re.sub(r"[^\d.,]", "", str(s))
        if "," in v and "." in v:
            v = v.replace(".", "").replace(",", ".")
        elif "," in v:
            v = v.replace(",", ".")
        return float(v) if v else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def conectar_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    import json as _json
    info = _json.loads(os.environ.get("GOOGLE_CREDENTIALS", ""))
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEETS_ID).sheet1


# ---------------------------------------------------------------------------
# Mercado Livre
# ---------------------------------------------------------------------------

def extrair_item_id_ml(url: str) -> tuple:
    m = re.search(r'/p/(MLB\d+)', url, re.IGNORECASE)
    if m:
        return ('catalog', m.group(1).upper())
    m = re.search(r'(MLB-?\d+)', url, re.IGNORECASE)
    if m:
        return ('item', m.group(1).upper().replace("-", ""))
    return (None, None)


def extrair_preco_ml(url: str) -> float | None:
    try:
        tipo, item_id = extrair_item_id_ml(url)
        if not item_id:
            log.warning("Item ID ML não encontrado: %s", url)
            return None

        token = obter_token_ml()
        headers_ml = {"Authorization": f"Bearer {token}"}

        if tipo == 'catalog':
            resp = requests.get(
                f"https://api.mercadolibre.com/products/{item_id}/items",
                headers=headers_ml, timeout=15
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    winner = results[0]
                    preco = winner.get("price")
                    if preco:
                        log.info("API ML catálogo winner (%s -> %s): R$ %.2f",
                                 item_id, winner.get("item_id"), float(preco))
                        return float(preco)
            log.warning("ML catálogo sem resultado (%s, status %s)", item_id, resp.status_code)
            return None

        resp = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=headers_ml, timeout=15
        )
        if resp.status_code == 200:
            preco = resp.json().get("price")
            if preco:
                log.info("API ML item: %s → R$ %.2f", item_id, float(preco))
                return float(preco)

        log.warning("Preço ML não encontrado: %s (status %s)", item_id, resp.status_code)
        return None
    except Exception as e:
        log.error("Erro ML (%s): %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Fetch genérico
# ---------------------------------------------------------------------------

def fetch_url(url: str):
    try:
        if SCRAPER_API_KEY:
            api_url = (
                f"http://api.scraperapi.com"
                f"?api_key={SCRAPER_API_KEY}"
                f"&url={requests.utils.quote(url, safe='')}"
                f"&country_code=br&render=true"
            )
            resp = requests.get(api_url, timeout=60)
        else:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.error("Erro ao buscar URL (%s): %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Extração de preço do fornecedor
# Retorna (preco_coluna_e, preco_base_calc_j)
#   preco_coluna_e  = P&P se disponível, senão preço normal  → coluna E
#   preco_base_calc_j = preço original sem P&P               → base para coluna J
# ---------------------------------------------------------------------------

def extrair_preco_fornecedor_soup(soup, url: str) -> tuple[float | None, float | None]:
    """
    Retorna (preco_coluna_e, preco_base_j).

    Para Amazon:
      - preco_coluna_e  = Programe e Poupe (se disponível) ou preço normal
      - preco_base_j    = preço original (compra única, sem P&P)
        A Amazon aplica MpM e cupons sobre o preço original, então a base
        do cálculo da coluna J precisa ser esse valor.

    Para outros fornecedores:
      - ambos são iguais (sem distinção P&P/original)
    """
    if "amazon" not in url:
        p = _extrair_preco_generico(soup)
        if p:
            log.info("Preço fornecedor: R$ %.2f", p)
        else:
            log.warning("Preço fornecedor não encontrado: %s", url)
        return p, p

    # Amazon — captura preço original e P&P separadamente
    preco_original = _extrair_preco_original_amazon(soup)
    preco_pp       = _extrair_preco_pp_amazon(soup)

    preco_coluna_e = preco_pp if preco_pp else preco_original
    preco_base_j   = preco_original if preco_original else preco_coluna_e

    if preco_coluna_e is None:
        log.warning("Preço Amazon não encontrado: %s", url)

    return preco_coluna_e, preco_base_j


def _extrair_preco_generico(soup) -> float | None:
    for meta_name in ["product:price:amount", "og:price:amount"]:
        tag = soup.find("meta", property=meta_name)
        if tag and tag.get("content"):
            v = re.sub(r"[^\d.]", "", tag["content"].replace(",", "."))
            if v:
                return float(v)
    tag = soup.find(attrs={"itemprop": "price"})
    if tag:
        valor = tag.get("content") or tag.get_text(strip=True)
        valor = re.sub(r"[^\d.,]", "", valor).replace(".", "").replace(",", ".")
        if valor:
            return float(valor)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                price = offers.get("price")
                if price:
                    return float(price)
        except Exception:
            pass
    for sel in ["[class*='price'] [class*='value']", "[class*='preco']", "[class*='price']"]:
        el = soup.select_one(sel)
        if el:
            nums = re.findall(r"\d+[.,]\d{2}", el.get_text(strip=True))
            if nums:
                try:
                    return float(nums[0].replace(".", "").replace(",", "."))
                except Exception:
                    pass
    return None


def _extrair_preco_original_amazon(soup) -> float | None:
    """Preço de compra única (sem Programe e Poupe)."""
    for sel in [
        "#apex-pricetopay-accessibility-label",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
        "span.a-price-whole",
    ]:
        el = soup.select_one(sel)
        if el:
            texto = el.get("content") or el.get_text(strip=True)
            nums = re.findall(r"[\d]+[.,][\d]{2}", texto)
            if nums:
                try:
                    p = float(nums[0].replace(".", "").replace(",", "."))
                    if p > 0:
                        log.info("Amazon preço original (%s): R$ %.2f", sel, p)
                        return p
                except Exception:
                    pass
    return None


def _extrair_preco_pp_amazon(soup) -> float | None:
    """Preço Programe e Poupe."""
    for sel in ["#sns-tiered-price", "#sns-base-price",
                "#subscriptionPrice", "#snsAccordionRowMiddle"]:
        el = soup.select_one(sel)
        if el:
            nums = re.findall(r"[\d]+[.,][\d]{2}", el.get_text(strip=True))
            if nums:
                try:
                    p = float(nums[0].replace(".", "").replace(",", "."))
                    if p > 0:
                        log.info("Amazon P&P (%s): R$ %.2f", sel, p)
                        return p
                except Exception:
                    pass
    return None


# ---------------------------------------------------------------------------
# Extração de descontos → lista estruturada + texto coluna I
# Suporta Amazon hoje; expansível com blocos elif para outros fornecedores
# ---------------------------------------------------------------------------

def extrair_descontos(soup, url: str) -> tuple[list[Desconto], str]:
    """
    Retorna (lista_descontos, texto_coluna_I).
    Não inclui Programe e Poupe (já refletido no preço da coluna E).
    """
    is_amazon = "amazon" in url

    descontos: list[Desconto] = []
    texto_pagina = soup.get_text(" ")

    if is_amazon:
        # DEBUG: loga trecho relevante da página para diagnóstico
        _debug_trecho_descontos(texto_pagina)

        # 1. Cupons resgatáveis (botão Resgatar)
        blocos = _coletar_blocos_cupom_amazon(soup)
        if not blocos:
            blocos = [texto_pagina]
        for bloco in blocos:
            descontos.extend(_parsear_cupons_do_bloco(bloco))

        # 2. Mais por Menos
        mpm_re = re.compile(
            r'Mais\s+por\s+Menos[:\s]*\+?([\d]+)%\s*off\s*em\s*([\d]+)\+?\s*iten?s?',
            re.IGNORECASE,
        )
        for m in mpm_re.finditer(texto_pagina):
            descontos.append(Desconto(
                tipo='mpm',
                pct=float(m.group(1)) / 100,
                min_qtd=int(m.group(2)),
            ))

    # elif "outrofornecedor" in url: ...

    descontos = _deduplicar(descontos)

    partes = []
    for d in descontos:
        if d.tipo == 'cupom_pct':
            prime = "[Prime] " if d.prime else ""
            partes.append(f"{prime}{d.codigo} {int(d.pct*100)}%≥R${d.min_valor:.0f}")
        elif d.tipo == 'cupom_fixo':
            prime = "[Prime] " if d.prime else ""
            partes.append(f"{prime}{d.codigo} -R${d.fixo:.0f}≥R${d.min_valor:.0f}")
        elif d.tipo == 'mpm':
            partes.append(f"MpM {int(d.pct*100)}%≥{d.min_qtd}un")

    texto_i = " | ".join(partes)
    if texto_i:
        log.info("Descontos: %s", texto_i)
    else:
        log.info("Nenhum desconto encontrado na página.")
    return descontos, texto_i


def _debug_trecho_descontos(texto_pagina: str) -> None:
    """Loga trechos ao redor de cada palavra-chave de desconto."""
    encontrou = False
    for palavra in ["cupom", "Economize", "BOLANAREDE", "Resgatar"]:
        idx = texto_pagina.lower().find(palavra.lower())
        if idx >= 0:
            trecho = texto_pagina[max(0, idx-30):idx+200]
            trecho = " ".join(trecho.split())
            log.info("DEBUG [%s]: ...%s...", palavra, trecho)
            encontrou = True
    if not encontrou:
        log.info("DEBUG: nenhuma palavra-chave de desconto encontrada na página.")


def _coletar_blocos_cupom_amazon(soup) -> list[str]:
    seletores = [
        "#couponFeature", "#vpcButton",
        "[id*='coupon']", "[class*='couponFeature']",
        "[class*='vpc']", "[data-feature-name='couponFeature']",
        "[data-feature-name='vpcButton']",
    ]
    blocos = set()
    for sel in seletores:
        for el in soup.select(sel):
            t = " ".join(el.get_text(" ", strip=True).split())
            if t:
                blocos.add(t)
    return list(blocos)


def _parsear_cupons_do_bloco(texto: str) -> list[Desconto]:
    resultado = []

    # Percentual: "10% de desconto em pedidos a partir de R$100 cupom: CODIGO"
    # Usa "de desconto" (não "off") para não confundir com MpM que usa "X% off em N+ itens"
    for m in re.finditer(
        r'(\d+)%\s*de\s*desconto[^:]{0,150}cupom\s*:\s*(\w+)',
        texto, re.IGNORECASE
    ):
        trecho = m.group(0)
        vals = re.findall(r'[\d.,]+', trecho)
        # Último número no trecho é o valor mínimo do pedido
        min_val = parse_valor(vals[-1]) if len(vals) > 1 else 0.0
        ctx = texto[max(0, m.start()-200):m.start()]
        prime = bool(re.search(r'prime', ctx, re.IGNORECASE))
        resultado.append(Desconto(
            tipo='cupom_pct',
            codigo=m.group(2).upper(),
            pct=float(m.group(1)) / 100,
            min_valor=min_val,
            prime=prime,
        ))

    # Fixo: "Economize R$50 em pedidos R$450+ cupom: CODIGO"
    for m in re.finditer(
        r'[Ee]conomize\s+R?\$?\s*([\d.,]+)[^:]{0,80}cupom\s*:\s*(\w+)',
        texto, re.IGNORECASE
    ):
        trecho = m.group(0)
        vals = re.findall(r'[\d.,]+', trecho)
        # group(1) = valor do desconto; último número = valor mínimo do pedido
        min_val = parse_valor(vals[-1]) if len(vals) >= 2 else 0.0
        ctx = texto[max(0, m.start()-200):m.start()]
        prime = bool(re.search(r'prime', ctx, re.IGNORECASE))
        resultado.append(Desconto(
            tipo='cupom_fixo',
            codigo=m.group(2).upper(),
            fixo=parse_valor(m.group(1)) or 0.0,
            min_valor=min_val,
            prime=prime,
        ))

    return resultado


def _deduplicar(descontos: list[Desconto]) -> list[Desconto]:
    vistos = set()
    saida = []
    for d in descontos:
        chave = (d.tipo, d.codigo, d.pct, d.fixo, d.min_valor, d.min_qtd)
        if chave not in vistos:
            vistos.add(chave)
            saida.append(d)
    return saida


# ---------------------------------------------------------------------------
# Cálculo do melhor preço unitário (coluna J)
#
# A Amazon aplica todos os descontos sobre o preco_original (compra única):
#   total = preco_original × qtd × (1 - pct_mpm - pct_cupons) - fixos_cupons
#
# O preco_base passado aqui é o preco_original (não o P&P), garantindo
# que o resultado bata com o que o checkout da Amazon mostra.
# ---------------------------------------------------------------------------

def calcular_melhor_preco_unitario(
    preco_original: float,
    descontos: list[Desconto],
) -> tuple[float, int]:
    """
    Retorna (preco_unitario, quantidade_necessaria).

    Ordem de aplicação (espelha o checkout da Amazon):
      1. MpM  → % sobre subtotal original
      2. Cupom % → % sobre subtotal original (acumulativo)
      3. Cupom fixo → subtrai diretamente
    """
    tiers_mpm = sorted(
        [(d.min_qtd, d.pct) for d in descontos if d.tipo == 'mpm'],
        key=lambda x: x[0]
    )

    qtds_testar = {1}
    for qtd, _ in tiers_mpm:
        qtds_testar.add(qtd)
    for d in descontos:
        if d.min_valor > 0 and preco_original > 0:
            qtd_necessaria = int(d.min_valor / preco_original) + 1
            qtds_testar.add(qtd_necessaria)

    melhor_unitario = preco_original
    melhor_qtd = 1

    for qtd in sorted(qtds_testar):
        subtotal = qtd * preco_original

        # MpM acumulativo: soma todos os tiers válidos para a quantidade
        pct_mpm_total = sum(pct for min_qtd, pct in tiers_mpm if qtd >= min_qtd)

        # Cupons % acumulativos
        pct_cupom_total = sum(
            d.pct for d in descontos
            if d.tipo == 'cupom_pct' and subtotal >= d.min_valor
        )

        # Total % de desconto (sobre subtotal original)
        pct_total = pct_mpm_total + pct_cupom_total

        # Cupons fixos
        fixo_total = sum(
            d.fixo for d in descontos
            if d.tipo == 'cupom_fixo' and subtotal >= d.min_valor
        )

        total_final = subtotal * (1 - pct_total) - fixo_total

        if total_final <= 0:
            continue

        unitario = total_final / qtd
        if unitario < melhor_unitario:
            melhor_unitario = unitario
            melhor_qtd = qtd

    return round(melhor_unitario, 2), melhor_qtd


# ---------------------------------------------------------------------------
# Orquestração: preço + descontos + melhor unitário (uma requisição por SKU)
# ---------------------------------------------------------------------------

def processar_fornecedor(url: str) -> tuple[float | None, str, str]:
    """
    Retorna (preco_coluna_e, texto_coluna_i, texto_coluna_j).
    Faz apenas uma requisição HTTP.
    """
    from bs4 import BeautifulSoup

    resp = fetch_url(url)
    if not resp:
        return None, "", ""

    soup = BeautifulSoup(resp.text, "lxml")

    preco_e, preco_base_j = extrair_preco_fornecedor_soup(soup, url)
    if preco_e is None:
        return None, "", ""

    descontos, texto_i = extrair_descontos(soup, url)

    texto_j = ""
    if preco_base_j:
        # Se há P&P, inclui o desconto implícito do P&P no cálculo da coluna J.
        # A Amazon aplica P&P junto com MpM e cupons, todos sobre o preço original.
        descontos_calc = list(descontos)
        if preco_e and preco_base_j > preco_e:
            pct_pp = (preco_base_j - preco_e) / preco_base_j
            descontos_calc.append(Desconto(tipo='cupom_pct', codigo='P&P', pct=pct_pp))
            log.info("P&P incluído no cálculo J: %.1f%%", pct_pp * 100)

        if descontos_calc:
            melhor_un, melhor_qtd = calcular_melhor_preco_unitario(preco_base_j, descontos_calc)
            texto_j = f"R${melhor_un:,.2f} ({melhor_qtd}un)"
            log.info("Melhor unitário: %s", texto_j)

    return preco_e, texto_i, texto_j


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def telegram_send(mensagem: str) -> None:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "SEU_TOKEN_AQUI":
        log.warning("Telegram não configurado.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram: mensagem enviada.")
    except Exception as e:
        log.error("Erro Telegram: %s", e)


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def processar() -> None:
    try:
        ws = conectar_sheets()
        log.info("Conectado ao Google Sheets ✅")
    except Exception as e:
        log.error("Erro ao conectar Google Sheets: %s", e)
        return

    todos_dados = ws.get_all_values()
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    alertas = []

    for row_idx, row in enumerate(todos_dados[1:], start=2):
        if len(row) < 3:
            continue

        sku       = row[COL_SKU].strip()
        link_ml   = row[COL_LINK_ML].strip()
        link_forn = row[COL_LINK_FORN].strip()

        if not sku or not link_ml or not link_forn:
            continue

        status_ant = row[COL_STATUS].strip() if len(row) > COL_STATUS else ""

        log.info("Processando SKU %s ...", sku)

        preco_ml = extrair_preco_ml(link_ml)
        time.sleep(1.5)

        preco_forn, texto_i, texto_j = processar_fornecedor(link_forn)
        time.sleep(1.5)

        # Grava I e J sempre que tiver dados do fornecedor
        if texto_i or texto_j:
            ws.update(f"I{row_idx}:J{row_idx}", [[texto_i, texto_j]])
            time.sleep(0.3)

        if preco_ml is None or preco_forn is None:
            ws.update(f"G{row_idx}:H{row_idx}", [["⚠️ Erro na leitura", agora]])
            continue

        ws.update(f"D{row_idx}:E{row_idx}", [[round(preco_ml, 2), round(preco_forn, 2)]])
        time.sleep(0.5)

        pmc_atualizado = parse_valor(ws.cell(row_idx, COL_PMC + 1).value)
        pmc = pmc_atualizado if pmc_atualizado else pmc_padrao(preco_ml)
        origem = "planilha" if pmc_atualizado else "padrão"
        log.info("PMC (%s): R$ %.2f", origem, pmc)

        if preco_forn > pmc:
            status = "🚨 ACIMA DO PMC"
            if status_ant != status:
                extra = f"\nDescontos: {texto_i}\nMelhor unit.: {texto_j}" if texto_i else ""
                alertas.append(
                    f"🚨 <b>ALERTA — Fornecedor acima do PMC</b>\n"
                    f"SKU: <code>{sku}</code>\n"
                    f"Preço ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Máximo: R$ {pmc:,.2f}\n"
                    f"Preço Fornecedor: R$ {preco_forn:,.2f} ❌"
                    f"{extra}\n"
                    f"Data: {agora}"
                )
        else:
            status = "✅ OK"
            if status_ant == "🚨 ACIMA DO PMC":
                extra = f"\nDescontos: {texto_i}\nMelhor unit.: {texto_j}" if texto_i else ""
                alertas.append(
                    f"✅ <b>NORMALIZADO — Fornecedor voltou abaixo do PMC</b>\n"
                    f"SKU: <code>{sku}</code>\n"
                    f"Preço ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Máximo: R$ {pmc:,.2f}\n"
                    f"Preço Fornecedor: R$ {preco_forn:,.2f} ✅"
                    f"{extra}\n"
                    f"Data: {agora}"
                )

        ws.update(f"G{row_idx}:H{row_idx}", [[status, agora]])
        log.info(
            " ML=R$%.2f Forn=R$%.2f PMC=R$%.2f → %s | I=%s | J=%s",
            preco_ml, preco_forn, pmc, status,
            texto_i or "—", texto_j or "—"
        )
        time.sleep(1)

    log.info("Planilha atualizada no Google Sheets ✅")

    for alerta in alertas:
        telegram_send(alerta)

    if not alertas:
        log.info("Nenhuma mudança de status detectada.")


if __name__ == "__main__":
    log.info("=== Container iniciado — aguardando agendamento do Dokploy ===")
    while True:
        time.sleep(60)
