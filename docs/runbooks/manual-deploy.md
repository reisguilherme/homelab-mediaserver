# Deploy manual

1. Auditar o host e confirmar a montagem por UUID.
2. Produzir artefato e manifesto com SHA do commit, digests de imagens e
   checksum do artefato.
3. Copiar o artefato por uma sessão SSH já autorizada e executar `deploy.sh` no
   Legion com `/etc/homeserver/deploy.env`.
4. Executar `smoke.sh` e revisar readiness, logs e versão antes de liberar
   admissões.

No servidor atual, a API está vinculada ao endereço Tailscale. Para o smoke de
produção, use a configuração que contém o UUID da mídia e esse endereço:

O `control-api` também usa uma bridge exclusiva não interna para que Docker
publique essa porta no endereço Tailscale; `apps` e `telemetry` continuam
internas, e o gateway de download não entra nessa bridge. Serviços que
consultam fontes externas usam `egress`; o qBittorrent usa
`egress_transfer`. Conferir que o `control-worker` permanece em `egress`
após recriações, pois ele consulta o cache de metadados na admissão e o SubDL
na seleção de legendas. Filmes não fazem prefetch de SRT: o finalizador busca
a legenda após validar o vídeo baixado e bloqueia a importação até encontrar
uma opção elegível ou confirmar áudio original pt-BR. Séries sem sidecar
continuam exigindo SRT persistido antes do permit.

```bash
sudo bash -c 'source /etc/homeserver/server.env; export HOMESERVER_HEALTH_URL="http://${TAILSCALE_BIND_IP}:8080"; bash /opt/homeserver/current/scripts/smoke.sh --config /etc/homeserver/server.env --environment prod'
```

O script não substitui `/srv/data`, `/srv/appdata` ou configurações preexistentes
e impede manifestos sem digests. Para operação real, registrar o SHA efetivo e
o resultado do smoke em `docs/evidence/` sanitizado.
