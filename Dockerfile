# Offisielt Playwright-image: Chromium + alle OS-avhengigheter er
# ferdig installert, og Playwright-versjonen matcher browserne i imaget.
# Hold taggen i synk med playwright-versjonen i requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Enkelt entrypoint - ingen HTTP-port, kjører som en bakgrunns-worker.
CMD ["python", "monitor.py"]
