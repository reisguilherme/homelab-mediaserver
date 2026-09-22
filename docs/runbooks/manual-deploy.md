# Deploy manual

1. Auditar o host e confirmar a montagem por UUID.
2. Produzir artefato e manifesto com SHA do commit, digests de imagens e
   checksum do artefato.
3. Copiar o artefato por uma sessão SSH já autorizada e executar `deploy.sh` no
   Legion com `/etc/homeserver/deploy.env`.
4. Executar `smoke.sh` e revisar readiness, logs e versão antes de liberar
   admissões.

O script não substitui `/srv/data`, `/srv/appdata` ou configurações preexistentes
e impede manifestos sem digests. Para operação real, registrar o SHA efetivo e
o resultado do smoke em `docs/evidence/` sanitizado.
