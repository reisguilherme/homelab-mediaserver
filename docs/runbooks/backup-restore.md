# Backup e restauração

O backup cobre configurações, bancos, estado do controlador, outbox e versões;
não cobre `/srv/data/media`, cache de transcodificação, logs extensos ou tokens
de sessão dispensáveis. O destino externo e a senha do Restic ficam fora do Git.

Fluxo no servidor:

```text
backup.sh --config /etc/homeserver/backup.env --capture-and-send
backup.sh --config /etc/homeserver/backup.env --send-pending
backup.sh --config /etc/homeserver/backup.env --verify-repository
restore.sh --config /etc/homeserver/backup.env --snapshot ID \
  --target /srv/recovery/ID --isolated
```

`restore.sh` exige `--isolated`, recusa raízes de produção e grava
`RECOVERY_MODE` com `admission_enabled=false`. Depois da restauração, revisar
reservas, tombstones e arquivos reais antes de reabrir admissões. Uma cópia
local bem-sucedida não prova que o envio externo foi concluído.
## Garantias de staging

Snapshots enviados recebem `.sent` no staging e no repositório. Uma execução
posterior de `--send-pending` verifica esse marcador e não copia novamente uma
geração já publicada. O staging remove gerações enviadas mais antigas conforme
`BACKUP_STAGING_GENERATIONS` e preserva a geração mais recente quando ela, sozinha,
ultrapassa `BACKUP_STAGING_MAX_BYTES`.
