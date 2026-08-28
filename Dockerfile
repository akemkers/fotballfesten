# Rent Python-image. Tidligere krevde scriptet Playwrights image fordi
# siden ble rendret i headless Chromium; nå hentes katalogen som JSON, og
# eneste avhengighet er requests.
FROM python:3.12-slim

WORKDIR /app

# Slå av stdout-buffering slik at print() vises fortløpende i Railway-loggen.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Enkelt entrypoint - ingen HTTP-port, kjører som en bakgrunns-worker.
CMD ["python", "monitor.py"]
