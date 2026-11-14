# Observabilidade opcional

Esta entrega prepara um exporter privado de snapshots, Prometheus e Grafana. O
arquivo [compose.observability.yaml](../compose.observability.yaml) forma o projeto
independente `homeserver-observability`; não é um override do Compose principal.
Os comandos abaixo são para ativação manual futura. Não representam instalação,
dashboard em execução, medição de recursos ou testes no servidor.

O exporter inicia `homeserver_telemetry.metrics:app` na imagem construída pelo
Dockerfile telemetry existente. Ele não inicializa a aplicação pública, abre banco,
consulta mídia ou recebe credenciais administrativas. A rede Docker é interna;
Prometheus e exporter não publicam portas. Apenas Grafana publica
`127.0.0.1:3000:3000`. Não há socket Docker, filesystem do host ou `.env` montado
nos containers. Os serviços podem reiniciar sem depender dos serviços de mídia.

## Antes de ativar

- Garantir Linux, Docker Compose e espaço em SSD local. Estes dados não devem
  ficar em NFS, mergerfs, HD USB ou diretório de mídia.
- Provisionar os três diretórios abaixo, sem formatar, montar ou alterar `fstab`.
  O Compose recusa criar automaticamente os caminhos de bind ausentes.
- Definir `HOMESERVER_GRAFANA_ADMIN_USER` e `HOMESERVER_GRAFANA_ADMIN_PASSWORD`
  no `.env` privado. Ambas são obrigatórias; não existe credencial padrão no
  Compose. Usar senha exclusiva, manter `.env` fora do Git e não imprimir o
  resultado completo de `docker compose config` com credenciais reais.
- Revisar e ativar a publicação sanitizada dos dois snapshots conforme a seção
  seguinte. Arquivo copiado uma única vez envelhece e deixa de alimentar valores
  operacionais. Sem publicador ativo, o exporter continua vivo e indica falta de
  evidência fresca.
- Confirmar que o acesso ao loopback funciona no ambiente escolhido. Acesso
  remoto exige um caminho privado autenticado configurado separadamente; este
  arquivo não abre roteador, cria proxy, configura MFA ou publica via Tailscale.

`HOMESERVER_OBSERVABILITY_DIR` é opcional e tem default
`/srv/appdata/observability`. Ele deve apontar para uma pasta dedicada no SSD, nunca
para `/srv/appdata/control` ou para um diretório com bancos, tokens ou inventário.
As variáveis são interpoladas pelo Compose; só as credenciais selecionadas chegam
ao Grafana. O `.env` inteiro não é repassado a nenhum container e não há `*_FILE`.
As credenciais iniciais criam o administrador no primeiro banco Grafana; alterar
essas variáveis não é um procedimento de rotação de uma conta já persistida.

Preparação manual dos caminhos padrão, após confirmar o SSD de destino:

```bash
sudo install -d -m 0755 /srv/appdata/observability
sudo install -d -o 1000 -g 65534 -m 2750 /srv/appdata/observability/snapshots
sudo install -d -o 65534 -g 65534 -m 0750 /srv/appdata/observability/prometheus
sudo install -d -o 472 -g 472 -m 0750 /srv/appdata/observability/grafana
```

Se o caminho for personalizado no `.env`, ajustar os comandos ao mesmo destino.
Os IDs acima correspondem aos usuários fixados no Compose. Os arquivos de
provisioning versionados precisam ser legíveis pelo Grafana e Prometheus.

O diretório de snapshots pertence ao produtor UID 1000 e ao grupo 65534 do
exporter, com setgid para arquivos novos herdarem esse grupo. O publicador grava
arquivos com modo 0640; o exporter não pode modificá-los.

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

## Recursos e persistência

| Componente | Teto de RAM | Persistência local |
|---|---:|---|
| Exporter | 128 MiB | Somente leitura de `snapshots/` |
| Prometheus | 512 MiB | `prometheus/`, retenção inicial de 7 dias ou 4 GiB |
| Grafana | 384 MiB | `grafana/`, banco/configuração/plugins |
| Total | 1 GiB | Dentro do orçamento existente do core |

O teto conjunto é **experimental**, não uma previsão de consumo ou garantia de
desempenho. Reservar inicialmente 8 GiB de SSD para Prometheus, pelo menos 1 GiB
separado para Grafana e margem adicional para logs, build e crescimento. Os binds
não criam quota nem reserva física: confirmar o orçamento disponível antes de
ativar. O limite `4GB` de Prometheus usa unidades em potência de dois; a primeira
política atingida encerra a retenção. WAL/head, compactação e limpeza assíncrona
exigem folga; retenção não impede todos os arquivos de exceder 4 GiB. Prometheus
requer armazenamento local compatível com POSIX. [Armazenamento oficial](https://prometheus.io/docs/prometheus/latest/storage/).

Há um único target, coleta a cada 15 s, timeout de 5 s, limite de 128 amostras por
scrape e corpo de até 1 MiB. O intervalo dá folga para a validade de 30 s da
capacidade; não corrige atraso do produtor. Logs Docker giram em dois arquivos de
10 MiB por container. Não há Loki, Alloy, traces ou coleta de todos os containers.

Monitorar espaço livre no SSD, crescimento dos diretórios, RAM/OOM e latência do
controlador durante o ensaio. Usar `df` no SSD, `du` somente nesses diretórios e
`docker stats` do projeto. Estes controles são manuais nesta etapa; o dashboard
não mede quota dedicada, não cria alertas de disco e não reserva a margem do host
para outra VM. O histórico é local e não protege contra falha do mesmo SSD/host.

## Build e operação manual

Após cumprir as pré-condições, executar na raiz do repositório. Manter
`-p homeserver-observability` em todas as operações desse projeto: o `.env` pode
conter `COMPOSE_PROJECT_NAME` do projeto principal, que sobrepõe o `name` do YAML.
A opção explícita `-p` mantém a stack do painel separada mesmo nesse caso.

```bash
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml config --quiet
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml build homeserver-metrics
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml up -d
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml ps
```

Abrir `http://127.0.0.1:3000` no host apropriado e entrar com a conta definida.
O datasource `HomeServer Prometheus` e o dashboard `HomeServer • host, rede e
pools` são provisionados por arquivos somente para leitura. Não mesclar esse
arquivo com `compose.yaml`, nem usar o nome do projeto principal. Reiniciar só o
painel com:

```bash
docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml restart grafana
```

`docker compose -p homeserver-observability --env-file .env -f compose.observability.yaml down` remove apenas
os containers/rede desse projeto; os dados nos binds persistem. Não apagar esses
diretórios para corrigir um problema de inicialização. Conferir permissões,
configuração e logs antes de qualquer procedimento de recuperação.

Os estados atuais usam consultas instantâneas; séries históricas preservam
lacunas. `up=1` significa coleta bem-sucedida. `snapshot_valid=1` significa
**envelope válido**: JSON, estrutura da fonte e timestamp aceitos. Não comprova
que cada sensor ou pool tem valor válido, nem que a evidência é recente;
`snapshot_fresh=1` exige envelope válido e idade dentro da janela. Valores
operacionais e pools passam por validação individual e podem ser omitidos mesmo
com envelope válido. Ausência de sensor/pool não é zero. Capacidade é exibida
separadamente por pool, sem soma física entre eles.

**Alertas ainda não são operacionais.** Canal de notificações, regras, duração,
severidade, responsável, resolução e teste de entrega continuam pendentes. Não
há contact point, Alertmanager ou sonda externa provisionados. Queda completa do
host pode derrubar o próprio painel; consultar o desenho de
[operação e governança](architecture/homelab-2026/operations.md).

## Versões e validação

Consulta às fontes oficiais em **14/11/2026**:

- `prom/prometheus:v3.13.5`: versão LTS publicada em 21/10/2026, escolhida para
  esta preparação. A página também lista 3.16.0 como latest; esta stack fixa a
  linha LTS. [Download Prometheus](https://prometheus.io/download/).
- `grafana/grafana:13.3.1`: Grafana OSS publicado em 10/11/2026, com essa tag Docker
  explicitamente documentada. [Download Grafana OSS](https://grafana.com/grafana/download/13.3.1?edition=oss&platform=docker).
- O repositório `grafana/grafana` é o repositório OSS mantido; `grafana/grafana-oss`
  deixou de receber atualizações a partir de 12.4.0. [Docker oficial](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/).

As tags estão fixadas, sem `latest`; esta preparação não alega ter baixado ou
executado essas imagens. Rever atualizações e validar as versões antes do ensaio.
Provisioning segue o [formato oficial Grafana](https://grafana.com/docs/grafana/latest/administration/provisioning/).

Os testes de contrato verificam isolamento, credenciais obrigatórias, mounts,
retenção, orçamento, datasource e queries do dashboard:

```bash
uv sync --frozen
.venv/bin/python -m pytest tests/contract/test_observability_compose.py -q
```

Na preparação, YAML/JSON e Compose foram validados com variáveis fictícias e
arquivo de ambiente temporário explícito, sem carregar o `.env` privado nem
contatar o daemon. Credenciais ausentes foram recusadas. Isso não comprova
provisioning em runtime, renderização visual, permissões do SSD, acesso remoto,
consumo de RAM, alertas ou funcionamento físico do servidor.
