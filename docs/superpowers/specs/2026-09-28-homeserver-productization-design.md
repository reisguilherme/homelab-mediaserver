# HomeServer — decisão atual de simplificação

Data: 28/09/2026. A decisão posterior do operador substitui o design de
produtização por uma instalação pessoal com Docker Compose. O design anterior
está preservado no
[histórico Git](https://github.com/reisguilherme/homelab-mediaserver/blob/89c84f26e4426d2f025573ce3728c5fa320f6267/docs/superpowers/specs/2026-09-28-homeserver-productization-design.md).

## Escopo atual

- Ubuntu Server 24.04 LTS amd64, Docker Engine/Compose e Tailscale no host.
- `compose.yaml` raiz, com execução por `docker compose up -d --build`.
- Preferências e credenciais diretamente no `.env`, sem secret files.
- Defaults estruturais no código/Compose: paths, portas, redes, URLs internas
  e UID/GID 1000.
- Init cria configurações ausentes; operator reconcilia as sete APIs e preserva
  dados, IDs e contas existentes.
- CPU padrão, Intel opcional por override explícito e teste de hardware.
- Métricas em container e painel web com links dos serviços.
- Sem CYD, MQTT exclusivo, CI/CD, instalador de infraestrutura, releases
  próprias, rollback ou gerenciador de backups.

## Regras preservadas

Aquisições usam gateway e capacidade pelos bytes reais; não há reserva fixa de
80 GB nem teto estático por arquivo. Qualidade precede seeds, com pisos do vídeo
principal. Séries podem baixar episódios em paralelo em janela configurável,
respeitando o limite global; importação/Jellyfin permanece em ordem estrita
de temporada e episódio.

Após cinco minutos lento, buscar e medir alternativa mantendo a atual,
promovendo somente fonte de qualidade/edição compatível e ETA melhor.
Preferência de áudio usa metadados declarados; original exige contexto Arr.
Legendas priorizam pt-BR release/edição e inglês release/edição.
Dispensa é restrita a áudio original pt-BR comprovado e pode ser desativada.

Arr mantém CDH desabilitado e hardlinks habilitados; o worker valida e importa.
Exclusão é explícita pelo Jellyfin e coordenada entre serviços. Não apagar mídia
automaticamente, abrir portas do roteador, formatar discos ou contornar o gateway.

## Referências atuais

[Arquitetura](../../architecture.md),
[configuração](../../configuration.md),
[instalação](../../installation.md) e
[plano de simplificação](../plans/2026-09-28-homeserver-productization.md)
são os guias ativos. [Auditoria](../../audits/2026-09-28-homeserver-productization-audit.md)
e [evidência anterior](../../evidence/productization-acceptance.md) preservam
o contexto histórico, sem declarar ativação da nova stack.
