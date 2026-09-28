# Aceite da stack pessoal com Compose

Data: 28/09/2026. A stack pessoal foi ativada no servidor com o código
[e6558e5560f69a666087306178a4b4d98d0e58d6](https://github.com/reisguilherme/homelab-mediaserver/commit/e6558e5560f69a666087306178a4b4d98d0e58d6).
Build, início, configuração das sete APIs e reaplicação sem mudanças passaram.
Downloads paralelos da mesma série foram observados; a cadeia real completa de
importação ainda não terminou.

Este registro descreve Compose raiz, preferências/credenciais no `.env`,
operator temporário e métricas em container. Provas da infraestrutura anterior
ficam no [registro histórico](productization-acceptance.md).

## Código e verificação local

Main, checkout principal e clone HTTPS/SSH limpo no Ubuntu foram alinhados
à versão publicada. O build final no clone e a validação Compose terminaram
com exit 0; o início da stack e restart dos cinco consumidores também.

Linux/WSL Ubuntu 24.04, Python 3.12: **938 testes passaram**, em 46,71 s,
com dois avisos de depreciação upstream. Caddy real executou os contratos
do monitor qBit, sem skips. Ruff, compilação e ShellCheck passaram.
Smoke Bash já havia passado antes dos últimos ajustes de código Python.

A suíte inclui janela paralela/importação sequencial, identidade real do
filesystem, payloads graváveis Seerr e alias raiz do dashboard.
Os 932 testes da execução anterior e os subconjuntos focados não são somados
aos 938. Os ajustes finais cobrem duas falhas observadas na transição:
UUID vazio no worker e campos somente leitura enviados ao Seerr.

```bash
uv sync --frozen
uv run make lint
uv run make test-unit test-contract test-integration smoke
uv run make compose-check
```

Gitleaks 8.28 inspecionou **72 commits / 2,62 MB** e árvores
services/scripts/deploy, sem achados. Isso não equivale a scan de vulnerabilidades
das dependências/imagens. O `.env` privado de 68 campos foi copiado para o
desktop e continua ignorado pelo Git, sem credenciais neste registro.

## Primeiro boot isolado

**simple-fresh1 passou com sete serviços reais**: qBittorrent, Radarr, Sonarr,
Prowlarr, Bazarr, Jellyfin e Seerr. Confirmou bootstrap, segundo apply sem
mudanças, qBit alterado para 3/7/10, read-back e prontidão.

A fixture usou CPU, diretórios novos e nenhuma mídia/torrent de produção.
Suas imagens precediam a última mudança de janela paralela; ela comprovou
bootstrap/configuração. O build e a ativação posteriores foram verificados
no clone da versão final, sem equiparar isso a uma VM Ubuntu vazia.

## Ativação e read-back reais

O apply terminou com **os sete serviços verified**. O segundo apply também
teve sete verified e **zero mudanças**. No qBit, login e limites nativos
4 downloads / 8 uploads ativos / 12 torrents foram confirmados.
Completed Download Handling no Sonarr foi lido como false; o operator mantém
também hardlinks habilitados nos Arr.

A stack tem **15 containers normais em execução**, os **seis healthchecks
existentes saudáveis** e init concluído com exit 0.
Control/telemetry readiness, Jellyfin público e status Seerr responderam HTTP 200.

Unidades antigas de stack, métricas, mount watch e backups foram desabilitadas.
A montagem física foi verificada antes de reconciliar a identidade do
armazenamento. Permissões legadas foram alinhadas ao UID1000.
Mídia, bancos e cópias existentes foram preservados.

O override Intel foi aplicado com render node e grupos reais do host.
Reprodução e transcodificação não foram revalidadas nessa ativação.

## Downloads paralelos e ordem de importação

Dois episódios da mesma série estavam baixando simultaneamente no qBit,
com progresso de 8,41% e 13,89% na leitura observada. Outras duas fontes
admitidas estavam sem seeds e aguardavam peers; admissão não garante velocidade.

`HOMESERVER_SERIES_DOWNLOAD_WINDOW=4` limita os episódios ainda não importados
por série, respeitando `DOWNLOAD_MAX_ACTIVE` global. E7 pronto aguarda E5/E6
validados/importados; uma temporada posterior aguarda a anterior para aparecer
no Jellyfin. Desmonitorados ou futuros conhecidos não bloqueiam a cadeia.
Completo aguardando importação não ocupa um download ativo.

Os testes cobrem essas guardas. A cadeia real completa ainda não terminou,
portanto este registro não a apresenta como importação/reprodução comprovada.

## Acesso Tailscale

Um cliente Windows recebeu HTTP 200 de Jellyfin, Sonarr, Radarr, monitor qBit
e dashboard raiz usando o IP Tailscale. Os cards usam as portas dos serviços;
o card qBit por IP foi confirmado.

O monitor permanece no proxy Caddy somente leitura em loopback. Serve usa
[o encaminhamento TCP oficial](https://tailscale.com/docs/reference/tailscale-cli/serve#use-a-tcp-forwarder):

```bash
sudo tailscale serve --bg --tcp=18080 tcp://127.0.0.1:18080
```

O navegador abre `http://IP_TAILSCALE:18080` ou MagicDNS.
O modo HTTP anterior retornava 404 pelo IP; o forward TCP corrigiu o acesso.
A API nativa qBit não tem publicação administrativa; suas portas Docker
publicadas são somente 6881 TCP/UDP para peers. O monitor está em
`127.0.0.1:18080`, alcançado dentro da tailnet.

## Limites do aceite

| Etapa | Estado comprovado |
|---|---|
| Lint e suíte final | PASS, 938 testes; Caddy real, sem skips |
| Bootstrap real isolado | simple-fresh1 PASS, sete APIs CPU |
| Código público, clone e build final | PASS na versão e655 |
| Apply/read-back e segundo apply | Sete verified; zero mudanças |
| Containers e healthchecks | 15 em execução; seis healthchecks saudáveis; init exit0 |
| Download paralelo da mesma série | Observado em dois episódios |
| Importação ordenada | Coberta por testes; cadeia real ainda não concluída |
| Acesso Tailscale | JF, Arr, qBit e dashboard HTTP 200 por cliente Windows |
| Gitleaks histórico/árvores | 72 commits e árvores sem achados |
| Nova VM, reboot físico, transcode Intel e TV | Não revalidados |

Não há CI/CD, releases próprias ou gerenciador de backups como etapa de
ativação. Consulte [instalação](../installation.md),
[operação](../operator-guide.md) e [desenvolvimento](../development.md).
