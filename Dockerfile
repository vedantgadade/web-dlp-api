FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends     ffmpeg curl ca-certificates unzip     && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY public ./public
RUN mkdir -p /app/app/downloads

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
