# HomeServer — guia do agente

## Migração para Proxmox

Estado observado em 16/11/2026: oito processos nativos do core estão vivos
(quatro Arr, Seerr, integration-proxy, qBittorrent e monitor); API, gateway e
worker estão em manutenção, sem readmissão da automação. Os 734 torrents estão
parados, um `missingFiles`.
Publicação por hardlinks integrou 8.209 caminhos/6.023 arquivos únicos (5,449 TB);
dois aliases degradados ficam excluídos, com origens preservadas. Backend edge
e isolamento PVE passaram; DNAT TCP 80/443 aplicado e 33 checks do proxy aprovados.
Publicação em ativação, sem certificado TLS: 80/443 externas dão timeout;
10443 recebeu conexões externas no diagnóstico. Depois da recusa da operadora,
o proprietário aceitou a URL com `:10443`, certificado DNS-01 e gestão privada
por Tailscale com domínio próprio. A configuração da porta 10443 está em preparação;
certificado, renovação/DDNS e acesso externo seguem pendentes. Cloudflare é
opcional; não há decisão de migrar o DNS nem zona criada. Gatilhos Jellyfin
continuam contidos. Esse estado não altera os limites abaixo; consulte o
[registro atual](docs/architecture/homelab-2026/implementation-status.md).

A etapa atual de playback usa somente `compose.playback.yaml`, conforme o
[runbook NFS](docs/runbooks/playback-nfs.md). Nesse destino, o proprietário
determinou mídia somente leitura; a regra histórica de Jellyfin RW abaixo
descreve o monólito anterior. Não iniciar o Compose monolítico nas novas VMs.
Preservar os gatilhos de scan suspensos enquanto o acervo estiver parcial.
Storage físico/exports e coordenação USB pertencem à plataforma; o consumidor
verifica mounts e contém sua aplicação. Não interpretar mount presente como
saúde física ou recuperação automática. Backup, cópias de referência e estado
privado ficam fora do Git.

Transcode usa ext4 no disco virtual dedicado de 32 GiB em `/srv/transcode`,
com subpasta RW e marcador de UUID RO no container; não retornar ao tmpfs
provisório nem aceitar fallback sem o mount. O limite de memória continua
em 3 GiB. As 18 senhas foram redefinidas e testadas por login/logout; a relação
fica somente no Windows privado, sem nomes ou credenciais no Git. Em 16/11, o
proprietário retomou Abyss, com fontes locais e home ajustada nas 18 contas. Login,
Spotlight, detalhes e vídeo passaram em desktop/celular com toque, assim como
13 checks do proxy interno. Busca passou; setas e roda dos carrosséis mantêm a
página desktop sem deslocamento lateral. A `r4`, com backdrop difuso restrito
à home, passou nos contextos desktop e Android com toque usados no aceite.
Um navegador já configurado como Modern preservou o cabeçalho anterior na `r4`.
A `r5` introduziu a migração local única para Legacy, marcada por
`homeserver.abyss.legacy-layout-v1=1`, preservando TV/Legacy/valores desconhecidos
e escolhas futuras. Modern salvo no desktop e Auto salvo no Android migraram;
uma escolha Modern posterior sobreviveu ao reload nos dois contextos. Login,
home, fontes, Spotlight, backdrop e logout passaram sem erros de página.
A `r6` oculta a barra Início/Favoritos por CSS restrito à home; o import de
Branding agora usa `r6`. Não reaplicar preferências das 18 contas. Backdrops
só são ativados quando ausentes. A regressão final de playback passou, incluindo
detalhes, vídeo 1080p e remoção do backdrop fora da home, sem erros de página.
Esse aceite não comprova WAN/TLS.
Consulte `docs/runbooks/abyss.md`; o adiamento anterior é histórico.
A lease obrigatória de storage de até 45 segundos está implantada; expiração,
`HOLD` e preservação de parada manual foram testados. `resume` exige ação
explícita e prova nova; não confundir retomada permitida por `unless-stopped`
com remoção de parada manual pelo Docker. Reboot com SSD e guard passou, incluindo
recusa da lease do boot anterior; após reboot do Proxmox, auditar o estado antes de readmitir. Não prometer
retorno integral desassistido. GPU e reintegração física USB seguem pendentes.

A preparação inicial do core ocorreu com apps inativos; a ativação parcial
posterior está registrada em `docs/runbooks/core-migration.md`. A verificação
somente leitura das cópias no
destino usa `scripts/verify-consolidation-target.py`; o executor transitório
iniciado em 16/11 foi parado a pedido do proprietário no mesmo dia. Não retomar
a varredura integral: a orientação atual é triagem de reprodução. Conferir os
recibos privados antes de usar resultados parciais. Não executar helpers históricos de
consolidação contra as novas identidades. Hashes desse escopo não encerram
publicação, delta final das origens, caso degradado ou readmissão do core.
Na triagem solicitada, erro de rede, autorização, codec, transcode ou caminho
ainda não publicado é inconclusivo, não arquivo corrompido. O proprietário
autorizou excluir as cópias que comprovadamente não reproduzem por defeito do
conteúdo; preservar as origens e registrar a evidência e os aliases antes de
qualquer exclusão. A exceção não permite limpeza indiscriminada por erro HTTP
ou código de saída do decoder. O piloto somente leitura não apagou arquivos.

Projeto pessoal executado pelo `compose.yaml` da raiz. Parâmetros de comportamento
e credenciais ficam no `.env` privado; defaults técnicos ficam no código/Compose.

## Comandos

- `docker compose up -d --build`: construir e iniciar a stack.
- `docker compose ps` e `docker compose logs --tail=100`: acompanhar serviços.
- `make lint`: Ruff, compilação, sintaxe Bash e ShellCheck quando instalado.
- `make test-unit`, `make test-contract`, `make test-integration`: checks locais.
- `make compose-check` e `make smoke`: Compose e verificações locais.

Use Python 3.12 e `uv sync --frozen` em Linux/WSL2 para desenvolvimento.
Windows não substitui testes de filesystem, GPU, rede e reprodução no servidor.

## Configuração e dados

- `.env` guarda credenciais diretamente, sem arquivos de segredo `*_FILE`.
- Layout padrão: `/srv/appdata`, `/srv/data`, `/srv/transcode` e `/srv/appdata/control` (runtime `/run/homeserver` nos containers).
- Fixtures usam diretórios temporários; nunca apontar testes para dados reais.
- Nunca colocar `.env`, bancos, tokens, mídia ou inventário bruto no Git.

## Limites operacionais

Não formatar/particionar discos nem alterar montagens sem autorização explícita
para os dispositivos e o escopo da operação. A expansão mergerfs opt-in está
documentada em `docs/storage.md`; o instalador só verifica mounts, prepara pastas
do projeto e testa fixtures, sem formatar, montar ou editar `fstab`.
Não ativar RTX, substituir `fstab`, reescrever o serviço Lenovo, abrir portas
no roteador ou publicar diretamente a porta administrativa nativa qBit.
O proprietário autorizou controle completo nos painéis autenticados: o proxy
administrativo qBit em loopback/Tailscale permite ações manuais. Downloads
automatizados continuam sujeitos ao gateway e à capacidade real. Não apagar
mídia automaticamente. Jellyfin tem escrita para edições e legendas; exclusões
de filmes, episódios e temporadas continuam coordenadas pelo controlador.

O projeto atual não tem CI/CD, releases próprias, gerenciador de backups ou
units do host. Não reintroduzir essas camadas. CYD/MQTT exclusivo foram retirados.
Documentos antigos de produtização são históricos; use README e guias atuais.
Informações declaradas não comprovam testes físicos, Tailscale ou reprodução.
