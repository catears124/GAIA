FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir .
ENV GAIA_HOST=0.0.0.0 GAIA_PORT=8501 GAIA_DB=/data/gaia.db
VOLUME ["/data"]
EXPOSE 8501
CMD ["gaia", "serve", "--host", "0.0.0.0", "--port", "8501"]
