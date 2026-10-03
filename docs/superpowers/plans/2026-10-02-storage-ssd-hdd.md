# SSD primeiro, HD USB como expansão — plano de implementação

> **For agentic workers:** Use superpowers:subagent-driven-development. Execute os testes em Linux/WSL, com fixtures temporárias.

**Goal:** Escolher um disco que comporte o torrent inteiro, preferindo SSD, e manter o fluxo de mídia e a visibilidade de ambos os discos.

**Architecture:** Permissões de download registram o pool físico e um diretório exclusivo. Uma visão mergerfs mantém os caminhos lógicos existentes para Arr/Jellyfin; a decisão de capacidade usa cada filesystem físico. A instalação atual permanece funcional sem o cadastro de expansão.

**Tech Stack:** Python 3.12, SQLite, Docker Compose, ext4, NTFS, mergerfs, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-storage-ssd-hdd-design.md`.

**Atualização de escopo:** o usuário autorizou formatar o HD após o NTFS recusar
escrita. A instalação atual usa ext4 em ambos os pools. Montagens e fixtures físicas
passaram; o usuário autorizou explicitamente as duas entradas no fstab para o boot,
preservando as existentes. A unidade gerada pelo systemd iniciou corretamente.
As restrições abaixo registram
o ponto de partida; esta autorização se aplica somente ao HD desta instalação.

## Global Constraints

- Não formatar, particionar, migrar ou apagar mídia existente.
- A ativação de mergerfs continua sujeita à exceção explícita do AGENTS.md; preparar e validar código não ativa montagens no servidor.
- UUIDs reais ficam somente na instalação; credenciais e preferências continuam no `.env`.
- Incluir bytes restantes da fila pausada; não somar espaço de discos diferentes para admitir um torrent.
- Nunca considerar um diretório vazio, bind Docker ou tamanho total como prova de identidade física.
- HD ausente, trocado, somente leitura ou com evidência vencida não recebe downloads nem confirma exclusões.
- Preservar importação sequencial, pacotes de temporada, gateway, legendas e exclusão explícita existente.
- Não reintroduzir CI/CD, releases, gerenciador de backups ou units próprias.

## Contratos compartilhados

Novo módulo `homeserver_common.storage` concentra cadastro e identidade física. `load_storage_registry(path=Path('/run/homeserver/storage.json'))` retorna `StorageRegistry | None`: ausência preserva modo legado; arquivo inválido gera erro. Registro versionado contém somente os dois pools fixos `ssd` e `hdd`, UUID esperado e prova de capacidades. Layout técnico: `/storage/ssd` e `/storage/hdd/homeserver` nos containers; host `/srv/data` e `/srv/external/homeserver`; visão `/srv/media-view` exposta como `/data`.

`StorageRegistry.inspect(pool_id, *, writable=False)` retorna amostra física validada ou lança `StorageUnavailable`. Inspeção confronta UUID, montagem real do host e dispositivo do bind. `prepare_destination(pool_id, permit_id)` cria somente `/data/torrents/.placements/<permit_id>` no pool escolhido e devolve esse caminho lógico; pais protegidos impedem recriação pelo qBit no outro disco. `resolve(pool_id, logical_path, *, writable=False)` retorna caminho físico seguro após a mesma validação. Assinaturas adicionais necessárias devem ser comunicadas antes de consumo por outra tarefa.

Snapshot mantém campos legados do SSD e adiciona `pools`, lista de objetos com `pool_id`, `label`, `filesystem_id`, `state`, `reason`, `measured_at`, `total_bytes`, `used_bytes`, `free_bytes`. Estado válido é `ready`; indisponibilidade usa números nulos. Host snapshot adiciona uso por categoria em cada pool, mantendo CPU/rede independentes.

`CapacityEvidence` preserva construtor legado e recebe evidência por pool. Cada permit ganha `pool_id` (legado `ssd`) e `filesystem_id`; o destino fica persistido. A transação SQLite faz seleção/admissão; gateway deriva destino da permissão autenticada, nunca do path enviado pelo Arr. Pool da reserva de temporada não representa o pool de todos os episódios.

## Review Focus

1. HD desaparece depois da admissão: não criar o mesmo destino no SSD ou disco do SO.
2. Downloads pausados e permissões concorrentes: não comprometer duas vezes o mesmo espaço livre.
3. Episódios de uma série em dois pools: importar na ordem e manter legendas junto do vídeo.
4. Exclusão pendente durante desconexão: ausência não significa exclusão concluída.
5. Reinício sem cadastro de expansão: preservar caminhos e comportamento da instalação legada.

### Task 1: Identidade física e preparação de destinos

**Files:** novo `services/common/src/homeserver_common/storage.py`, `tests/unit/test_storage_pools.py`.

- [x] Criar testes de registro ausente/inválido, UUID errado, montagem ausente/RO, bind em diretório comum, path traversal, symlink e preparação exclusiva.
- [x] Executar testes e registrar falha inicial.
- [x] Implementar inspeção com dependências de filesystem injetáveis para fixtures; nunca inventar evidência física em produção.
- [x] Implementar resolução e preparação no pool selecionado, com recusa de contraparte e pais sem permissão de criação para UID 1000.
- [x] Validar testes e Ruff; registrar contrato e evidência para consumidores.

### Task 2: Admissão e roteamento por disco

**Files:** `worker/capacity_evidence.py`, `gateway/permits.py`, `gateway/probe_permits.py`, `gateway/app.py`, `adapters/qbittorrent.py`, `worker/acquisition.py`, `worker/series_acquisition.py`, testes correspondentes.

- [x] Testar SSD suficiente, fallback HDD, soma insuficiente por disco, HDD indisponível, fila pausada, download parcial e concorrência.
- [x] Estender evidência e migração SQLite de permits, preservar hashes/tokens já emitidos.
- [x] Selecionar pool dentro de BEGIN IMMEDIATE e persistir destino antes do envio ao qBit.
- [x] Encaminhar destino autorizado, AutoTMM desativado e incompletos no próprio destino; validar pool em retomada e probes.
- [x] Ajustar pré-filtros de tamanho para considerar qualquer pool individual, sem soma.
- [x] Executar testes de gateway, aquisição, capacidade e pacotes; registrar resultados.

### Task 3: Painel e coleta independente

**Files:** `scripts/host-metrics.py`, `scripts/capacity-snapshot.py`, `services/telemetry/src/homeserver_telemetry/status.py`, `templates/status.html`, testes e contrato do painel.

- [x] Testar dois pools independentes, HDD ausente com CPU/rede atualizados, erro/staleness/identidade, uso físico deduplicado por hardlink.
- [x] Publicar snapshots por pool usando contrato compartilhado; manter aliases SSD no modo legado.
- [x] Mostrar cards SSD e HD USB, usado/livre/fila/disponível e estado; números ausentes não viram zero.
- [x] Executar testes de métricas/status e validar HTML existente.

### Task 4: Importação, legendas, recuperação e exclusão

**Files:** `worker/finalization.py`, `worker/series_finalization.py`, `worker/deletion_coordinator.py`, `api/deletion_capture.py`, `worker/__main__.py`, consumidores de filesystem/destino e respectivos testes.

- [x] Testar fontes em destinos exclusivos, episódios entre pools, pacote inteiro, HDD desconectado, exclusão pendente e legendas no pool da fonte.
- [x] Resolver fonte a partir de permit.destination e validar identidade física; não inferir pool pelo st_dev da visão.
- [x] Impedir cópia silenciosa para outro pool; instalar mídia/legenda com hardlinks demonstrados.
- [x] Associar exclusão ao permit físico, preservar jobs legados e impedir confirmação se pool ausente.
- [x] Integrar cadastro no worker/gateway sem tornar indisponibilidade do HD uma falha de todos os pedidos SSD.
- [x] Executar testes de importação, legendas, ordem, pacotes e exclusão.

### Task 5: Compose e preparação documentada

**Files:** Compose opcional de expansão, renderizador, preparação da instalação, README, `docs/installation.md`, guia de armazenamento e testes de configuração.

- [x] Manter Compose básico e mídia atual funcionando; expansão opt-in exige cadastro e montagens verificadas.
- [x] Gerar configuração técnica privada de identidade a partir de montagens explicitamente informadas; não gravar UUID real no Git.
- [x] Preparar exemplo revisável de montagem NTFS e mergerfs sem substituir fstab nem ativar mergerfs nesta etapa.
- [x] Prover probe de fixture pequena para UID/GID, hardlink, rename, unlink e proteção contra pool ausente.
- [x] Documentar preparação, boot, ausência/reconexão, painel, limitações NTFS e comando Compose.
- [x] Rodar suíte completa, Ruff, Compose e revisão independente; ativação física somente após resolver a restrição explícita.

## Verificação inicial

Base `aa99f00`: 1.247 testes passaram no WSL/Python 3.12. HD inspecionado somente leitura e desmontado ao final; nenhuma mudança em mídia ou montagens persistentes.
