FROM python:3.12-slim

WORKDIR /app

# Vis loggen fortløpende i Railway.
ENV PYTHONUNBUFFERED=1
# Utenfor /root, så nobody finner nettleseren.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Node-en som følger med playwright trenger libatomic, som mangler i slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --no-shell chromium

COPY monitor.py .

USER nobody
# Chrome krasjer uten skrivbar HOME, og nobody har /nonexistent.
ENV HOME=/tmp

# Worker uten HTTP-port.
CMD ["python", "monitor.py"]
