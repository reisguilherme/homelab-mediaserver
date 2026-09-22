# Revisão da implementação — 22/09/2026

## Incremento executado nesta revisao

Apos o diagnostico inicial, o branch codex/implementation-safety recebeu correcoes de seguranca e operacao: fixtures de FFprobe portaveis, permits atomicos em memoria/SQLite com digest de metadado, readiness dependente de banco/capacidade, restore confinado por caminho canonico, worker fail-closed com reservas persistentes, extracao segura de releases e staging de backup sem reenvio duplicado. O workflow de producao agora continua manual, exige manifesto, valida CI bem-sucedido para o SHA exato em main e usa Dockerfiles com uv sync --frozen.

Os testes regressivos foram escritos antes das correcoes. A validacao Linux final passou; os bloqueios de integracao real e hardware continuam abertos.

## Parecer

O projeto tem uma base de código e testes útil, mas os planos não foram integralmente executados. O estado observado é de implementação parcial, com componentes isolados e fluxos de produção ainda incompletos. Não há evidência suficiente para aprovar G0–G6 ou liberar aquisições reais.

Referencias: design v1.3 de 21/09/2026 e os cinco documentos em docs/superpowers/plans. A analise considerou o checkout atual e nao acessou nem alterou servicos do Legion. As correcoes descritas acima estao no Git; nenhuma evidencia fixture e tratada como prova fisica.

## Validacao executada

Foi criada uma copia temporaria em filesystem Linux do WSL Ubuntu 24.04, com Python 3.12.3 e uv sync --frozen.

| Verificacao | Resultado observado |
|---|---|
| make lint | Ruff em services/tests/scripts, compileall e sintaxe Bash passaram; ShellCheck nao estava instalado e foi pulado pelo Makefile |
| make test-unit | 35 passaram |
| make test-contract | 20 passaram |
| make test-integration | 19 passaram |
| make smoke | 2 testes de politica e quatro scripts Bash passaram |
| suites Windows (unit/contract/integration + politica) | 71 passaram, 5 skips esperados por filesystem Linux |
| Compose dev renderizado no Docker CLI Windows | passou |
| build de imagens e validacao fisica | ainda nao executados |

Total pytest Linux: **76 passaram**. Os avisos de deprecacao do TestClient nao foram causa de falha.

## Achados prioritários

### R01 — P1: o worker não executa o ciclo de automação

Na revisão inicial, `services/control/src/homeserver_control/worker/__main__.py:29` apenas esperava com `time.sleep`. O incremento atual adicionou `worker/runtime.py` e um ciclo opcional de polling do Seerr que cria reservas persistentes. Ainda não instancia seleção, dispatch, reconciliação de downloads, validação, importação, exclusão ou emissão persistente de eventos. A API mantém fila, catálogo e eventos em memória; o processo worker separado ainda não compartilha esses objetos.

Consequência: com as variáveis de integração ausentes, subir os containers continua em modo ocioso seguro; mesmo configurado, o ciclo atual termina em pedido → reserva. O fluxo reserva → download → disponível ainda não é executado. C01–C06 permanecem parcialmente implementadas. As telas HTML são textos estáticos e não apresentam fila dinâmica, login por sessão ou confirmação operacional de exclusão.

Ação: implementar a composição dos serviços e máquina de estados persistente; testar o fluxo completo e retomada após reinício em processos separados.

### R02 — P1: gateway não está conectado ao cliente real e não verifica o conteúdo autorizado

`gateway/app.py:126` cria `PermitRegistry()` em memória e `UnconfiguredQbitClient()`. O Compose não injeta um adapter configurado. Mesmo na factory testável, `gateway/app.py:87` confia em `x-infohash`; não deriva o hash dos bytes recebidos nem compara `metadata_sha256`, seleção e orçamento do permit. O parser de domínio recebe um dicionário pronto, não decodifica/verifica um arquivo torrent real.

Reprodução sem rede: um permit com hash e orçamento definidos aceitou `b'not-a-torrent'` via multipart e chamou o upstream simulado, retornando HTTP 200. Isso comprova falha de validação na fronteira, não um download real.

O adapter qBittorrent exige URL, enquanto o multipart do gateway produz `torrent_bytes`; conectar as classes diretamente ainda não resolve o contrato. URLs HTTP(S) também não têm allowlist de destino no adapter. O gateway expõe apenas versão e adição, faltando os contratos de autenticação/consulta necessários ao uso como cliente Arr. `upstream-versions.json` declara explicitamente `development-fixtures-only` e `production_verified: false`.

Ação: fechar C01 com contratos observados nas versões escolhidas, usar exatamente os metadados verificados e implementar a validação integral de C04 antes de liberar o upstream.

### R03 — P1: permits exigem reconciliacao externa antes da producao

gateway/permits.py agora usa locks por token em memoria e uma tabela SQLite com transicao atomica authorized -> dispatching -> confirmed/unknown. O estado sobrevive a reinicio quando o gateway recebe HOMESERVER_DB_PATH, e uma segunda chamada retorna o resultado confirmado ou bloqueia um efeito cujo resultado ficou incerto. O digest de metadado tambem e comparado ao permit.

Ainda falta reconciliar unknown por identidade no qBittorrent e integrar a emissao dos permits ao scheduler persistente; portanto a garantia nao esta pronta para aquisicao real.

Acao: implementar a consulta por infohash/categoria antes de qualquer repeticao e ligar a emissao ao fluxo de reserva.

### R04 — P1: deploy e rollback alteram o marcador sem implantar a aplicação

O incremento atual passou a exigir um tarball de release, validar e extrair seus caminhos sem links/`..`, verificar `deploy/compose.yaml` e scripts operacionais, e só então trocar `current`. Ainda não prepara imagens, valida montagem, captura backup, migra banco, controla systemd ou executa smoke contra um host real; essas etapas continuam abertas. A unidade `homeserver-stack.service` só será utilizável quando o artefato tiver a estrutura esperada.

`rollback.sh:23` apenas troca o symlink; o snapshot informado só é impresso. `validate-release.py` aceita checksums de configuração vazios e não exige o mapa completo dos serviços. `smoke.sh` pode anunciar sucesso em dev sem consultar qualquer serviço quando a URL de saúde não foi configurada.

Ação: completar I04/O02 com um artefato extraído e validado, atualização real dos processos e ensaios de falha/migração incompatível. O marcador de release só deve avançar após a validação exigida.

### R05 — P1: backup não implementa consistência, transporte e retenção planejados

`scripts/backup.sh:54` ainda executa `cp -a` sem parar a stack ou usar exportação consistente dos bancos, e o envio continua sendo uma cópia local sem Restic/SFTP. O incremento atual marca `.sent` também na origem, evita recópia e poda gerações enviadas conforme os limites de staging. O teto por snapshot e a poda não substituem a consistência dos bancos nem o transporte externo.

Consequência: os testes provam cópia/checksum de fixtures, não recuperação consistente de SQLite/WAL e dos bancos de todas as aplicações, nem envio com desktop offline.

Ação: implementar O01 antes de introduzir biblioteca real: captura consistente com recuperação do estado da stack, staging limitado e envio externo criptografado com retenção.

### R06 — P1: restore e recovery agora bloqueiam a aplicacao propria

restore.sh agora canonicaliza raiz e destino, exige caminho absoluto, recusa .., symlinks ancestrais e destinos fora de BACKUP_RESTORE_ROOT. O teste de regressao cobre o escape allowed/../escaped.

API, worker e gateway agora leem RECOVERY_MODE no inicio/antes de mutacoes. Um marcador existente sem admission_enabled=true mantem readiness 503 e rejeita mutacoes do gateway; health/live permanece disponivel. A remocao ou liberacao continua exigindo reconciliacao manual.

Acao: testar uma instancia restaurada com os tres processos, depois validar UUID, schema, reservas e efeitos externos antes de remover o marcador.

### R07 — P1: Compose bloqueia saída externa e contém inconsistências de volumes

Todas as redes em `deploy/compose.yaml:3` são `internal: true`, sem rede adicional de saída. Isso impede a conectividade externa necessária a indexadores, peers e metadados no desenho atual. O isolamento entre Arr e qBittorrent deve ser mantido ao corrigir a saída.

No Compose dev renderizado, Jellyfin recebe simultaneamente o bind em `/data` e o volume nomeado em `/data/media`; o mount mais específico encobre a biblioteca esperada. Os caminhos relativos padrão resolvem para `deploy/.runtime/dev`, enquanto a documentação descreve `.runtime/dev` na raiz. Worker e gateway não recebem o mesmo bind de estado da API no override dev.

Em produção, Seerr, Prowlarr, telemetria e Mosquitto mantêm volumes nomeados em vez de todos os diretórios `/srv/appdata` previstos. Isso precisa ser reconciliado com o manifesto de backup. As identidades e permissões por imagem também não estão configuradas conforme I03.

Ação: validar o Compose combinado por ambiente, corrigir mounts/egress/persistência e ensaiar permissões e hardlinks dentro dos containers.

### R08 — P1: readiness foi endurecida, mas ainda depende do processo operacional

api/app.py agora exige tokens, raizes/planner, caminho de banco inicializado e snapshot de capacidade com filesystem, total/free e timestamp validos. Os testes cobrem ausencia de banco e capacidade.

Ainda falta distinguir liveness, readiness e recovery e registrar a saude do worker separado. Uma resposta 200 local nao substitui a montagem UUID real.

Acao: incorporar estado do worker e idade maxima da medicao no smoke e validar no Legion.

### R09 — P1: telemetria e notificações não têm execução integrada

`homeserver_telemetry/app.py:33` lê `/run/homeserver/snapshot.json`; não há processo que produza o snapshot agregado. O serviço de métricas escreve `host.json` no host, enquanto o Compose monta um volume Docker nomeado em `/run/homeserver`, sem ligação ao diretório do host. `collect_sources`, `SnapshotPublisher` e `NotificationService` existem, mas não são chamados pelo entrypoint.

Eventos e ack da API estão em memória; a rota de limite de upload apenas salva um dicionário, sem aplicar o valor ao qBittorrent. O firmware CYD tem base Wi-Fi/MQTT, mas não assinatura/renderização de snapshot, telas/touch ou indicação de dados antigos.

Ação: completar M01/M02 com coleta periódica, snapshot, cursor/outbox persistentes e transportes reais; depois implementar e validar M03 na revisão exata da placa.

### R10 — P1: base Linux e imagens foram corrigidas; builds ainda precisam de prova

As fixtures de FFprobe usam o interpretador Python, e make lint agora inclui scripts. Os Dockerfiles copiam uv.lock, instalam uv fixado e executam uv sync --frozen --no-dev --no-install-project; o control tambem instala FFmpeg.

A suite Linux passou. Ainda falta construir as imagens em um daemon Docker e provar que os processos reais iniciam com os healthchecks. O CI agora executa o alvo smoke completo, incluindo os scripts Bash.

### R11 — P1: workflow de deploy agora exige CI aprovado e manifesto

.github/workflows/deploy.yml permanece somente workflow_dispatch, serializado sem cancelamento. O job valida SHA lowercase, confirma o checkout, consulta HomeServer CI concluido com sucesso para o mesmo SHA em main, aceita somente caminhos do workspace ou HTTPS, transfere artefato e manifesto e chama deploy.sh --manifest sem interpolar inputs diretamente em comandos shell.

A publicacao automatica de artefatos pelo CI, a identidade do host e a transferencia Tailscale/SCP ainda nao foram verificadas em uma conta/Legion reais. O deploy continua manual e fail-closed.

Acao: adicionar build/publicacao identificada no CI quando o contrato de release estiver fechado e fazer um ensaio manual com host/known-hosts administrados.

### R12 — P2: bootstrap e evidencias ainda nao correspondem ao aceite

scripts/lib/bootstrap.sh ainda verifica apenas um subconjunto das dependencias; apply_missing registra estados sem instalar requisitos, e check_power nao prova a politica efetiva. A auditoria fixture nao e evidencia fisica.

acceptance.md continua sem matriz individual A01-A34. O repositorio agora esta inicializado e os commits sao rastreaveis, mas bootstrap, inventario real e aceite permanecem abertos.

Acao: separar relatorios fixture dos reais, classificar cada observacao como reported, verified, missing ou unknown e preencher a matriz por ambiente.

## Estado dos subplanos

| Subplano | Avaliação |
|---|---|
| I01–I05 — infraestrutura | Guardas e configuração inicial existem; bootstrap, Compose e deploy precisam correção; hardware não validado |
| C01–C06 — automação | Domínio e parte da persistência testados; contratos reais, worker integrado, admissão e exclusão coordenada incompletos |
| M01–M03 — monitoramento | Modelos/outbox/helpers existem; coleta/publicação/consumo e painel ainda incompletos |
| O01–O04 — operação | Scripts iniciais e documentação existem; backup consistente, restore seguro, release efetiva e aceite não concluídos |

## Próximos passos recomendados

1. **Estabelecer a base Linux reproduzível:** preservar o trabalho atual, registrar arquivos em Git após revisão de segredos, definir checkout canônico no WSL e corrigir R10. Critério: suíte Linux e build das imagens com lockfile aprovados.
2. **Fechar a prova de arquitetura G2 em dev:** corrigir Compose, capturar contratos nas versões fixadas e demonstrar Arr → gateway → qBittorrent com torrent de teste autorizado. Provar bloqueio de todas as rotas sem autorização e rejeição de metadados adulterados.
3. **Completar o controlador/G3:** worker, permits persistentes, reconciliação, catálogo/fila/eventos e exclusão coordenada. Ensaiar filme, temporada completa e em lançamento, falta de espaço, concorrência e crash sem efeitos duplicados.
4. **Concluir recuperação e release:** R04–R06, R08 e R11; demonstrar backup consistente, restore isolado sem admissão, deploy real e retorno após migração incompatível. Fazer isso antes de introduzir biblioteca real.
5. **Concluir a experiência:** coleta, MQTT, notificações e limite de upload; UI operacional; firmware CYD após identificar placa/tela/touch.
6. **Validar no Legion:** inventário e UUID reais, UID/GID, hardlinks dentro dos containers, Intel UHD, SSH/Tailscale, destino Windows/SFTP, duas sessões de reprodução e ensaio de 48–72 horas. Preencher A01–A34 com data, versão e evidência.

Não é necessário descartar a base existente. O trabalho prioritário é completar e integrar os componentes, corrigindo as garantias de admissão/recuperação antes de tratar a implantação física como próximo marco.
