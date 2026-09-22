# Revisão da implementação — 22/09/2026

## Incremento executado nesta revisão

Após o diagnóstico inicial, o branch `codex/implementation-safety` recebeu uma primeira correção de base: fixtures de FFprobe portáveis, permits com transição atômica em memória/SQLite e digest de metadado, readiness dependente de banco/capacidade, restore confinado por caminho canônico e configuração do gateway para o banco compartilhado. Os testes regressivos foram escritos antes das correções; a validação Linux posterior passou. Os demais bloqueios deste documento continuam abertos.

O worker também passou a ter um ciclo explícito de reconciliação de pedidos Seerr aprovados para reservas persistentes. Ele permanece fail-closed quando URL, credencial ou snapshot de capacidade não estão configurados e não dispara Arr/qBittorrent; busca, validação, importação, eventos e retomada continuam etapas posteriores.

## Parecer

O projeto tem uma base de código e testes útil, mas os planos não foram integralmente executados. O estado observado é de implementação parcial, com componentes isolados e fluxos de produção ainda incompletos. Não há evidência suficiente para aprovar G0–G6 ou liberar aquisições reais.

Referências: design v1.3 de 21/09/2026 e os cinco documentos em `docs/superpowers/plans`. A revisão considerou os arquivos atuais, inclusive arquivos ainda não rastreados pelo Git. Nenhum serviço do Legion foi acessado ou alterado. Não foram feitas correções no código nesta revisão.

## Validação executada

Foi criada uma cópia temporária em filesystem Linux do WSL Ubuntu 24.04, em `/tmp/homeserver-review-Wip2QL/project`, com Python 3.12.3 e `uv sync --frozen`.

| Verificação | Resultado observado |
|---|---|
| `make lint` | Ruff, compilação Python e sintaxe Bash passaram; ShellCheck não estava instalado e foi pulado pelo Makefile |
| `make test-unit` | 25 passaram, 1 falhou |
| `make test-contract` | 15 passaram |
| `make test-integration` | 16 passaram, usando fixtures e filesystem local |
| `make smoke` | 2 testes Python de política do workflow passaram; não executa os scripts Bash de sistema |
| Quatro scripts em `tests/system/*.sh`, executados explicitamente | Todos passaram com fixtures |
| `make compose-check` no WSL | Pulado por indisponibilidade do Compose nesse ambiente |
| Render do Compose dev pelo Docker CLI Windows | Passou; o merge revelou problemas de mounts descritos abaixo |
| Testes unitários e de contrato no Python 3.12.14 Windows | 41 passaram; isso não substitui a execução Linux |

Total pytest no Linux: **58 passaram e 1 falhou**. Os avisos de depreciação do TestClient não foram a causa da falha. Não foram executados builds de imagens, integração com containers vivos, ShellCheck, ESPHome ou testes físicos.

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

### R03 — P1: permits não são persistentes nem atômicos

`gateway/permits.py:26` mantém um dicionário local. `authorize`, nas linhas 70–71, verifica resultado e executa o efeito sem lock ou transação. Campos de reserva são opcionais e não são conferidos no SQLite.

Reprodução local com duas threads e uma barreira: **duas chamadas ao efeito remoto para o mesmo permit**. Reiniciar o processo apaga o registro; uma falha após o efeito e antes de salvar o resultado deixa a operação sem reconciliação de resultado incerto.

Ação: usar a tabela persistente de permits, transições atômicas e reconciliação por identidade antes de repetir efeitos. Cobrir concorrência, crash e resposta perdida.

### R04 — P1: deploy e rollback alteram o marcador sem implantar a aplicação

O incremento atual passou a exigir um tarball de release, validar e extrair seus caminhos sem links/`..`, verificar `deploy/compose.yaml` e scripts operacionais, e só então trocar `current`. Ainda não prepara imagens, valida montagem, captura backup, migra banco, controla systemd ou executa smoke contra um host real; essas etapas continuam abertas. A unidade `homeserver-stack.service` só será utilizável quando o artefato tiver a estrutura esperada.

`rollback.sh:23` apenas troca o symlink; o snapshot informado só é impresso. `validate-release.py` aceita checksums de configuração vazios e não exige o mapa completo dos serviços. `smoke.sh` pode anunciar sucesso em dev sem consultar qualquer serviço quando a URL de saúde não foi configurada.

Ação: completar I04/O02 com um artefato extraído e validado, atualização real dos processos e ensaios de falha/migração incompatível. O marcador de release só deve avançar após a validação exigida.

### R05 — P1: backup não implementa consistência, transporte e retenção planejados

`scripts/backup.sh:54` executa `cp -a` sem parar a stack ou usar exportação consistente dos bancos. O envio é outra cópia local, sem Restic/SFTP. O teto é verificado depois de copiar e por geração; não há retenção de três gerações nem limite acumulado de staging. Na captura, `.sent` é criado no destino, mas não na origem; a próxima tentativa pode copiar a geração novamente para dentro do diretório já existente.

Consequência: os testes provam cópia/checksum de fixtures, não recuperação consistente de SQLite/WAL e dos bancos de todas as aplicações, nem envio com desktop offline.

Ação: implementar O01 antes de introduzir biblioteca real: captura consistente com recuperação do estado da stack, staging limitado e envio externo criptografado com retenção.

### R06 — P1: restore escapa da raiz permitida e recovery não bloqueia a aplicação

`scripts/restore.sh:35` confere prefixo textual e apenas o symlink do destino final. Não canonicaliza `..` nem valida symlinks nos ancestrais. Reprodução Linux com dados sintéticos: `allowed/../escaped` retornou 0 e escreveu fora de `BACKUP_RESTORE_ROOT`. Nenhum diretório de produção foi usado.

O arquivo `RECOVERY_MODE` escrito na linha 46 não é lido pela API, worker ou gateway. `ControlState.admission_enabled` começa como `True`. Portanto, o marcador não implementa o bloqueio de recuperação previsto em O01.

Ação: validar caminhos resolvidos e confinamento de origem/destino; implementar modo recovery efetivo nos processos e testá-lo iniciando uma instância restaurada.

### R07 — P1: Compose bloqueia saída externa e contém inconsistências de volumes

Todas as redes em `deploy/compose.yaml:3` são `internal: true`, sem rede adicional de saída. Isso impede a conectividade externa necessária a indexadores, peers e metadados no desenho atual. O isolamento entre Arr e qBittorrent deve ser mantido ao corrigir a saída.

No Compose dev renderizado, Jellyfin recebe simultaneamente o bind em `/data` e o volume nomeado em `/data/media`; o mount mais específico encobre a biblioteca esperada. Os caminhos relativos padrão resolvem para `deploy/.runtime/dev`, enquanto a documentação descreve `.runtime/dev` na raiz. Worker e gateway não recebem o mesmo bind de estado da API no override dev.

Em produção, Seerr, Prowlarr, telemetria e Mosquitto mantêm volumes nomeados em vez de todos os diretórios `/srv/appdata` previstos. Isso precisa ser reconciliado com o manifesto de backup. As identidades e permissões por imagem também não estão configuradas conforme I03.

Ação: validar o Compose combinado por ambiente, corrigir mounts/egress/persistência e ensaiar permissões e hardlinks dentro dos containers.

### R08 — P1: readiness pode declarar pronto sem invariantes essenciais

`api/app.py:125` testa tokens, raízes configuradas e existência do planner, mas não banco/schema, UUID, idade da medição de capacidade ou disponibilidade operacional do worker.

Reprodução: `ControlState` com diretório comum existente, dois tokens e sem banco nem UUID retornou **HTTP 200, `admission_enabled: true`**. Diretórios inexistentes são rejeitados pelo planner, mas um diretório existente não comprova a montagem física esperada.

Ação: tornar readiness dependente das invariantes persistentes e da medição validada; separar liveness, readiness e recuperação. Incorporar esses casos no smoke.

### R09 — P1: telemetria e notificações não têm execução integrada

`homeserver_telemetry/app.py:33` lê `/run/homeserver/snapshot.json`; não há processo que produza o snapshot agregado. O serviço de métricas escreve `host.json` no host, enquanto o Compose monta um volume Docker nomeado em `/run/homeserver`, sem ligação ao diretório do host. `collect_sources`, `SnapshotPublisher` e `NotificationService` existem, mas não são chamados pelo entrypoint.

Eventos e ack da API estão em memória; a rota de limite de upload apenas salva um dicionário, sem aplicar o valor ao qBittorrent. O firmware CYD tem base Wi-Fi/MQTT, mas não assinatura/renderização de snapshot, telas/touch ou indicação de dados antigos.

Ação: completar M01/M02 com coleta periódica, snapshot, cursor/outbox persistentes e transportes reais; depois implementar e validar M03 na revisão exata da placa.

### R10 — P1: CI Linux falha e imagens não reproduzem o ambiente testado

`tests/unit/test_probe.py:17` escreve um `.cmd` com `@echo` e tenta executá-lo diretamente no Linux. A falha reproduzida é `PermissionError`, encapsulada em `MediaProbeError`. O teste de JSON inválido também usa `.cmd` e pode passar pelo erro de execução em vez de verificar o parser.

Ambos os Dockerfiles usam `pip install` sem versões e não copiam `uv.lock`; as imagens não usam necessariamente as dependências da suíte. O Dockerfile control também não instala o FFprobe necessário à validação de mídia.

`make smoke` ignora os quatro testes `.sh`, e o CI não chama esse alvo. O teste de deploy verifica somente rejeição de um manifesto inválido; o de restauração usa texto chamado `control.sqlite3`, sem iniciar um controlador restaurado. Essas coberturas não comprovam O01/O02.

Ação: fixtures portáveis com o interpretador Python, instalação travada nas imagens, dependências de execução explícitas e testes dos processos/serviços reais.

### R11 — P1: workflow de deploy não exige CI aprovado nem disponibiliza o artefato

`.github/workflows/deploy.yml:32` compara o checkout ao SHA informado, mas não verifica resultado de CI ou pertencimento à branch/release confiável. O input de artefato diz aceitar caminho/URL, mas o job apenas passa esse texto a `scp`; não baixa artefato do CI, não transfere seu manifesto e o CI atual não o constrói/publica.

Ação: produzir artefato e manifesto identificados, exigir CI aprovado para o SHA exato, validar origem confiável e transferir ambos antes do acesso de implantação. Evitar interpolação direta de inputs em shell; passar valores por ambiente e validá-los.

### R12 — P2: bootstrap e evidências ainda não correspondem ao aceite

`scripts/lib/bootstrap.sh` verifica apenas um subconjunto das dependências; `apply_missing` registra estados, mas não instala os requisitos faltantes. `check_power` confere existência de logind.conf, não a política efetiva. Não atende ainda a reconstrução fresh de I02.

A auditoria marca qualquer comando bem-sucedido como `verified`, sem interpretar suas propriedades. O teste de auditoria deixa `AUDIT_REPORT_PATH` no padrão e sobrescreve `docs/evidence/server-audit.md` com fixtures. O arquivo atual já contém `fixture-safe-value` com status geral `verified`, que não deve ser usado como evidência física.

`acceptance.md` contém uma declaração geral, sem matriz individual A01–A34. Parte das pendências é de implementação local, não apenas `blocked_external`. A maior parte do código ainda aparece como não rastreada no Git; o checkout Linux existente em `/home/reis/HomeServer` contém somente documentos e não representa esta implementação.

Ação: separar fixtures de relatórios reais, classificar fatos corretamente, implementar bootstrap conforme escopo e manter uma matriz por tarefa/critério com evidência e ambiente.

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
