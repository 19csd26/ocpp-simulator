# Build the UI, then serve it and the API from one Python process on port 8100.
FROM node:22-alpine AS ui
WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ocpp_simulator backend/ocpp_simulator
COPY --from=ui /app/ui/dist ui/dist
# Hugging Face Spaces runs containers as uid 1000.
RUN useradd --create-home --uid 1000 sim
USER sim
WORKDIR /app/backend
EXPOSE 8100
# SIM_TOKEN is required (on Spaces: Settings > Variables and secrets): the server refuses to listen on 0.0.0.0 without it.
CMD ["python", "-m", "ocpp_simulator", "--host", "0.0.0.0", "--port", "8100"]
