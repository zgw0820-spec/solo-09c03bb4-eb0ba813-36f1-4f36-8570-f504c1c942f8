FROM python:3.11-slim

WORKDIR /srv

RUN pip install --no-cache-dir "flask==3.0.*"

COPY app/ /srv/app/
COPY samples/ /srv/samples/

ENV MOUNT_DIR=/srv/samples \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-m", "app.webapp"]
