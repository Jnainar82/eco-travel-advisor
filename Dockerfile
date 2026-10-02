FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends nginx && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chown -R user:user /app
USER user
# Train at build time so the container starts fast; secrets are injected at runtime, never baked in.
RUN rasa train --quiet
EXPOSE 7860
CMD ["./start.sh"]
