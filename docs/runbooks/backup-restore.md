# Backup e restauração

O servidor captura diariamente `/srv/appdata`, `/etc/homeserver` e os releases
em `/opt/homeserver/releases` num repositório Restic criptografado em
`/srv/backup-staging/restic`. O script `scripts/backup-restic.sh` verifica a
montagem de mídia, para a pilha durante a captura e a reinicia mesmo se a
captura falhar. A senha de recuperação não entra no snapshot. A retenção local
é de três snapshots. Mídia, downloads, cache de transcodificação e logs não
fazem parte deste backup.

No Windows, a tarefa `HomeServer Backup Pull` executa
`scripts/pull-backup.ps1` diariamente às 04:30, quando o usuário está conectado.
Ela usa WSL/rsync com uma chave SSH restrita à leitura do repositório, copia
incrementalmente para `backup/restic` sem apagar snapshots que já estão no
desktop, verifica a integridade e aplica retenção externa de 7 diárias, 4
semanais e 3 mensais. `backup/` está no `.gitignore`. Se o desktop estiver
desligado, a tarefa tenta executar quando ficar disponível; o servidor mantém
suas três cópias mais recentes. A senha do Restic no desktop fica em
`~/.config/homeserver/restic-password` do usuário WSL, fora de `backup/`.
Guarde uma cópia dessa senha em um gerenciador de senhas: perder a senha impede
a recuperação do repositório.

Verificação manual no Windows:

```powershell
powershell.exe -NoProfile -File scripts/pull-backup.ps1
wsl.exe -- restic -r /mnt/c/Users/Guilherme/Downloads/HomeServer/backup/restic --password-file /home/reis/.config/homeserver/restic-password snapshots
```

Para ensaiar uma restauração, escolha um snapshot com `restic snapshots` e
restaure primeiro para um diretório isolado dentro de `backup/`, nunca
diretamente para `/srv/appdata`. Verifique os dados restaurados e o estado das
reservas antes de recolocar os serviços em operação. O procedimento antigo
`scripts/backup.sh` permanece para fixtures e testes; ele não é a rotina
ativa do servidor.
