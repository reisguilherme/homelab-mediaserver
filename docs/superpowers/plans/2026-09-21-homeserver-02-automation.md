# HomeServer — Implementação 02: automação, capacidade e ciclo da mídia

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Executar os checkboxes em ordem; não liberar downloads reais antes dos testes de admissão.

**Goal:** implementar pedidos que respeitem qualidade, idioma, tamanho e espaço reservado, com retomada e exclusão coordenadas.

**Architecture:** Seerr registra pedidos e Arr mantém catálogo e importação. Um controlador próprio agenda e reserva; um gateway compatível com o subconjunto necessário da API qBittorrent valida cada mutação antes de encaminhá-la ao cliente real. SQLite guarda estado, autorizações e operações idempotentes.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, HTTPX, SQLite/WAL, pytest, FFprobe, APIs Seerr/Arr/qBittorrent/Bazarr/Jellyfin.

**Spec:** [Escopo v1.3](../specs/2026-09-21-homeserver-design.md), seções 8–11; [plano principal](2026-09-21-homeserver-implementation.md), seções 5–6.

## Restrições globais

- Limites: 50 GB/filme, 5 GB/episódio, 100 GB/temporada; GB decimal.
- 2160p preferencial, mínimo 1080p; sem upgrades de versões válidas.
- Reservar a temporada completa antes de iniciar; em lançamento, reservar orçamento para episódios futuros.
- Sem espaço: manter pendente; exclusão da biblioteca sempre por solicitação do usuário.
- Sem automação que adicione primeiro e pause depois para simular controle prévio de capacidade.
- Todas as integrações de mutação devem ser testadas com versões reais fixadas antes de operar sem supervisão.

## C01 — Provar o fluxo de integração e fixar contratos externos

**Arquivos:** `docs/contracts/upstream-apis.md`, `docs/contracts/upstream-versions.json`, `tests/fixtures/upstream/`, `tests/contract/test_upstream.py`, `services/control/src/homeserver_control/adapters/{seerr,arr,qbittorrent,subtitles,jellyfin}.py`.

**Consome:** I03 e uma stack dev. **Produz:** contratos capturados, prova de pedido sem download nativo e interfaces locais estáveis.

- [ ] Exportar os schemas/API e registrar versões realmente escolhidas. Não desenvolver contra o branch de desenvolvimento do Sonarr por engano. Guardar respostas de exemplo sanitizadas e mínimo conjunto de campos exigidos.
- [ ] No Seerr, desabilitar `Enable Automatic Search`, manter aprovação automática para a conta e uma instância de cada Arr. Criar pedido e demonstrar que ele cadastra/monitora o título sem iniciar aquisição. [Configuração oficial](https://docs.seerr.dev/using-seerr/settings/services/)
- [ ] Desabilitar RSS/grabs autônomos nos indexadores dos Arr e a remoção automática de downloads concluídos/falhos. Registrar as propriedades exatas das versões escolhidas. O controlador passa a procurar novos episódios periodicamente.
- [ ] Desabilitar importação automática dos downloads até a validação C05; provar importação manual via API na versão escolhida, com hardlink e identificação do episódio/filme.
- [ ] Capturar a sequência HTTP que Sonarr/Radarr usam ao testar um cliente qBittorrent, adicionar um torrent, consultar estado e importar. Essa lista vira a allowlist de C04.
- [ ] Confirmar polling paginado de pedidos Seerr, incluindo temporadas futuras, pedidos removidos e disponibilidade já existente. Webhook pode reduzir latência, mas polling de reconciliação continua obrigatório.

Interfaces internas a declarar em `adapters/contracts.py`:

```python
from dataclasses import dataclass
from typing import Literal, Protocol

@dataclass(frozen=True)
class MediaRef:
    media_key: str
    kind: Literal["movie", "episode", "season"]
    source_id: int

@dataclass(frozen=True)
class ReleaseRef:
    guid: str
    indexer_id: int
    app: Literal["sonarr", "radarr"]
    title: str
    reported_bytes: int
    torrent_url: str

class ArrAdapter(Protocol):
    async def search(self, media: MediaRef) -> list[ReleaseRef]: ...
    async def grab(self, release: ReleaseRef) -> str: ...
    async def import_verified(self, job_id: str) -> str: ...
    async def unmonitor(self, media_key: str) -> None: ...

class RequestAdapter(Protocol):
    async def list_approved(self, page: int) -> dict: ...
```

As reticências representam métodos de `Protocol`, não implementações. `grab` retorna ID externo confirmado ou uma condição de resultado incerto; timeout depois do envio não significa que o download não foi adicionado. `import_verified` recebe ID interno e resolve os campos concretos a partir do registro validado no banco.

APIs externas a verificar e mapear: Arr `/api/v3/release`, `/api/v3/queue`, `/api/v3/manualimport` e comandos de importação; Seerr pedidos paginados; qBittorrent `/api/v2/...`. O contrato exato de importação é capturado nesta tarefa, sem pressupor que um corpo JSON seja idêntico em todas as versões. [Sonarr](https://sonarr.tv/docs/api/), [Radarr](https://radarr.video/docs/api/), [qBittorrent](https://github.com/qbittorrent/wiki/blob/master/WebUI-API-%28qBittorrent-5.0%29.md)

- [ ] Provar obtenção de `.torrent` por fonte de teste e inspeção dos tamanhos antes do payload. Na versão inicial, candidatos somente com magnet sem metadados verificáveis ficam em `waiting_source`; não liberar payload para descobrir tamanhos depois. Registrar essa limitação de cobertura das fontes no manual.
- [ ] Tratar 401/403 como erro de credencial sem retry infinito, 429 com backoff, timeout com estado incerto e resposta incompatível como falha de contrato.
- [ ] Executar `make test-contract`; registrar requisições/respostas e a prova de que nenhuma fonte nativa pode iniciar uma aquisição por fora do caminho previsto.

**Aceite G2 parcial:** integração e arquivos verificáveis demonstrados. Se uma API escolhida impedir o desenho, revisar este adaptador antes de C04; o restante do domínio continua testável por fixtures.

## C02 — Modelo de domínio e seleção dentro dos limites

**Arquivos:** `domain/models.py`, `domain/policy.py`, `domain/torrent_manifest.py`, `domain/selection.py`, `tests/unit/test_policy.py`, `tests/unit/test_manifest.py`, `config/policy.yaml`.

**Consome:** `ReleaseRef` e metadados `.torrent`. **Produz:** manifesto verificado e plano de seleção elegível.

Definições públicas a implementar:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class EpisodeFile:
    episode_key: str
    path: str
    size_bytes: int

def within_limits(movie_bytes: int | None,
                  episode_bytes: list[int]) -> bool:
    if movie_bytes is not None:
        return 0 < movie_bytes <= 50_000_000_000 and not episode_bytes
    return (bool(episode_bytes)
            and all(0 < size <= 5_000_000_000 for size in episode_bytes)
            and sum(episode_bytes) <= 100_000_000_000)
```

`within_limits` valida um conjunto já mapeado. O parser verifica tamanhos inteiros, arquivos duplicados, caminhos e relação episódio/arquivo antes dessa chamada. Dados não confiáveis não podem ser convertidos implicitamente de `bool`, float ou tamanho negativo.

Testes essenciais:

```python
from homeserver_control.domain.policy import within_limits

def test_movie_upper_boundary():
    assert within_limits(50_000_000_000, [])
    assert not within_limits(50_000_000_001, [])

def test_episode_limit_is_independent_from_season_total():
    assert not within_limits(None, [5_000_000_001])

def test_season_upper_boundary():
    assert within_limits(None, [5_000_000_000] * 20)
    assert not within_limits(None, [5_000_000_000] * 21)
```

- [ ] Escrever os testes e observar falha antes de criar o módulo; implementar e executar `uv run pytest tests/unit/test_policy.py -q`.
- [ ] Parser de torrent deve validar infohash, limites de tamanho do próprio metadado, caminhos absolutos/`..`, entradas duplicadas, links e arquivos compactados. Tratar torrent v1, v2 e híbrido somente conforme suporte explicitamente testado; os demais candidatos permanecem inelegíveis com motivo.
- [ ] Mapear arquivos a episódios sem confiar apenas no nome do torrent. Se o mapeamento for ambíguo, aguardar análise; não dividir o tamanho total pelo número de episódios para supor conformidade.
- [ ] Aplicar limite ao total de episódios de toda a temporada, incluindo arquivos já válidos; extras/padding que forem transferidos entram no orçamento físico. Arquivos não selecionados não podem escapar da previsão de bytes por peças compartilhadas.
- [ ] Implementar ordenação configurável de idioma/resolução/qualidade e registrar explicitamente qual interpretação foi escolhida antes de produção. Evitar score de tamanho que prefira sempre o maior arquivo.
- [ ] Planejar temporada completa como conjunto sem episódios duplicados: explorar alternativas de 2160p/1080p até encontrar cobertura total ≤100 GB. Busca combinatória tem limite de trabalho; se esgotado, explicar o motivo e aguardar, nunca exceder o teto.
- [ ] Conteúdo já válido é imutável para seleção automática. Falha técnica/idioma incorreto tem estado separado de upgrade.

**Aceite:** A08–A11 com fixtures de fronteira, temporadas longas e pacotes mistos; zero acesso de rede nos testes de domínio.

## C03 — Reservas, fila persistente e retomada

**Arquivos:** `persistence/migrations/001_initial.sql`, `persistence/db.py`, `domain/capacity.py`, `worker/scheduler.py`, `worker/reconcile.py`, `tests/unit/test_capacity.py`, `tests/integration/test_reservations.py`.

**Consome:** seleção de C02, snapshot do filesystem e catálogo de temporadas. **Produz:** reserva atômica e intenção de aquisição persistente.

Schema mínimo, com tipos/checks, índices e chaves estrangeiras a completar com as colunas de auditoria indicadas:

```sql
PRAGMA foreign_keys = ON;
CREATE TABLE requests (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL UNIQUE,
  media_key TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE reservations (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES requests(id),
  media_key TEXT NOT NULL UNIQUE,
  filesystem_id TEXT NOT NULL,
  budget_bytes INTEGER NOT NULL CHECK(budget_bytes >= 0),
  allocated_bytes INTEGER NOT NULL DEFAULT 0 CHECK(allocated_bytes >= 0),
  state TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE operations (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE tombstones (
  media_key TEXT PRIMARY KEY,
  deleted_at TEXT NOT NULL,
  source_generation TEXT NOT NULL
);
```

Adicionar em migrações versionadas `downloads`, `download_files`, `permits`, `operation_events` e `outbox`; os contratos destas tabelas são definidos em C04–C06. Não guardar credenciais no JSON de operação. Índices devem incluir estados pendentes e relação request/download, evitando varredura da biblioteca por ciclo.

Função pura:

```python
def available_bytes(free: int, total: int, commitments: list[int]) -> int:
    safety = max(20_000_000_000, (total + 19) // 20)
    return max(0, free - safety - sum(commitments))

def remaining_commitment(budget: int, allocated: int) -> int:
    return max(0, budget - allocated)
```

Teste de não duplicar alocação:

```python
from homeserver_control.domain.capacity import available_bytes, remaining_commitment

def test_bytes_already_allocated_are_not_reserved_twice():
    remaining = remaining_commitment(100_000_000_000, 40_000_000_000)
    assert available_bytes(200_000_000_000, 500_000_000_000, [remaining]) == 115_000_000_000
```

- [ ] Implementar repositório com `BEGIN IMMEDIATE`, timeout de lock e leitura recente do filesystem antes de reservar. Inserir reserva + intenção de operação na mesma transação.
- [ ] Escrever teste concorrente com dois processos, não apenas duas corrotinas no mesmo lock Python. Se há capacidade para um pedido, apenas uma admissão pode ser confirmada.
- [ ] Para arquivos esparsos, medir blocos alocados; deduplicar hardlinks. Orçamento inclui margem e bytes de todos os arquivos transferidos, inclusive incompletos/auxiliares.
- [ ] Reservar temporada concluída integralmente antes do primeiro episódio. Em lançamento, reservar 100 GB + margem; acompanhar futuros episódios e liberar sobra apenas após fim confirmado ou cancelamento.
- [ ] Temporada que cresce ou ultrapassaria orçamento não substitui episódios existentes nem ultrapassa o limite: estado explicativo e bloqueio de novas admissões.
- [ ] Registrar todas as temporadas no pedido de série, admitindo unidades que caibam com prioridade de temporadas antigas. FIFO entre unidades elegíveis e motivo visível quando um pedido for ultrapassado.
- [ ] Reconciliar a cada 60 segundos e após exclusão/conclusão: UUID, free bytes, reserva, downloads existentes e estado das operações. Recuperar após crash sem duplicar reserva, torrent ou pedido.
- [ ] Fonte indisponível usa backoff e não trava toda a fila. Pedido com download já autorizado mantém compromisso até confirmar que os bytes foram removidos ou estabilizados na biblioteca.

**Aceite:** A12–A15 e A17. Testar reboot com reserva e torrent incompleto, variação de espaço externa, timeout de banco e duas admissões em disputa.

## C04 — Gateway de admissão e execução idempotente

**Arquivos:** `gateway/app.py`, `gateway/auth.py`, `gateway/allowlist.py`, `gateway/permits.py`, `adapters/qbittorrent.py`, `worker/dispatch.py`, `persistence/migrations/002_downloads.sql`, `tests/contract/test_qb_gateway.py`, `tests/integration/test_admission.py`.

**Consome:** reserva C03 e metadados C02. **Produz:** transferência real identificada, vinculada à reserva e impossível de iniciar por um Arr sem autorização.

Uma permissão de download contém `permit_id`, `operation_id`, `reservation_id`, `infohash`, hash do metadado completo, destino permitido, categoria, conjunto de arquivos, orçamento e validade. Estados: `authorized → dispatching → confirmed`, com `unknown` para efeito externo incerto e `revoked` para cancelamento. Uso repetido da mesma operação retorna o resultado anterior; não cria outro torrent.

- [ ] Implementar gateway com autenticação própria para Arr e credencial upstream somente do lado servidor. Não fazer proxy aberto para qualquer método/path.
- [ ] Allowlist inicial inclui login/versão e consultas comprovadas em C01; toda mutação é negada por padrão. Adições exigem permit; resume/start exige transferência admitida; delete exige operação de exclusão C06. Ajustes de localização/prioridade/seleção também precisam respeitar o orçamento.
- [ ] Configurar Arr apontando exclusivamente para o gateway. qBittorrent real fica na rede `transfer`, sem API publicada para Arr; a credencial real não aparece em Seerr, Arr ou CYD.
- [ ] Antes de chamar `ArrAdapter.grab`, salvar permit para o torrent já inspecionado. Quando Arr enviar arquivo/URL, validar novamente o conteúdo e encaminhar o mesmo metadado; mudanças de hash, destino ou seleção revogam a admissão.
- [ ] URLs de torrent só podem apontar para Prowlarr/indexadores previamente configurados. Restringir redirects, tamanho, timeout e destinos; nunca permitir que uma URL arbitrária consulte serviços internos ou metadados de nuvem.
- [ ] A gravação do permit e a reserva antecedem o efeito remoto. Se houver timeout, procurar por infohash/categoria antes de repetir; não usar retry cego em POST.
- [ ] Armazenar mapeamento `download_id`, infohash, caminho e arquivos. Limitar transferências ativas ao parâmetro inicial 2; filas do cliente não substituem o scheduler.
- [ ] Gateway indisponível deve impedir novas aquisições; seeding e reprodução existentes continuam. Uma API desconhecida após upgrade é recusada e gera erro de contrato, em vez de encaminhamento irrestrito.

Teste crítico de bloqueio, usando fixtures HTTP criadas nesta tarefa:

```python
def test_add_without_permit_never_reaches_upstream(gateway_client, upstream_spy, torrent_fixture):
    response = gateway_client.post(
        "/api/v2/torrents/add",
        files={"torrents": ("fixture.torrent", torrent_fixture)},
    )
    assert response.status_code == 403
    assert upstream_spy.add_calls == []
```

Fixtures: `gateway_client` autenticado como Arr, `upstream_spy` servidor de teste com lista `add_calls`, `torrent_fixture` bytes válidos sem dado privado. Testar também permit válido/expirado, repetição, hash trocado, reinício entre POST e resposta, mutação de arquivos selecionados e tentativa de conexão direta a qBittorrent.

- [ ] Rodar a stack real dev com busca inicial, RSS reativado propositalmente em teste, novo episódio e clique manual de grab sem permit: nenhum pode iniciar payload. Depois verificar caminho autorizado completo.
- [ ] Registrar os endpoints necessários à importação e retomada da versão escolhida. Falha em compatibilidade bloqueia G2/G3; não liberar serviços nativos apontando diretamente ao cliente como solução provisória de produção.

**Aceite:** A10, A11, A16 e A26. Esta é a prova determinante da arquitetura de automação.

## C05 — Validação, importação e disponibilidade

**Arquivos:** `worker/validation.py`, `worker/imports.py`, `worker/subtitles.py`, `domain/media_probe.py`, `persistence/migrations/003_events.sql`, `tests/unit/test_probe.py`, `tests/integration/test_imports.py`.

**Consome:** download completo, metadados e identidade de mídia. **Produz:** versão válida importada, legenda quando exigida e evento único de disponibilidade.

- [ ] Executar FFprobe em subprocesso com argumentos separados, timeout e limites, interpretando JSON e falhando em arquivos não reconhecidos. Não executar nomes de arquivo como shell.
- [ ] Validar arquivo real: tamanho, resolução, faixas de áudio, subtítulos e identificação do episódio/filme. Tags ausentes/ambíguas geram estado de revisão, não confirmação falsa de dual áudio.
- [ ] Arquivo válido pode ser importado via API do Arr; confirmar hardlink comparando inode/device nos caminhos correspondentes. Falha de hardlink suspende a operação em vez de aceitar duplicação sem contabilização.
- [ ] Solicitar legendas ao Bazarr para áudio original quando faltarem. Manter `waiting_subtitles` até existir legenda confirmada; testar idioma e correspondência ao release, sem prometer qualidade perfeita da sincronização só pelo nome do arquivo.
- [ ] O arquivo pode aparecer no catálogo Jellyfin após a importação enquanto a legenda é obtida. O status detalhado e a notificação de pronto só são emitidos quando os requisitos forem satisfeitos. Não depender apenas do estado nativo de disponibilidade do Seerr para esses alertas.
- [ ] Notificações de conclusão nativas conflitantes serão desativadas; o evento próprio `media.available` é a fonte do alerta M02. Refletir estado detalhado na UI do controlador.
- [ ] Implementar exportação paginada e confirmação de eventos pelas rotas internas do plano principal. Gravar mudança de estado e evento na mesma transação, com ID deduplicável; uma falha do coletor não perde a transição de disponibilidade.
- [ ] Confirmar presença no Jellyfin, persistir versão válida e bloquear upgrade. Permitir seeding com a referência de download original intacta; reduzir reserva a zero somente após reconciliar alocação física e obrigações futuras.
- [ ] Arquivo inválido fica isolado por job e não vai à biblioteca. Qualquer limpeza de download inválido deve atuar somente nos caminhos do job, confirmar remoção e preservar biblioteca válida; caso ambíguo, manter erro para intervenção.
- [ ] Testar download corrompido, áudio divergente, legenda ausente, API de importação indisponível e crash após importação mas antes de registrar o resultado. Recuperação deve localizar o arquivo já importado e não copiá-lo novamente.

**Aceite:** A07–A09 e A18. Não anunciar disponibilidade a partir de percentual 100% isoladamente.

## C06 — Interface da fila e exclusão coordenada

**Arquivos:** `api/routes.py`, `api/auth.py`, `api/templates/queue.html`, `api/templates/deletion.html`, `worker/deletions.py`, `domain/deletion_plan.py`, `tests/integration/test_deletions.py`, `tests/system/test_ui.py`.

**Consome:** mapa de arquivos/torrents, reservas e IDs estáveis. **Produz:** UI autenticada e operação de exclusão recuperável.

- [ ] Implementar rotas do plano principal, paginação da fila, motivo de bloqueio, espaço livre/reservado/admissível e última atualização. Mostrar GB/GiB explicitamente.
- [ ] Criar autenticação para a conta única; cookie seguro no canal configurado, proteção CSRF e ausência de segredos no HTML. Acesso privado via Tailscale não elimina a autenticação da UI.
- [ ] `deletions/preview` resolve filme, temporada ou série em uma lista finita de IDs/caminhos e dependências. Retornar bytes estimados deduplicados por inode; não prometer liberação igual à soma dos nomes de arquivo.
- [ ] Confirmar exclusão com token de preview, expiração, versão do conjunto de objetos e chave idempotente. Se as dependências mudaram, responder 409 e solicitar novo preview.
- [ ] Worker primeiro grava tombstone e desmonitora os objetos. Em seguida interrompe downloads envolvidos, remove somente referências selecionadas e atualiza Arr/Seerr/Jellyfin/Bazarr.
- [ ] Não aceitar caminhos livres enviados pela UI. Resolver cada caminho dentro dos roots de mídia/download, rejeitar symlinks/escape e conferir identidade antes de apagar. Pacote com itens fora da seleção exige ampliar explicitamente a seleção ou manter bloqueado.
- [ ] Para excluir temporada de série ainda monitorada, persistir tombstone por temporada/episódio e reaplicá-lo durante reconciliação. Novo pedido explícito gera uma nova `source_generation` e pode retirar a supressão, após confirmação do usuário na UI.
- [ ] Depois de confirmar remoções, medir bytes realmente liberados, atualizar reservas, retomar fila e concluir a operação. Etapas já confirmadas não são repetidas após crash.

Teste de domínio obrigatório, usando cenário isolado e seus IDs:

```python
def test_deleted_season_is_not_recreated_by_series_sync(scenario):
    scenario.add_monitored_series("tvdb:456", seasons=[1, 2])
    scenario.delete_season("tvdb:456", season=1)
    scenario.sync_series()
    assert scenario.is_suppressed("season:tvdb:456:1")
    assert not scenario.has_pending_download("season:tvdb:456:1")
    assert scenario.is_monitored("season:tvdb:456:2")
```

`scenario` é um harness de integração criado nesta tarefa com banco temporário e adaptadores falsos; complementar com um pacote multifile real de teste para validar exclusão e seeding.

**Aceite:** A19; chamadas sem autorização ou CSRF válido não alteram dados. Não disponibilizar exclusão isolada de episódio de pacote quando ela afetar mídia não selecionada.

## Revisão de conclusão

- [ ] Todos os limites e reservas persistem em restart; nenhum caminho de aquisição passa fora do gateway.
- [ ] Cadastros e episódios futuros são reconciliados, sem upgrades de arquivos válidos.
- [ ] Nomes de estados/API coincidem com o plano principal e os fixtures de telemetria.
- [ ] Testes de falha incluem timeout após efeito externo, crash entre passos e quota insuficiente.
- [ ] Atualizar manual com limites de metadados/magnet, pacotes e legendas; não afirmar cobertura de fontes que não foi testada.

Executar `make test-unit`, `make test-contract` e `make test-integration` para este subsistema; salvar evidências sanitizadas e commitar por tarefa. Iniciar biblioteca real somente após O01 estabelecer backup e restauração.
