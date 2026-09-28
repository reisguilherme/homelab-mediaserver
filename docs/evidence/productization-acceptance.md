# Implementação e aceite de produtização

Data: 28/09/2026. Baseline da auditoria: `2cffa20798574b3b0916458e11b3461ffe8d58b3`.
Este registro acompanha a implementação autorizada do [plano P01–P13](../superpowers/plans/2026-09-28-homeserver-productization.md).
A [auditoria A01–A16](../audits/2026-09-28-homeserver-productization-audit.md)
preserva a fotografia anterior; não descreve os novos componentes.
Entrega de código publicada:
[f0c092f5096a2462e9bad385d110acd1ef264399](https://github.com/reisguilherme/homelab-mediaserver/commit/f0c092f5096a2462e9bad385d110acd1ef264399).
`main`, a branch padrão remota e o checkout principal estão alinhados nessa SHA.

## Tratamento dos achados

| Achado | Implementação | Evidência e limite |
|---|---|---|
| A01, branches divergentes | Histórico consolidado e código publicado em `main` | Ancestralidade preservada; `main`, default remoto e checkout principal alinhados na entrega pública |
| A02, configuração dispersa | Settings/loader/schema, catálogo, `.env.example`, CLI e aliases de migração | Contratos de tipos, paths, segredos, `_FILE`, precedência e caracteres literais |
| A03, preferências nativas | Operator autentica, planeja, aplica e relê sete APIs | Sete APIs reais verificadas no ensaio fresh7, incluindo guardas Arr; reaplicação sem diff e IDs estáveis |
| A04, overrides externos | Compose/units gerados pelo mesmo `.env` | Redes, binds, persistência e paths compartilhados testados; não depender de override pessoal |
| A05, bootstrap incompleto | Instalação fresh/adopt com preflight/journal/ownership | Testes de idempotência e recusa de arquivos desconhecidos; primeiro boot Docker CPU isolado passou, adopt de produção pendente |
| A06, deploy/rollback parcial | Artefato limpo por SHA, digests, backup, migração, ativação e retorno compatível | Testes de falha/rollback/segredos preservados; release nova ainda não ativada em produção |
| A07, reinício inconsistente | Supervisor com lock, manutenção, guarda de UUID, tentativas limitadas e heartbeat | Falhas simuladas; reboot/perda física de disco em VM continuam unknown |
| A08, backup diferente do teste | Restic real, SQLite consistente, copy independente e restore isolado testados | Fixtures passaram; primeira captura do estado real recusada por aliases de logs Seerr, correção e novo backup/restore pendentes |
| A09, dependência do servidor | Roots/UID/GID/binds configuráveis; CPU default e Intel opcional | Docker CPU real e paths com espaços; Intel/reprodução em outro hardware não inferidos |
| A10, publicação incompleta | Ignores, contexto privado excluído, Gitleaks, pip-audit, Trivy, CI/release/security e contribuição | Gitleaks de 69 commits e árvores sem achados; sentinelas env/DB/chave ausentes nas duas imagens reais |
| A11, CYD/MQTT | Firmware e dependência exclusiva removidos, painel HTTP mantido | Código/templates/testes não exigem display ou broker; estado antigo preservado na migração |
| A12, documentação fragmentada | README e guias de instalação/configuração/arquitetura/operação/diagnóstico | Runbooks antigos atualizados ou identificados como históricos; comandos correspondem à CLI |
| A13, falta de diagnóstico | Doctor, readiness, estado por serviço, snapshots recentes e cards nativos | Prazo fixo de ciclo e heartbeat; dados ausentes/antigos não são inventados e diagnóstico não publica tokens/inventário bruto |
| A14, crescimento sem política | Rotação de logs, seeding/retenção/backup/staging editáveis | Logs externos registram contexto/classe sem corpo/URL de credenciais; mídia não é apagada automaticamente |
| A15, CI limitada | Build CPU, native firstboot/read-back, Caddy real, Restic real e scanners | Checks locais e Docker reais passaram; job GitHub CI não iniciou por bloqueio externo de billing, sem runner/steps |
| A16, fonte lenta | Busca aos cinco minutos, avaliação concorrente, qualidade/edição/capacidade e ETA medidos | Fonte atual continua até alternativa melhor confirmada; pisos de vídeo também valem para candidatas e proteção persistente cobre worker/gateway |

## Verificação observada

Linux/WSL Ubuntu 24.04 / Python 3.12: a suíte final observada passou com
**930 testes**: 375 unitários, 318 contratos, 226 integrações e 11 de sistema,
além de Ruff, compilação, ShellCheck e scripts smoke. Essa execução inclui as
últimas mudanças de preferência de áudio, dispensa de legenda e guardas Arr.
Houve dois avisos de depreciação de dependências. Caddy 2.10.2 real executou os
105 casos de allowlist/read-only; não foram convertidos skips em aprovação.
Testes Restic usam repositórios temporários criptografados e restore do mesmo
formato entregue. Os testes pertinentes de política de workflows e ciclo de
release passaram novamente após sua última alteração: **17 testes**.
O vínculo Byparr/Prowlarr teve verificação posterior: **44 contratos**, dos
quais 14 novos, e Ruff passaram. Esse conjunto está na entrega publicada e
não deve ser somado aos 930, pois contém testes anteriores reaproveitados.

Docker no servidor real, com roots novos, portas loopback, CPU e nenhuma mídia
de produção: imagens construídas/importadas e Compose válidos. Arquivos fictícios
`.env`, DB e chave dentro do contexto de source ficaram ausentes das imagens.
O ensaio final **fresh7 passou com os sete serviços reais**: qBit, Radarr, Sonarr,
Prowlarr, Bazarr, Jellyfin e Seerr. Confirmou bootstrap, segundo apply sem diff,
alteração/read-back dos limites qBit e prontidão com snapshots recentes.
Diferenças reais de schema, máscaras, arredondamento KiB, primeiro usuário,
campos nulos omitidos pelo Arr e bibliotecas do Jellyseerr 2.7.3 foram corrigidas.
O projeto isolado não configurou indexadores, não adicionou torrents e não
importou mídia. Containers/redes pertencentes ao ensaio foram removidos; seus
arquivos privados permanecem fora do Git. Esse ensaio inclui as últimas
mudanças de áudio e guardas Arr; os limites qBit foram alterados para 3/7/10
na fixture e confirmados por leitura nativa.

Trivy 0.73.0 após atualizar Python 3.12.14 e pacotes Debian: **zero CRITICAL com
correção disponível** nas duas imagens próprias. Control retém 1 CRITICAL sem
correção disponível e 212 HIGH; telemetry: 44 HIGH / zero CRITICAL. Relatórios não
usam lista de ignores. `exit 0` do scanner significa o threshold publicado,
não ausência de vulnerabilidades. Dependências Python congeladas passaram pip-audit.
Gitleaks inspecionou **69 commits / 2,43 MB** de histórico e as árvores de
services/scripts/deploy, sem achados. A ausência de sentinelas nas imagens e
os scans de código não equivalem a ausência de vulnerabilidades upstream.

O achado CRITICAL residual é `CVE-2026-6653` em libxml2, dependência do FFmpeg.
O [Debian Security Tracker](https://security-tracker.debian.org/tracker/CVE-2026-6653)
classifica o impacto como negação de serviço por XML e registra Trixie como
vulnerável sem atualização estável disponível. Isso permanece no relatório;
não foi ignorado nem declarado corrigido. Não trocar bibliotecas do sistema
por pacotes de distribuição diferente para ocultar o achado.

## Contratos acrescentados na revisão final

Os pisos de qualidade usam o tamanho do vídeo principal e duração declarada,
sem somar amostras, legendas ou preenchimento. Valem na aquisição inicial e
no failover, com mínimos equivalentes no Arr e sem teto estático. Duração
desconhecida bloqueia uma candidata quando seu piso está habilitado. O mesmo
episódio protegido não recebe seleção, troca, pausa/retomada ou reordenação
automática pelo worker ou gateway.

`AUDIO_LANGUAGES` ordena preferências usando apenas os idiomas declarados nos
metadados da release. `original` exige contexto de idioma original fornecido
pelo Arr; nomes de arquivo e IDs numéricos isolados não o comprovam. Dados
desconhecidos não vetam uma release nem recebem preferência. Fonte, resolução,
Dolby Vision e Atmos continuam precedendo áudio e seeds na escolha da release.

`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` aceita somente `pt-BR` nesta versão;
valor vazio desativa a dispensa. A detecção exige evidência de áudio original
brasileiro no arquivo. A configuração de idiomas de legenda permanece
`pt-BR,en-US`, com prioridades por release/edição e margem para créditos.

O operator mantém dois invariantes no Sonarr/Radarr:
`enableCompletedDownloadHandling=false`, para preservar validação/importação
coordenadas pelo worker, e `copyUsingHardlinks=true`, para compartilhar os
bytes com o torrent e manter seeding. São guardas internas, não preferências
editáveis adicionais. Apply preserva IDs e chaves estrangeiras e confirma
ambos os valores por leitura nativa.

Uma consulta real ao UIndex passou após associar indexador e proxy Byparr pela
mesma tag Prowlarr. A configuração anterior deixava as tags do proxy vazias,
impedindo sua seleção e causando HTTP 429 nas buscas/sincronização Arr. A busca
após correção retornou HTTP 200. Seeds anunciados pela fonte continuam sendo
metadados, não peers conectados ou garantia de velocidade. A persistência desse
vínculo está na entrega pública e passou pelos contratos pertinentes.
A sincronização do UIndex no Sonarr foi confirmada por leitura nativa, com
RSS e busca automática desabilitados e busca interativa habilitada. Caps e
busca responderam HTTP 200; essa prova não equivale a conclusão de download.

## Estado de publicação e ativação

| Etapa | Estado comprovado |
|---|---|
| Implementação no worktree isolado | Código/documentação publicados; correção posterior do backup real em andamento |
| Consolidação de `main` e branch padrão | Verificadas na SHA pública, com checkout principal alinhado e histórico preservado |
| GitHub CI da SHA entregue | Run criado; job não iniciou por bloqueio externo de billing, sem steps/runner; CI não aprovada |
| Release de produção por digest | Fluxo e fixtures verificados; publicação/ativação da release nova pendentes |
| Adopt do runtime existente | Configuração privada preparada; troca da gestão/produção ainda pendente |
| Instalação Ubuntu vazia e aceite físico | Não comprovados pelo ensaio Docker sobre o host existente |

O [job GitHub CI 109158378396 da execução 36490713775](https://github.com/reisguilherme/homelab-mediaserver/actions/runs/36490713775/job/109158378396)
registra a anotação: “The job was not started because your account is locked
due to a billing issue.” Nenhum step foi executado e nenhum runner iniciou.
Essa restrição da conta não é uma falha observada dos testes do projeto;
também não permite declarar a CI aprovada. A liberação da conta e a repetição
do workflow da SHA entregue continuam necessárias para o fluxo GitHub de release.

A primeira tentativa de backup novo com o estado existente foi recusada por
aliases de dois logs Seerr. Ela não foi contada como captura/restore aprovados.
A exclusão configurável de logs/cache está em correção; nova captura, check,
cópia independente e restore isolado do estado real permanecem pendentes.
O formato testado nas fixtures e o backup legado não substituem essa prova.

## Aceite externo

VM Ubuntu vazia, reboot físico, retirada/retorno do disco, Intel, rede Tailscale,
reprodução na TV e fluxo completo com mídia de teste autorizada precisam de
evidência própria. Containers novos sobre o host existente verificam bootstrap
nativo e CPU, mas não substituem esses ensaios. Nenhum resultado reported é
transformado em verified por inferência.

Adopt/produção exigem backup e importação privada dos IDs/credenciais existentes.
Até a ativação comprovada da nova release, o runtime anterior permanece em uso.
Uma ação pontual sobre a aquisição protegida exige decisão explícita do
operador e reconciliação; não remove a proteção automática dos demais fluxos.
Nesta sessão, o usuário autorizou retomar o mesmo torrent; a reserva válida
foi reconciliada e o gateway confirmou a retomada, sem substituir a fonte ou
excluir arquivos. Isso não comprova velocidade de peers nem conclusão da mídia.
Seu hash não aparece neste registro público. Não há exclusão automática de
mídia, alteração de fstab/Lenovo/roteador ou bypass do gateway.
