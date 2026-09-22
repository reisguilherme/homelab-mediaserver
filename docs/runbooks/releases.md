# Releases, smoke e retorno

Uma release é identificada pelo SHA de 40 caracteres, pelo checksum do artefato e por um manifesto que contém digests imutáveis das imagens. O script valida tudo antes de criar `/opt/homeserver/releases/<sha>` e trocar o apontador `current`; appdata, secrets e `/srv/data` ficam fora da release.

```bash
bash scripts/deploy.sh --release SHA --artifact release.tar --manifest release.json --config /etc/homeserver/deploy.env
bash scripts/smoke.sh --config /etc/homeserver/deploy.env --environment prod
bash scripts/rollback.sh --release PREVIOUS_SHA --config /etc/homeserver/deploy.env
```

Deploys concorrentes são recusados por lock. Falha de smoke mantém o registro da tentativa; retorno de imagem não desfaz migrações de banco. Quando o schema não é compatível, restaurar primeiro um snapshot consistente em modo isolado.
## Estado atual do extrator

O artefato recebido por `scripts/deploy.sh` é um tarball da raiz do checkout e
precisa conter `deploy/compose.yaml`, `scripts/check-mount.sh` e
`scripts/smoke.sh`. O script valida o SHA, o manifesto e o checksum antes de
extrair. Caminhos absolutos, `..`, links e arquivos especiais no tarball são
recusados; o symlink `current` só muda depois que a estrutura extraída passa
essas verificações.

O primeiro incremento ainda não inicia/paralisa a unidade systemd nem executa
migrações ou smoke contra um Legion real. Essas etapas permanecem parte do
fechamento de O02 e exigem ambiente Linux de produção validado.
