FROM python:3.12-slim

RUN useradd --create-home --uid 10001 syber
WORKDIR /opt/syberwork
COPY --chown=syber:syber . .
RUN pip install --no-cache-dir . "psycopg[binary]>=3.1" \
    && mkdir -p /home/syber/cell && chown syber:syber /home/syber/cell

USER syber
ENV SYBERWORK_HOME=/home/syber/cell
EXPOSE 8766
CMD ["syberwork", "--home", "/home/syber/cell", "serve", "--port", "8766"]
