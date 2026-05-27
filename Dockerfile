FROM python:3.12-slim
 
WORKDIR /app
 
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
COPY monitor_precos.py .
COPY config.py .
COPY service-account.json .
 
RUN mkdir -p /data
 
CMD ["python", "monitor_precos.py"]
