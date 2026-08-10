# Control-plane image. Small — it only runs the SaaS front door, not the tenant
# instances (those use the changedetection.io image).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY saas/requirements.txt /app/saas/requirements.txt
RUN pip install --no-cache-dir -r /app/saas/requirements.txt

COPY saas /app/saas

ENV SAAS_PORT=8099
EXPOSE 8099
CMD ["gunicorn", "-b", "0.0.0.0:8099", "-w", "2", "saas.app:app"]
