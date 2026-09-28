# Diagnóstico

Comece pelos containers, logs e validação do `.env`:

```bash
docker compose ps
docker compose logs --tail=100
docker compose run --rm operator config validate --env-file /project/.env
df -hT /srv/data
tailscale status
```

Revise saídas nativas antes de compartilhar: logs podem conter informações
privadas. Reiniciar sem corrigir a causa costuma repetir o erro.

| Sintoma | Verificação e ação |
|---|---|
| Configuração inválida | Corrigir a chave/tipo/lista indicada; não usar `source .env`. |
| Diretório ausente ou sem escrita | Conferir /srv e UID/GID 1000; conferir o filesystem montado com `findmnt --target /srv/data`. |
| Native HTTP 401/403 | Importar a chave/senha real no .env; init/apply não resetam contas existentes. |
| Native unsupported | Conferir versão, definição, fields e IDs reais; não tratar capacidade não suportada como sucesso. |
| Read-back drift | Conferir preferências nativas e configurações concorrentes; repetir plan/apply/verify. |
| Container encerrado/unhealthy | Ver logs, memória e mounts; repetir a inicialização após corrigir a causa. |
| Requested por horas | Conferir Arr/qBit/controlador: pode aguardar espaço, fonte, janela de episódios, legenda ou importação. |
| Fonte aparece no site, mas Arr não encontra | Conferir indexador, categorias/caps e sincronização Prowlarr; proxy Byparr deve compartilhar a tag gerenciada com a fonte. |
| Indexador responde 429 | Revisar seleção do proxy e limitação da fonte; respeitar o retry configurado. |
| Torrent pequeno anunciado como 1080p | Conferir bytes do vídeo principal e duração contra o piso MiB/min; samples/sidecars não contam. |
| qBit 100%, mas nada no Jellyfin | Ver validação, legenda e importação worker; episódio pronto aguarda anteriores importados. Depois conferir sync Arr/Jellyfin/Seerr; manter CDH desabilitado e hardlinks habilitados. |
| Fonte lenta sem troca | Conferir janela, qualidade/edição, peers medidos, capacidade conjunta e ETA; pausa/completo ou candidata inferior não deve ser promovida. |
| Preferência alterada sem efeito | Rodar operator apply e reiniciar os cinco consumidores conforme o guia do operador. |
| Painel sem métricas | Ver logs de host-metrics/telemetry e idade dos snapshots; primeira amostra não tem taxa. |
| Serviço inacessível pelo Tailscale | Conferir autenticação/status, IP atual e acesso na mesma tailnet; usar a porta do serviço. |
| Monitor qBit inacessível | Conferir loopback18080 e `tailscale serve status`; usar o endereço MagicDNS informado pelo Serve. |
| Intel inacessível | Conferir renderD128 e GIDs reais no override; consultar o guia de hardware e testar reprodução. |

## Comandos por serviço

```bash
docker compose logs --tail=100 control-worker download-gateway
docker compose logs --tail=100 sonarr radarr prowlarr
docker compose logs --tail=100 jellyfin seerr bazarr
docker compose logs --tail=100 host-metrics telemetry
docker compose run --rm operator config plan --env-file /project/.env --in-container
docker compose run --rm operator config verify --env-file /project/.env --in-container
```

O healthcheck do gateway verifica processo HTTP; autorização de downloads
também exige capacidade recente, reservas válidas e estado consistente.
Não contorne o gateway para fazer um pedido começar.

## Evidência

Registre comando, horário, versão e resultado sanitizado. Separe declarado,
verificado, ausente e desconhecido. Fixtures Linux/WSL comprovam contratos;
rede, reinício, montagem física, GPU e reprodução precisam da máquina real.
Nunca adicione bancos, mídia, credenciais ou inventário bruto ao Git.

Veja [operação](operator-guide.md), [serviços](runbooks/service-setup.md) e
[hardware Jellyfin](runbooks/jellyfin-hardware.md).
