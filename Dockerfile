# Build the UI, then serve it and the API from one Python process on port 8100.
FROM node:22-alpine AS ui
WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
# the web UI bundles the guide (docs/guide) so users can read it in the app
COPY docs/guide /app/docs/guide
RUN npm run build

FROM python:3.13-slim
LABEL org.opencontainers.image.title="Volkai" \
      org.opencontainers.image.description="Volkai: simulate any charger, break it on purpose. An OCPP 1.6J and 2.0.1 charge point simulator" \
      org.opencontainers.image.authors="Raghav (19csd26)" \
      org.opencontainers.image.source="https://github.com/19csd26/ocpp-simulator" \
      org.opencontainers.image.licenses="All rights reserved"
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ocpp_simulator backend/ocpp_simulator
COPY --from=ui /app/ui/dist ui/dist
# Fixed uid 1000, which hosted container platforms commonly expect.
RUN useradd --create-home --uid 1000 sim
USER sim
WORKDIR /app/backend
EXPOSE 8100
# SIM_TOKEN is required: the server refuses to listen on 0.0.0.0 without it (docker-compose.yml opts out for local use).
CMD ["python", "-m", "ocpp_simulator", "--host", "0.0.0.0", "--port", "8100"]
