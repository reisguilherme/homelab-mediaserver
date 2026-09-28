# Registro histórico de implementação e aceite

Data: 28/09/2026. Baseline da auditoria:
`2cffa20798574b3b0916458e11b3461ffe8d58b3`.

Este registro preserva as provas da produtização anterior. A decisão posterior
do operador retirou CI/CD, instalação gerenciada, releases, rollback e backups
próprios. A operação atual usa [Compose pessoal](../installation.md).
Os resultados abaixo não comprovam a stack simplificada depois dessas remoções.

## Código publicado naquela etapa

A entrega de mídia/configuração foi publicada em
[f0c092f5096a2462e9bad385d110acd1ef264399](https://github.com/reisguilherme/homelab-mediaserver/commit/f0c092f5096a2462e9bad385d110acd1ef264399);
a correção de backup real foi consolidada em
[89c84f26e4426d2f025573ce3728c5fa320f6267](https://github.com/reisguilherme/homelab-mediaserver/commit/89c84f26e4426d2f025573ce3728c5fa320f6267).
A sincronização de main, default remoto e checkout principal foi verificada
naquela etapa. A simplificação posterior exige publicação e verificação próprias.

## Ensaios observados antes da simplificação

- Linux/WSL Ubuntu 24.04, Python 3.12: **961 testes passaram** na execução
  consolidada, além de Ruff, compilação, ShellCheck e smoke Bash.
  Caddy e Restic reais participaram dos conjuntos pertinentes.
- O ensaio fresh7 passou com sete APIs reais em Docker CPU isolado:
  qBittorrent, Radarr, Sonarr, Prowlarr, Bazarr, Jellyfin e Seerr.
  Bootstrap, segundo apply sem diff, alteração qBit para 3/7/10,
  read-back e prontidão foram confirmados.
- Esse ensaio não configurou indexadores, não adicionou torrents nem importou
  mídia de produção. Containers novos no host existente não comprovam uma
  instalação Ubuntu vazia, GPU, montagem física ou reprodução.
- Sentinelas fictícias .env/DB/chave ficaram ausentes das duas imagens próprias.
  Gitleaks inspecionou **69 commits / 2,43 MB** e árvores de código/scripts/deploy,
  sem achados. Dependências Python congeladas passaram pip-audit.
- Trivy 0.73.0 registrou zero CRITICAL com correção disponível, sem ignores.
  Control ainda tinha 1 CRITICAL sem correção e 212 HIGH; telemetry, 44 HIGH
  e zero CRITICAL. Exit 0 no threshold não significou ausência de vulnerabilidades.
  O residual libxml2 foi
  [CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653).

Esses números descrevem versões anteriores, incluindo testes de camadas
retiradas. Não devem ser somados nem apresentados como a suíte atual.

## Provas do fluxo de mídia

A preferência de áudio foi verificada por metadados declarados;
original requer contexto Arr. Nomes/IDs isolados não comprovam idioma.
Dispensa de legenda aceita apenas original pt-BR comprovado; valor vazio a
desativa. CDH Arr permanece desabilitado e hardlinks habilitados.

O vínculo Byparr/Prowlarr passou por 44 contratos focados, incluindo 14 novos.
Uma busca real UIndex respondeu HTTP 200 após proxy/indexador compartilharem
a tag gerenciada. Sincronização no Sonarr foi confirmada, com RSS e busca
automática desabilitados e busca interativa habilitada. Seeds anunciados
não foram tratados como peers conectados ou velocidade comprovada.

O usuário autorizou retomar a aquisição protegida pela mesma fonte.
A reserva foi reconciliada e o gateway confirmou a retomada, sem substituição
nem exclusão. Posteriormente, conclusão, importação Sonarr e disponibilidade
Jellyfin foram confirmadas. Hashes e IDs privados ficam fora deste registro.
Essa ação pontual não removeu a proteção dos fluxos automáticos.

## Captura e restauração históricas

A primeira captura foi recusada por aliases de logs Seerr. Após a correção,
backup real, check e restore isolado passaram: **2,9724 GB de estado**,
sem mídia, em repositório criptografado de aproximadamente **186 MB**.
A cópia independente para desktop não foi comprovada nessa etapa.

Essas provas são históricas; o gerenciador foi retirado do produto.
Não existe etapa atual obrigatória de backup/release. Dados e cópias legadas
não foram autorizados para exclusão por essa retirada.

## GitHub Actions histórico

O [job 109158378396 da execução 36490713775](https://github.com/reisguilherme/homelab-mediaserver/actions/runs/36490713775/job/109158378396)
não iniciou, sem steps ou runner. A anotação foi:
“The job was not started because your account is locked due to a billing issue.”

Não houve CI aprovada. Workflows foram retirados por decisão do operador,
portanto desbloquear billing ou repetir Actions não é requisito da operação atual.

## Limite do registro

A produção ainda utilizava o runtime anterior no momento destas provas.
Nenhuma ativação da stack simplificada é afirmada aqui. Lint/suíte, build,
primeiro boot após simplificação e cutover precisam de provas novas.
Rede/Tailscale, Intel, reboot físico e reprodução na TV têm validação própria;
reported não vira verified por inferência.

A [auditoria original](../audits/2026-09-28-homeserver-productization-audit.md)
preserva a fotografia anterior. Os guias ativos são
[instalação](../installation.md), [configuração](../configuration.md),
[operação](../operator-guide.md) e [diagnóstico](../troubleshooting.md).

As verificações posteriores à simplificação ficam no
[aceite da stack pessoal](personal-compose-acceptance.md).
