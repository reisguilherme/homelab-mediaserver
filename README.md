# homelab-mediaserver (Spoke 1)

Stack nativa de streaming e automação de mídia para o cluster **K3s + FluxCD** no namespace `spoke-mediaserver`.

> [!NOTE]
> O estado anterior ao refactor (`5854e0b`) foi preservado na branch/tag `archive/legacy-2026-11`. A branch `main` contém apenas a stack Kubernetes declarativa limpa e o build reproduzível do tema Abyss.

## Componentes

| Serviço | Papel | Acesso / Storage |
|---|---|---|
| `jellyfin` | Player de mídia com tema Abyss + Intel QuickSync (`/dev/dri`) | `jellyfin.greis.dev.br` + LAN `:8096` \| `/data/media` (**RO**) + `/transcode` NVMe |
| `seerr` | Portal de pedidos e descoberta | `overseer.greis.dev.br` \| SSD PVC (`local-path`) |
| `radarr` / `sonarr` | Automação de filmes e séries | `radarr.greis.dev.br` / `sonarr.greis.dev.br` (SSO Hub) \| `/data` (**RW**) |
| `prowlarr` / `byparr` | Indexadores e resolução de desafios Cloudflare | `prowlarr.greis.dev.br` (SSO Hub) / interno `:8191` |
| `bazarr` | Gerenciamento nativo de legendas | `bazarr.greis.dev.br` (SSO Hub) \| `/data` (**RW**) |
| `qbittorrent` | Cliente BitTorrent preservando seeding e hardlinks | `qbittorrent.greis.dev.br` (SSO Hub) \| `/data` (**RW**) + porta `6881` |
| `recyclarr` | CronJob de perfis Trash Guides (1080p + Dual-Áudio/PT-BR) | ConfigMap sincronizado via GitOps |
| `arr-mcp` | Ponte MCP para o Spoke `homelab-hermes` | Interno ClusterIP `:6060` + `media-mcp.greis.dev.br` (SSO Hub) |

## Validação e Build Local

```bash
node scripts/validate.mjs
node docker/jellyfin-abyss/test-defaults.mjs
node docker/jellyfin-abyss/test-runtime.mjs docker/jellyfin-abyss/runtime
```

Para construir a imagem local do Jellyfin com tema Abyss na `VM 200` durante o cutover:

```bash
docker build -t homeserver-jellyfin-abyss:12.2-2f809171-r6 docker/jellyfin-abyss
```
