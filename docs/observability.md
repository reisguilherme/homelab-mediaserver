# Observabilidade opcional de mídia

Este consumidor mantém o exporter privado e o dashboard de mídia. Prometheus e
Grafana pertencem a [homelab-platform](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/observability.md),
onde ficam credenciais, persistência e instruções centrais. A configuração não foi
aplicada ao servidor; não comprova reprodução, acesso remoto ou consumo físico.

[compose.observability.yaml](../compose.observability.yaml) forma o projeto
independente `homeserver-observability` contendo **somente** `homeserver-metrics`.
Não mesclar com o Compose principal. O exporter usa o Dockerfile telemetry
existente e inicia `homeserver_telemetry.metrics:app`; não inicializa a aplicação
pública, abre banco, consulta mídia ou recebe credenciais administrativas.

Ele adere à rede Docker externa `homelab-metrics` com alias `media-metrics`, na
porta interna 8090, sem porta no host. A plataforma cria essa rede como interna;
ela precisa existir no **mesmo daemon Docker** antes da adesão. O alias não cria
conectividade entre hosts/VMs/K3s. Rede interna não fornece ACL entre participantes.
Não há socket Docker, devices, filesystem de controle ou `.env` montados.

## Antes de ativar

- Preparar a observabilidade central na plataforma e confirmar a rede privada.
- Preparar o diretório dedicado de snapshots no SSD, sem formatar, montar ou
  alterar `fstab`. Bind ausente é recusado com `create_host_path: false`.
- Revisar e ativar o publicador sanitizado abaixo. Cópia única envelhece; exporter
  vivo sem publicador não comprova evidência operacional fresca.
- Fazer a adesão de target e dashboard na plataforma explicitamente; um exporter
  ativo não é coletado automaticamente.

`HOMESERVER_OBSERVABILITY_DIR` permanece compatível com o publicador e tem default
`/srv/appdata/observability`; aqui só o subdiretório `snapshots` é montado. Não
apontar para `/srv/appdata/control`, bancos, tokens, inventário ou mídia. Nenhuma
credencial Grafana é necessária neste Compose. Campos `HOMESERVER_` aceitos pela
aplicação não mudaram; credenciais centrais usam `HOMELAB_` no repo da plataforma.

Instruções para o destino padrão, após confirmar o SSD:

```bash
sudo install -d -m 0755 /srv/appdata/observability
sudo install -d -o 1000 -g 65534 -m 2750 /srv/appdata/observability/snapshots
```

Ajustar o caminho se personalizado no `.env` privado. UID 1000 é o produtor;
grupo 65534 permite leitura pelo exporter. Setgid mantém o grupo dos arquivos
novos; o publicador usa modo 0640. O exporter UID/GID 65534 recebe somente leitura.

## Publicação dos snapshots

O exporter só lê `host.json` e `capacity.json` no diretório dedicado, montado
somente para leitura. Não montar todo o diretório de controle para obter esses
arquivos e não usar links simbólicos. A publicação é uma etapa separada da stack
do painel; o Compose de observabilidade não liga coletores de mídia.

O publicador deve selecionar uma allowlist de campos dos snapshots já produzidos
no SSD. Copiar integralmente o JSON original não é sanitização. O contrato mínimo
consumido é:

| Arquivo | Campos permitidos |
|---|---|
| `host.json` | `generated_at`; `host.cpu_percent`, `host.ram_percent`, `host.cpu_celsius`, `host.uptime_seconds`; `network.rx_bps`, `network.tx_bps` |
| `capacity.json` | `measured_at`; lista `pools` com `pool_id`, `filesystem_id`, `state`, `measured_at`, `total_bytes`, `free_bytes`, `used_bytes` |

`filesystem_id` deve ser uma identidade interna opaca e não vazia, derivada de
modo estável pelo publicador; nunca publicar UUID bruto. Ela participa da
validação e nunca vira label. `pool_id` admite apenas `ssd`, `hdd`, `hdd2`, `hdd3`;
`state` admite `ready` ou `unavailable`. Remover paths, interfaces livres, nomes de
host, títulos, tokens, erros, labels descritivas, categorias e inventário bruto.
Pools inexistentes não são sintetizados. Capacidade coerente aceita
`free_bytes + used_bytes <= total_bytes`, pois o filesystem pode reservar blocos.

Cada atualização deve escrever um arquivo temporário regular no **mesmo
diretório/filesystem**, ajustar permissões de leitura para o UID/GID 65534, fazer
flush/fsync e substituir o nome final por rename atômico; sincronizar o diretório
quando for necessária durabilidade após queda de energia. Não truncar o arquivo
final em uso. Publicar os dois arquivos individualmente; cada fonte e cada pool
têm timestamp próprio, sem presumir uma transação conjunta. Não renovar o
timestamp original durante uma simples cópia: dado antigo deve permanecer antigo.

O limite é 1 MiB por arquivo, regular e sem symlink. Campos numéricos devem ser
finitos; booleanos não são números válidos. O host vence após 90 s, a capacidade e
cada pool após 30 s. Timestamp mais de 5 s no futuro é inválido. Programar a
publicação com folga dentro dessas janelas e testar a interrupção do publicador:
os valores operacionais devem desaparecer, sem virar zero saudável.

A integração opt-in está em
[compose.observability-source.yaml](../compose.observability-source.yaml). Esse
override pertence ao **projeto principal** e adiciona ao `host-metrics` a opção
`--metrics-output-dir /run/homeserver-metrics`, o bind dedicado de escrita e o grupo
65534. Ele mantém o produtor UID 1000 e publica apenas a allowlist sanitizada,
com identidade opaca. O Compose padrão continua sem essa publicação.

A ativação exige revisão separada porque recria o coletor existente. Preservar
o mesmo nome de projeto, arquivo de ambiente e todos os overrides de storage
atualmente usados no projeto principal. Acrescentar
`-f compose.observability-source.yaml` ao final dessa mesma lista de arquivos.
Em cada uma das etapas abaixo, reutilizar exatamente essa invocação do projeto
principal:

1. Validar a configuração acrescentando `config --quiet`.
2. Construir a imagem atualizada acrescentando `build telemetry`. O serviço
   `host-metrics` usa `homeserver-telemetry:dev` e não tem build próprio; o script
   fica dentro dessa imagem. O build do serviço `telemetry` atualiza a tag usada
   pelo produtor e não reinicia containers. O build do exporter no projeto
   independente não atualiza essa tag.
3. Só após o build bem-sucedido, em janela apropriada, acrescentar
   `up -d --no-deps --force-recreate host-metrics` para recriar somente o produtor
   com a nova imagem e opção, sem reiniciar seus dependentes.

Não fornecer uma lista incompleta de arquivos Compose: isso pode trocar os mounts
do coletor. Esta preparação não executa esses comandos na stack ativa. Confirmar
a publicação e as permissões antes de iniciar o painel.

## Adesão à coleta e ao dashboard

No diretório privado `HOMELAB_PROMETHEUS_TARGETS_DIR` da plataforma, o operador
pode adicionar um arquivo regular `.json` com:

```json
[{"targets": ["media-metrics:8090"], "labels": {"job": "homeserver"}}]
```

O arquivo deve ser legível pelo UID 65534 de Prometheus. A configuração central
começa sem targets de consumidores: nenhum catálogo ou contrato ativa essa coleta.
A label `job` mantém as queries de disponibilidade do dashboard. Remover o target
interrompe coleta futura, preservando histórico já persistido.

O dashboard específico fica em
[deploy/metrics/dashboards/homeserver.json](../deploy/metrics/dashboards/homeserver.json).
Ele usa o datasource central UID `homelab-prometheus`. O operador pode obter esse
arquivo de um commit revisado e montar um diretório privado de dashboards por
um override opt-in na plataforma, conforme o guia central. A base da plataforma
não depende de um caminho de checkout de mídia nem contém esse dashboard.

## Build e operação manual

Após cumprir as pré-condições, executar na raiz deste repo. Manter
`-p homeserver-observability` em todas as operações: `COMPOSE_PROJECT_NAME` no
`.env` ou no processo pode sobrepor o `name` do YAML. O exporter é independente
do projeto de mídia e da persistência central. Estes comandos são instruções
para ativação futura, não foram executados nesta preparação:

```bash
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml config --quiet
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml build homeserver-metrics
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml up -d
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml ps
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml logs --tail=100
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml restart homeserver-metrics
```

Para desligar somente o exporter:

```bash
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml down
```

A rede externa pertence à plataforma e não é removida pelo consumidor. Para
parar a central, primeiro parar os exporters aderidos; não forçar remoção de
rede ocupada. Snapshots persistem; não os apagar para corrigir inicialização.
O Compose de source permanece um opt-in separado e não é desligado por este
comando. A autoridade de recuperação da aplicação continua coordenando bloqueios,
readiness e retomada; painel não autoriza downloads, exclusões ou remount.

## Recursos, interpretação e limites

O exporter mantém teto de 128 MiB, filesystem somente para leitura, capabilities
removidas e `no-new-privileges`. Logs giram em dois arquivos de 10 MiB. Plataforma
reserva tetos experimentais de 512 MiB para Prometheus e 384 MiB para Grafana:
1 GiB com este exporter, sem garantia de consumo, quota ou capacidade real. Não
há persistência de TSDB ou banco Grafana neste consumidor. O histórico central
retém 7 dias ou 4 GiB; exige margem local para WAL, compactação e logs. Operador
deve verificar espaço, permissões, RAM/OOM e latência durante o ensaio.

A central coleta a cada 15 s, timeout de 5 s, até 128 amostras e corpo de 1 MiB por
scrape. Isso dá folga para a validade de 30 s da capacidade, sem corrigir produtor
atrasado. Estados atuais usam consultas instantâneas e históricos preservam
lacunas. `up=1` comprova coleta. `snapshot_valid=1` significa envelope aceito
(JSON, estrutura da fonte e timestamp), sem garantir cada sensor/pool válido ou
recente. `snapshot_fresh=1` exige envelope válido e idade dentro da janela.
Valores e pools passam por validação individual e podem ser omitidos mesmo com
envelope válido. Ausência não é zero; capacidade não é somada entre pools.

Alertas, regras, canal, duração, severidade, responsável, resolução, sonda externa
e teste de entrega continuam pendentes. Queda do host pode derrubar a própria
central. Configuração local não comprova acesso Tailscale, reprodução, dashboard
renderizado, isolamento físico, permissões do SSD ou recuperação após energia.

Os testes verificam isolamento do exporter, mount dedicado, compatibilidade do
dashboard com métricas reais e Compose com ambiente fictício explícito, sem ler
`.env` real ou acessar daemon:

```bash
uv sync --frozen
.venv/bin/python -m pytest tests/contract/test_observability_compose.py -q
```
