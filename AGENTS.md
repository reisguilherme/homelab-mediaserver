# homelab-mediaserver — Guia do Agente (Spoke 1)

Este repositório é o **Spoke de Streaming e Automação de Mídia**, executado no namespace `spoke-mediaserver` dentro da `VM 200 (homelab-k3s)` e reconciliado pelo FluxCD do `homelab-platform`.

## Arquitetura e Fronteiras

- **Aplicações nativas (zero wrappers customizados):**
  - `jellyfin`: Imagem `homeserver-jellyfin-abyss:12.2-2f809171-r6` (tema Abyss local em `docker/jellyfin-abyss/`), aceleração Intel QuickSync (`/dev/dri`), transcode no disco `/mnt/transcode/jellyfin` e biblioteca montada em `/data/media` com `readOnly: true`.
  - `qbittorrent`: Monta `/data` (`/mnt/storage/homeserver`) RW preservando os **hardlinks** existentes e mantendo os **733 torrents concluídos em seeding**.
  - `radarr`, `sonarr`, `prowlarr`, `bazarr`, `seerr`, `byparr`, `recyclarr`: Fluxo nativo padrão Trash Guides com `UID:GID 1000:1000`, `restartPolicy: Always` e probes HTTP reais.
  - `arr-mcp`: Endpoint MCP interno (`http://arr-mcp.spoke-mediaserver.svc.cluster.local:6060/mcp`) consumido via `NetworkPolicy` restrita pelo `spoke-hermes` (token de escrita com preview/confirmação) e pelo `spoke-dashboard` (token dedicado de leitura `read`).

## Regras Estritas

1. **Nunca reintroduzir o controlador legado:** Proibido criar proxies intermediários de qBit/Seerr/Jellyfin, leases temporais, recibos SHA-256 ou `restart: 'no'`.
2. **Semântica POSIX de `/data`:** Writers (`radarr`, `sonarr`, `bazarr`, `qbittorrent`) compartilham `/data` no mesmo filesystem `ext4` para manter hardlinks atômicos; `jellyfin` recebe exclusivamente `/data/media` em modo **read-only**.
3. **Infra compartilhada pertence ao Hub:** Não versionar aqui configuração de Tailscale, Cloudflare ACME, Authelia ou OpenTofu do Proxmox.
4. **Segredos e higiene:** Segredos vão apenas em `secrets/*.sops.yaml` (base em `secrets/mediaserver-secrets.example.yaml`). Nunca criar `.artifacts/`, `.runtime/`, `.worktrees/` ou `backup/`.

## Validação Local

```bash
node scripts/validate.mjs
node docker/jellyfin-abyss/test-defaults.mjs
node docker/jellyfin-abyss/test-runtime.mjs docker/jellyfin-abyss/runtime
```
