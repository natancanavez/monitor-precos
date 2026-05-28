"""
monitor_precos.py

Colunas da planilha:
  A=SKU, B=Link ML, C=Link Fornecedor, D=Preço ML (número),
  E=Preço Fornecedor (número), F=PMC Máximo (fórmula do usuário),
  G=Status, H=Última Atualização, I=Descontos Amazon, J=Melhor Preço Unit.
"""

import re
import json
import time
import logging
import requests
import os
from dataclasses import dataclass, field
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
COL_PRECO_FORN = 4   # E
COL_PMC        = 5   # F
COL_STATUS     = 6   # G
COL_ATUALIZADO = 7   # H
COL_DESCONTOS  = 8   # I
COL_MELHOR_UN  = 9   # J


# ---------------------------------------------------------------------------
# Estrutura de desconto
# ---------------------------------------------------------------------------

@dataclass
class Desconto:
    tipo: str            # 'cupom_pct', 'cupom_fixo', 'mpm', 'pp'
    codigo: str = ""     # código do cupom (vazio para mpm/pp)
    pct: float = 0.0     # ex: 0.10 para 10%
    fixo: float = 0.0    # ex: 50.0 para -R$50
    min_valor: float = 0.0   # pedido mínimo em R$ para o cupom ser válido
    min_qtd: int = 1         # qtd mínima (MpM)
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

        headers_ml = {"Authorization": f"Bearer {ML_ACCESS_TOKEN}"} if ML_ACCESS_TOKEN else {}

        if tipo == 'catalog':
            resp = requests.get(
                f"https://api.mercadolibre.com/products/{item_id}/items",
                headers=headers_ml, timeout=15
            )
            if resp.status_code == 200:
                resultados = resp.json().get("results", [])
                precos = [float(r["price"]) for r in resultados if r.get("price")]
                if precos:
                    preco = min(precos)
                    log.info("API ML catálogo: %s → R$ %.2f", item_id, preco)
                    return preco

        resp = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=headers_ml, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            preco = data.get("price")
            if preco:
                return float(preco)

        log.warning("Preço ML não encontrado: %s", item_id)
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
# Extração de preço do fornecedor (lógica original, sem alteração)
# ---------------------------------------------------------------------------

def extrair_preco_fornecedor_soup(soup, url: str) -> float | None:
    """Extrai preço do soup. Prioriza Programe e Poupe na Amazon."""
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

    if "amazon" in url:
        # Programe e Poupe — prioridade
        for sel in ["#sns-tiered-price", "#sns-base-price",
                    "#subscriptionPrice", "#snsAccordionRowMiddle"]:
            el = soup.select_one(sel)
            if el:
                nums = re.findall(r"[\d]+[.,][\d]{2}", el.get_text(strip=True))
                if nums:
                    try:
                        preco = float(nums[0].replace(".", "").replace(",", "."))
                        if preco > 0:
                            log.info("Amazon P&P (%s): R$ %.2f", sel, preco)
                            return preco
                    except Exception:
                        pass

        # Fallback: preço normal
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
                        preco = float(nums[0].replace(".", "").replace(",", "."))
                        if preco > 0:
                            log.info("Amazon preço normal (%s): R$ %.2f", sel, preco)
                            return preco
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

    log.warning("Preço fornecedor não encontrado: %s", url)
    return None


# ---------------------------------------------------------------------------
# Extração de descontos → lista estruturada + texto para coluna I
# Atualmente com seletores HTML para Amazon; expansível para outros fornecedores
# ---------------------------------------------------------------------------

def extrair_descontos(soup, url: str) -> tuple[list[Desconto], str]:
    """
    Retorna (lista_de_descontos, texto_coluna_I).
    Não inclui Programe e Poupe (preço já está na coluna E).
    Hoje suporta Amazon; adicione blocos `if "outrofornecedor" in url` para expandir.
    """
    is_amazon = "amazon" in url

    descontos: list[Desconto] = []
    texto_pagina = soup.get_text(" ")

    if is_amazon:
        # --- 1. Cupons resgatáveis (botão Resgatar) ---
        # Tenta pegar pelo HTML estruturado primeiro
        blocos_cupom = _coletar_blocos_cupom_amazon(soup)

        # Fallback: texto completo da página
        if not blocos_cupom:
            blocos_cupom = [texto_pagina]

        for bloco in blocos_cupom:
            descontos.extend(_parsear_cupons_do_bloco(bloco))

        # --- 2. Mais por Menos ---
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

    # Futuramente: elif "magazineluiza" in url: ...
    # elif "shopee" in url: ...

    # Remove duplicatas mantendo ordem
    descontos = _deduplicar(descontos)

    # Monta texto da coluna I
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
    return descontos, texto_i


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

    # Padrão percentual: "10% de desconto em pedidos a partir de R$100 cupom: CODIGO"
    for m in re.finditer(
        r'(\d+)%[^R$\d]*?(?:a partir de\s*)?R?\$?\s*([\d.,]+)[^:]*cupom\s*:\s*(\w+)',
        texto, re.IGNORECASE
    ):
        prime = bool(re.search(r'prime', texto[:m.start()], re.IGNORECASE))
        resultado.append(Desconto(
            tipo='cupom_pct',
            codigo=m.group(3).upper(),
            pct=float(m.group(1)) / 100,
            min_valor=parse_valor(m.group(2)) or 0.0,
            prime=prime,
        ))

    # Padrão fixo: "Economize R$50 em pedidos R$299+ cupom: CODIGO"
    for m in re.finditer(
        r'[Ee]conomize\s+R?\$?\s*([\d.,]+)[^R$\d]*R?\$?\s*([\d.,]+)\+?[^:]*cupom\s*:\s*(\w+)',
        texto, re.IGNORECASE
    ):
        prime = bool(re.search(r'prime', texto[:m.start()], re.IGNORECASE))
        resultado.append(Desconto(
            tipo='cupom_fixo',
            codigo=m.group(3).upper(),
            fixo=parse_valor(m.group(1)) or 0.0,
            min_valor=parse_valor(m.group(2)) or 0.0,
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
# ---------------------------------------------------------------------------

def calcular_melhor_preco_unitario(
    preco_base: float,
    descontos: list[Desconto],
) -> tuple[float, int]:
    """
    Dado o preço base (coluna E) e a lista de descontos, calcula o menor
    custo unitário possível aplicando todos os descontos cabíveis.

    Retorna (preco_unitario, quantidade_necessaria).

    Lógica de aplicação (ordem do checkout Amazon):
      1. Subtotal = qtd × preco_base
      2. MpM: aplica o maior % de desconto para a quantidade
      3. Cupom % : aplica se subtotal_pos_mpm >= min_valor
      4. Cupom fixo: subtrai se subtotal_pos_mpm >= min_valor
      (cupons % e fixo se acumulam se houver mais de um)
    """
    # Tiers MpM: [(min_qtd, pct), ...] ordenados
    tiers_mpm = sorted(
        [(d.min_qtd, d.pct) for d in descontos if d.tipo == 'mpm'],
        key=lambda x: x[0]
    )

    # Quantidades a testar:
    #   - 1 unidade (sem MpM)
    #   - cada tier de MpM
    #   - quantidade mínima para atingir o min_valor de cada cupom
    qtds_testar = {1}
    for qtd, _ in tiers_mpm:
        qtds_testar.add(qtd)

    # Para cupons com min_valor, calcular quantas unidades precisa comprar
    # (antes de MpM) para bater o mínimo
    for d in descontos:
        if d.min_valor > 0 and preco_base > 0:
            qtd_necessaria = int(d.min_valor / preco_base) + 1
            qtds_testar.add(qtd_necessaria)

    melhor_unitario = preco_base
    melhor_qtd = 1

    for qtd in sorted(qtds_testar):
        subtotal = qtd * preco_base

        # 1. Aplica MpM: pega o maior desconto para a qtd
        pct_mpm = 0.0
        for min_qtd, pct in tiers_mpm:
            if qtd >= min_qtd:
                pct_mpm = pct  # já está ordenado, então sobrescreve com o maior válido

        subtotal_pos_mpm = subtotal * (1 - pct_mpm)

        # 2. Aplica cupons % (todos que forem válidos)
        total_pct_cupom = 0.0
        for d in descontos:
            if d.tipo == 'cupom_pct' and subtotal_pos_mpm >= d.min_valor:
                total_pct_cupom += d.pct

        subtotal_pos_cupom_pct = subtotal_pos_mpm * (1 - total_pct_cupom)

        # 3. Aplica cupons fixos (todos que forem válidos)
        total_fixo_cupom = 0.0
        for d in descontos:
            if d.tipo == 'cupom_fixo' and subtotal_pos_mpm >= d.min_valor:
                total_fixo_cupom += d.fixo

        total_final = subtotal_pos_cupom_pct - total_fixo_cupom

        # Proteção: total não pode ser negativo
        if total_final <= 0:
            continue

        unitario = total_final / qtd

        if unitario < melhor_unitario:
            melhor_unitario = unitario
            melhor_qtd = qtd

    return round(melhor_unitario, 2), melhor_qtd


# ---------------------------------------------------------------------------
# Orquestração: preço + descontos + melhor unitário
# ---------------------------------------------------------------------------

def processar_fornecedor(url: str) -> tuple[float | None, str, str]:
    """
    Faz uma única requisição à página do fornecedor e retorna:
      (preco, texto_coluna_I, texto_coluna_J)
    """
    from bs4 import BeautifulSoup

    resp = fetch_url(url)
    if not resp:
        return None, "", ""

    soup = BeautifulSoup(resp.text, "lxml")

    preco = extrair_preco_fornecedor_soup(soup, url)
    if preco is None:
        return None, "", ""

    descontos, texto_i = extrair_descontos_amazon(soup, url)

    if descontos:
        melhor_un, melhor_qtd = calcular_melhor_preco_unitario(preco, descontos)
        if melhor_qtd > 1:
            texto_j = f"R${melhor_un:,.2f} ({melhor_qtd}un)"
        else:
            # Sem ganho: mesmo preço, mas mostra por clareza
            texto_j = f"R${melhor_un:,.2f} (1un)"
        log.info("Melhor unitário: %s", texto_j)
    else:
        texto_j = ""  # não Amazon ou sem descontos → deixa coluna J vazia

    return preco, texto_i, texto_j


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

        if preco_ml is None or preco_forn is None:
            ws.update(f"G{row_idx}:H{row_idx}", [["⚠️ Erro na leitura", agora]])
            continue

        # Atualiza D e E (preços brutos)
        ws.update(f"D{row_idx}:E{row_idx}", [[round(preco_ml, 2), round(preco_forn, 2)]])
        time.sleep(0.5)

        # Atualiza I (descontos) e J (melhor unitário)
        ws.update(f"I{row_idx}:J{row_idx}", [[texto_i, texto_j]])
        time.sleep(0.3)

        # Relê F (fórmula do usuário recalculada pelo Sheets)
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
