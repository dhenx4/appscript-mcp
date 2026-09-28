FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8080
EXPOSE 8080

# Shell form (not JSON array) so $PORT gets substituted - Render assigns its
# own port at runtime. Uses fastmcp's own CLI rather than `python server.py`
# / mcp.run(), matching the exact invocation FastMCP's docs test Google OAuth
# against, in case the OAuth-proxy discovery routes only get wired up there.
CMD fastmcp run server.py --transport http --host 0.0.0.0 --port ${PORT:-8080}
