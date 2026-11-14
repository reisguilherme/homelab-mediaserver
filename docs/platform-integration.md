# Integração com homelab-platform

A [plataforma](https://github.com/reisguilherme/homelab-platform/tree/refactor)
é a fonte canônica da arquitetura geral, dos recursos compartilhados e dos
contratos de integração. Este repositório mantém os serviços de mídia, sua
configuração, seus testes e runbooks. Nenhuma implantação é acionada pelo Git.

## Primeira unidade: media-metrics

O [contrato](../platform.contract.json) descreve **somente o exporter privado de
métricas**, implantável pelo [Compose independente](../compose.observability.yaml).
Não certifica o Compose principal, sua administração, Jellyfin somente leitura
ou isolamento entre core e playback. Esses componentes ainda precisam dos
manifests segregados e de contratos próprios antes do aceite do to-be.

`runtime: compose-vm` descreve o destino previsto. A primeira integração também
pode ser ensaiada no daemon Docker atual, em janela autorizada. Não há VM criada
por este contrato. CPU e storage são orçamentos solicitados; o Compose limita
RAM a 128 MiB, mas não aplica quota de CPU nem reserva 1 GiB de disco. O diretório
de snapshots é pequeno, dedicado, em storage local do SSD, montado somente
leitura no exporter. Ele não recebe a biblioteca nem bancos, tokens ou Docker socket.

Prometheus/Grafana e a rede interna `homelab-metrics` são criados pela plataforma.
O exporter ingressa nessa rede externa com alias `media-metrics`, TCP 8090, sem
porta de host. Prometheus acessa `/metrics`; `/health/live` informa apenas vida do
processo. Os indicadores de validade e idade de snapshot precisam ser avaliados
separadamente. A plataforma inicia sem targets de aplicações cadastrados.

O dashboard específico fica em `deploy/metrics/dashboards/homeserver.json`.
Sua ativação e a publicação opt-in são descritas no
[guia de observabilidade](observability.md). A rede Docker não atravessa VMs;
endereçamento, autenticação e firewall entre guests exigem outra etapa.

## Versão e validação

[platform.lock.json](../platform.lock.json) fixa o repositório, commit e schema
da plataforma usados por esta integração. É um registro de compatibilidade
revisada, sem download ou execução automáticos. Para validar:

1. Ler o commit no lock e obter esse commit do repositório da plataforma em um
   checkout separado; conferir `git rev-parse HEAD` antes de executar seu código.
2. No checkout fixado da plataforma, executar `uv sync --frozen` e:

   ```bash
   uv run python scripts/validate.py service /caminho/do/homelab-mediaserver/platform.contract.json
   ```

3. Revisar o Compose e testar os recursos reais na janela autorizada. Validação
   JSON não aplica ACL, não verifica conectividade, não reserva capacidade e não
   comprova conformidade operacional.

A plataforma registra este consumidor por commit imutável e estado `proposed`.
O catálogo é atualizado depois do commit do consumidor; o lock do consumidor
fixa uma versão anterior compatível do schema. Assim não há ciclo de commits
que precisem referenciar seus próprios hashes futuros. Evoluções do contrato
exigem atualização explícita, revisão e nova validação; não há herança de uma
branch móvel nem de credenciais administrativas.

## Recuperação e fronteiras

O exporter só observa snapshots. Arquivo ausente, inválido ou vencido produz
indicadores de indisponibilidade e omissão das métricas de domínio afetadas;
não deve ser interpretado como valor zero ou storage saudável. Quando o produtor
volta a fornecer snapshots válidos e atuais, a leitura se recupera. O endpoint
de liveness pode continuar saudável durante esse incidente.

Se o bind não existir, o Compose usa `create_host_path: false` e não deve criar
um diretório substituto. Não montar outro volume no path esperado apenas para
fazer o exporter iniciar. Primeiro validar a origem, permissões e produtor.

O exporter não pausa/retoma downloads, remove manutenção, monta USB ou repara
filesystem. Preserva pausas manuais por não alterar o estado da aplicação.
As regras de recuperação física pertencem à plataforma, enquanto bloqueio,
reconciliação e retomada da mídia permanecem no consumidor, sob uma autoridade
por incidente. O [runbook atual do storage](storage.md) e o
[registro da migração](architecture/homelab-2026/implementation-status.md)
continuam aplicáveis; a coordenação host/guest/NFS ainda precisa ser implantada.
