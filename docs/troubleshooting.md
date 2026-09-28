# Diagnóstico

Comece por `config validate`, `doctor` e readiness; preserve a mídia e o estado
antes de tentar recuperação. O `.env` privado continua sendo a configuração
editável. Saídas de comandos nativos precisam de sanitização antes de compartilhar.

```bash
sudo .venv/bin/python scripts/homeserver config validate --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver doctor --env-file /etc/homeserver/.env
systemctl status homeserver-stack.service homeserver-metrics.service
journalctl -u homeserver-stack.service --since '30 minutes ago'
```

| Sintoma | Verificação e ação |
|---|---|
| Configuração inválida/exit 2 | Revisar a chave indicada, tipo, listas, portas duplicadas, modo e conflito `_FILE`. Não usar `source .env`. |
| UUID/mount recusado | `findmnt --mountpoint <MEDIA_ROOT>` e `check-mount.sh <path> <UUID>`. Confirmar filesystem correto, montagem rw e permissão. Um diretório vazio em `/` não é prova de disco. |
| Native HTTP 401/403 | Importar a chave/senha real da instância. Init de env não altera credenciais existentes. Repetir plan; nunca resetar conta para contornar erro. |
| Native `unsupported` | Verificar versão, schema/definition/fields/IDs reais do serviço. Configurar capability suportada ou fazer adoção explícita; não aceitar como sucesso. |
| Native read-back drift | Conferir preferências efetivas e outra configuração concorrente. Repetir plan após corrigir; writes com resposta perdida são relidos antes de repetir. |
| Arquivo gerado modificado | Comparar com backup/journal. Migrar intenção para `.env`; não apagar o journal ou forçar overwrite de arquivo desconhecido. |
| Container unhealthy/encerrado | Conferir supervisor.json, tentativas na janela, memória, logs e mounts. Supervisor respeita manutenção/recovery e tem limite de reinícios. |
| Readiness 503 | Checar RECOVERY_MODE, heartbeat do worker, idade do snapshot e capacidade. `/health/live` 200 sozinho não permite novas aquisições. |
| Pedido Requested por horas | Consultar fila Arr/qBit e estado do controlador: pode aguardar espaço, fonte, sequência de série, legenda ou importação. Repetir o pedido não resolve essa condição. |
| Fonte existe no site, mas Arr não a encontra | Conferir indexador habilitado, categorias/caps da aplicação e sincronização Prowlarr. Se usar Byparr, proxy e indexador precisam de tag correspondente; proxy sem tags não comprova que a busca o utiliza. |
| Indexador responde HTTP 429 | Conferir logs sanitizados, limitação da fonte e seleção do proxy. Não insistir em buscas contínuas; corrigir a configuração e respeitar o retry antes de testar novamente. |
| Release pequena anunciada como 1080p | Conferir duração e bytes do vídeo principal contra o piso MiB/min. Tamanho do torrent inteiro/seeds não tornam um encode elegível; samples/sidecars não contam. |
| qBit 100%, mas mídia indisponível | Conferir validação, legenda e importação pelo worker; depois sincronização Arr/Jellyfin/Seerr. Completed Download Handling Arr permanece desabilitado e hardlinks devem estar habilitados. |
| Fonte lenta sem troca | Verificar janela de 300 s, qualidade/edição, peers medidos, bytes conjuntos, ETA e proteção de hash. Download pausado/completo ou candidato inferior não deve ser promovido. |
| Painel mostra dados antigos | Conferir homeserver-metrics.service e arquivos em RUN_ROOT. Ausência de host/capacidade é indisponibilidade explícita; não fabricar métricas. |
| Intel inacessível | Revisar render node e GIDs reais, permissões e driver. CPU funciona sem GPU; habilitar Intel exige teste no host e playback. |
| Backup/copy falha | Revisar Restic, repository, senha `_FILE`, espaço de staging e host key SFTP. Falha de copy não invalida automaticamente o snapshot local; validar cada repo. |
| Release falha | Conferir manifesto, digest, SHA e compatibilidade de schema. Preservar maintenance/RECOVERY_MODE até reconciliar; rollback não restaura banco silenciosamente. |

## Manutenção e retorno

As operações do projeto coordenam `RUN_ROOT/operation.lock` e o marcador
`RUN_ROOT/maintenance`. Backup/deploy/configuração de runtime param escritores
e só retomam após guarda da montagem. Não iniciar containers manualmente enquanto
esses marcadores representam uma operação real.

Depois de uma falha, confirme que nenhum backup/deploy está executando, preserve
o estado e examine a razão registrada. Execute doctor novamente. Restart do
supervisor pode ser feito com `sudo systemctl restart homeserver-stack.service`
apenas após corrigir a causa e confirmar a montagem; recovery continuará bloqueando
admissão. Não remova RECOVERY_MODE como tentativa de fazer healthcheck ficar verde.

Para recuperação de backup, siga [restore isolado](operator-guide.md#backup-e-restore)
e [reconciliação](runbooks/recovery.md). A API/worker/gateway aceitam operação normal
somente depois da revisão de UUID, capacidade, schema, tombstones e efeitos
externos. O supervisor também respeita a presença do marcador de recovery.

## Evidência

Registre versão/SHA, comando, resultado sanitizado, horário e ambiente. Use
`reported`, `verified`, `missing` e `unknown` para separar declaração de medição.
Fixtures e testes WSL validam contratos; reprodução, reboot, UUID físico,
Intel, rede, energia, SFTP e Tailscale exigem máquina real. Nenhum banco, token,
inventário bruto ou nome privado de mídia deve ser adicionado ao Git.
