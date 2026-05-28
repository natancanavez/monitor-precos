"""
monitor_precos.py
1. Lê produtos do Google Sheets
2. Raspa preços do ML via API oficial
3. Raspa preços do fornecedor via ScraperAPI
4. Calcula PMC via fórmula da planilha (col G) ou padrão
5. Atualiza colunas no Google Sheets
6. Envia alertas via Telegram quando necessário
"""
 
import re
import json
import time
import logging
import requests
import os
from datetime import datetime
 
import gspread
from google.oauth2.service_account import Credentials
 
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    COMISSAO_ML, IMPOSTO_DAS, MARGEM_MIN, FRETE_FIXO,
)
 
# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
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
 
DESCONTO        = COMISSAO_ML + IMPOSTO_DAS + MARGEM_MIN
ML_ACCESS_TOKEN = os.environ.get("ML_ACCESS_TOKEN", "")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SHEETS_ID       = os.environ.get("SHEETS_ID", "")
 
# ── Colunas ───────────────────────────────────────────────────────────────────
COL_SKU        = 0   # A
COL_LINK_ML    = 1   # B
COL_LINK_FORN  = 2   # C
COL_PRECO_ML   = 3   # D
COL_PRECO_FORN = 4   # E
COL_PMC        = 5   # F
COL_FORMULA    = 6   # G ← fórmula personalizada
COL_STATUS     = 7   # H
COL_ATUALIZADO = 8   # I
 
# ── Fórmula PMC ──────────────────────────────────────────────────────────────
def calcular_pmc(preco_ml: float, formula_valor: str = "") -> float:
    """
    Se formula_valor for um número válido (calculado pelo Sheets), usa ele.
    Senão, usa a fórmula padrão.
    """
    if formula_valor:
        try:
            # Remove R$, espaços e converte vírgula para ponto
            v = re.sub(r"[^\d.,]", "", str(formula_valor)).replace(",", ".")
            if v:
                return round(float(v), 2)
        except Exception:
            pass
    # Fórmula padrão
    return round(preco_ml * (1 - DESCONTO) - FRETE_FIXO, 2)
 
# ── Google Sheets ─────────────────────────────────────────────────────────────
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
 
 
def garantir_cabecalhos(ws) -> None:
    headers = ws.row_values(1)
    esperados = [
        "SKU", "Link ML", "Link Fornecedor",
        "Preço ML (R$)", "Preço Fornecedor (R$)", "PMC Calculado (R$)",
        "Fórmula PMC", "Status", "Última Atualização",
    ]
    if headers != esperados:
        ws.update("A1:I1", [esperados])
        log.info("Cabeçalhos atualizados na planilha.")
 
 
# ── Scraper ML ────────────────────────────────────────────────────────────────
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
                    log.info("API ML catálogo (menor preço Pix): %s → R$ %.2f", item_id, preco)
                    return preco
 
        resp = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=headers_ml, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            preco = data.get("price")
            if preco:
                log.info("API ML item: %s → R$ %.2f", item_id, float(preco))
                return float(preco)
 
        log.warning("Preço ML não encontrado: %s", item_id)
        return None
    except Exception as e:
        log.error("Erro ML (%s): %s", url, e)
        return None
 
 
# ── Scraper Fornecedor ────────────────────────────────────────────────────────
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
 
 
def extrair_preco_fornecedor(url: str) -> float | None:
    from bs4 import BeautifulSoup
    try:
        resp = fetch_url(url)
        if not resp:
            return None
 
        soup = BeautifulSoup(resp.text, "lxml")
 
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
            el = soup.select_one("span.a-price-whole")
            if el:
                v = re.sub(r"[^\d]", "", el.get_text())
                if v:
                    return float(v)
 
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
    except Exception as e:
        log.error("Erro fornecedor (%s): %s", url, e)
        return None
 
 
# ── Telegram ─────────────────────────────────────────────────────────────────
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
 
 
# ── Main ──────────────────────────────────────────────────────────────────────
def processar() -> None:
    try:
        ws = conectar_sheets()
        log.info("Conectado ao Google Sheets ✅")
    except Exception as e:
        log.error("Erro ao conectar Google Sheets: %s", e)
        return
 
    garantir_cabecalhos(ws)
 
    # Busca todos os valores (fórmulas já calculadas pelo Sheets)
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
 
        # Fórmula PMC personalizada (col G) — já calculada pelo Sheets
        formula_val = row[COL_FORMULA].strip() if len(row) > COL_FORMULA else ""
        status_ant  = row[COL_STATUS].strip() if len(row) > COL_STATUS else ""
 
        log.info("Processando SKU %s ...", sku)
 
        preco_ml   = extrair_preco_ml(link_ml)
        time.sleep(1.5)
        preco_forn = extrair_preco_fornecedor(link_forn)
        time.sleep(1.5)
 
        if preco_ml is None or preco_forn is None:
            ws.update(f"H{row_idx}:I{row_idx}", [["⚠️ Erro na leitura", agora]])
            continue
 
        # PMC: usa valor da coluna G se disponível, senão fórmula padrão
        pmc = calcular_pmc(preco_ml, formula_val)
 
        if preco_forn > pmc:
            status = "🚨 ACIMA DO PMC"
            if status_ant != status:
                alertas.append(
                    f"🚨 <b>ALERTA — Fornecedor acima do PMC</b>\n"
                    f"SKU: <code>{sku}</code>\n"
                    f"Preço ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Máximo: R$ {pmc:,.2f}\n"
                    f"Preço Fornecedor: R$ {preco_forn:,.2f}  ❌\n"
                    f"Data: {agora}"
                )
        else:
            status = "✅ OK"
            if status_ant == "🚨 ACIMA DO PMC":
                alertas.append(
                    f"✅ <b>NORMALIZADO — Fornecedor voltou abaixo do PMC</b>\n"
                    f"SKU: <code>{sku}</code>\n"
                    f"Preço ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Máximo: R$ {pmc:,.2f}\n"
                    f"Preço Fornecedor: R$ {preco_forn:,.2f}  ✅\n"
                    f"Data: {agora}"
                )
 
        # Atualiza D, E, F, H, I (não toca em G que é a fórmula do usuário)
        ws.update(f"D{row_idx}:F{row_idx}", [[
            f"R$ {preco_ml:.2f}",
            f"R$ {preco_forn:.2f}",
            f"R$ {pmc:.2f}",
        ]])
        ws.update(f"H{row_idx}:I{row_idx}", [[status, agora]])
 
        log.info("  ML=R$%.2f  Forn=R$%.2f  PMC=R$%.2f  → %s",
                 preco_ml, preco_forn, pmc, status)
 
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
