FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

RUN useradd --create-home --uid 10001 kasgate

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chown -R kasgate:kasgate /srv
USER kasgate

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --h11-max-incomplete-event-size 65536 --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" --workers 2"]
