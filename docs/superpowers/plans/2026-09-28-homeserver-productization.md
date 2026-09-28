# HomeServer Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Plano autorizado para implementação. As checklists abaixo preservam o plano original; o [registro de implementação e aceite](../../evidence/productization-acceptance.md) informa a execução e evidência efetiva de cada ponto.

**Goal:** Tornar o HomeServer reproduzível em outro servidor, configurável por `.env`, sem CYD, com documentação de instalação/operação e publicação GitHub verificadas.

**Architecture:** Preservar Compose, SQLite e os serviços existentes. Introduzir biblioteca compartilhada de configuração tipada e reconciliadores idempotentes para preferências nativas; gerar a infraestrutura a partir do `.env`. Completar instalação, supervisão, release e recuperação, preservando estado e mídia.

**Tech Stack:** Ubuntu Server 24.04 LTS amd64, Python 3.12, uv com versão fixa, Pydantic, FastAPI, SQLite, Bash, Docker Compose v2, systemd, Restic, pytest e GitHub Actions.

**Spec:** [Design de produtização e catálogo do `.env`](../specs/2026-09-28-homeserver-productization-design.md). Ler também a [auditoria e evidências](../../audits/2026-09-28-homeserver-productization-audit.md).

## Global Constraints

- Escopo autorizado: implementação do plano. O registro de aceite distingue código entregue, ensaios executados e validações físicas ainda desconhecidas.
- Baseline proposto: Ubuntu Server 24.04 LTS amd64, systemd e Docker Engine/Compose v2.
- `.env` é a única interface de configuração gerenciada pelo projeto; arquivos derivados não são editados manualmente.
- Default de nova instalação: 4 downloads, 8 seedings, 12 torrents ativos e upload de 20 Mbit/s; adopt preserva os valores efetivos existentes.
- Séries sempre mantêm a sequência. Não reintroduzir teto de filme/episódio nem reserva fixa de 80 GB.
- Após cinco minutos abaixo do limiar configurado, buscar alternativa sem interromper o download atual; preservar qualidade e trocar somente por candidata com vantagem de desempenho demonstrada.
- Não aplicar essa mudança ao The Rookie S02E04 que estava baixando quando ela foi solicitada. Persistir a exceção da aquisição antes da ativação; próximos episódios seguem a regra nova.
- pt-BR mesma release → pt-BR edição compatível → inglês mesma release → inglês edição compatível; áudio original pt-BR pode dispensar legenda.
- CYD será retirada. Preservar painel HTTP, métricas e integrações necessárias.
- Não formatar/particionar, ativar mergerfs/RTX, substituir `fstab`, reescrever serviço Lenovo, abrir portas no roteador, expor API qBit na LAN, contornar gateway ou apagar mídia automaticamente.
- Produção só escreve no disco de mídia após verificação de UUID; recovery bloqueia admissão.
- Nenhum banco, token, mídia ou inventário bruto de produção no Git. Testes usam fixtures sanitizadas e mídia sintética/autorizada.
- Um commit por entrega revisável após seus testes; não incluir arquivos locais não relacionados. Sem force-push ou descarte de branches/worktrees.

## Review Focus

1. `.env` com senha contendo `$`, `#`, aspas, espaços e caminhos com espaços deve sobreviver ao loader/render sem execução de shell ou vazamento; testes P02/P07.
2. `apply` interrompido após alterar somente um aplicativo deve retomar sem duplicar bibliotecas, clientes, perfis ou indexadores; testes P03/P06.
3. Docker reiniciando antes do disco de mídia deve manter escritas bloqueadas; um container encerrando sozinho deve ser recuperado sem corrida com backup; testes P09/P10.
4. `adopt` com mudanças manuais legítimas, portas diferentes e IDs de mídia em uso deve preservá-las até um diff explícito; testes P01/P06/P07.
5. Restore de banco antigo não deve reativar downloads excluídos nem reviver tombstones; release com schema incompatível deve impedir rollback superficial; testes P10/P11.

## Sequência e entregas

| Etapa | Dependências | Entrega verificável |
|---|---|---|
| P01 | — | Baseline e branch canônica coerentes |
| P02 | P01 | Loader/schema/CLI de configuração e `.env.example` |
| P03 | P02 | Downloads e seeding controlados por env |
| P04 | P02 | Qualidade, resolução e failover parametrizados |
| P05 | P02 | Política de idiomas/legendas parametrizada |
| P06 | P03–P05 | Integrações nativas aplicadas sem divergência |
| P07 | P02, P06 | Compose portátil e instalação fresh/adopt |
| P08 | P07 | Runtime/documentação sem CYD |
| P09 | P07–P08 | Supervisão, métricas, diagnóstico e logs |
| P10 | P07, P09 | Backup/restore Restic real e transporte consistente |
| P11 | P09–P10 | Build, deploy e rollback operacionais |
| P12 | P01–P11 | GitHub e CI validando a distribuição |
| P13 | Todas | README e ensaio completo em outra máquina |

Documentação acompanha cada etapa. P13 consolida e testa os guias, não inicia sua escrita. Não há estimativa de prazo antes de executar a migração de configuração e medir o ensaio de instalação.

## Mapa de arquivos e interfaces

Biblioteca nova `services/common/src/homeserver_common/` é código compartilhado, **não um serviço adicional**:

- `settings.py`: modelo `Settings`, defaults, tipos, unidades, regras cruzadas e metadados de segredo/aplicação.
- `env.py`: `load_settings(path: Path, *, mode: Literal['dev', 'prod']) -> Settings`; parser de dados sem executar shell.
- `catalog.py`: catálogo e exemplo gerados dos campos de `Settings`.
- `render.py`: `render_stack(settings: Settings) -> dict[str, object]` e renderização de templates systemd.
- `native_config.py`: `SettingChange(service, key, before, after, restart_required, secret)` e comparação restrita a chaves gerenciadas.
- `cli.py`: entrypoint de `scripts/homeserver`; códigos de saída 0=sucesso, 2=configuração inválida, 3=dependência indisponível, 4=aplicação parcial.

Campos Python usam snake_case sem prefixo; variáveis usam `HOMESERVER_`. Os consumidores recebem `Settings` ou objetos de política derivados, em vez de ler `os.environ` por conta própria. `Settings` exporta `redacted_dict()` e o catálogo de ownership por consumidor.

Configuração nativa fica em `services/control/src/homeserver_control/configuration/`, com módulos por aplicativo. Reutilizar adapters HTTP existentes para transporte e autenticação; não aumentar `gateway/app.py` com uma segunda lógica de provisionamento. O configurador administrativo alcança a API nativa do qBit somente pela rede interna, sem publicar endpoint que contorne o gateway de downloads.

Os snippets de testes abaixo definem contratos a implementar. Os comandos/arquivos novos indicados como criar não existem na base auditada. Cada tarefa inclui ciclo de teste falhando, implementação, teste passando e revisão; não executar exemplos de migração diretamente na produção.

## P01 — Consolidar a base e mapear a migração

**Arquivos:** atualizar `docs/architecture.md`, `docs/runbooks/server-baseline.md`, `docs/runbooks/github-actions.md`; criar `docs/migrations/2026-09-productization.md`.

- [ ] Registrar refs remotas e `git diff --stat main...codex/download-gateway`; verificar diferenças e eventual trabalho exclusivo da branch antiga antes de integrar.
- [ ] Registrar baseline sanitizado: release, tipos de mounts, nomes de serviços, chaves de configuração, preferências gerenciadas e localização dos backups. Valores privados permanecem localmente em área ignorada.
- [ ] Mapear cada campo do override/systemd vivo para um campo do schema ou template versionado; produzir tabela `origem → destino → consumidor → verificação`.
- [ ] Integrar a branch atual por PR/revisão para `main`, executar CI, depois alinhar branch padrão e checkout de trabalho. Não simular integração trocando somente o nome da branch.
- [ ] Registrar configuração inicial importável e procedimento de retorno. Guardar backup das configurações antes da futura migração.

**Aceite:** clone temporário recebe a versão consolidada; CI dessa mesma SHA está disponível para o deploy. Não iniciar a migração operacional antes de P07 e dos testes pertinentes.

```bash
git ls-remote --symref origin HEAD refs/heads/main
uv sync --frozen
uv run make lint test-unit test-contract test-integration compose-check smoke
```

**Commit sugerido:** `docs: define canonical baseline and configuration migration`.

## P02 — Criar schema, loader e CLI do `.env`

**Criar:** biblioteca comum acima; `scripts/homeserver`; `.env.example`; `docs/configuration.md`; `tests/unit/test_settings.py`; `tests/contract/test_configuration_cli.py`.

**Modificar:** `pyproject.toml`, `uv.lock`, `Makefile`, `deploy/Dockerfile.control`, `deploy/Dockerfile.telemetry`. Empacotar/incluir a biblioteca nos dois containers e nos testes; verificar import a partir da imagem, não apenas via PYTHONPATH do pytest.

**Interface:** `load_settings` e `Settings` definidos no mapa; `config validate/show/plan/verify`; renderização e apply serão conectados nas etapas seguintes.

- [ ] Escrever testes para parsing, tipos, listas, booleanos, unidade de bandwidth, segredo oculto, chave desconhecida, campos obrigatórios de produção, env externo conflitante e `_FILE` conflitante com valor direto.

```python
def test_invalid_download_limit_names_the_key(tmp_path):
    import pytest
    from homeserver_common.env import load_settings

    path = tmp_path / '.env'
    path.write_text('HOMESERVER_DOWNLOAD_MAX_ACTIVE=zero\n')
    with pytest.raises(ValueError, match='HOMESERVER_DOWNLOAD_MAX_ACTIVE'):
        load_settings(path, mode='dev')
```

- [ ] Executar `uv run pytest tests/unit/test_settings.py tests/contract/test_configuration_cli.py -q`; confirmar falha por ausência do contrato.
- [ ] Implementar loader único, validações cruzadas e geração de `.env.example`/catálogo; entradas de produção sem credenciais requeridas falham antes de subir serviços. Não serializar segredos em JSON de diagnóstico.
- [ ] Acrescentar comandos `env init`, `config validate` e `show --redacted`; gerar segredos quando necessário, escrita atômica e arquivo restrito; nunca sobrescrever um `.env` existente por default.
- [ ] Gerar tabela de consumidor e aplicação por variável; para cada chave, teste de consumo ou exclusão explícita como estado interno. Testar senha literal com `$HOME`, `#`, aspas e espaços sem interpretá-la.
- [ ] Rodar os testes acima, lint e smoke de import nas imagens. Documentar a gramática aceita e comandos no README.

**Aceite:** não há variável anunciada sem schema, tipo/default/unidade e consumidor. `.env` inválido não produz deploy parcial. **Commit:** `feat: add typed environment configuration`.

## P03 — Aplicar limites de downloads e seeding

**Criar:** `configuration/qbittorrent.py`; `tests/unit/test_native_config.py`; `tests/contract/test_qbit_configuration.py`.

**Modificar:** adapter qBit, `worker/__main__.py`, `docs/configuration.md`; todos sob os diretórios existentes de control.

**Interface:** `qbit_preferences(settings: Settings) -> dict[str, object]`; `plan_settings(service: str, current: dict, desired: dict) -> list[SettingChange]` em `native_config.py`; `apply_qbit_settings(settings: Settings, client: httpx.AsyncClient) -> list[SettingChange]` aplica apenas diffs e faz read-back.

```python
def test_twenty_mbit_is_bytes_per_second(tmp_path):
    from homeserver_common.env import load_settings
    from homeserver_control.configuration.qbittorrent import qbit_preferences

    path = tmp_path / '.env'
    path.write_text('HOMESERVER_UPLOAD_LIMIT_MBIT=20\n')
    assert qbit_preferences(load_settings(path, mode='dev'))['up_limit'] == 2_500_000
```

- [ ] Criar testes de diff vazio, conversão, interação de limites, limites desabilitados, autenticação inválida, timeout e read-back diferente do pedido.
- [ ] Rodar testes para confirmar falha; implementar leitura/planejamento/aplicação com allowlist de preferências. Não oferecer alterações arbitrárias via gateway público.
- [ ] Conectar `config plan/apply/verify`; registrar somente campos com segredos ocultos. Importar valor atual em adopt para evitar converter silenciosamente 2.499.584 em 2.500.000 bytes/s.
- [ ] Testar falha após resposta ambígua: nova execução relê o servidor e aplica só o que falta. Alterar total/concorrência não retoma torrent bloqueado por capacidade/recuperação.
- [ ] Executar `uv run pytest tests/unit/test_native_config.py tests/contract/test_qbit_configuration.py tests/contract/test_qb_gateway.py -q`.

**Aceite:** editar `.env`, aplicar e ler API nativa comprova os valores; segundo apply não muda nada. **Commit:** `feat: reconcile qbit queue and seeding settings`.

## P04 — Parametrizar aquisição, qualidade e troca de fontes

**Modificar:** `worker/release_quality.py`, `acquisition.py`, `series_acquisition.py`, `source_health.py`, `source_reconciliation.py`, `movie_priority.py`, `__main__.py`, `gateway/permits.py`, `gateway/app.py`; testes existentes correspondentes.

**Criar:** `worker/replacement_evaluation.py`, `persistence/replacement_evaluations.py`, migração SQL com próximo número disponível e `tests/integration/test_replacement_evaluation.py`. Persistir avaliação, fonte principal, candidata, observações e exceção da aquisição; não usar apenas estado em memória.

**Interface:** `release_rank(release: dict[str, object], policy: Settings) -> tuple | None`; `SourceHealthStore` recebe `stall_seconds`, `slow_window_seconds`, `minimum_bytes_per_second` explicitamente; defaults vêm do schema. `SeriesAcquirer` mantém ordem por temporada/episódio; `MoviePrioritizer` só ordena elegíveis.

Em `replacement_evaluation.py`, definir `should_promote(*, current_remaining_bytes: int, current_rate_bps: float, candidate_remaining_bytes: int, candidate_rate_bps: float, minimum_gain_percent: float) -> bool`. Entradas inválidas/rates sem amostra suficiente bloqueiam promoção; velocidade maior por si só não basta se o arquivo maior atrasar a conclusão. Esta função é usada somente após qualidade, gateway e capacidade terem sido validados.

```python
def test_env_can_allow_720_webdl(tmp_path):
    from homeserver_common.env import load_settings
    from homeserver_control.worker.release_quality import release_rank

    path = tmp_path / '.env'
    path.write_text('HOMESERVER_MEDIA_RESOLUTIONS=720\nHOMESERVER_MEDIA_SOURCES=webdl\n')
    release = {'quality': {'quality': {'resolution': 720, 'source': 'webdl',
                                      'modifier': 'none'}}, 'seeders': 30, 'size': 1000}
    assert release_rank(release, load_settings(path, mode='dev')) is not None
```

- [ ] Acrescentar testes de resolução diferente do default, ordem de fontes, DV/Atmos preferenciais, prioridade de seeds e episódio anterior sem fonte.
- [ ] Testar limites temporais exatos com relógio controlado; pausado por capacidade/operador e torrent completo não causam failover.
- [ ] Acrescentar casos aos 299 e 300 segundos: média abaixo de 1 MiB/s habilita busca aos 300, mas não emite `stop` para a fonte atual. Buscar a cada 300 segundos, com backoff e sem buscas concorrentes duplicadas para a mesma aquisição.
- [ ] Acrescentar testes de busca vazia, erro de provedor, candidata de qualidade inferior e espaço insuficiente; todos preservam o download lento atual. Validar resolução/fonte iguais ou superiores, episódio/edição corretos e política de legenda.
- [ ] Implementar avaliação temporária autorizada pelo gateway: uma candidata por aquisição, somente para o mesmo episódio, com checagem dos bytes pendentes de ambas as fontes. Medir velocidade durante 60 segundos, sem tomar seeds anunciados como prova de desempenho. Criar estados persistidos de avaliação e promoção, com timeout e recuperação após reinício.
- [ ] Testar que a fonte atual permanece principal enquanto candidata baixa/é medida; promover apenas com velocidade sustentada maior e previsão de conclusão pelo menos 20% melhor. Parar a antiga somente após confirmar a nova. Se a atual recuperar velocidade, terminar, for cancelada ou perder a montagem válida durante o teste, reavaliar antes de qualquer efeito externo.
- [ ] Testar reinício entre cada etapa, falha de promoção e candidata que perde peers; nenhuma falha pode abandonar a fonte que ainda baixa ou retomar item cancelado. Registrar bytes de tentativas no cálculo de capacidade; não apagar mídia automaticamente.
- [ ] Antes da ativação em produção, persistir a exceção da aquisição de The Rookie S02E04 indicada pelo usuário. Testar que ela não é parada, reiniciada, substituída nem usada em avaliação, inclusive após restart do worker. Se já estiver concluída, não a reabrir. Validar que S02E05 não herda a exceção.
- [ ] Rodar testes falhando; injetar políticas nas fábricas e remover números operacionais duplicados. Preservar persistência das observações e histórico de fontes rejeitadas.
- [ ] Testar que mudança de política vale para seleção futura e não cancela arquivos em andamento; verificar capacidade real, sem reserva fixa, em seleção concorrente.
- [ ] Executar `uv run pytest tests/unit/test_release_quality.py tests/unit/test_source_health.py tests/unit/test_movie_priority.py tests/integration/test_movie_acquisition.py tests/integration/test_series_acquisition.py -q`.
- [ ] Executar `uv run pytest tests/integration/test_replacement_evaluation.py tests/integration/test_gateway_reservation.py tests/contract/test_qb_gateway.py -q`; validar que a autorização de avaliação não permite bypass para downloads arbitrários.

```python
def test_larger_candidate_must_finish_sooner():
    from homeserver_control.worker.replacement_evaluation import should_promote

    assert not should_promote(
        current_remaining_bytes=1_200_000_000, current_rate_bps=400_000,
        candidate_remaining_bytes=3_100_000_000, candidate_rate_bps=1_000_000,
        minimum_gain_percent=20,
    )
    assert should_promote(
        current_remaining_bytes=1_200_000_000, current_rate_bps=400_000,
        candidate_remaining_bytes=3_100_000_000, candidate_rate_bps=2_000_000,
        minimum_gain_percent=20,
    )
```

**Aceite:** nenhuma edição Python para resolução, fontes ou timeout de failover; busca começa após cinco minutos, mantém a fonte atual até comprovar substituta vantajosa e respeita a exceção do episódio já em andamento. Episódio/temporada mantém sequência, e maior velocidade não autoriza reduzir qualidade. **Commits sugeridos:** `feat: configure acquisition and source policies through env` e `feat: evaluate faster sources without interrupting active downloads`, cada um após seus testes.

## P05 — Parametrizar legendas e áudio com semântica única

**Criar:** `worker/subtitle_policy.py`; `tests/unit/test_subtitle_policy.py`.

**Modificar:** `worker/subdl.py`, `subtitle_language.py`, `finalization.py`, `series_finalization.py`, `persistence/subtitle_artifacts.py` e testes existentes; `Settings` e catálogo.

**Interface:** `subtitle_attempts(languages: tuple[str, ...], modes: tuple[str, ...]) -> tuple[tuple[str, str], ...]`; registry de idiomas com tag canônica, aliases por provedor e sufixo de arquivo; filme e episódio usam a mesma política.

```python
def test_language_precedes_release_matching():
    from homeserver_control.worker.subtitle_policy import subtitle_attempts

    assert subtitle_attempts(('pt-BR', 'en-US'), ('release', 'compatible_edition')) == (
        ('pt-BR', 'release'), ('pt-BR', 'compatible_edition'),
        ('en-US', 'release'), ('en-US', 'compatible_edition'),
    )
```

- [ ] Criar fixtures de mesma release, créditos longos, extended/director's cut, pt-PT, inglês genérico e filme brasileiro com/sem evidência de áudio original.
- [ ] Rodar teste falhando; implementar política compartilhada, margem configurável e migração dos códigos persistidos `BR_PT`/`EN`, sem perder legendas existentes.
- [ ] Testar idioma adicional suportado pelo provedor e erro para idioma sem mapping. Permitir alterar ordem/desabilitar fallback sem mudar código. Não aceitar pt-PT como pt-BR.
- [ ] Conectar providers configurados de verdade. Se Bazarr não fornecer determinada busca de fallback, relatar capability e usar adapter adequado; não anunciar integração inexistente.
- [ ] Executar `uv run pytest tests/unit/test_subtitle_policy.py tests/unit/test_subdl.py tests/unit/test_subtitle_language.py tests/integration/test_subtitle_artifacts.py tests/integration/test_movie_finalization.py tests/integration/test_series_finalization.py -q`.

**Aceite:** schema, worker, Bazarr e nomes vistos pelo Jellyfin concordam; duração não é apresentada como garantia de sincronismo. **Commit:** `feat: centralize subtitle and audio policy`.

## P06 — Reconciliar Arr, indexadores, bibliotecas e contas

**Criar:** `configuration/arr.py`, `configuration/prowlarr.py`, `configuration/bazarr.py`, `configuration/media_servers.py`, `configuration/reconcile.py`; `tests/contract/test_native_configuration.py`.

**Modificar:** `docs/runbooks/service-setup.md`, `services/common/src/homeserver_common/cli.py`; adapters existentes quando faltar uma operação nativa necessária.

**Interface:** `async plan_native_settings(settings: Settings) -> list[SettingChange]` e `async apply_native_settings(settings: Settings) -> list[SettingChange]`; cada recurso gerenciado usa chave estável/ID adotado. Resultado parcial produz exit 4 e relatório por serviço, sem segredo.

- [ ] Escrever testes HTTP com estado nativo simulado: instalação vazia, recursos equivalentes já existentes, recurso manual não gerenciado, erro 401, serviço indisponível e retomada após criação bem-sucedida com resposta perdida.
- [ ] Implementar perfis de qualidade/idioma e clientes Arr apontando ao gateway; autenticação, categorias, root folders e auto-upgrades coerentes com o seletor.
- [ ] Implementar Prowlarr/indexadores e Byparr opcional com detecção de capabilities; reusar IDs. Configurar Bazarr/providers e as conexões Seerr/Jellyfin sem recriar bibliotecas.
- [ ] Implementar init/adopt de contas/chaves; gerar/importar segredos no `.env` atomicamente. Preserve contas/permissões não gerenciadas; mudança de senha exige aparecer como mudança explícita no plano.
- [ ] Testar diferença entre configuração persistida e valor efetivo. Registrar `unsupported` quando a versão da API não suporta uma opção e impedir sucesso global enganoso.

```bash
uv run pytest tests/contract/test_native_configuration.py tests/contract/test_upstream.py -q
scripts/homeserver config plan --env-file .runtime/dev/.env
scripts/homeserver config apply --env-file .runtime/dev/.env
scripts/homeserver config verify --env-file .runtime/dev/.env
# Repetir plan: resultado sem mudanças, sem novos IDs nativos.
```

**Aceite:** nova instalação e adopt têm a mesma configuração gerenciada; solicitações, IDs e personalizações alheias ao projeto são preservados. **Commit:** `feat: reconcile native media service configuration`.

## P07 — Gerar infraestrutura portátil e instalar fresh/adopt

**Modificar:** `deploy/compose.yaml`, `compose.dev.yaml`, `compose.prod.yaml`, `deploy/systemd/`, `scripts/bootstrap-server.sh`, `scripts/lib/bootstrap.sh`, `scripts/check-mount.sh`, `scripts/verify-layout.sh`, `scripts/verify-gpu.sh`, `scripts/host-metrics.py`, `scripts/capacity-snapshot.py`.

**Criar:** `deploy/compose.intel.yaml`, `docs/installation.md`, `tests/unit/test_stack_render.py`, `tests/integration/test_installation.py`; `render.py` já definido em P02.

```python
def test_cpu_profile_has_no_gpu_device(tmp_path):
    from homeserver_common.env import load_settings
    from homeserver_common.render import render_stack

    path = tmp_path / '.env'
    path.write_text('HOMESERVER_TRANSCODE_MODE=cpu\n')
    stack = render_stack(load_settings(path, mode='dev'))
    assert not stack['services']['jellyfin'].get('devices')
```

- [ ] Testar render CPU/Intel, caminhos com espaços, dois binds iguais, portas repetidas, ausência de Tailscale, UID/GID e layout de hardlinks. Dev usa paths temporários e loopback.
- [ ] Incorporar intenção dos overrides do servidor, especialmente persistência Seerr/Prowlarr e acesso Tailscale; nenhum segredo no template. Parametrizar cards e todos os paths do host.
- [ ] Implementar instalação de diretórios/identidades/templates/units. Separar preparação opcional de pacotes de checagem; exigir mount existente e preservar arquivos de rede/energia/disco do operador.
- [ ] Implementar `install --plan/--apply` fresh/adopt; journaling das alterações próprias permite repetir sem duplicação. Falha de preflight não altera host.
- [ ] Atualizar scripts legados para consumir loader e marcar YAMLs/envs antigos como migrados; depois de equivalência comprovada, remover `config/policy.yaml` e exemplos concorrentes do caminho ativo.
- [ ] Executar `uv run pytest tests/unit/test_stack_render.py tests/integration/test_installation.py -q`, `uv run make compose-check` e testes shell do bootstrap/layout. Conferir montagem real em VM.

**Aceite:** instalação sem GPU/Lenovo, com paths e usuário diferentes, funciona seguindo o guia; segunda aplicação é idempotente. **Commit:** `feat: provision portable homeserver deployments`.

## P08 — Remover CYD e simplificar telemetria

**Remover após dependências verificadas:** `firmware/cyd/`, `docs/runbooks/cyd-flash.md`, contratos/testes exclusivos do display; Mosquitto e `deploy/mosquitto/` se não houver outro consumidor.

**Modificar:** Compose, `.env.example` gerado, `services/telemetry/src/homeserver_telemetry/app.py`, `publisher.py`, `notifications.py`, `bandwidth.py`, `docs/runbooks/notifications.md`, design normativo e `AGENTS.md` se trouxer premissas obsoletas.

- [ ] Inventariar clientes MQTT em produção sem registrar segredos; associar cada um a uma necessidade remanescente. Bloquear somente a remoção do broker se houver consumidor real; CYD permanece fora do produto.
- [ ] Definir teste de integração em que painel/métricas funcionam sem broker, certificados ou arquivo `snapshot.json` antigo.
- [ ] Retirar código/serviços exclusivos da CYD e unificar contrato web; remover rota antiga ou adaptá-la ao produtor real, registrando compatibilidade.
- [ ] Ligar alertas configuráveis ao runtime ou retirar promessas de envio existentes; não deixar classes isoladas contarem como funcionalidade entregue. Upload adaptativo só permanece anunciado se tiver produtor de estado e aplicação testados.
- [ ] Preservar dados antigos em backup durante migração. Não usar `docker compose down -v` nem limpeza global de volumes.

```bash
uv run pytest tests/contract/test_telemetry_api.py tests/integration/test_status_dashboard.py -q
rg -n -i 'cyd|esp32|mosquitto|mqtt' README.md deploy services docs/runbooks .env.example
# Cada ocorrência remanescente precisa ser histórica ou uma dependência explicitamente preservada.
```

**Aceite:** instalação completa e monitoramento sem CYD; nenhum pré-requisito residual de firmware/certificado do display. **Commit:** `refactor: retire cyd and keep web monitoring`.

## P09 — Completar supervisão, diagnóstico e logs

**Criar:** `scripts/supervise-stack.py`, `tests/integration/test_supervisor.py`, `tests/contract/test_worker_health.py`.

**Modificar:** unit stack/mount-watch, `worker/__main__.py`, `worker/runtime.py`, API/gateway/telemetry health, `services/telemetry/src/homeserver_telemetry/status.py`, template do painel e Compose logging.

- [ ] Criar testes com eventos: Docker inicia antes do mount, UUID incorreto, queda individual, pedido de manutenção e perda/retorno de disco. Usar executor fake para provar ausência de `up` quando bloqueado, e VM para comportamento real.
- [ ] Implementar um supervisor com validação antes de iniciar/reconciliar serviços e mecanismo explícito de manutenção compartilhado com backup/deploy. Não usar reinício automático irrestrito que burle a guarda.
- [ ] Persistir heartbeat/etapa, último ciclo completo, próximo retry e motivo da espera. Configuração essencial ausente causa erro claro ou modo desabilitado explicitamente reportado, nunca worker aparentemente saudável sem trabalho.
- [ ] Healthchecks distinguem processo vivo de pronto; prontos verificam dependências pertinentes sem pedir buscas externas caras. Não reiniciar por uma busca longa ainda dentro de deadline.
- [ ] Parametrizar rotação de logs e dashboard links. Expor consumo por categoria, espaço livre, idade do backup e estado da fila; não contar hardlinks duas vezes em relatório de uso físico.
- [ ] Implementar `doctor` sanitizado e alerts opcionais com deduplicação/retry. Tokens, nomes privados de mídia e credenciais não entram em diagnóstico público.

```bash
uv run pytest tests/integration/test_supervisor.py tests/contract/test_worker_health.py tests/unit/test_host_metrics_dashboard.py tests/integration/test_status_dashboard.py -q
scripts/homeserver doctor --env-file .runtime/dev/.env
```

**Aceite:** falhas induzidas em VM recuperam conforme política; perda de mídia bloqueia escrita; logs respeitam retenção. **Commit:** `feat: supervise services and expose actionable health`.

## P10 — Unificar backup e restore Restic

**Modificar:** `scripts/backup-restic.sh`, `scripts/restore.sh`, `scripts/pull-backup-wsl.sh`, `scripts/pull-backup.ps1`, `scripts/register-backup-task.ps1`, units/timers de backup, `docs/runbooks/backup-restore.md`, `docs/runbooks/recovery.md`.

**Criar:** `tests/integration/test_restic_lifecycle.py`; fixtures de appdata pequenas e sem dados reais. Tirar scripts legados de fixtures da documentação normativa ou movê-los explicitamente para helpers de teste.

- [ ] Criar repositórios Restic temporários em teste; capturar SQLite/WAL e arquivos de configuração; restaurar isoladamente e comparar hashes/consultas. Cobrir senha errada, snapshot ausente e target não vazio.
- [ ] Conectar backup à manutenção do supervisor, trap de retorno e timeout; validar snapshot e reativação também na falha. Parametrizar destino, horário, retenção e limite de staging.
- [ ] Implementar transferência de snapshots com `restic copy` ou espelho congelado coordenado. Provisionar transporte/chave restrita compatível, preservando repositório atual até validar a cópia. Testar origem em prune e pull interrompido.
- [ ] Restore grava recovery, valida DB/UUID do novo host sem copiar UUID antigo cegamente, preserva tombstones e reconcilia arquivos presentes/ausentes antes da liberação explícita.
- [ ] Testar backup sem mídia e demonstrar isso no manifesto/guia. Medir tempo e tamanho num ensaio representativo; preencher RPO/RTO com observação, não expectativa.

```bash
uv run pytest tests/integration/test_restic_lifecycle.py tests/integration/test_backup_restore.py tests/unit/test_recovery.py -q
scripts/homeserver backup create --env-file .runtime/dev/.env
scripts/homeserver backup verify --env-file .runtime/dev/.env
# Restaurar o ID retornado pelo create em diretório temporário vazio, com --isolated.
```

**Aceite:** restauração do formato real de produção passa em máquina sem estado anterior; falha/interrupção não inutiliza o último backup. **Commit:** `feat: validate restic backup and recovery lifecycle`.

## P11 — Completar build, deploy e rollback

**Criar:** `scripts/build-release.sh`, `.github/workflows/release.yml`.

**Modificar:** `scripts/deploy.sh`, `rollback.sh`, `validate-release.py`, `smoke.sh`, `config/release.schema.json`, `config/versions.env`, `.github/workflows/deploy.yml`, `tests/unit/test_release.py`, `tests/integration/test_deploy.py`, runbooks de release/deploy.

- [ ] Estender testes: digest diferente da imagem efetiva, checksum de configuração errado, schema incompatível, backup obrigatório ausente, migração interrompida, startup falhando e smoke reprovado.
- [ ] Implementar build de imagens/artefato a partir da SHA limpa, lockfile e versões de ferramentas fixas. Manifesto referencia todas as imagens realmente usadas e versões de schema/configuração.
- [ ] Deploy: lock → preflight/env → imagem por digest → backup exigido → manutenção → migração compatível → troca atômica de release → ativação → readiness/smoke → liberação. Qualquer fase registra resultado sanitizado.
- [ ] Rollback reverte runtime e configuração quando schema permitir. Incompatibilidade entra em recovery com procedimento de restauração; nunca baixa schema de SQLite implicitamente.
- [ ] CI manual consome artefato autenticado/associado à SHA cujo CI passou, usando o mesmo script local. Documentar autenticação SSH/Tailscale e host-key, incluindo primeiro deploy sem `/opt/homeserver/current`.
- [ ] Testar deploy duas vezes da mesma release de modo idempotente; reter release anterior e impedir prune de releases necessárias à recuperação.

```bash
uv run pytest tests/unit/test_release.py tests/integration/test_deploy.py tests/system/test_workflow_policy.py -q
uv run make lint compose-check smoke
```

**Aceite:** imagem/label/manifesto/SHA vivos coincidem; falha pós-ativação recupera conforme contrato demonstrado em VM. **Commit:** `feat: activate verified releases with safe rollback`.

## P12 — Preparar GitHub e CI para distribuição

**Modificar:** `.gitignore`, `.github/workflows/ci.yml`, `Makefile`, `pyproject.toml`/lock se necessário.

**Criar:** `.dockerignore`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, configuração de scanner de segredos e atualização de dependências; licença somente após escolha do proprietário, sem presumir autorização de redistribuição.

- [ ] Expandir ignores para sidecars SQLite/WAL/SHM, chaves privadas e dumps/artefatos locais. Manter `.env.example` e fixtures deliberadas rastreáveis; não ignorar documentação inteira para esconder achados.
- [ ] Implementar `.dockerignore` com allowlist/contexto mínimo: `.git`, worktrees, `.env` privado, backup, runtime, bancos e mídia não entram no build. Verificar contexto/imagem com arquivo sentinela fictício.
- [ ] Executar scanner de segredos no histórico e árvores atuais; revisar falsos positivos. Se houver segredo real, revogar e tratar exposição; reescrita de histórico é operação separada, não etapa automática.
- [ ] Fixar uv/ferramentas de CI; adicionar scanner de dependências/imagens, validação do catálogo env, build, Restic temporário e instalação com fixtures. Scans não imprimem tokens nem enviam inventário privado.
- [ ] Criar targets `make test-config`, `make test-install`, `make test-restic`; incluir na CI com Docker real. Não aceitar skips de testes exigidos nesse job.
- [ ] Publicar contrato de suporte/plataforma, contribuição e relato de falhas. Conservar deploy manual e permissões mínimas de workflows.

```bash
git check-ignore .env backup/example .runtime/dev/state.db .runtime/dev/state.db-wal
git check-ignore .env.example  # Deve sair 1: o exemplo não é ignorado.
uv run make lint test-unit test-contract test-integration compose-check smoke test-config test-install test-restic
```

**Aceite:** clone/build não carrega estado pessoal; CI testa a distribuição real. Publicar no GitHub não exige tornar o repo público ou escolher licença aberta sem decisão do proprietário. **Commit:** `ci: verify portable distribution and repository hygiene`.

## P13 — README completo e aceite em máquina nova

**Modificar:** `README.md`, `AGENTS.md`, runbooks afetados e índices de docs.

**Criar/completar:** `docs/installation.md`, `configuration.md`, `architecture.md`, `operations.md`, `troubleshooting.md`, `development.md`; evidência sanitizada da nova aceitação.

- [ ] README com diagrama e tabela de serviços; quickstart fresh/adopt; `.env`; URLs; administração; update/rollback; backup/restore; desenvolvimento e limitações. Todos os comandos devem corresponder a ferramentas entregues.
- [ ] Explicar estados reais da fila, sequência de séries, qualidade versus seeds, legenda/edição, seeding e hardlinks; diagnóstico com comando e resultado esperado por sintoma.
- [ ] Marcar design/planos/evidências antigos como histórico. Substituir as instruções ativas de limites fixos/CYD por links para o design atual.
- [ ] Em VM Ubuntu limpa e sem GPU, seguir somente README: clone → pré-requisitos → `.env` → install → doctor → configuração dos serviços. Repetir install/apply e comprovar ausência de duplicatas.
- [ ] Alterar downloads/seeding, resolução e idioma pelo `.env`; verificar API nativa e resultado do seletor. Executar fluxo com torrent/mídia de teste autorizada, legenda, importação e disponibilidade no Jellyfin.
- [ ] Validar atualização, queda individual, restart do host, mount ausente, backup e restore isolado. Intel/Tailscale/reprodução em hardware real ficam com evidências próprias; desconhecido não vira aprovado.
- [ ] Executar adopt no ambiente existente após backup e plano sanitizado; conferir IDs, contagens e hashes de estado relevante antes/depois, sem modificar ou apagar mídia. Registrar qualquer desvio antes de encerrar.
- [ ] Conferir links e copiar comandos do README literalmente. Revisão final do conjunto e registro de limitações restantes.

**Aceite global:** instalação reproduzível sem editar YAML/Python ou depender de CYD, do usuário do desktop original ou de configuração secreta não documentada. **Commit:** `docs: publish verified installation and operator guides`.

## Matriz de cobertura e definição de conclusão

| Requisito/achado | Tarefas | Prova exigida |
|---|---|---|
| Auditoria de projeto/infra | Documento de auditoria | Evidências, gravidade e limites explícitos |
| Retirar CYD | P08, P13 | Core/status sobem sem firmware/MQTT exclusivo |
| Tudo editável no `.env` | P02–P07 | Schema e catálogo sem variáveis mortas; apply/verify nativos |
| Downloads/seeding | P03 | Leitura qBit após mudança e reaplicação idempotente |
| Idiomas/resolução/qualidade | P04–P06 | Fixtures alterando valores mudam seleção e perfis coerentemente |
| Fonte lenta por cinco minutos, mantendo download | P04 | Busca aos 300 s; sem alternativa vantajosa, a atual segue ativa; mesma qualidade e episódio |
| Preservar episódio em andamento | P04 | Exceção persistida de S02E04, resistente a restart e não herdada por S02E05 |
| Instalação em outra máquina | P07, P13 | Ensaio Ubuntu CPU limpo e adopt sem perda de estado |
| README operacional | P13 | Comandos copiados e executados por ordem; troubleshooting útil |
| GitHub/ignore | P01, P12 | Branch canônica, scanners, contexto de build sanitizado |
| Deploy/rollback confiáveis | P11 | Testes de falha/recuperação e SHA efetiva |
| Backup de verdade | P10 | Restore Restic, incluindo ledger e tombstones |
| Logs/health/drift | P06, P09 | Config verify, falhas induzidas e rotação comprovada |

O trabalho futuro estará concluído quando essa matriz tiver evidências e o README instalar o produto a partir da branch padrão. Passar somente nos testes antigos ou manter o servidor atual ligado não satisfaz esse aceite.
