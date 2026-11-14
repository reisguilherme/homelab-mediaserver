# Migração preservando o acervo e implantação do homelab

> **For agentic workers:** Use superpowers:executing-plans ou superpowers:subagent-driven-development para implementar cada etapa após verificar suas precondições. Não confundir este plano com evidência de execução no servidor.

**Objetivo:** consolidar o acervo dos discos atuais no HD de 18 TB, preservar os originais e implantar o hub e as fronteiras de operação aprovadas, publicando o trabalho na branch `refactor`.

**Arquitetura:** Proxmox com edge/playback/core; mídia e automação local no core, NFS RO para playback, gestão privada e observabilidade. A migração dos dados antecede mudanças destrutivas no sistema; a instalação do hipervisor depende de console, disco identificado e cópia consistente independente.

**Stack:** Python 3.12/Linux, Docker Compose, ext4/mergerfs, NFS, Proxmox, Grafana/Prometheus; recuperação USB existente a ampliar.

**Spec:** [to-be](../../architecture/homelab-2026/to-be.md), [operação](../../architecture/homelab-2026/operations.md), [energia](../../architecture/homelab-2026/power-recovery.md) e [gaps](../../architecture/homelab-2026/gaps.md).

## Restrições globais

- Não apagar mídia nem limpar os discos de origem nesta execução. A migração começa por cópia, com publicação somente depois da prova de integridade e preservação de hardlinks.
- Não formatar, particionar, alterar montagens ou instalar SO sobre um disco sem identificar dispositivo e escopo autorizado. A autorização genérica de refatoração não escolhe qual SSD pode ser sobrescrito.
- Não substituir `fstab` inteiro, ativar RTX, reescrever serviço Lenovo, abrir portas do roteador ou publicar a WebUI administrativa nativa qBit.
- Manter configurações privadas, bancos, credenciais, inventários, manifestos operacionais e mídia fora do Git. Não despejar `.env`, `docker inspect` ou Compose interpolado em logs de conversa.
- Não reaproveitar autorizações históricas de cleanup, aceitação de conteúdo degradado ou alteração de disco. Revalidar o estado real e o trabalho já em andamento.
- Preservar arquivos locais preexistentes e sua autoria. Sem reset, stash destrutivo, force-push ou merge na `main`.
- Fixtures somente em diretórios temporários Linux; testes nunca apontam ao acervo. Não declarar proteção elétrica sem nobreak nem isolamento/recuperação sem ensaio real.

## Foco da revisão

1. Mesmo caminho relativo em dois discos com bytes diferentes: impedir sobrescrita e produzir conflito privado; conservar ambas as fontes.
2. Biblioteca e torrents compartilham hardlinks: copiar cada árvore física inteira e verificar grupos, evitando duplicar silenciosamente a capacidade necessária.
3. Cópia anterior ainda ativa ou prova antiga: impedir um segundo escritor concorrente e recapturar geração/quiescência antes do corte.
4. Queda de USB/energia durante cópia ou publicação: preservar originais, staging e journal; nenhuma liberação baseada apenas em tamanho do arquivo.
5. Mudança de host/mount sem rebind coerente de banco e claims: manter serviços parados e permitir retorno ao before-state consistente, sem reinterpretar autorizações.

## Etapa 1 — Baseline e identificação, sem mutação do servidor

**Arquivos:** este plano; evidências privadas em `.artifacts/private/` ou diretório privado correspondente no servidor. Não criar inventário público.

- [x] Examinar branch e alterações locais; manter o checkout existente que contém a aplicação não commitada.
- [x] Criar branch local `refactor` sem descartar alterações.
- [x] Verificar que a branch remota `refactor` não existia no instante da consulta.
- [x] Identificar WSL Ubuntu 24.04 com Python 3.12.3 e uv disponíveis.
- [x] Auditar suporte singleton e helpers anteriores de cópia/publicação; não executá-los.
- [x] Confirmar alvo SSH e autenticar mantendo a validação da host key existente.
- [x] Inspecionar SO/kernel, sudo, boot/rootfs, RAM, dispositivos/mounts e espaço disponível, sem varrer conteúdo de mídia na primeira consulta.
- [x] Localizar checkout e serviços efetivos sem revelar ambiente/credenciais; identificar jobs de cópia, verificação e recuperação existentes.
- [x] Guardar inventário privado de IDs estáveis e produzir resumo de origem/destino, staging, SSD do SO e destinos de cópia do estado; autorização específica para sobrescrita continua pendente.
- [x] Confirmar console, dados adicionais e inclusão das alterações anteriores. Janela condicionada a novo aviso do proprietário, sem agendamento de parada.

**Aceite:** servidor autenticado e identificado, nenhum disco alterado, mapa de origem/destino revisável e exceções antigas visíveis. A falta dessas respostas permite preparar código, mas bloqueia comandos que dependem delas.

## Etapa 2 — Cópia e verificação, mantendo originais

**Bases existentes:** `compose.storage.single.yaml`, `services/common/src/homeserver_common/storage.py`, `storage_startup.py`, scripts de preparação e recuperação; helpers privados de consolidação e suas fixtures. Os helpers não são um instalador genérico pronto.

- [x] Executar regressões singleton/guards em Python 3.12/Linux e fixtures temporárias; registrar comandos/resultados novos, sem reutilizar contagens históricas como prova.
- [ ] Reconciliar cópia/staging anterior, provas e alterações concorrentes; não reiniciar o pipeline histórico sem confirmar sua origem e estado.
- [ ] Medir dados únicos, alocação sparse, hardlinks, colisões e espaço realmente necessário no destino, incluindo staging, margem e dados que já existem nele.
- [ ] Apresentar o mapa exato de dispositivos/paths e autorizações necessárias antes de qualquer nova montagem ou preparação de disco.
- [ ] Revisar o procedimento de cópia restrito ao destino já verificado, preservando metadados/ACLs/xattrs e sem opções de remoção das fontes.
- [ ] Copiar/reconciliar em staging não publicado; verificar conteúdo e topologia. Manter conflitos e fontes danificadas preservados, sem aceitar exceção degradada por omissão.
- [ ] Sob quiescência autorizada, fazer delta final, prova nova e cópia consistente dos bancos/configurações/claims para destino independente do SSD que será alterado.

**Aceite:** conteúdo correspondente comprovado, conflitos resolvidos explicitamente, originais intactos e capacidade suficiente. Uma transferência terminada sem verificação não encerra esta etapa.

## Etapa 3 — Corte de storage com retorno verificável

**Lacuna constatada:** o contrato privado de cutover exige um backend real de host ainda ausente. Contagens/identidades de um incidente anterior estão fixadas em helpers e não podem ser aplicadas automaticamente ao estado atual.

- [ ] Definir o backend operacional a partir do inventário renovado e criar testes de interrupção para coordenação de DB, claims, configuração e publicação.
- [ ] Demonstrar em clone/fixtures que IDs lógicos, permits, reservas, pausas e histórico permanecem coerentes após mudança física.
- [ ] Revisar independentemente o conjunto de comandos e before-state concreto; obter a autorização específica de montagem exigida pelo projeto.
- [ ] Parar escritores dentro da janela, publicar somente dados provados, ajustar apenas a configuração/linha de montagem necessária e reconciliar o estado pelo plano validado.
- [ ] Verificar biblioteca, fila, hardlinks, capacidade, guard de disco ausente e serviços, mantendo os discos de origem sem cleanup.

**Aceite:** leitura nativa e estado de controle coerentes; retorno ensaiado sem depender de apagar as origens. O fluxo antigo exige cleanup em algumas fases: adaptá-lo ao requisito atual de preservar fontes, sem burlar guards.

## Etapa 4 — Proxmox, VMs e hub

- [ ] Conferir console independente, suporte de virtualização/IOMMU, destino do SO e recuperação do estado antes de agendar a instalação.
- [ ] Escolher migração do SO compatível com a distribuição real. Não tentar converter Ubuntu em Proxmox por instalação arbitrária de pacotes nem substituir o sistema via SSH sem recuperação independente.
- [ ] Após autorização do SSD exato, implantar Proxmox e VMs com o orçamento do to-be; confirmar acesso de gestão antes de mover dados/serviços.
- [ ] Entregar USB exclusivamente ao core e adaptar guards conforme identidade real do guest, sem dupla montagem RW do filesystem.
- [ ] Implementar exports por consumidor e mounts de playback com guard NFS próprio; provar RO no servidor e impedir fallback vazio.
- [ ] Separar serviços/segredos e fluxo privado de edição/exclusão, preservando autorização independente da playback.
- [ ] Verificar firewall inter-VM/LAN/IPv6, iGPU Intel, reprodução e acesso privado antes de alterar o caminho público existente.

**Aceite:** VMs reais, mídia RO, gestão independente e contenção comprovadas. Sem console/destino recuperável, a instalação do hipervisor permanece pendente enquanto a consolidação e a preparação seguem separadamente.

## Etapa 5 — Operação, energia e aceite final

- [ ] Implantar métricas e dashboards dentro do orçamento medido, com tokens de observação; provar operações administrativas no Proxmox e isolamento por VM.
- [ ] Definir e testar canal de alerta, ausência de dados e indisponibilidade do core; não considerar monitor no mesmo host como detecção independente de queda total.
- [ ] Integrar recuperação host/core/NFS com uma autoridade, journal, watchdog e respeito a novas intenções administrativas durante incidentes.
- [ ] Testar corrupção de mesmo tamanho, retorno do USB, handles antigos e espera segura em I/O bloqueado usando mídia de teste e dispositivos/janela autorizados.
- [ ] Manter nobreak como pendência até aquisição e ensaio da autonomia/desligamento; não declarar resolvido o risco de perda abrupta de energia do HD.
- [ ] Executar regressões, revisão independente e readback em produção; publicar commits revisados na `refactor`, sem merge na `main`.
- [ ] Registrar o que foi implantado, pendências e instruções de retorno; conservar fontes e cópias do estado até decisão futura explícita.

## Registro inicial de execução — 14/11/2026

- Branch local criada: `refactor`; nenhum push ou commit desta execução até este registro.
- Solicitação de validações enviada: alvo SSH/sudo, janela, console, dados adicionais/destino do estado e alcance das alterações anteriores no Git.
- Sem conexão SSH ao servidor confirmada, sem parada de serviços e sem operação de disco executada.
- Os planos antigos citam staging e tarefas pendentes; o estado atual deve ser consultado antes de lançar outro processo.
- Auditoria estática: singleton existe; cópia/verificação/publicação têm helpers privados, mas cutover produtivo ainda exige integração. Provas degradadas e exceções históricas não autorizam perda de acervo nesta migração.
- Decisão: trabalhar no checkout existente e branch solicitada, preservando a aplicação não commitada. Isolar fixtures/testes e evidências privadas; não criar outro checkout que omita essas mudanças.

### Validações posteriores do proprietário

- Incluir também as alterações locais anteriores na `refactor`, após revisão para excluir segredos e dados privados.
- Manter o servidor disponível por enquanto. O proprietário avisará expressamente quando a indisponibilidade puder começar; menção a "hoje à noite" não é agendamento ou autorização automática de parada.
- Há acesso físico e acompanhamento possível para a instalação do hipervisor.
- Os discos têm somente acervo, conforme declaração do proprietário. Isso não dispensa inventário de conflitos/ocupação nem autoriza apagar fontes.
- Autorizadas cópias privadas consistentes de bancos/configurações no HD de 18 TB e neste computador Windows, verificando espaço e mantendo tudo fora do Git.
- Objetivo do corte: disponibilizar o conteúdo já validado no HD de 18 TB e acrescentar o restante por cópia ao longo dos dias. Isso exige estado/readiness por conteúdo e suspensão de efeitos destrutivos de scans; o pipeline histórico de corte integral não atende esse modo incremental automaticamente.
- Primeiro acesso SSH confirmou host Ubuntu 24.04.5 LTS pelo usuário não root; a elevação sudo requer autenticação. Nenhuma credencial ou endereço privado é registrado neste documento.
- Baseline Linux atual: 105 testes passaram e um foi pulado por exigir fixture real root/UID1000. Houve aviso de permissão de cache pytest; não houve falha de teste. Essa execução cobriu os quatro arquivos de teste singleton/storage/consumers/recovery/Compose selecionados, não a suíte inteira nem o hardware.

### Preparação concluída antes da janela

- Acesso SSH e inspeção concluídos sem parar serviços ou alterar discos/montagens. A consolidação anterior está em verificação, com locks ativos e pendência de reparo; não foi iniciado outro pipeline.
- Checkpoint privado de código/configuração criado e relido no HD de 18 TB; cópia Windows com SHA-256 idêntico e ACL restrita. Não inclui bancos nem comprova consistência conjunta de aplicações.
- Exporter, publicador sanitizado opt-in e stack Grafana/Prometheus preparados no repositório, sem ativação em produção. [Plano de observabilidade](2026-11-14-observability-preparation.md).
- Suíte ampla posterior: 4.362 testes aprovados, 156 pulados e nenhum erro; os skips dependem de root, binários/imagens de proxy ou Compose no WSL. Correções posteriores de integração do `.env` têm validação focada própria. [Registro de implementação](../../architecture/homelab-2026/implementation-status.md).
