FROM python:3.12-slim
 
WORKDIR /app
 
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
COPY monitor_precos.py .
COPY config.py .
 
RUN mkdir -p /data
 
# Se existir versão corrigida no volume, usa ela
CMD ["bash", "-c", "[ -f /data/monitor_precos.py ] && cp /data/monitor_precos.py /app/monitor_precos.py; python monitor_precos.py"]
