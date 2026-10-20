FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html ./
ENV PYTHONUNBUFFERED=1 PORT=8000 AUCTION_DB=/data/auction.sqlite3
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "server.py"]
