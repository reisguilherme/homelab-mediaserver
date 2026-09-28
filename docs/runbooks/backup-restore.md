# Backup e restauração

Habilite `BACKUP_ENABLED` e configure repositories, senhas `_FILE`, agenda e
retenção no `.env`. Restic captura appdata/configuração/releases consistentes;
a stack para durante a captura. SQLite é materializado com a API de backup,
sem depender de sidecars WAL/SHM. Mídia, torrents e transcode não são incluídos.

```bash
sudo .venv/bin/python scripts/homeserver backup create --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup verify --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup copy --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup restore --env-file /etc/homeserver/.env \
  --snapshot ID_RETORNADO --target /srv/restore-isolado
```

A captura valida a nova cópia antes da retenção. Receipt persistente inclui
horário, duração e tamanho observados; o painel mostra a idade da última cópia
verificada. Copy usa `restic copy` sob locks e criptografia/retenção independente.
A cópia externa precisa ser verificada separadamente; rsync de um repositório
em prune não é o transporte atual. Preserve repositórios antigos até comprovar
que os snapshots necessários foram copiados e restaurados.

Restore só publica árvore isolada vazia após validar checksums e SQLite.
Cria RECOVERY_MODE, preserva tombstones e não assume UUID do host antigo.
Não copie a árvore sobre os serviços ativos; siga [recuperação](recovery.md).
Guarde as senhas fora dos repositórios: sem elas não há recuperação.

No desktop Windows com WSL/Python/uv/Restic, use um `.env` privado de transporte:
repository origem via SFTP, target em `/mnt/c/.../HomeServer/backup/restic`,
passwords e roots locais. `backup/` é ignorado pelo Git.

```powershell
powershell.exe -NoProfile -File scripts/pull-backup.ps1 -EnvFile C:\caminho\backup.env
powershell.exe -NoProfile -File scripts/register-backup-task.ps1 -EnvFile C:\caminho\backup.env -At 04:30
```

A chave de transporte deve ter acesso somente ao repositório necessário e a
host key deve ser verificada. Desligamento do desktop não comprova cópia externa.
Meça RPO/RTO com restore real; sucesso do timer sozinho não cumpre esse aceite.
