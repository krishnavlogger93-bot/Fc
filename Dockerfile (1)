FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run sets PORT (8080) automatically; server.py reads it.
CMD ["python3", "server.py"]
