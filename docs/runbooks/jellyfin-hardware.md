# Jellyfin e Intel UHD

O perfil de produção só recebe um render node Intel identificado no Legion.
`scripts/verify-gpu.sh` exige o caminho real e o usuário efetivo do container;
`renderD128` não é assumido.

Antes do aceite, registrar para cada combinação H.264 1080p, HEVC 4K SDR,
HEVC 10-bit HDR→SDR e burn-in de legenda: codec, bitrate, cliente, FPS,
temperatura, buffer, modo direto/transcodificado e logs FFmpeg que comprovem
uso do hardware. A RTX não faz parte da configuração.
