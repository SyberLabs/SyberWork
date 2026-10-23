FROM python:3.12-slim

# Refresh the OS trust store (eMudhra intermediate CA distrust, Oct 2026); urllib uses it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 syber
WORKDIR /opt/syberwork
COPY --chown=syber:syber . .
RUN pip install --no-cache-dir . "psycopg[binary]>=3.1" "certifi>=2026.10.19" \
    && mkdir -p /home/syber/cell && chown syber:syber /home/syber/cell

USER syber
ENV SYBERWORK_HOME=/home/syber/cell
EXPOSE 8766
CMD ["syberwork", "--home", "/home/syber/cell", "serve", "--port", "8766"]
