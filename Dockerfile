FROM python:3.12-slim

WORKDIR /opt/syberwork
COPY . .
RUN pip install --no-cache-dir . "psycopg[binary]>=3.1"

ENV SYBERWORK_HOME=/var/lib/syberwork
EXPOSE 8766
CMD ["syberwork", "--home", "/var/lib/syberwork", "serve", "--port", "8766"]
