FROM python:3.12-slim

# Instala rclone para sincronização com Google Drive
RUN apt-get update && apt-get install -y curl unzip && \
    curl https://rclone.org/install.sh | bash && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código do projeto
COPY monitor_precos.py .
COPY config.py .

# Pasta de dados persistente (planilha + log)
RUN mkdir -p /data

CMD ["python", "monitor_precos.py"]
