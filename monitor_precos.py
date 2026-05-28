"""
monitor_precos.py
1. Lê produtos do Google Sheets
2. Renova token ML automaticamente via refresh_token
3. Busca preço do vencedor do catálogo ML via API oficial
4. Raspa preços do fornecedor via ScraperAPI
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
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SHEETS_ID       = os.environ.get("SHEETS_ID", "")

# ── Credenciais ML ────────────────────────────────────────────────────────────
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
        "access_token": _ML_INITIAL_ACCESS_TOKEN,
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


# ── Fórmula PMC ──────────────────────────────────────────────────────────────
def calcular_pmc(preco_ml: float) -> float:
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
        "Preco ML (R$)", "Preco Fornecedor (R$)",
        "PMC Maximo (R$)", "Status", "Ultima Atualizacao",
    ]
    if headers != esperados:
        ws.update("A1:H1", [esperados])
        log.info("Cabecalhos criados na planilha.")


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
            log.warning("Item ID ML nao encontrado: %s", url)
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
                    winner_item_id = winner.get("item_id")
                    preco = winner.get("price")
                    if preco:
                        log.info("API ML catalogo winner (%s -> %s): R$ %.2f",
                                 item_id, winner_item_id, float(preco))
                        return float(preco)
            log.warning("Nenhum item encontrado no catalogo %s (status %s)",
                        item_id, resp.status_code)
            return None

        resp = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=headers_ml, timeout=15
        )
        if resp.status_code == 200:
            preco = resp.json().get("price")
            if preco:
                log.info("API ML item: %s -> R$ %.2f", item_id, float(preco))
                return float(preco)

        log.warning("Preco ML nao encontrado: %s", item_id)
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

        # ── Drogasil / Drogaraia ──────────────────────────────────────────
        if "drogasil" in url or "drogaraia" in url:
            el = soup.select_one(".floating-price-position-lmpm")
            if el:
                nums = re.findall(r"\d+[.,]\d{2}", el.get_text(strip=True))
                if nums:
                    try:
                        v = nums[0].replace(".", "").replace(",", ".")
                        preco = float(v)
                        if preco > 0:
                            log.info("Drogasil Leve+Pague: R$ %.2f", preco)
                            return preco
                    except Exception:
                        pass
            el = soup.select_one("#pdp-price-container")
            if el:
                nums = re.findall(r"\d+[.,]\d{2}", el.get_text(strip=True))
                if nums:
                    try:
                        v = nums[0].replace(".", "").replace(",", ".")
                        preco = float(v)
                        if preco > 0:
                            log.info("Drogasil preco normal: R$ %.2f", preco)
                            return preco
                    except Exception:
                        pass

        # ── Amazon ────────────────────────────────────────────────────────
        if "amazon" in url:
            for sel in ["#sns-tiered-price", "#sns-base-price", "#subscriptionPrice", "#snsAccordionRowMiddle"]:
                el = soup.select_one(sel)
                if el:
                    nums = re.findall(r"\d+[.,]\d{2}", el.get_text(strip=True))
                    if nums:
                        try:
                            v = nums[0].replace(".", "").replace(",", ".")
                            preco = float(v)
                            if preco > 0:
                                log.info("Amazon Programe e Poupe (%s): R$ %.2f", sel, preco)
                                return preco
                        except Exception:
                            pass
            for sel in ["#apex-pricetopay-accessibility-label", ".a-price .a-offscreen", "span.a-price-whole"]:
                el = soup.select_one(sel)
                if el:
                    nums = re.findall(r"\d+[.,]\d{2}", el.get_text(strip=True))
                    if nums:
                        try:
                            v = nums[0].replace(".", "").replace(",", ".")
                            preco = float(v)
                            if preco > 0:
                                log.info("Amazon preco normal (%s): R$ %.2f", sel, preco)
                                return preco
                        except Exception:
                            pass

        # ── Seletores genéricos ───────────────────────────────────────────
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

        log.warning("Preco fornecedor nao encontrado: %s", url)
        return None
    except Exception as e:
        log.error("Erro fornecedor (%s): %s", url, e)
        return None


# ── Telegram ─────────────────────────────────────────────────────────────────
def telegram_send(mensagem: str) -> None:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "SEU_TOKEN_AQUI":
        log.warning("Telegram nao configurado.")
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
        log.info("Conectado ao Google Sheets")
    except Exception as e:
        log.error("Erro ao conectar Google Sheets: %s", e)
        return

    garantir_cabecalhos(ws)

    todos_dados = ws.get_all_values()
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    alertas = []

    for row_idx, row in enumerate(todos_dados[1:], start=2):
        if len(row) < 3:
            continue

        sku       = row[0].strip()
        link_ml   = row[1].strip()
        link_forn = row[2].strip()

        if not sku or not link_ml or not link_forn:
            continue

        status_ant = row[6].strip() if len(row) > 6 else ""

        log.info("Processando SKU %s ...", sku)

        preco_ml   = extrair_preco_ml(link_ml)
        time.sleep(1.5)
        preco_forn = extrair_preco_fornecedor(link_forn)
        time.sleep(1.5)

        if preco_ml is None or preco_forn is None:
            ws.update(f"G{row_idx}:H{row_idx}", [["Erro na leitura", agora]])
            continue

        # Atualiza D e E com numeros puros -- deixa F intacta (formula do usuario)
        ws.update(f"D{row_idx}:E{row_idx}", [[round(preco_ml, 2), round(preco_forn, 2)]])
        time.sleep(0.3)

        # Rele coluna F apos atualizar D e E (Sheets recalcula a formula)
        try:
            pmc_cell = ws.cell(row_idx, 6).value
            pmc_str = re.sub(r"[^\d.,]", "", str(pmc_cell or ""))
            if "," in pmc_str and "." in pmc_str:
                pmc_str = pmc_str.replace(".", "").replace(",", ".")
            elif "," in pmc_str:
                pmc_str = pmc_str.replace(",", ".")
            pmc = float(pmc_str) if pmc_str else calcular_pmc(preco_ml)
        except Exception:
            pmc = calcular_pmc(preco_ml)

        if preco_forn > pmc:
            status = "ACIMA DO PMC"
            if status_ant != status:
                alertas.append(
                    f"ALERTA -- Fornecedor acima do PMC\n"
                    f"SKU: {sku}\n"
                    f"Preco ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Maximo: R$ {pmc:,.2f}\n"
                    f"Preco Fornecedor: R$ {preco_forn:,.2f}\n"
                    f"Data: {agora}"
                )
        else:
            status = "OK"
            if status_ant == "ACIMA DO PMC":
                alertas.append(
                    f"NORMALIZADO -- Fornecedor voltou abaixo do PMC\n"
                    f"SKU: {sku}\n"
                    f"Preco ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Maximo: R$ {pmc:,.2f}\n"
                    f"Preco Fornecedor: R$ {preco_forn:,.2f}\n"
                    f"Data: {agora}"
                )

        ws.update(f"G{row_idx}:H{row_idx}", [[status, agora]])

        log.info("  ML=R$%.2f  Forn=R$%.2f  PMC=R$%.2f  -> %s",
                 preco_ml, preco_forn, pmc, status)

        time.sleep(1)

    log.info("Planilha atualizada no Google Sheets")

    for alerta in alertas:
        telegram_send(alerta)

    if not alertas:
        log.info("Nenhuma mudanca de status detectada.")


if __name__ == "__main__":
    log.info("=== Container iniciado -- aguardando agendamento do Dokploy ===")
    while True:
        time.sleep(60)
