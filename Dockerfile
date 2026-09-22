FROM python:3.12-slim

WORKDIR /app

# Vis loggen fortløpende i Railway.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

USER nobody

# Worker uten HTTP-port.
CMD ["python", "monitor.py"]
