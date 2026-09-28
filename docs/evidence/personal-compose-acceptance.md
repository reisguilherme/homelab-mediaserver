# Aceite da stack pessoal com Compose

Data: 28/09/2026. Este registro acompanha o projeto simplificado:
Compose raiz, preferências/credenciais no `.env`, operator temporário e métricas
em container. Provas da infraestrutura anterior ficam no
[registro histórico](productization-acceptance.md) e não são somadas ao aceite atual.

## Verificação local

Linux/WSL Ubuntu 24.04, Python 3.12: a suíte final terminou com
**932 testes aprovados**, em 46,50 s, e dois avisos de depreciação upstream.
Ela inclui a janela de downloads paralelos e importação sequencial de séries.
Caddy real executou os contratos do monitor qBit; não houve skips.
Ruff, compilação, ShellCheck, sintaxe Bash e smoke também passaram.

A revisão focada executou **177 testes** sem bloqueios identificados.
Esse conjunto se sobrepõe à suíte final e não deve ser somado aos 932.

```bash
uv sync --frozen
uv run make lint
uv run make test-unit test-contract test-integration smoke
uv run make compose-check
```

As credenciais são lidas como dados pelo entrypoint próprio, sem execução de
shell ou interpolação Docker para segredos. O operator escreve as keys adotadas
no `.env`; os consumidores precisam de restart para recarregar o arquivo.

## Primeiro boot real isolado

O ensaio **simple-fresh1 passou com sete serviços reais** no servidor:
qBittorrent, Radarr, Sonarr, Prowlarr, Bazarr, Jellyfin e Seerr.
Confirmou bootstrap, segundo apply sem mudanças, qBit alterado para 3/7/10,
read-back e prontidão com snapshots recentes.

Esse ensaio usou CPU, diretórios novos e nenhuma mídia/torrent de produção.
As imagens próprias foram construídas antes da última mudança de janela
paralela. Portanto, ele comprova o bootstrap/configuração do Compose pessoal,
mas não execução dessa última política de séries dentro das imagens finais.

## Política de séries verificada em testes

`HOMESERVER_SERIES_DOWNLOAD_WINDOW=4` limita a janela de episódios ainda não
importados por série e respeita o limite global `DOWNLOAD_MAX_ACTIVE`.
Episódios dessa janela podem baixar em paralelo; um episódio pronto aguarda
os anteriores validados/importados. E7 aguarda E5/E6, e uma temporada posterior
aguarda a anterior para aparecer no Jellyfin.

Episódios desmonitorados ou futuros conhecidos não bloqueiam a cadeia.
Um episódio completo aguardando importação não ocupa um download ativo.
Essa prova de contrato não equivale a reprodução/importação real da nova
política com mídia na biblioteca existente.

## Estado externo

| Etapa | Estado comprovado |
|---|---|
| Lint e suíte final | PASS, 932 testes; contratos Caddy reais |
| Revisão focada | 177 testes aprovados; sem bloqueios identificados |
| Compose pessoal / bootstrap real | simple-fresh1 PASS, sete APIs CPU isoladas |
| Imagens próprias anteriores à janela paralela | Docker build PASS |
| Build final contendo a última política de séries | Aguardando prova |
| Publicação da simplificação no GitHub | Aguardando prova |
| Ativação da stack simplificada na produção | Aguardando prova |
| Rede Tailscale, Intel e reprodução na TV da stack nova | Exigem validação própria |

Nenhum deploy novo é declarado por este registro. O runtime anterior e seus
dados são preservados durante a transição. Não há CI/CD, releases próprias ou
gerenciador de backups como etapa de ativação.

Consulte [instalação](../installation.md), [operação](../operator-guide.md)
e [desenvolvimento](../development.md) para reproduzir os comandos.
