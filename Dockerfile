FROM python:3.12-slim

WORKDIR /app

# Vis loggen fortløpende i Railway.
ENV PYTHONUNBUFFERED=1
# Utenfor /root, så nobody finner nettleseren.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --no-shell chromium

COPY monitor.py .

USER nobody

# Worker uten HTTP-port.
CMD ["python", "monitor.py"]
