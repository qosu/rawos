# rawos — API tier only
#
# This image runs the rawos API layer (FastAPI + JWT + agent orchestration).
# Substrate features are NOT available in this container:
#   - BPF LSM enforcement (requires bare-metal kernel with BPF LSM enabled)
#   - Landlock self-MAC (requires bare-metal kernel ≥ 5.13)
#   - systemd unit authorship (requires systemd PID 1)
#   - Deadman heartbeat (requires systemd socket activation)
#   - Self-reload with kernel policy flip (requires direct kernel access)
#
# To use the full substrate (all safety floors), deploy rawos as a systemd
# service on a bare-metal or VM Linux host. See README.md for details.

# ── Stage 1: build ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml ./
COPY rawos/ ./rawos/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir build && \
    python -m build --wheel --outdir /dist

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user for principle of least privilege
RUN groupadd --gid 1001 rawos && \
    useradd --uid 1001 --gid rawos --no-create-home --shell /bin/false rawos

WORKDIR /app

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Runtime-only dependencies not captured in wheel (e.g. uvicorn)
RUN pip install --no-cache-dir uvicorn[standard]

USER rawos

EXPOSE 8002

# Secrets must be injected via environment variables or Docker secrets.
# Never bake credentials into the image.
ENV RAWOS_ENV=production \
    PORT=8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen(http://localhost:/health)" || exit 1

CMD ["python", "-m", "uvicorn", "rawos.app:app", "--host", "0.0.0.0", "--port", "8002"]
