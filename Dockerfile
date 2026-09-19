FROM python:3.12-slim

LABEL org.opencontainers.image.title="AgentDynamics" \
      org.opencontainers.image.description="Application performance monitoring for AI agents"

RUN useradd --create-home --uid 10001 agentdynamics
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentdynamics ./agentdynamics
RUN pip install --no-cache-dir . && mkdir -p /data && chown agentdynamics /data

USER agentdynamics
ENV AGENTDYNAMICS_DATA=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"

# Claude Code transcripts are optional in a server deployment; mount ~/.claude/projects and pass --claude-root to enable.
ENTRYPOINT ["agentdynamics", "--claude-root", ""]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
