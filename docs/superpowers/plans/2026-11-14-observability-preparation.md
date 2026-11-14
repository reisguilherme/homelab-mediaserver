# Preparação da observabilidade sem interromper o servidor

> **For agentic workers:** Use superpowers:subagent-driven-development com responsabilidades separadas e testes de contrato. Não implantar ou reiniciar a stack ativa nesta etapa.

**Objetivo:** entregar um exporter privado de snapshots e uma stack opcional Grafana/Prometheus, testados e publicados na `refactor`, sem alterar o Compose ativo.

**Arquitetura:** processo separado de métricas lê somente snapshots no SSD; Prometheus coleta pela rede interna; Grafana tem bind somente em loopback. Não reutilizar o endpoint público de telemetry, montar mídia ou entregar credenciais de administração ao coletor.

**Stack:** Python 3.12/FastAPI, texto Prometheus, Docker Compose, Grafana/Prometheus com versões fixadas, provisioning versionado sem segredos.

**Spec:** [operação e governança](../../architecture/homelab-2026/operations.md), [plano de migração](2026-11-14-homelab-refactor-migration.md).

## Restrições globais

- Nenhum deploy/restart da stack atual; arquivo Compose separado e opt-in.
- Exporter não abre banco, mídia, `.env` nem socket Docker; lê somente arquivos de snapshot explicitamente autorizados. Sem scans de diretórios de mídia.
- Labels limitadas a fonte fixa, pool conhecido e estado enumerado. Não emitir path, UUID, token, hostname livre, título, infohash ou conteúdo de erro.
- Dado ausente, inválido, futuro ou atrasado não vira zero saudável; freshness e validade têm métricas próprias.
- Segredos ficam no `.env` privado já adotado; nenhuma credencial padrão habilita o painel e nenhum segredo é commitado.
- Dados do Prometheus/Grafana ficam em SSD; RAM/métrica/retenção entram no orçamento global. A solução inicial não coleta tudo nem resolve auditoria/controle de VMs.

## Foco da revisão

1. Snapshot válido fica velho: retirar valores operacionais e manter a indicação de fonte sem dados frescos.
2. Conteúdo malformado, NaN/Infinity, bool como número, tamanho excessivo, symlink ou arquivo não regular: recusar de forma sanitizada.
3. Pools duplicados ou fora da enumeração: não publicar capacidade ambígua nem labels controladas por input.
4. Commit/build não pode publicar senha, montar o `.env` inteiro no exporter ou expor porta de Prometheus/exporter em todas as interfaces.
5. Reiniciar apenas o painel não deve exigir iniciar serviços de mídia nem depender de um HD USB montado.

## Task 1 — Exporter de snapshots

**Arquivos:** criar `services/telemetry/src/homeserver_telemetry/metrics.py`, `tests/unit/test_prometheus_metrics.py`, `tests/contract/test_prometheus_exporter.py`.

**Interface:** `create_metrics_app(*, snapshot_dir: Path | None = None, clock: Callable[[], float] = time.time) -> FastAPI`; `app` de módulo para Uvicorn. Quando não injetado, usar `HOMESERVER_METRICS_SNAPSHOT_DIR`, default `/run/homeserver-metrics`. Endpoints somente `GET /metrics` e `GET /health/live`; sem operações de mutação.

**Fontes fixas:** `host.json` (`generated_at`, tolerância 90 s) e `capacity.json` (`measured_at`, tolerância 30 s). Clock injetável; mais de 5 s no futuro é inválido. Ler no máximo 1 MiB por arquivo regular, sem seguir symlink. Arquivo ausente/ilegível/malformado não quebra a exposição de saúde do exporter.

**Métricas de contrato:** `homeserver_snapshot_fresh{source="host|capacity"}`, `homeserver_snapshot_age_seconds{source="host|capacity"}` quando timestamp válido; `homeserver_host_cpu_percent`, `homeserver_host_ram_percent`, `homeserver_host_cpu_celsius`, `homeserver_host_uptime_seconds`; `homeserver_network_receive_bytes_per_second`, `homeserver_network_transmit_bytes_per_second`; `homeserver_pool_ready{pool="ssd|hdd|hdd2|hdd3"}`, `homeserver_pool_total_bytes`, `homeserver_pool_free_bytes`, `homeserver_pool_used_bytes` para pool fresco/ready e capacidade coerente. Não sintetizar pools não presentes nem somar capacidade física entre eles. Pools usam seu timestamp e validam estado, identidade interna não vazia, total/free/used inteiros e coerência; identidade não vira label.

- [x] Escrever testes RED de freshness, valores válidos/inválidos, corrupção, duplicação, labels e ausência de I/O de mídia.
- [x] Implementar o contrato mínimo e testar o texto exposto por FastAPI; preservar `homeserver_telemetry.app` existente sem novo endpoint público.
- [x] Executar suites do exporter e telemetry/status relacionadas em Linux; revisão de segurança e limites antes de commit.

Acrescentada `homeserver_snapshot_valid{source}` para distinguir envelope inválido
de fonte apenas atrasada. Ela não afirma que todos os sensores/pools são válidos.

## Task 2 — Stack e dashboards opcionais

**Arquivos:** criar `compose.observability.yaml`, `deploy/observability/prometheus.yml`, provisioning Grafana em `deploy/observability/grafana/`, dashboard JSON e `tests/contract/test_observability_compose.py`. Guia `docs/observability.md`.

**Consome:** métricas da Task 1 em `homeserver-metrics:8090/metrics` e imagens construídas pelo Dockerfile telemetry existente. O exporter usa `python -m uvicorn homeserver_telemetry.metrics:app --host 0.0.0.0 --port 8090` sem porta publicada no host.

**Produz:** stack independente com projeto próprio, rede interna, Prometheus sem bind de host, Grafana `127.0.0.1:3000:3000`. Exigir usuário/senha Grafana via variáveis obrigatórias, sem `admin/admin` ou habilitar acesso anônimo. Não montar socket Docker, `/`, `/sys` ou `.env` nos três containers. Snapshots em diretório sanitizado dedicado no SSD montado RO pelo exporter; definir no guia a necessidade de publicar/copy somente os dois snapshots de forma atômica, nunca montar todo o diretório de controle com bancos/tokens.

Retenção Prometheus inicial 7 dias/4 GiB; limites de memória somados de até 1 GiB para os três componentes como teto de ensaio, sem prometer consumo ou desempenho. Provisionar datasource e dashboard de host/rede/pools/freshness; valores ausentes não devem ser preenchidos artificialmente com zero. Volumes persistentes locais, reserva/monitoramento de SSD descritos. Sem Loki/Alloy/traces nesta etapa.

- [x] Criar testes RED para rede/binds, ausência de mounts sensíveis, autenticação obrigatória, limites, retenção, provisioning e métricas do dashboard.
- [x] Verificar versões mantidas em fontes oficiais e fixar tags explícitas ou digest; documentar consulta.
- [x] Implementar Compose/provisioning e validar parse/merge com env fictício; não interpolar `.env` real.
- [x] Documentar build/start manual e precondições. Não afirmar dashboard instalado nem canal de alertas configurado.

## Task 3 — Publicação sanitizada opt-in

**Arquivos:** `services/common/src/homeserver_common/metrics_snapshot.py`, integração em `scripts/host-metrics.py`, `compose.observability-source.yaml` e testes unitários/de contrato próprios.

O coletor confiável existente publica, somente quando recebe `--metrics-output-dir`, uma projeção por allowlist dos snapshots que já produz. Não adicionar scans, acesso a DB ou outro coletor com acesso ao diretório de controle. Preservar timestamps da medição original; nunca renovar freshness ao republicar. Campos exportados limitados aos consumidos pela Task 1; a identidade interna de filesystem é um digest estável, sem UUID bruto. Não copiar caminhos, nomes, razões de erro, títulos ou conteúdo livre.

O destino deve existir, ser diretório real e não ser symlink. Arquivos regulares são publicados atomicamente com modo `0640`. Falha de exportação gera somente diagnóstico fixo e não interrompe a coleta existente: a cópia anterior ficará atrasada e o exporter sinalizará isso. Sem flag, nenhum novo arquivo é criado.

O override acrescenta apenas o bind RW dedicado ao serviço `host-metrics`, grupo suplementar `65534` e flag de publicação. Source `${HOMESERVER_OBSERVABILITY_DIR:-/srv/appdata/observability}/snapshots`, com `create_host_path: false`; preparar diretório `1000:65534`, modo `2750`, para os arquivos herdarem o grupo que o exporter RO usa. Não ativar o override em produção nesta rodada.

- [x] Demonstrar RED/GREEN para allowlist, timestamps preservados, identidade opaca, publicação atômica, permissões, falha sem interromper coleta e opt-in desabilitado por padrão.
- [x] Validar merge do override com fixtures sem `.env` real e sem apagar mounts/argumentos preexistentes.
- [x] Testar o percurso produtor → arquivo → exporter → texto Prometheus, incluindo dados inválidos e antigos.

Integração adicional: `env.py` aceita apenas três variáveis Compose-only da stack,
sem registrá-las em Settings/catálogo ou repassar a senha aos serviços. O teste
`test_observability_environment.py` cobre dev/prod, todas as roles e recusa de nomes
desconhecidos. `.env.example` contém somente comentários opcionais.

## Integração e aceite

- [x] Conferir métricas consumidas pelo dashboard contra o exporter; não entregar painel com queries inexistentes.
- [x] Revisar a estratégia de publicação dos dois snapshots: tarefa separada antes do deploy, sem montar diretório inteiro de controle nem depender de leitura da mídia.
- [x] Rodar testes, lint/compilação e checks de Compose disponíveis; registrar ausência de runtime quando aplicável.
- [x] Commit/push na `refactor` com revisão de segredos e sem merge/deploy.

Preparação e alterações anteriores autorizadas publicadas em `e7aca0d` na branch
`refactor`. A publicação de código não iniciou a stack nem o corte do servidor.

Revisão independente corrigiu symlink ancestral, build da imagem do produtor e
precedência do nome do projeto. Fixture Linux com root confirmou produtor UID1000,
herança GID65534/setgid2750, arquivo0640 e leitor65534 sem permissão de escrita.
Isso não substitui ensaio dos binds reais no servidor. Tags, provisioning e limites
de memória ainda não foram exercitados em containers em execução.
