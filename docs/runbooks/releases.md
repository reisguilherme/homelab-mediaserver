# Releases e rollback

Release é a SHA Git de 40 caracteres, artefato verificado e manifesto schema2
com todas as imagens por digest. Appdata, mídia e `.env` ficam fora da release.
Build exige checkout limpo e acesso ao registry; não cria tags de produção a
partir de arquivos modificados.

```bash
bash scripts/build-release.sh --image-prefix ghcr.io/SEU_NAMESPACE/homeserver --output /tmp/homeserver-release
sudo bash scripts/deploy.sh --env-file /etc/homeserver/.env \
  --release SHA_DE_40_HEXADECIMAIS \
  --artifact /tmp/homeserver-release/homeserver-SHA.tar \
  --manifest /tmp/homeserver-release/homeserver-SHA.json
sudo bash scripts/smoke.sh --env-file /etc/homeserver/.env
sudo bash scripts/rollback.sh --env-file /etc/homeserver/.env --release SHA_ANTERIOR
```

Troque os placeholders pelos arquivos/SHA realmente produzidos. O fluxo valida
checksum, caminhos tar, digests, labels, schema e configuração antes de
ativar. Para os escritores sob manutenção, migra o SQLite, troca `current`
atomicamente, configura serviços e verifica saúde antes de liberar admissões.

Falhas deixam diagnóstico sanitizado e tentam retorno somente quando compatível.
Rollback restaura runtime/configuração e preserva segredos atuais; não reverte
bancos implicitamente. Schema incompatível entra em RECOVERY_MODE e requer
[restore isolado](backup-restore.md) seguido de [reconciliação](recovery.md).
Nenhum prune pode remover release necessária ao retorno/recuperação.

[GitHub Actions](github-actions.md) executa os mesmos scripts a partir de um
artefato autenticado associado à CI aprovada.
