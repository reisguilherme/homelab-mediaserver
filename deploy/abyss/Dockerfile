# Build context is deploy/abyss only. Run `python3 prepare.py --check` first.
FROM jellyfin/jellyfin@sha256:357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037

LABEL org.opencontainers.image.title="HomeServer Jellyfin with Abyss" \
      io.homeserver.abyss.upstream="2f809171ed7f3a34b5e78297608b5f2b63dd3f0f" \
      io.homeserver.abyss.revision="r4"

COPY runtime/ /jellyfin/jellyfin-web/ui/

# Fail closed if the exact reviewed Jellyfin index or prepared assets changed.
# No package installation, external download, or change to the server entrypoint.
RUN set -eu; \
    cd /jellyfin/jellyfin-web/ui; \
    sha256sum -c SHA256SUMS; \
    cd /jellyfin/jellyfin-web; \
    printf '%s  index.html\n' '692ac1582fd119b0ec4712de52ec3fc82375bcd844741467df050372ccde5b40' | sha256sum -c -; \
    test "$(grep -o '</body>' index.html | wc -l)" -eq 1; \
    test "$(grep -o '<head>' index.html | wc -l)" -eq 1; \
    if grep -q 'data-abyss-spotlight' index.html; then exit 1; fi; \
    sed -i 's#<head>#<head><script src="ui/abyss-defaults.js?v=2f809171-r4" data-abyss-defaults></script>#' index.html; \
    sed -i 's#</body>#<script src="ui/spotlight-loader.js" data-abyss-spotlight></script></body>#' index.html; \
    test "$(grep -o 'data-abyss-spotlight' index.html | wc -l)" -eq 1
