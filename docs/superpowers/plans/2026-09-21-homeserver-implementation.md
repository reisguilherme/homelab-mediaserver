# HomeServer — Plano técnico de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans para executar este plano tarefa por tarefa. As etapas usam checkboxes. Delegação somente quando autorizada; este documento não exige agentes paralelos.

**Goal:** implementar no desktop um projeto reproduzível de mídia, automação e monitoramento, implantado por SSH/Tailscale no Legion já preparado.

**Architecture:** Ubuntu e Docker existentes hospedam a suíte de mídia e serviços próprios em containers. Um controlador persistente reserva capacidade e autoriza downloads; uma camada de admissão entre Arr e qBittorrent impede que rotas automáticas ignorem essas regras. Um coletor alimenta a interface de operação, notificações e o painel CYD.

**Tech Stack:** desenvolvimento em WSL2/Ubuntu, Git, Docker Compose, Python 3.12, FastAPI, HTTPX, Pydantic, SQLite, pytest, Bash, ESPHome/LVGL, MQTT/Mosquitto, Restic e GitHub Actions. Dependências e imagens serão fixadas após a primeira validação de compatibilidade.

**Spec:** [Escopo v1.3](../specs/2026-09-21-homeserver-design.md). Este plano incorpora a base de servidor informada pelo usuário e a decisão de usar somente o SSD atual para mídia.

**Status:** plano para execução futura. Nenhum componente descrito aqui foi instalado ou desenvolvido nesta entrega. As verificações de servidor são tarefas futuras; não representam auditoria já realizada.

## 1. Restrições globais

- Uso pessoal, somente o usuário e seus dispositivos inicialmente.
- Até duas reproduções simultâneas.
- Downloads em 4K preferencialmente, com 1080p como resolução mínima.
- Filmes com até 50 GB por arquivo.
- Séries com até 5 GB por episódio e 100 GB por temporada.
- Não substituir automaticamente uma versão válida por outra de qualidade ou áudio melhores.
- Biblioteca rotativa com exclusão manual; sem apagar automaticamente conteúdos assistidos.
- Manter seeding até a exclusão manual, desde que não prejudique o upload da rede.
- Usar Tailscale para acesso remoto; sem VPN adicional para saída dos torrents.
- CYD somente para monitoramento; sem controles de serviços ou downloads na primeira versão.
- Usar somente o SSD já montado em `/srv/data` para mídia na versão 1.
- **Preservar:** OpenSSH, Tailscale SSH, Docker, montagem por UUID, serviço Lenovo, configurações de energia e isolamento da RTX relatados pelo usuário.
- Não formatar, repartir, reinstalar o Ubuntu, habilitar mergerfs ou reativar a RTX como parte deste plano.
- GB decimal; limites em bytes: filme `50000000000`, episódio `5000000000`, temporada `100000000000`.
- Código e configurações sem segredos no Git; dados de produção nunca entram no ambiente de desenvolvimento.
- Desenvolvimento no desktop; validação de UHD, SSD, energia, Wi-Fi/CYD e reprodução nos equipamentos reais.

## 2. Estado inicial e lacunas objetivas

| Informado como concluído pelo usuário | Como tratar no plano |
|---|---|
| OpenSSH ativo na LAN | Verificar; preservar a rota administrativa existente |
| Tailscale no host, autenticado, com SSH ativado | Verificar permissões de rede e regras SSH da tailnet |
| logind e targets de suspensão configurados | Auditar no modo de adoção; não substituir arquivos cegamente |
| `lenovo-conservation.service` ativo | Registrar conteúdo/status e leitura de conservação, sem reescrever |
| `nouveau` bloqueado e RTX desativada | Verificar GPU render disponível e módulos carregados |
| Docker/Compose instalados oficialmente | Inventariar versões e aproveitar a instalação |
| Usuário no grupo docker | Registrar UID/GID; reconhecer o privilégio administrativo existente |
| SSD em `/srv/data` via UUID/noatime | Capturar UUID/tipo/capacidade; guardas exigem a montagem real |
| Diretórios e hardlinks testados no host | Repetir teste dentro dos containers com as identidades reais |

Faltam valores concretos de hostname, IPs, usuário Linux, UUID, versão do Ubuntu, filesystem e grupos da GPU. A tarefa I01 gera um inventário local restrito com esses valores; o plano não os inventa. O nome `nvme0n1` não será gravado como identidade permanente.

**Correções de interpretação:** targets mascarados são uma configuração do systemd. Hardlinks e renomeações atômicas são propriedades diferentes e recebem testes próprios. Blacklist de `nouveau` não comprova que todos os drivers NVIDIA estão ausentes.

## 3. Ordem e documentos de execução

| Ordem | Documento | Tarefas | Entrega verificável |
|---|---|---|---|
| 1 | [Infraestrutura e ambiente](2026-09-21-homeserver-01-infra.md) | I01–I05 | Auditoria, bootstrap, Compose isolado, primeiro deploy e teste UHD |
| 2 | [Automação de mídia](2026-09-21-homeserver-02-automation.md) | C01–C06 | Reservas, admissão, importação validada, fila e exclusão |
| 3 | [Monitoramento e CYD](2026-09-21-homeserver-03-monitoring.md) | M01–M03 | Métricas, alertas e painel físico |
| 4 | [Operação, backup e CI/CD](2026-09-21-homeserver-04-operations.md) | O01–O04 | Recuperação, pipeline e aceite final |

I01 → I02 → I03 → I04 → I05 → C01 → C02 → C03 → C04 → C05 → C06 → M01 → M02 → M03 → O01 → O02 → O03 → O04 é a ordem recomendada. Criar CI de testes em I02; O03 acrescenta implantação. O01 deve estar concluída antes de receber a biblioteca real ou executar migrações persistentes em produção.

Antes de liberar downloads reais, concluir a prova de integração C01 e o bloqueio C04. A simples instalação dos containers não atende às regras de reserva por temporada.

## 4. Ambientes e estrutura do repositório

Manter o repositório de desenvolvimento em filesystem Linux do WSL2, com uma cópia Git canônica para cada ambiente. Primeiro registrar e transferir os documentos existentes; não perder as alterações locais feitas no Windows. Evitar alternar ferramentas Windows/WSL escrevendo simultaneamente no mesmo checkout.

Estrutura alvo, a ser criada durante a execução:

```text
HomeServer/
  README.md
  AGENTS.md
  .gitignore
  .gitattributes
  pyproject.toml
  uv.lock
  Makefile
  config/
    policy.yaml
    versions.env
    server.example.yaml
    dev.env.example
  deploy/
    compose.yaml
    compose.dev.yaml
    compose.prod.yaml
    Dockerfile.control
    Dockerfile.telemetry
    systemd/
    mosquitto/
  scripts/
    audit-server.sh
    bootstrap-server.sh
    check-mount.sh
    verify-layout.sh
    deploy.sh
    rollback.sh
    backup.sh
    restore.sh
    smoke.sh
  services/control/src/homeserver_control/
    api/
    domain/
    adapters/
    persistence/
    worker/
    gateway/
  services/telemetry/src/homeserver_telemetry/
  firmware/cyd/
  tests/
    unit/
    contract/
    integration/
    system/
    fixtures/
  docs/
    contracts/
    runbooks/
    evidence/
    superpowers/
  .github/workflows/
    ci.yml
    deploy.yml
```

`AGENTS.md` documentará comandos, separação entre dev/prod e as operações proibidas nesta versão. `docs/evidence/` só recebe relatórios sanitizados; inventário bruto, configurações locais e segredos ficam ignorados pelo Git. Os caminhos de código nos subplanos são relativos a essa raiz.

### 4.1 Stack própria e persistência

- Python 3.12 em imagens Linux, com `uv.lock` versionado e `uv sync --frozen` no CI.
- FastAPI/Pydantic para API e contratos; HTTPX para integrações com timeout; SQLite com migrações SQL versionadas para estado do controlador.
- Um processo worker é responsável pelas transições de estado; a API grava intenções e o gateway valida autorizações no banco. Transações curtas, `busy_timeout` e WAL em volume local.
- Sem Redis/Celery/PostgreSQL na versão 1. SQLite só é compartilhado por processos no mesmo host e filesystem local, com o comportamento de lock testado.
- Telas administrativas simples renderizadas no servidor; não criar uma SPA ou aplicativo móvel próprio.
- Mosquitto para telemetria compacta à CYD; Home Assistant não é dependência.

### 4.2 Containers e isolamento

| Serviço | Persistência | Permissões/dados |
|---|---|---|
| jellyfin | `/srv/appdata/jellyfin` | `/data/media` somente leitura; `/transcode` próprio; render node Intel somente em produção |
| seerr | `/srv/appdata/seerr` | Sem acesso ao filesystem da biblioteca |
| sonarr / radarr | Diretório próprio em `/srv/appdata` | Mesmo mount `/srv/data:/data`, leitura/escrita compatível com grupo de mídia |
| prowlarr | `/srv/appdata/prowlarr` | Sem mount de mídia |
| qbittorrent | `/srv/appdata/qbittorrent` | Mesmo `/data`; API disponível apenas na rede de transferência |
| bazarr | `/srv/appdata/bazarr` | Biblioteca organizada para legendas; sem necessidade de acesso administrativo ao host |
| control-api / control-worker / download-gateway | `/srv/appdata/control` | Mesmo banco; atribuições separadas e segredos limitados por processo |
| telemetry | `/srv/appdata/telemetry` | APIs de leitura e snapshot de métricas; sem socket Docker |
| mosquitto | `/srv/appdata/mosquitto` | Contas e ACL de tópicos |

Redes Compose: `apps`, `transfer` e `telemetry`. Arr não participa de `transfer` e não recebe credencial do qBittorrent real. O `download-gateway` conecta `apps` a `transfer`. Acesso administrativo excepcional ao qBittorrent ocorre por túnel controlado e deve pausar novas admissões antes de qualquer alteração manual.

Os mapeamentos de usuário variam por imagem: não aplicar `PUID/PGID` a uma imagem que não os suporta. Em I03, fixar por serviço imagem, versão/digest, usuário efetivo e permissões.

## 5. Valores iniciais de política

Arquivo alvo `config/policy.yaml`; parâmetros técnicos iniciais, sem credenciais:

```yaml
schema_version: 1
limits:
  movie_bytes: 50000000000
  episode_bytes: 5000000000
  season_bytes: 100000000000
  resolutions: [2160, 1080]
  automatic_upgrades: false
capacity:
  media_mount: /data
  floor_bytes: 20000000000
  floor_fraction: 0.05
  operation_margin_bytes: 1000000000
  ongoing_season_reservation_bytes: 100000000000
downloads:
  max_active: 2
  reconcile_seconds: 60
  search_retry_minutes: 60
  seed_upload_bps_idle: 2500000
  seed_upload_bps_remote_playback: 625000
telemetry:
  transfer_seconds: 5
  host_seconds: 15
  stale_seconds: 30
backup:
  external_stale_hours: 72
  retry_minutes: 60
  staging_max_bytes: 20000000000
  staging_generations: 3
```

Campos `bps` acima significam **bytes por segundo**, seguindo a API de transferência; a UI exibirá unidade explícita. Não confundir 2.500.000 B/s com 2,5 Mbps.

Idioma: original + português é confirmado; a precedência entre dual 1080p e original 4K e a variante pt-BR ainda são propostas. C02 deve expor ambas as escolhas em configuração e exigir uma política explicitamente registrada antes de liberar downloads reais. O desenvolvimento usa fixtures determinísticas, sem presumir uma nova aprovação do usuário.

## 6. Contratos transversais

### 6.1 Identidades e estados

- `request_id`: ID próprio UUID; Seerr identificado por `source_id` separado.
- `media_key`: identificador estável de domínio como `movie:tmdb:123` ou `season:tvdb:456:2`, usado em reservas e exclusões.
- `download_id`: ID do trabalho; `infohash` obtido de metadados verificados, não extraído de título.
- `filesystem_id`: UUID registrado para a montagem de mídia; caminhos não comprovam identidade física.
- `operation_id`: chave idempotente para efeitos externos, inclusive download e exclusão.
- Horários UTC ISO 8601 e tamanhos inteiros em bytes; nunca usar ponto flutuante para limites.

Estados públicos: `requested`, `searching`, `waiting_source`, `waiting_space`, `reserved`, `downloading`, `waiting_episodes`, `validating`, `waiting_subtitles`, `available`, `error`, `deleted`.

### 6.2 API própria

| Método e rota | Contrato |
|---|---|
| `GET /health/live` | Processo vivo; não depende de todos os serviços externos |
| `GET /health/ready` | Banco/migrações/montagem e invariantes suficientes para operar; 503 caso contrário |
| `GET /api/v1/queue` | Lista paginada com pedido, estado, motivo, reserva e última atualização |
| `GET /api/v1/capacity` | UUID, total, livre, margem, comprometido, admissível e horário da medição |
| `POST /api/v1/deletions/preview` | `media_key` → plano de efeitos e token assinado/expirável |
| `POST /api/v1/deletions` | token do preview + chave idempotente → operação 202; 409 se o plano mudou |
| `GET /api/v1/operations/{operation_id}` | Estado e efeitos confirmados de exclusão/retomada |
| `GET /api/v1/telemetry` | Snapshot versionado conforme plano 03 |
| `GET /internal/v1/events?after_id=N&limit=M` | Eventos persistentes ordenados por sequência; credencial específica do coletor |
| `POST /internal/v1/events/ack` | Confirmar sequência importada pelo consumidor, sem apagar eventos ainda necessários |
| `POST /internal/v1/seed-limit` | `bytes_per_second` e `reason`, com teto validado; credencial exclusiva do coletor |

API administrativa exige sessão/autenticação, autorização e proteção CSRF em operações via navegador. CYD nunca recebe essa sessão; acessa somente tópicos MQTT autorizados. Chaves de integração só trafegam entre serviços e não são devolvidas pela API/UI.

Rotas de fila/operações pertencem ao `control-api`; telemetria pertence ao serviço `telemetry` e a UI a consulta pelo backend, sem expor credenciais ao navegador. Eventos têm sequência monotônica, ID estável, tipo, objeto, geração, horário e payload sem segredos. O coletor salva eventos e cursor na própria transação antes de confirmar recebimento; depois entrega notificações por sua outbox. Manter eventos não confirmados até limite configurado, alertar backlog e nunca descartar silenciosamente ao atingir o limite. Rotas internas não são publicadas para LAN/CYD.

### 6.3 Reservas

`available = max(0, free - safety_margin - future_unallocated_commitments)`

`safety_margin = max(20 GB, ceil(total × 0.05))`.

Uma reserva representa o orçamento final do trabalho mais margem, descontando somente blocos já alocados fisicamente **para esse mesmo trabalho**. Não subtrair duas vezes bytes já presentes no valor livre do filesystem. Deduplicar por `(st_dev, st_ino)` ao contar hardlinks. Leitura de `statvfs` e `stat` ocorre no filesystem de dados real; testes no Windows não substituem esse comportamento.

Não pré-alocar o arquivo inteiro no cliente como mecanismo de reserva. Manter reserva lógica persistente e reconciliar arquivos esparsos, blocos ocupados e alterações externas; a concorrência será controlada por transação e lock de admissão. Novos downloads ficam bloqueados se o UUID ou a idade das métricas não puderem ser confirmados.

## 7. Critérios de avanço

| Marco | Condição objetiva |
|---|---|
| G0 — Base adotada | Auditoria registra fatos e diferenças; bootstrap não modifica itens já corretos |
| G1 — Deploy básico | Stack de teste funciona; mount guard e hardlinks dentro de containers passam |
| G2 — Integração viável | Pedido sem busca automática, inspeção de torrent e gateway demonstrados com versões fixadas |
| G3 — Automação segura | Reservas concorrentes, limites, interrupções e exclusões passam em testes de integração |
| G4 — Experiência disponível | CYD, notificações e clientes reais com dados e reprodução validados |
| G5 — Recuperação | Backup e restauração demonstrados; retorno de versão documentado |
| G6 — Projeto concluído | Todos os critérios A01–A34 aplicáveis registrados, incluindo teste 48–72h |

Uma falha em G2 bloqueia a liberação dos downloads automáticos, mas não impede desenvolver UI, testes, telemetria e backup. Não substituir silenciosamente as regras por um Compose padrão.

## 8. Cobertura dos critérios do escopo

| Critérios | Tarefas responsáveis |
|---|---|
| A01, A17, A18 | I01, I03, I04 |
| A02, A04, A05, A06 | I05, O04 |
| A03 | I05 |
| A07, A08, A09 | C01, C02, C05 |
| A10, A11 | C02, C04 |
| A12, A13, A14, A15 | C03, C04 |
| A16, A26 | C01, C04 |
| A19 | C06 |
| A20 | M01, O04 |
| A21, A22 | M01, M02, M03 |
| A23, A24 | O01 |
| A25, A30 | I04, O03 |
| A27 | I04, O02 |
| A28, A29 | O02, O03 |
| A31, A32, A33, A34 | I01, I02 |

Requisitos de fontes/idioma e prioridade D04–D12: C01/C02/C05. Uso de uma conta D01: I03. Política de disco D18: I01/I03. Limites de exposição D17: I04/O03. Notificações D20: M02. Ambiente desktop e bootstrap D23–D26: I01/I02/O02/O03.

## 9. Rotina de execução

Cada tarefa deve começar pelos arquivos e contratos indicados no subplano, produzir sua evidência, executar os testes relevantes e terminar em um commit pequeno. Usar branch `codex/implementacao-homeserver` quando começar a codificação. Não commitar credenciais, dados de teste privados ou relatórios brutos de rede.

Os exemplos de código são contratos e casos de teste essenciais. Implementar suas funções nas tarefas indicadas, sem tratar snippets como aplicações prontas. Todos os comandos deste plano são instruções para a execução futura; comandos destrutivos de preparação de disco não fazem parte dele.

Começar por **I01**, no desktop, produzindo o auditor de leitura e o modelo de inventário. Acesso ao servidor só será necessário para preencher o inventário e testar o que depende do hardware.
