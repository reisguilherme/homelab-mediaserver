# Recuperação

Em perda de serviço, preservar a mídia e bloquear admissões. Verificar
`/health/live` para diferenciar processo vivo de prontidão. Restaurar em um
diretório isolado, validar manifesto/checksums e reconciliar arquivos antes de
iniciar o worker.

Uma instância recuperada deve responder readiness 503 com
`admission_enabled=false` até que UUID, espaço atual, schema e efeitos externos
tenham sido revisados. Não recriar mídia ausente nem reabrir scheduler baseado
em uma reserva antiga.

O arquivo `RECOVERY_MODE` é montado em `/var/lib/homeserver/RECOVERY_MODE` nos
processos próprios. API, worker e gateway permanecem bloqueados enquanto o
arquivo existir sem `admission_enabled=true`; remover ou alterar o marcador
somente depois da reconciliação manual e de uma nova medição de capacidade.
