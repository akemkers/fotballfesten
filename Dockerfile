# Offisielt Playwright-image: Chromium + alle OS-avhengigheter er
# ferdig installert. Image-taggen MÅ matche den låste playwright-versjonen
# i requirements.txt (v1.62.0-jammy <-> playwright==1.62.0), ellers leter
# Playwright etter en Chromium-build som ikke finnes i imaget, og launch()
# feiler. Bump begge samtidig ved oppgradering.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

# Slå av stdout-buffering slik at print() vises fortløpende i Railway-loggen.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Enkelt entrypoint - ingen HTTP-port, kjører som en bakgrunns-worker.
CMD ["python", "monitor.py"]
