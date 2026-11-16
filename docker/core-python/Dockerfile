# BASE_IMAGE must resolve to the authenticated local Python worker image.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER 0:0
WORKDIR /app
SHELL ["/bin/sh", "-eu", "-c"]

# Keep the authenticated interpreter/dependencies; replace only public sources.
COPY --chown=0:0 services/common/src /app/services/common/src
COPY --chown=0:0 services/control/src /app/services/control/src
COPY --chown=0:0 scripts/core-storage-guard.py scripts/core-python-entrypoint.py scripts/core-worker-runtime.py scripts/capacity-snapshot.py /app/scripts/
RUN /usr/bin/env -i PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    /bin/sh -eu -c '\
      test -x /app/.venv/bin/python; \
      chmod 0444 /app/scripts/core-storage-guard.py /app/scripts/core-python-entrypoint.py /app/scripts/core-worker-runtime.py /app/scripts/capacity-snapshot.py; \
      /app/.venv/bin/python -I -B -c "import os, time; assert hasattr(os, \"waitid\") and hasattr(time, \"CLOCK_BOOTTIME\")"'

HEALTHCHECK NONE
USER 1000:1000
ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONPATH=/app/services/common/src:/app/services/control/src", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "LANG=C.UTF-8", "/app/.venv/bin/python", "-B", "/app/scripts/core-python-entrypoint.py"]
CMD []
