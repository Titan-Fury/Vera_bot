FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY vera_bot ./vera_bot
COPY pyproject.toml ./

RUN adduser --disabled-password --gecos "" vera
USER vera

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; \
urllib.request.urlopen('http://127.0.0.1:8080/v1/healthz', timeout=2); sys.exit(0)" || exit 1

CMD ["python", "-m", "vera_bot"]
