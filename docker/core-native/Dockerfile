# The operator must resolve BASE_IMAGE to the authenticated local image first.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG PYTHON_APK_VERSION
USER 0:0
SHELL ["/bin/sh", "-eu", "-c"]

# Clear captured image environment before APK/Python. Keep APK's default keys
# and signature verification; dependencies are resolved and recorded, not guessed.
RUN /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHON_APK_VERSION="${PYTHON_APK_VERSION:?explicit Python APK version required}" \
    /bin/sh -eu -c '\
      case "$PYTHON_APK_VERSION" in 3.12.16-r0|3.14.10-r1) ;; *) exit 64 ;; esac; \
      test "$(apk --print-arch)" = x86_64; \
      apk add --no-cache "python3=$PYTHON_APK_VERSION"; \
      /usr/bin/python3 -I -B -c "import os, time; assert hasattr(os, \"waitid\") and hasattr(time, \"CLOCK_BOOTTIME\")"; \
      mkdir -p /usr/local/share/core-native; \
      apk info -v > /usr/local/share/core-native/apk-installed.unsorted; \
      sort /usr/local/share/core-native/apk-installed.unsorted > /usr/local/share/core-native/apk-installed.txt; \
      rm /usr/local/share/core-native/apk-installed.unsorted; \
      test -s /usr/local/share/core-native/apk-installed.txt; \
      chmod 0444 /usr/local/share/core-native/apk-installed.txt; \
      /usr/bin/python3 -I -B --version'

COPY --chown=0:0 --chmod=0444 scripts/core-storage-guard.py /usr/local/libexec/core-storage-guard.py
HEALTHCHECK NONE
USER 1000:1000
ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8", "/usr/bin/python3", "-I", "-B", "/usr/local/libexec/core-storage-guard.py"]
CMD []
