# Ensaio nativo de checkpoint em fixture isolada

Este comando manual cria configurações novas de Jellyfin, Sonarr e Radarr e um
vídeo sintético de dois segundos no Linux local. Não recebe caminhos, IDs,
Compose project, `.env`, endpoint Docker ou configuração externa. Não integra o
runtime nem cria agenda ou gerenciador de backups.

Use Python 3.12 como usuário comum em Linux/WSL2, GNU tar e Docker local no socket
Unix `/var/run/docker.sock`. Prepare o ambiente com `uv sync --frozen`. As três
imagens declaradas no módulo precisam estar no cache local; o comando apenas
resolve IDs com `image inspect`, usa `--pull never` e nunca baixa imagens:

- Jellyfin: `jellyfin/jellyfin@sha256:357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037`.
- Sonarr: `linuxserver/sonarr:4.0.15`.
- Radarr: `lscr.io/linuxserver/radarr@sha256:adb6c09d6b729ea5e642c99cea35af72702ef476bf4763f153299ac5db9f0b4f`.

```bash
uv run python scripts/rehearse-native-checkpoint.py --scenario success
uv run python scripts/rehearse-native-checkpoint.py --scenario unexpected_restart
```

O sucesso retorna JSON agregado e exit 0. O cenário negativo deliberadamente
reinicia e para apenas um clone próprio; deve rejeitar a contenção antiga antes
de criar o bundle, retornar `containment_changed` e exit 1. Ambos mantêm
`partial_restart_safe=false` e `production_ready=false`.

## O que é verificado

Os clones usam rede `none`, sem portas publicadas, dispositivos ou socket Docker,
filesystem de imagem somente leitura, usuário comum, capabilities removidas e
limites de CPU, memória, processos e logs. APIs são chamadas por loopback dentro
dos clones, via `docker exec`, com credenciais somente no stdin do curl.

O Jellyfin cria usuário e biblioteca locais, indexa um vídeo válido com NFO local
e relê identidade, caminho e dados de mídia pelo catálogo nativo. A biblioteca
desativa realtime e os fetchers de metadados/imagens de Movie/BoxSet em
`TypeOptions`; o GET da biblioteca confere as opções e o caminho aceitos.
O antigo `EnableInternetProviders` é obsoleto no
[modelo oficial do Jellyfin](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Model/Configuration/LibraryOptions.cs).
`network=none` garante isolamento externo. O endpoint público pode responder antes da conclusão
do bootstrap: o ensaio aguarda também o GET `/Startup/User` retornar pronto antes
de enviar o primeiro POST, sem repetir POSTs em falhas.
Na reabertura, um GET autenticado comprova readiness antes do novo POST de login.
A criação não solicita refresh automático: depois do readback, o ensaio aguarda
o job `RefreshLibrary` ficar `Idle`, envia um único refresh explícito e confere
o catálogo. Isso evita depender de um pedido agrupado ao scan inicial vazio.

Sonarr e Radarr criam e releem tags locais por API. Essas tags são objetos de
configuração nativos: **não foi exercitado scan do catálogo Arr**. O campo
`native_scan_baseline_verified` representa o catálogo sintético Jellyfin; os
campos `arr_tag_objects_verified` e `arr_catalog_scan_exercised` explicitam a
diferença.

Antes de parar, o conjunto inteiro de IDs completos, imagens, nonce, labels e
binds é conferido. A parada usa `docker stop --timeout -1`; não há kill nem
remoção forçada. Running, Restarting, Paused, PID, StartedAt e FinishedAt são
relidos. O callback do checkpoint rejeita qualquer alteração observável durante
a captura. Isso depende da exclusão de escritores no ambiente privado e não
impede um administrador externo de reiniciar containers.

Depois da parada, a biblioteca de checkpoint captura bytes/metadados/SQLite.
O bundle é copiado opacamente para **outro diretório no mesmo Linux local**,
relido por hash e restaurado em árvore nova. Os hashes esperados ficam separados
do bundle. Outra cópia da restauração é montada nos novos clones, que devem
preservar usuário/item Jellyfin e IDs/labels das tags Arr. A árvore de referência
não é aberta pelas aplicações e fica preservada até terminar a verificação.

## Privacidade e retenção

Cada fixture fica sob pai privado 0700 em `/tmp`. A restauração preserva o modo
da raiz original; é o pai privado que protege toda a árvore. O ledger com IDs
reais é persistido com modo 0600 e fsync antes de iniciar os clones. Corpos HTTP,
credenciais, tracebacks e diagnósticos detalhados ficam somente em arquivos
privados da fixture; não entram no JSON público nem no Git.

Qualquer incerteza de identidade, visibilidade, parada ou remoção mantém hold;
o cleanup não tenta novamente uma parada que falhou. Uma resposta HTTP mal
interpretada não prova que o POST deixou de ocorrer. Falhas preservam artefatos
para inspeção. Remoção de diretório só ocorre depois da remoção confirmada dos
consumidores próprios. Recuperação manual de um lote retido é uma ação separada,
com escopo e autorização próprios; não transforma a rodada falha em sucesso.

## Testes e limites

Em 15/11/2026, a CLI de sucesso verificou cinco bancos e preservou o catálogo
Jellyfin e as tags Arr após restore, criando/removendo seis clones. As versões
obtidas pelas APIs foram Jellyfin 12.2.0, Sonarr 4.0.15.2941 e Radarr 6.4.4.10685.
Depois do ajuste de readiness e refresh explícito, o teste opt-in de sucesso
passou em 40,21 s. A CLI negativa no código final retornou exit 1 com somente
`containment_changed`, confirmou ausência de efeito de captura e removeu seus
três clones. Nenhuma dessas rodadas libera retomada parcial ou produção.

```bash
uv run pytest tests/unit/test_native_containment.py -q
HOMESERVER_NATIVE_REHEARSAL=1 uv run pytest tests/integration/test_native_checkpoint_rehearsal.py -q
```

O segundo comando é opt-in e cria clones reais novos. Sem a variável, os testes
são pulados; isso não é evidência nativa. O comando pode levar vários minutos.

O escopo é `isolated_native_fixture`, com
`containment_kind=docker_stopped_native_applications`. Este ensaio não cobre os
21 serviços, overrides, imagens independentes para recuperação, bancos reais,
playback, retomada com biblioteca parcial, Tailscale, perda de energia ou USB.
A cópia local não ensaia transferência/restauração Windows nem backup físico em
USB. Windows e USB continuam destinos aprovados para o checkpoint operacional
futuro, que precisa de sua própria execução e verificação. O corte de produção
continua sujeito à [preparação operacional](media-precutover-review.md).
