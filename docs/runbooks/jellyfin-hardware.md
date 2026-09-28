# Jellyfin e Intel UHD opcional

CPU é o padrão; reprodução Direct Play usa o arquivo original quando o cliente
suporta seu codec. Intel pode acelerar transcodificação quando houver render
node compatível e permissão no host.

```bash
ls -l /dev/dri
getent group render
getent group video
```

O override [compose.intel.yaml](../../deploy/compose.intel.yaml) adiciona
`/dev/dri/renderD128` ao Jellyfin. Confirme que esse device corresponde à Intel
e ajuste `group_add` aos GIDs reais de `render` e `video` na sua máquina.
Os números do exemplo não são prova do hardware.

```bash
docker compose -f compose.yaml -f deploy/compose.intel.yaml up -d --build
```

Use os dois arquivos Compose nos comandos posteriores dessa instalação.
Na administração Jellyfin, configure aceleração Intel QSV ou VA-API conforme
[as instruções oficiais Jellyfin](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/intel/).
Não habilite uma GPU diferente por suposição.

Teste no cliente real H.264 1080p, HEVC 4K, HDR→SDR e legenda com burn-in, quando
aplicáveis. Confira modo de reprodução, buffering e logs FFmpeg que comprovem
aceleração. Um device visível no container não comprova transcodificação
correta, e um teste no PC não comprova capacidade da TV.
