FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PERMIT_HOST=0.0.0.0

WORKDIR /app
RUN addgroup --system permit && adduser --system --ingroup permit permit

COPY --chown=permit:permit . /app

USER permit
EXPOSE 8000

CMD ["python", "web_app/server.py"]
