# Executar o servidor

O procedimento atual é [instalação Ubuntu e Tailscale](../installation.md).
No checkout com `.env` preenchido e diretórios preparados:

```bash
docker compose up -d --build
docker compose run --rm operator config apply --env-file /project/.env --in-container
docker compose restart control-api control-worker download-gateway telemetry host-metrics
```

Para atualizar código, use `git pull --ff-only` e `docker compose up -d --build`.
Veja [operação](../operator-guide.md) para alterações de preferências.
Não há pipeline GitHub, publicação de releases, manifests de deploy ou comandos
próprios de backup.
