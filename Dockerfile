FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pybambu lives inside the Home Assistant Bambu Lab integration repo
# (greghesp/ha-bambulab); the standalone PyPI package is stale.
RUN git clone --depth 1 https://github.com/greghesp/ha-bambulab.git

COPY app.py .

ARG BUILD_NUMBER=dev
ENV BUILD_NUMBER=${BUILD_NUMBER}

RUN pip install --no-cache-dir \
    paho-mqtt \
    requests \
    beautifulsoup4 \
    python-dateutil \
    Pillow

EXPOSE 8080

CMD ["python", "app.py"]
