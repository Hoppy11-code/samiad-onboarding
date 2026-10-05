FROM python:3.12-slim
# LibreOffice turns the Word documents into PDFs
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer fonts-dejavu fonts-liberation fonts-crosextra-carlito tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "-m", "samiad.main"]
