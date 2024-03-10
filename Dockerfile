FROM python:3.10.11 as base

ARG RELEASE_VERSION
ARG MODULE_NAME

# Update and install dependencies
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get clean \
    && apt-get update -y \
    && apt-get upgrade -y --fix-missing \
    && apt-get install -y --fix-missing chromium chromium-driver xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/chromedriver /usr/local/bin/chromedriver

RUN pip3.10 install pandas \
        aioprometheus==23.3.0 \
        opentelemetry-api \
        opentelemetry-sdk \
        opentelemetry-exporter-jaeger \
        opentelemetry-instrumentation-aiohttp-client \
        opentelemetry-sdk opentelemetry-exporter-otlp

RUN pip3.10 install --no-cache-dir --upgrade "git+https://github.com/exorde-labs/exorde_data"
FROM base as with_module
RUN pip3.10 install --no-cache-dir --upgrade "git+https://github.com/exorde-labs/${MODULE_NAME}"
RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*
COPY ./src /app

ENV scraper_module="${MODULE_NAME}"
CMD ["python3.10", "/app/scraper.py"]
