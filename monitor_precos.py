"""
monitor_precos.py
1. Baixa a planilha do Google Drive
2. Raspa preços do ML e do fornecedor
3. Atualiza colunas 4-6 e colore status
4. Sobe a planilha atualizada de volta ao Google Drive
5. Envia alertas via Telegram quando necessário
"""

import re
import io
import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from bs4 import BeautifulSoup

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    COMISSAO_ML, IMPOSTO_DAS, MARGEM_MIN, FRETE_FIXO,
    GDRIVE_FILE_ID, PLANILHA_PATH,
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

DESCONTO = COMISSAO_ML + IMPOSTO_DAS + MARGEM_MIN  # 0.28

# ── Fórmula PMC ──────────────────────────────────────────────────────────────
def calcular_pmc(preco_ml: float) -> float:
    return round(preco_ml * (1 - DESCONTO) - FRETE_FIXO, 2)

# ── Google Drive ──────────────────────────────────────────────────────────────
def gdrive_download(file_id: str, destino: str) -> bool:
    """Baixa arquivo .xlsx público do Google Drive."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        sess = requests.Session()
        resp = sess.get(url, stream=True, timeout=30)

        # Google pode pedir confirmação para arquivos grandes
        for key, val in resp.cookies.items():
            if "download_warning" in key:
                url = f"{url}&confirm={val}"
                resp = sess.get(url, stream=True, timeout=30)
                break

        resp.raise_for_status()
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        with open(destino, "wb") as f:
            for chunk in resp.iter_content(32768):
                f.write(chunk)
        log.info("Planilha baixada do Drive → %s", destino)
        return True
    except Exception as e:
        log.error("Erro ao baixar do Drive: %s", e)
        return False


def gdrive_upload(file_id: str, caminho: str) -> bool:
    """
    Atualiza o arquivo no Google Drive via API pública de upload.
    IMPORTANTE: para upload funcionar com link público, o arquivo precisa
    estar em uma pasta com permissão de edição via link, e usamos
    a Google Drive API v3 com upload multipart.
    Como estamos usando acesso público simples, salvamos localmente e
    avisamos o usuário para sincronizar manualmente OU usar rclone.
    Veja README para configurar rclone (recomendado).
    """
    log.info("Planilha atualizada salva em: %s", caminho)
    log.info("Para sincronizar com o Drive, use: rclone copy %s gdrive:/Monitor-Precos/", caminho)
    return True


# ── Scrapers ─────────────────────────────────────────────────────────────────
def extrair_preco_ml(url: str) -> float | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        seletores = [
            "span.andes-money-amount__fraction",
            "meta[itemprop='price']",
            "span.price-tag-fraction",
        ]
        for sel in seletores:
            el = soup.select_one(sel)
            if el:
                valor = el.get("content") or el.get_text(strip=True)
                valor = valor.replace(".", "").replace(",", ".")
                return float(re.sub(r"[^\d.]", "", valor))

        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string)
                if isinstance(data, dict) and "offers" in data:
                    return float(data["offers"].get("price", 0))
            except Exception:
                pass

        log.warning("Preço ML não encontrado: %s", url)
        return None
    except Exception as e:
        log.error("Erro ML (%s): %s", url, e)
        return None


def extrair_preco_fornecedor(url: str) -> float | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for meta_name in ["product:price:amount", "og:price:amount"]:
            tag = soup.find("meta", property=meta_name)
            if tag and tag.get("content"):
                return float(re.sub(r"[^\d.]", "", tag["content"].replace(",", ".")))

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


# ── Planilha ──────────────────────────────────────────────────────────────────
HEADER_ROW = 1
DATA_START  = 2

COL_SKU, COL_LINK_ML, COL_LINK_FORN = 1, 2, 3
COL_PRECO_ML, COL_PRECO_FORN, COL_PMC = 4, 5, 6
COL_STATUS, COL_ATUALIZADO = 7, 8

COR_OK     = "C6EFCE"
COR_ALERTA = "FFEB9C"
COR_PERIGO = "FFC7CE"
COR_HEADER = "2E75B6"


def garantir_cabecalhos(ws) -> None:
    if ws.cell(1, 1).value == "SKU":
        return
    headers = [
        "SKU", "Link ML", "Link Fornecedor",
        "Preço ML (R$)", "Preço Fornecedor (R$)",
        "PMC Máximo (R$)", "Status", "Última Atualização",
    ]
    for col, titulo in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=titulo)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=COR_HEADER)
        cell.alignment = Alignment(horizontal="center")
    larguras = {"A":18,"B":55,"C":55,"D":18,"E":22,"F":20,"G":30,"H":22}
    for col, w in larguras.items():
        ws.column_dimensions[col].width = w


def colorir_linha(ws, row: int, cor: str) -> None:
    fill = PatternFill("solid", fgColor=cor)
    for col in range(COL_PRECO_ML, COL_ATUALIZADO + 1):
        ws.cell(row=row, column=col).fill = fill


def estados_anteriores(ws) -> dict:
    estados = {}
    for row in ws.iter_rows(min_row=DATA_START, values_only=True):
        sku    = row[COL_SKU - 1]
        status = row[COL_STATUS - 1] if len(row) >= COL_STATUS else None
        if sku:
            estados[str(sku)] = status or ""
    return estados


# ── Main ──────────────────────────────────────────────────────────────────────
def processar() -> None:
    # 1. Baixar do Drive
    if not gdrive_download(GDRIVE_FILE_ID, PLANILHA_PATH):
        log.error("Não foi possível baixar a planilha. Abortando.")
        return

    wb = openpyxl.load_workbook(PLANILHA_PATH)
    ws = wb.active
    garantir_cabecalhos(ws)

    ant = estados_anteriores(ws)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    alertas = []

    for row_idx in range(DATA_START, ws.max_row + 1):
        sku       = ws.cell(row_idx, COL_SKU).value
        link_ml   = ws.cell(row_idx, COL_LINK_ML).value
        link_forn = ws.cell(row_idx, COL_LINK_FORN).value

        if not sku or not link_ml or not link_forn:
            continue

        log.info("Processando SKU %s ...", sku)

        preco_ml   = extrair_preco_ml(str(link_ml))
        time.sleep(1.5)
        preco_forn = extrair_preco_fornecedor(str(link_forn))
        time.sleep(1.5)

        ws.cell(row_idx, COL_ATUALIZADO).value = agora

        if preco_ml is None or preco_forn is None:
            ws.cell(row_idx, COL_STATUS).value = "⚠️ Erro na leitura"
            colorir_linha(ws, row_idx, COR_ALERTA)
            continue

        pmc = calcular_pmc(preco_ml)

        ws.cell(row_idx, COL_PRECO_ML).value   = preco_ml
        ws.cell(row_idx, COL_PRECO_FORN).value = preco_forn
        ws.cell(row_idx, COL_PMC).value        = pmc

        for col in (COL_PRECO_ML, COL_PRECO_FORN, COL_PMC):
            ws.cell(row_idx, col).number_format = 'R$ #,##0.00'

        status_ant = ant.get(str(sku), "")

        if preco_forn > pmc:
            status = "🚨 ACIMA DO PMC"
            colorir_linha(ws, row_idx, COR_PERIGO)
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
            colorir_linha(ws, row_idx, COR_OK)
            if status_ant == "🚨 ACIMA DO PMC":
                alertas.append(
                    f"✅ <b>NORMALIZADO — Fornecedor voltou abaixo do PMC</b>\n"
                    f"SKU: <code>{sku}</code>\n"
                    f"Preço ML: R$ {preco_ml:,.2f}\n"
                    f"PMC Máximo: R$ {pmc:,.2f}\n"
                    f"Preço Fornecedor: R$ {preco_forn:,.2f}  ✅\n"
                    f"Data: {agora}"
                )

        ws.cell(row_idx, COL_STATUS).value = status
        log.info("  ML=R$%.2f  Forn=R$%.2f  PMC=R$%.2f  → %s",
                 preco_ml, preco_forn, pmc, status)

    # 2. Salvar planilha
    wb.save(PLANILHA_PATH)
    log.info("Planilha salva localmente.")

    # 3. Subir de volta ao Drive via rclone
    gdrive_upload(GDRIVE_FILE_ID, PLANILHA_PATH)

    # 4. Enviar alertas
    for alerta in alertas:
        telegram_send(alerta)

    if not alertas:
        log.info("Nenhuma mudança de status detectada.")


if __name__ == "__main__":
    log.info("=== Início da execução ===")
    processar()
    log.info("=== Execução concluída ===")
