# Disponibilidade observada da playback

O consumidor publica quatro gauges sem rótulos em
`/var/lib/prometheus/node-exporter/playback.prom`. O node_exporter existente
já escuta somente no endereço privado do guest; seu coletor textfile precisa
usar esse diretório. A plataforma coleta pelo target existente com
`job="guests", role="playback"`. Este procedimento não abre portas e não ativa
as métricas nativas do Jellyfin, a automação de mídia, scans ou Abyss.

| Métrica | Interpretação |
| --- | --- |
| `homeserver_playback_jellyfin_up` | 1 somente se HTTP saudável **e** lease admitida nesta observação |
| `homeserver_playback_storage_admitted` | 1 somente para lease válida deste boot, `ready`, com validade restante maior que zero e de até 45 s |
| `homeserver_playback_lease_seconds_remaining` | Validade restante observada; zero em recusa |
| `homeserver_playback_observed_timestamp_seconds` | Horário Unix desta observação concluída |

O produtor usa somente Python 3.12 stdlib e lê a lease fixa
`/var/lib/homelab-storage/admission/lease`, o boot ID e uptime do guest. Exige
os mesmos cinco campos ordenados, UUID minúsculo, geração de 32 hexadecimais
minúsculos e expiração inteira usados pelo guard. Arquivo e diretório pai
devem ser de root, sem escrita de grupo/outros; a lease deve ser regular,
sem symlink ou hardlink e ter até 512 bytes. Os ancestrais de
`/var/lib/homelab-storage/admission` são diretórios de sistema confiáveis,
mantidos pela plataforma. O produtor não cria ou corrige esse diretório.

A única sonda é `http://IP_PRIVADO:8096/jellyfin/health`, sem credenciais,
proxies do ambiente ou redirects, com timeout de 3 s e leitura de até 32 bytes.
Somente HTTP 200 e corpo `Healthy` (ou uma quebra de linha final) passam.
O endereço é um literal IPv4 RFC1918; URL, nome DNS, porta e caminho adicionais
são recusados. A sonda precede a leitura da lease para não consumir sua validade
durante o timeout HTTP. Lease ausente, inválida, antiga, expirada ou HOLD
resulta em admissão zero mesmo com resposta HTTP saudável.

## Instalação revisada somente no guest dedicado

Os exemplos em [`deploy/playback`](../../deploy/playback) destinam-se à VM
playback. Não instalar units no Proxmox ou em host compartilhado. A plataforma
continua dona do storage, do exporter e dos alertas; esta unit apenas observa
o consumidor. Não lê `.env`, bancos, mídia ou socket Docker, não monta discos
e não concede lease, reinicia serviço ou remove parada manual.

1. Confira Python 3.12, diretório textfile já existente e argumento
   `--collector.textfile.directory=/var/lib/prometheus/node-exporter` do exporter.
   Preserve seu bind privado e as ACLs existentes.
2. Instale o script como root 0755 em
   `/usr/local/lib/homeserver/playback-health.py`. Instale o exemplo de configuração
   em `/etc/homeserver/playback-health.conf`, root 0600, substituindo o placeholder
   pelo endereço privado revisado. Nenhum valor real precisa ir ao Git.
3. Instale as duas units como root 0644 em `/etc/systemd/system/`. Execute
   `systemd-analyze verify` nos arquivos e `systemctl daemon-reload` no guest.
   Execute uma vez `systemctl start homeserver-playback-health.service` e confira
   o exit status, os quatro valores, o timestamp e o modo 0644 do `.prom`.
4. Habilite o timer com `systemctl enable --now homeserver-playback-health.timer`.
   O intervalo é 15 s e cada execução tem limite de 8 s. O serviço roda como
   root para atravessar o diretório privado da plataforma, sem capabilities,
   com filesystem protegido e escrita somente no diretório textfile.
5. Confira no Prometheus as séries do guest, os três novos painéis de playback
   e as regras `PlaybackJellyfinUnavailable`, `PlaybackStorageAdmissionRefused`
   e `PlaybackCollectorStale`. A plataforma provisiona esses arquivos; não
   aplicar configurações amplas ou recriar a stack de mídia para instalá-los.

## Falha e interpretação

O arquivo é substituído atomicamente após escrita e fsync. Falha da sonda
publica zero; falha do produtor ou da escrita preserva o timestamp anterior,
que fica desatualizado. Observação ausente, mais antiga que 60 s ou no futuro
não é evidência de saúde. As regras sustentam a condição por 1 minuto; painéis
de disponibilidade só exibem valores com observação recente, válida e exporter
alcançável. O painel de idade permite identificar atraso ou relógio no futuro.

Para ensaiar coleta parada sem tocar no Jellyfin, pare somente o timer e
aguarde o alerta de coleta desatualizada. Depois reinicie o timer e confira
timestamp novo e resolução. Ensaio de HOLD ou lease expirada exige a autoridade
de storage e autorização operacional; este coletor nunca provoca essa falha.
Na retirada, pare/desabilite somente este timer. Remover o `.prom` produz
ausência explícita; não deixe valores antigos aparentarem saúde.

Essas métricas não comprovam reprodução em cliente, autenticação de usuários,
integridade física, leitura de todo o acervo ou recuperação USB. A sonda HTTP e
a lease não substituem as verificações de mounts do guard. Não existe retomada
automática adicional: HOLD, `resume` explícito e paradas manuais continuam com
seus responsáveis atuais. Alertas locais não comprovam notificação externa.

Os testes usam fixtures temporárias e dependências injetadas:
`uv run --frozen pytest tests/unit/test_playback_health.py -q`. A implantação,
as permissões efetivas da unit e a resolução dos alertas precisam de prova no
guest; testes locais não atestam essa etapa.
