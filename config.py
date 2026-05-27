# ============================================================
#  CONFIGURAÇÕES DO SISTEMA — edite aqui antes de fazer deploy
# ============================================================

# --- Telegram ---
TELEGRAM_BOT_TOKEN = "8583665201:AAEiypnwXtItDay2hymNztATZzPZhpySZgw"       # Ex: "7312345678:AAF..."
TELEGRAM_CHAT_ID   = "588598476"     # Ex: "123456789"

# --- Google Drive ---
# Cole aqui o ID do arquivo .xlsx no Google Drive
# Como pegar: abra o arquivo no Drive → olhe a URL:
# https://docs.google.com/spreadsheets/d/XXXXXXXXXX/edit
# ou para .xlsx:
# https://drive.google.com/file/d/XXXXXXXXXX/view
# O ID é a parte XXXXXXXXXX
GDRIVE_FILE_ID = "1Nt01XyjJBkeG85s4Qr-5GFfV_tAiGkrL"

# --- Taxas (sobre o preço de venda ML) ---
COMISSAO_ML   = 0.12   # 12%
IMPOSTO_DAS   = 0.06   # 6%
MARGEM_MIN    = 0.10   # 10%
FRETE_FIXO    = 20.00  # R$ 20,00

# Fórmula: PMC = Preço_ML × (1 - 0,12 - 0,06 - 0,10) - 20
#          PMC = Preço_ML × 0,72 - 20

# --- Planilha local (caminho dentro do container) ---
PLANILHA_PATH = "/data/produtos.xlsx"
