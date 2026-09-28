# Deploy manual

Prepare o host com [instalação fresh/adopt](../installation.md), mantendo o
`.env` privado em `/etc/homeserver/.env`. `source` e `eval` não são usados para
carregar configuração. Compose e units são gerados pelo loader validado.

Em checkout limpo, produza uma release conforme [releases](releases.md).
Depois transfira artefato e manifesto por SSH autorizado e execute:

```bash
sudo bash scripts/deploy.sh --env-file /etc/homeserver/.env \
  --release SHA_DE_40_HEXADECIMAIS \
  --artifact /caminho/homeserver-SHA.tar --manifest /caminho/homeserver-SHA.json
sudo .venv/bin/python scripts/homeserver config verify --env-file /etc/homeserver/.env
sudo bash scripts/smoke.sh --env-file /etc/homeserver/.env
```

Deploy usa lock, guarda de UUID, backup quando exigido, manutenção, migrações,
configuração nativa e readiness. IDs/bancos/mídia existentes são preservados;
erro não autoriza limpar estado ou admitir downloads sem gateway.

No primeiro deploy, install prepara o runtime bootstrap em `INSTALL_ROOT/current`.
O modo adopt exige importar credenciais e limites nativos atuais. Configuração
não gerenciada não é substituída silenciosamente. Verifique o diff antes da
aplicação e registre SHA/resultado sanitizado, sem inventário privado no Git.
