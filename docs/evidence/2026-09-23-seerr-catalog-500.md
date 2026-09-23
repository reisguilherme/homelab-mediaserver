# Seerr catalog HTTP 500 during active downloads — 2026-09-23

The Seerr Movies, Series, and Discover views returned HTTP 500 and request cards showed “Not Found”. Its logs at 14:33–14:35 UTC showed repeated TMDb fetch failures for movie and TV details, discovery, genres, and trending items. This was a metadata egress failure, not a missing movie in the Arr databases.

From the Seerr container, Node's default `fetch()` to the unauthenticated TMDb configuration endpoint failed with `ETIMEDOUT` on its IPv4 candidates and `ENETUNREACH` on IPv6 candidates. The same endpoint returned the expected HTTP 401 when Node's network family autoselection was disabled. The exact `NODE_OPTIONS` flags were tested in the existing container before the Compose change.

Release `a5e547b8adac86ee333dc8093d8e192e573a6a5e` sets `NODE_OPTIONS=--no-network-family-autoselection --dns-result-order=ipv4first` only for production Seerr. The production backup and `docker compose config --quiet` passed. Only the Seerr container was recreated; qBittorrent, Sonarr, and Radarr remained running.

After deployment, the Node TMDb connectivity check returned HTTP 401. Authenticated Seerr API checks for `/discover/movies`, `/discover/tv`, `/movie/152532`, and `/tv/97546` all returned HTTP 200 while qBittorrent reported two active downloads and five queued downloads. The Seerr logs had no new catalog fetch failures in the following three-minute check. The HomeServer stack was active and production smoke passed.
