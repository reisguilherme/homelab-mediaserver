# BASE_IMAGE is a local tag resolved to the authenticated original image ID.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS clean-binary

USER 0:0
# Copy bytes to a newly created inode; COPY alone can preserve file capabilities.
# A pre-existing artifact directory or symlink source fails the build.
RUN /usr/bin/env -i PATH=/usr/bin:/bin SOURCE=/usr/bin/caddy \
    DESTINATION=/.core-caddy-binary /bin/sh -eu -c '\
      test -f "$SOURCE"; \
      test ! -L "$SOURCE"; \
      mkdir -m 0755 "$DESTINATION"; \
      umask 022; \
      cat "$SOURCE" > "$DESTINATION/caddy"; \
      cmp -s "$SOURCE" "$DESTINATION/caddy"; \
      chmod 0755 "$DESTINATION/caddy"'

FROM ${BASE_IMAGE}
USER 0:0
# Remove the inherited inode before copying the capability-free artifact.
RUN /usr/bin/env -i PATH=/usr/bin:/bin /bin/rm -- /usr/bin/caddy
COPY --from=clean-binary --chown=0:0 /.core-caddy-binary/caddy /usr/bin/caddy
HEALTHCHECK NONE
USER 1000:1000
ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "HOME=/config", "XDG_CONFIG_HOME=/config", "XDG_DATA_HOME=/data", "/usr/bin/caddy"]
CMD ["version"]
