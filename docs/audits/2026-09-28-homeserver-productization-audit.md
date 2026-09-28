# Auditoria de produtização do HomeServer

Data: 2026-09-28. Escopo: diagnóstico e planejamento, sem mudanças operacionais.

> Esta é a fotografia anterior à implementação. Achados e medições abaixo são
> preservados como baseline; consulte o [registro de implementação e aceite](../evidence/productization-acceptance.md)
> para o tratamento de A01–A16 e as limitações de validação atuais.
> Decisão posterior: CI/CD foi retirado a pedido do operador. As recomendações
> de pipeline nesta fotografia não são requisitos da operação atual.
> A decisão posterior também retirou instalador, releases e backups próprios;
> o projeto pessoal usa [Compose raiz](../installation.md) e defaults técnicos
> no código. Requisitos estruturais abaixo pertencem ao plano anterior.

**Conclusão:** a automação principal tem implementação substancial e boa cobertura de testes, mas a instalação atual ainda depende de conhecimento e ajustes específicos do servidor. O projeto não está pronto para que outra pessoa faça um clone, configure um `.env` e reproduza o ambiente. A prioridade é fechar essa distância, preservando o fluxo de mídia já utilizado.

Documentos desta entrega:

- [Design de produtização e contrato de configuração](../superpowers/specs/2026-09-28-homeserver-productization-design.md).
- [Plano de implementação, dependências e critérios de aceite](../superpowers/plans/2026-09-28-homeserver-productization.md).

## 1. Base auditada e limites da evidência

| Item | Estado | Evidência |
|---|---|---|
| Código mais recente | verified | Worktree `.worktrees/download-gateway`, branch `codex/download-gateway`, commit `2cffa20798574b3b0916458e11b3461ffe8d58b3`; limpo no início da auditoria |
| Pasta principal aberta no IDE | verified | Branch `codex/implementation-safety`, commit `9cd97c1`; não contém todo o desenvolvimento recente |
| Branch padrão remota | verified | `git ls-remote --symref origin HEAD`: `codex/implementation-safety`, commit `9cd97c1` |
| `main` remota | verified | `91edaf9`; workflow de deploy exige CI de um push em `main` |
| Servidor ativo | verified | Consultado por SSH; release `dbbb85e3f6d505c6da97bcb38af505dbc113f999`; commit posterior `2cffa20` registra evidência |
| Containers | verified | 14 em execução; apenas Jellyfin e Byparr apresentavam healthcheck configurado |
| Configuração Compose | verified | Renderização dev e produção efetiva, incluindo override externo, sem erros no servidor |
| API de controle | verified | `/health/ready` retornou 200 |
| Painel web | verified | `/api/v1/status` retornou 200 |
| Telemetria antiga | verified | `/api/v1/telemetry` retornou 503 |
| Backup agendado | verified, limitado | Última execução de `homeserver-backup.service` terminou com sucesso em 2026-09-28 às 03:30:39 UTC; não comprova restauração |
| Restauração completa numa máquina vazia | unknown | Não executada nesta auditoria |
| CYD | reported | Usuário decidiu deixar de usar; retirada ainda não executada |
| Segurança de todo o histórico Git | unknown | Feita triagem limitada; scanner completo de conteúdo histórico e dependências ainda é necessário |

O Graphify local contém 47 nós do design antigo; serviu como índice inicial, não como prova do runtime atual. Foram conferidos fontes, Compose, scripts, workflows, testes e estado vivo. Não foram efetuados downloads, exclusões, reinícios ou alteração de configurações de produção nesta auditoria. Inventário bruto, bancos e credenciais não fazem parte destes documentos.

## 2. Verificação executada

Na worktree atual, em Linux/WSL com Python 3.12:

```bash
uv sync --frozen
uv run make lint test-unit test-contract test-integration smoke
```

| Verificação | Resultado |
|---|---|
| Dependências congeladas, Ruff e verificações de scripts do Makefile | Passaram |
| Unitários | 227 passaram |
| Contratos | 124 passaram |
| Integração | 172 passaram |
| Sistema em pytest | 9 passaram; 2 ignorados por indisponibilidade do Docker Compose no WSL |
| Scripts de smoke, montagem, layout, bootstrap e auditoria | Passaram |

Total: **532 testes pytest passaram, 2 ignorados**. Houve avisos de depreciação de Starlette/httpx e AnyIO; não houve falha de teste. A renderização Compose foi conferida separadamente no servidor. Isso não substitui instalação limpa, testes de falha de containers, restauração, reprodução em TV ou testes de compatibilidade com novas versões dos serviços.

## 3. O que merece ser preservado

- Gateway entre Arr e qBittorrent, permissões persistidas e checagem de capacidade antes de admitir efeitos externos.
- Guarda de montagem por UUID, isolamento dev/produção e proteção de caminhos de mídia.
- Capacidade baseada em tamanho conhecido e bytes ainda pendentes, sem reservar 80 GB fictícios por filme. Ver [capacity.py](../../services/control/src/homeserver_control/domain/capacity.py), [capacity_evidence.py](../../services/control/src/homeserver_control/worker/capacity_evidence.py) e [runtime.py](../../services/control/src/homeserver_control/worker/runtime.py).
- Sequência de temporadas/episódios, troca de fontes paradas, importação e exclusão coordenadas, com testes de integração e persistência.
- Preferência por remux/Blu-ray, fallback WEB-DL, preferência por Dolby Vision/Atmos e regras de legenda pt-BR/inglês. O problema principal nesta área é parametrização e coerência entre componentes.
- SQLite com WAL, operações idempotentes, recuperação bloqueando admissão e validação de artefatos de release.
- Monitoramento web já existente: não é necessário introduzir outro produto apenas para substituir a CYD.
- `.gitignore`, lockfile, CI e runbooks já existem. Devem ser corrigidos e organizados, não recriados do zero.

## 4. Achados priorizados

Prioridades: **P1** bloqueia instalação reproduzível ou operação confiável; **P2** melhora manutenção e experiência; **P3** evolução posterior. Não foi demonstrado incidente P0 nesta auditoria.

### A01 — P1 — Versão publicada, pasta do IDE e produção divergentes

O clone padrão do GitHub recebe a branch antiga. `main` também está atrasada, enquanto o deploy só aceita CI bem-sucedida em `main`. Há risco de corrigir a versão errada ou publicar uma instalação que não corresponde à usada em casa.

Evidência: referências locais e remotas acima;
[deploy.yml histórico na base auditada](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/.github/workflows/deploy.yml),
passo de seleção de CI. Ação proposta na auditoria: consolidar o desenvolvimento
em `main` com revisão, CI e histórico preservado; mudar a branch padrão depois
de validar a consolidação. CI foi posteriormente substituída por checks locais.
Não apagar worktrees ou branches antes da consolidação.

### A02 — P1 — Não há uma interface única de configuração

Há valores em `config/server.example.yaml`, `config/policy.yaml`, `config/versions.env`, envs parciais, Compose, systemd, constantes Python, arquivos secretos e bancos nativos dos aplicativos. `policy.yaml` ainda anuncia filme de 50 GB, temporada de 100 GB, piso de capacidade e dois downloads, incompatíveis com decisões posteriores. Não foi encontrado carregador desse YAML no runtime atual.

Evidência: [policy.yaml](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/config/policy.yaml), [dev.env.example](../../config/dev.env.example), [worker/__main__.py](../../services/control/src/homeserver_control/worker/__main__.py), [source_health.py](../../services/control/src/homeserver_control/worker/source_health.py), [release_quality.py](../../services/control/src/homeserver_control/worker/release_quality.py). As três variáveis de portas do exemplo dev não são consumidas fora dele; a porta de controle indicada também não corresponde ao Compose dev.

Ação: schema tipado, `.env.example` completo, validação, diagnóstico sanitizado e aplicação explícita. Remover fontes concorrentes após migração. Idiomas exigem também generalizar `SubtitleArtifactStore` e finalizadores, hoje restritos a `BR_PT`/`EN`.

### A03 — P1 — `.env` não controla preferências dos aplicativos nativos

Na leitura viva, qBittorrent usa 4 downloads, 8 uploads ativos, 12 torrents ativos, upload de 2.499.584 bytes/s e exclusão de torrents lentos da contagem. Esses valores estão na configuração nativa. Apenas passá-los como variáveis ao container não os altera. O mesmo problema se aplica a perfis de qualidade, idiomas, clientes e indexadores.

Ação: reconciliador idempotente de configurações gerenciadas, usando APIs dos aplicativos, com `plan`, `apply`, verificação por leitura e detecção de divergência. Preservar IDs e itens não gerenciados. Explicar que limites de fila e exclusão de torrents lentos interagem; 12 não é um teto absoluto de torrents carregados.

### A04 — P1 — Parte necessária da instalação existe somente no servidor

O Compose vivo inclui `/etc/homeserver/compose.override.yaml`. Ele modifica redes, portas, ambientes e volumes de diversos serviços. Seerr e Prowlarr, por exemplo, recebem ajustes de persistência pelo override. Existe também drop-in de systemd não reproduzido pelo repositório. O Compose base/produção sozinho não representa a instalação utilizada.

Evidência: labels/inspeção Docker e unit efetiva; [compose.yaml](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/deploy/compose.yaml), [compose.prod.yaml](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/deploy/compose.prod.yaml), [homeserver-stack.service](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/deploy/systemd/homeserver-stack.service). Ação: extrair a intenção dos overrides para templates versionados e valores locais no `.env`, sem publicar seu conteúdo bruto.

### A05 — P1 — Bootstrap ainda não provisiona uma máquina nova

`apply_missing()` registra `no-host-changes-needed` no modo adopt e `fresh-prerequisites-only` no fresh; não instala os componentes. A verificação exige serviços que já deveriam estar preparados. O README começa com um Legion já configurado.

Evidência: [bootstrap-server.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/bootstrap-server.sh), [lib/bootstrap.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/lib/bootstrap.sh), [README.md](../../README.md). Ação: separar pré-requisitos explícitos, preparação do host e instalação da aplicação; implementar fresh/adopt idempotentes, com simulação e verificação. Não automatizar formatação nem reescrever `fstab` ou políticas de energia existentes.

### A06 — P1 — Deploy e rollback não completam a troca operacional

`deploy.sh` valida/extrai o artefato e troca `current`; não constrói/obtém imagens, ativa a stack, executa migração, exige backup prévio ou confirma readiness. `rollback.sh` também troca apenas o link. `validate-release.py` verifica formato de digests e checksum do tar, mas não cruza as imagens efetivas/configurações com o manifesto nem executa a política `requires_backup`.

Evidência: [deploy.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/deploy.sh), [rollback.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/rollback.sh), [validate-release.py](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/validate-release.py), [release.schema.json](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/config/release.schema.json). Ação: pipeline completo e único para execução local e CI, com imagens imutáveis, schema compatível, backup quando necessário, ativação, smoke e recuperação de falhas. Não prometer rollback de banco incompatível apenas trocando o symlink.

### A07 — P1 — Política de reinício inconsistente e recuperação parcial de falhas

Treze containers estavam com `restart=no`; Byparr usa `unless-stopped`. O unit versionado usa `up --abort-on-container-exit`, mas o drop-in vivo usa `up` simples. Nesse arranjo, a queda de um container pode não derrubar o processo Compose nem acionar `Restart=always` do systemd. Não foi provocada falha para medir o comportamento nesta auditoria.

Ação: definir uma política coerente, testada para queda individual, reboot, backup e perda do disco. Reinício automático deve respeitar a guarda de montagem; não basta trocar todos para `unless-stopped`, pois o Docker poderia iniciá-los antes do supervisor/UUID guard.

### A08 — P1 — Restore testado não corresponde ao backup real

O backup de produção usa Restic e para a stack durante a captura, o que é uma medida de consistência válida. Já os testes principais de backup/restore exercitam `backup.sh`/`restore.sh`, que usam diretórios e `manifest.sha256`; o runbook identifica esse fluxo como antigo/fixtures. Não foi demonstrada a restauração completa do backup Restic numa instalação vazia.

O pull externo usa rsync do repositório, exclui locks e aplica retenção/prune independente no destino. Há risco de corrida com alterações/prune da origem e a sincronização não se torna atômica por terminar com `restic check`. Não foi constatada corrupção; falta teste e coordenação. A chave atual restrita a rsync também precisa ser considerada ao mudar o transporte.

Evidência: [backup-restic.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/backup-restic.sh), [pull-backup-wsl.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/pull-backup-wsl.sh), [restore.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/restore.sh), [test_backup_restore.py](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/tests/integration/test_backup_restore.py), [runbook](../runbooks/backup-restore.md). Ação: testar o caminho Restic real, usar cópia de snapshots entre repositórios ou espelho coordenado, parametrizar retenção/horários e realizar ensaio isolado. A documentação oficial prevê [cópia de snapshots entre repositórios](https://restic.readthedocs.io/en/stable/045_working_with_repos.html#copying-snapshots-between-repositories).

### A09 — P1 — Portabilidade depende do hardware, caminhos e contas atuais

Paths `/srv`, `/etc/homeserver` e `/opt/homeserver`, usuário do backup, dispositivo Intel e portas estão espalhados. O Compose produção exige mapeamento de render device mesmo em máquina sem GPU Intel. O exemplo de inventário assume serviço Lenovo; dashboard aceita especificamente hostname Tailscale e usa portas fixas.

Evidência: [server.example.yaml](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/config/server.example.yaml), [compose.prod.yaml](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/deploy/compose.prod.yaml), [pull-backup-wsl.sh](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/scripts/pull-backup-wsl.sh), [telemetry/app.py](../../services/telemetry/src/homeserver_telemetry/app.py). Ação: baseline Ubuntu/amd64 com reprodução por CPU; Intel opcional e validado; caminhos, UID/GID, rede e URLs derivados do `.env`. Preservar configuração de energia já existente no Legion sem torná-la obrigatória em outra máquina.

### A10 — P1 — Preparação para publicação ainda incompleta

`.gitignore` já protege `.env`, envs locais, backup, bancos principais, PEMs e diretórios de trabalho. Faltam `.dockerignore`, cobertura explícita de artefatos de execução/chaves auxiliares e controles contínuos para segredos. Não há política de segurança/contribuição ou licença de distribuição identificada.

Triagem: 240 arquivos rastreados; busca por alguns padrões de tokens/chaves encontrou somente nomes de funções de teste, conferidos como falsos positivos. Busca por nomes sensíveis em objetos históricos não retornou candidatos. Isso **não equivale a varredura completa de segredos no histórico**, nem avalia todas as senhas curtas. Não foi confirmado vazamento nesta auditoria.

Ação: scanner de segredos no histórico e CI, `.dockerignore`, exemplos sanitizados, auditoria de dependências/imagens e revisão de metadados pessoais. O Git esclarece que [ignorar um arquivo não remove conteúdo já rastreado](https://git-scm.com/docs/gitignore). Se houver exposição, revogar o segredo antes de qualquer saneamento de histórico; não reescrever histórico automaticamente.

### A11 — P2 — CYD e MQTT permanecem na arquitetura e no contrato antigo

Há firmware, runbook de flash, ACL do cliente CYD, broker/certificados e limite de payload de 8 KiB. `SnapshotPublisher`, `NotificationService` e `SeedBandwidthPolicy` têm testes, mas não foram encontradas chamadas de produção que os componham. O endpoint antigo de telemetria depende de `snapshot.json` e retorna 503; o painel web usa `host.json`/`capacity.json` e funciona.

Evidência: [firmware/cyd](../../firmware/cyd), [publisher.py](https://github.com/reisguilherme/homelab-mediaserver/blob/2cffa20798574b3b0916458e11b3461ffe8d58b3/services/telemetry/src/homeserver_telemetry/publisher.py), [app.py](../../services/telemetry/src/homeserver_telemetry/app.py), [notifications.md](../runbooks/notifications.md). Ação: retirar CYD da documentação normativa e do runtime. Confirmar consumidores antes de remover MQTT; manter métricas e painel HTTP. Decidir explicitamente quais alertas serão ligados ao runtime, removidos ou marcados como não implementados.

### A12 — P2 — Documentação fragmentada não guia o ciclo completo

README curto, runbooks especializados e evidências de várias datas não deixam claro qual comportamento é vigente. Faltam quickstart verificável, diagrama do fluxo, tabela de portas/URLs, catálogo do `.env`, configuração inicial dos aplicativos, atualização, recuperação e diagnóstico por etapa de uma request.

Ação: README como ponto de entrada; separar guias atuais de histórico. Registrar que `requested`, download a 100%, validação/legendas, importação e disponibilidade no Jellyfin são etapas diferentes. Explicar hardlinks, espaço físico e seeding para que cópias aparentes não sejam confundidas com duplicação física.

### A13 — P2 — Falta diagnóstico operacional de ponta a ponta

Existe readiness da API de controle, mas não há healthcheck Docker para worker/gateway/telemetria; um worker sem variáveis essenciais pode construir ciclo `None` e continuar vivo. Endpoint HTTP vivo não prova progresso da fila. A rotina de upload adaptativo tem classe testada, porém não há integração de runtime comprovada.

Ação: heartbeat com etapa atual e último ciclo completo, motivo de espera/rejeição, próxima tentativa, saúde de dependências e versão/configuração efetiva sanitizada. Evitar declarar worker travado só por uma busca longa; medir deadline por operação e progresso. Manter fail-closed para admissão sem evidência de capacidade.

### A14 — P2 — Logs e crescimento operacional sem política explícita

Todos os containers inspecionados usam `json-file` com opções vazias; não havia rotação explícita na configuração observada. O backup inclui todos os releases; retenção de releases, cache, transcode e tamanho de logs não estão centralizados. Não foi comprovado esgotamento de disco por logs.

Ação: rotação parametrizada, limites e métricas por categoria, limpeza somente de artefatos temporários comprovadamente descartáveis. Manter mídia e torrents fora de qualquer limpeza automática; preservar releases referenciados por rollback e backups.

### A15 — P2 — CI valida muita lógica, mas não a promessa de instalação

CI cobre lint, testes e Compose, mas não instala o produto do zero, não gera a release consumida pelo deploy e não valida restore Restic completo. `uv` é instalado sem versão fixa no workflow; não há matriz CPU/Intel opcional ou conferência automática de catálogo/env. Foram vistos avisos de depreciação nas dependências de testes.

Ação: smoke de instalação com fixtures sem Internet/trackers, verificação de configuração por alteração real, testes Restic isolados, build de imagens e scanner de dependências. Não concluir que imagens estão vulneráveis apenas pela idade de seus tags; obter inventário/digests e resultados de scanner antes de recomendar atualização específica.

### A16 — P1 — Buscar substituta após cinco minutos, mantendo a fonte atual durante a busca

Atualização de requisito em 2026-09-28, solicitada pelo usuário após o diagnóstico de lentidão de The Rookie. **Este registro altera o comportamento planejado; não representa ativação da regra em produção.**

Evidência do diagnóstico: o teste HTTP dentro do container do qBittorrent atingiu aproximadamente 266 Mb/s, enquanto S02E04 recebia cerca de 340–440 KB/s de cinco a seis seeds conectados, sem limite de download ativo. A configuração de código observada usa janela de 3.600 segundos abaixo de 1 MiB/s. A busca encontrou uma alternativa maior, com mais seeds anunciados, mas sua velocidade de transferência não foi medida. Esses números não comprovam que substituí-la terminaria antes.

Requisito atualizado:

- Após **5 minutos de lentidão sustentada**, iniciar busca por alternativa para o mesmo filme/episódio. Default proposto: média abaixo de 1 MiB/s; janela e limiar editáveis pelo `.env`.
- **Manter o download atual ativo enquanto procura e avalia alternativas.** Nenhum resultado, timeout, indexador indisponível ou falta de espaço para avaliar uma candidata deve interromper a fonte que ainda está avançando.
- Preservar a qualidade de imagem exigida: fontes/resoluções permitidas, ordem de preferência e edição compatível. Não baixar resolução ou aceitar uma fonte pior apenas para ganhar velocidade.
- Mais seeds anunciados ajudam a selecionar candidatas, mas não comprovam velocidade. A troca precisa de evidência de transferência e vantagem de tempo considerando tamanho, progresso atual e custo de começar outro arquivo.
- Confirmar a nova fonte antes de abandonar a anterior; se a avaliação falhar ou não mostrar ganho, continuar a atual. Respeitar gateway, espaço disponível e sequência de temporadas/episódios.
- **Excluir da aplicação dessa mudança o episódio já em andamento no momento do pedido: The Rookie S02E04.** A exceção deve ser persistida por identificação da aquisição no estado local antes de ativar a nova regra, sem pausar, reiniciar ou substituir esse download. Não estender a exceção automaticamente aos próximos episódios.

Evidência de código: [source_health.py](../../services/control/src/homeserver_control/worker/source_health.py), [acquisition.py](../../services/control/src/homeserver_control/worker/acquisition.py) e [series_acquisition.py](../../services/control/src/homeserver_control/worker/series_acquisition.py). Hoje a fonte só é parada no despacho da substituição, mas a candidata ainda não teve sua velocidade real comprovada nessa etapa. Portanto, somente mudar `3600` para `300` não cumpre o requisito inteiro.

Ação: ampliar P04 com busca antecipada, avaliação controlada da candidata, continuidade da fonte atual e proteção persistida do episódio em andamento. Critério de aceite: sem candidata comprovadamente vantajosa, a fonte lenta permanece baixando; a busca começa após cinco minutos, sem mudança involuntária de qualidade ou ordem.

## 5. Ordem recomendada

1. Consolidar a versão correta e registrar baseline sanitizado, sem mudar comportamento de mídia.
2. Criar contrato `.env` e aplicação de configurações nativas; preservar os valores atuais na migração.
   Incluir a política de cinco minutos de A16, com continuidade da fonte atual e exceção do episódio em andamento.
3. Incorporar overrides e retirar CYD; produzir instalação CPU portável e perfil Intel opcional.
4. Completar supervisão, release/deploy/rollback e restore Restic.
5. Publicar README e guias testados em instalação limpa, com controles de Git/CI.

A documentação começa junto com a configuração, e não somente na última etapa. Cada fase termina com uma evidência de aceite descrita no plano. A remoção da CYD tem prioridade funcional, embora seu risco técnico seja menor que o de deploy/configuração divergentes.

## 6. Fora do escopo desta auditoria

Não foi executado benchmark de rede, pentest, restore de produção, mudança de branch padrão, publicação, atualização de imagens, exclusão de dados nem desativação da CYD/MQTT. Esses itens não são implicitamente classificados como aprovados ou comprovados. O design e o plano anexos descrevem o trabalho futuro.


## Adendo durante a implementação: proteção contra releases pequenas

O pedido de 28/09 acrescentou rejeitar encodes muito pequenos mesmo quando
anunciam 1080p e muitos seeds. O filtro anterior verificava fonte/resolução,
mas não uma relação mínima entre vídeo e duração. A implementação adiciona
pisos editáveis no `.env` e aplica o mesmo contrato ao Arr e ao seletor do
worker, incluindo alternativas por lentidão. Os bytes vêm do vídeo principal
do torrent, sem somar samples/sidecars. Duração desconhecida não é inventada;
nenhum teto fixo de 80 GB é reintroduzido. Veja [configuração](../configuration.md)
e o [registro de aceite](../evidence/productization-acceptance.md).
