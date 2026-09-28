# Operações

O [guia do operador](operator-guide.md) reúne os comandos atuais de configuração,
supervisão, backup e atualização. Edite somente o `.env` privado; Compose,
operator.env, units e configurações nativas gerenciadas são derivados. Use
`config plan` antes de apply e `config verify` depois dele.

| Situação | Procedimento |
|---|---|
| Preparar ou adotar um host | [Instalação fresh/adopt](installation.md) e [migração de setembro](migrations/2026-09-productization.md) |
| Alterar limites, idioma, qualidade ou credenciais | [Guia do operador](operator-guide.md) e [catálogo](configuration.md) |
| Conferir contas, bibliotecas, clientes e perfis | [Preparação dos serviços](runbooks/service-setup.md) |
| Acompanhar pedidos, importação e exclusão explícita | [Operação diária](runbooks/operations.md) |
| Investigar indisponibilidade ou drift | [Diagnóstico](troubleshooting.md) e [baseline do host](runbooks/server-baseline.md) |
| Conferir métricas e alertas | [Painel](runbooks/status-dashboard.md) e [notificações](runbooks/notifications.md) |
| Capturar, copiar ou restaurar estado | [Backup/restore](runbooks/backup-restore.md) e [recuperação](runbooks/recovery.md) |
| Atualizar ou voltar de release | [Releases/rollback](runbooks/releases.md), [deploy manual](runbooks/manual-deploy.md) e [GitHub Actions](runbooks/github-actions.md) |
| Habilitar Intel opcional | [Jellyfin e hardware](runbooks/jellyfin-hardware.md) |

`/health/live` confirma processo; readiness verifica condições para operar.
Snapshot antigo, worker sem progresso dentro do prazo, UUID incorreto e recovery
bloqueiam admissão. Não remova marcadores de manutenção/recovery para obter um
healthcheck verde. Backup, deploy e config apply coordenam o mesmo lock; a stack
deve permanecer sob essa gestão durante operações de estado.

O monitor nativo qBit usa autenticação própria e permite consulta e login/logout.
Mutações retornam 403. Adições e retomadas passam pelo gateway com checagem de
capacidade; uma mudança de configuração não autoriza contorná-lo. Hashes
protegidos na migração exigem preservar a aquisição existente, conforme o guia
de migração. Exclusão de mídia é explícita e coordenada; assistir não apaga.

Registre SHA, horário, ambiente, comando e resultado sanitizado. A
[evidência de produtização](evidence/productization-acceptance.md) distingue
fixtures e primeiro boot isolado de adoção da produção e aceite físico.
Uma execução de timer ou um painel acessível não comprova restore, reprodução
ou funcionamento de outra máquina.
