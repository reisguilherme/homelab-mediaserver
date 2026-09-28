# Preparação dos serviços

Use o [guia de instalação](../installation.md) fresh/adopt e o `.env` privado.
Compose no repositório é referência; runtime/units/configuração nativa são
produzidos pelo instalador. Não edite os derivados nem mantenha overrides
concorrentes. Credenciais geradas/importadas ficam em arquivos 0600 fora do Git.

Execute `config plan`, `config apply`, `config verify` conforme o
[guia do operador](../operator-guide.md#aplicar-uma-mudança). Read-back confirma
valores efetivos, IDs existentes são preservados e serviços sem capacidades
compatíveis retornam unsupported. Apply parcial pode ser repetido após corrigir
a dependência; recursos estrangeiros não são apagados.

## Recursos gerenciados

- qBit: downloads/seeding/torrents ativos, bandwidth, conexões, porta de peers,
  flags de fila e limites de seed. UPnP permanece desativado. 20 Mbit/s solicita
  2.500.000 B/s; a versão 5.1.2 grava KiB inteiros, ou 2.499.584 B/s. Adopt importa
  a precisão nativa em UPLOAD_LIMIT_BYTES/DOWNLOAD_LIMIT_BYTES.
- Arr: rootfolders `/data/media/movies` e `/data/media/tv`, perfil HomeServer
  compatível com resolução/fontes e cliente **HomeServer Gateway**. Este usa
  usuário arr/token do gateway; qBit não é um cliente direto alternativo.
  Definições de qualidade recebem pisos de vídeo/minuto e tamanho máximo
  ilimitado. Completed Download Handling fica desabilitado, e hardlinks ficam
  habilitados: o worker coordena validação/importação antes de publicar a mídia.
- Prowlarr: aplicações Arr, indexadores declarados por schema nativo e proxy
  FlareSolverr quando BYPARR_ENABLED. Credenciais mascaradas exigem teste de
  conexão e evidência privada persistente; máscara sozinha não comprova igualdade.
- Bazarr: providers declarados, conexões Arr e perfil HomeServer derivado de
  idiomas. Worker aplica prioridades pt-BR/release/edição e inglês fallback;
  não habilitar pt-PT como se fosse pt-BR. Chaves/contas não vão no Git.
- Jellyfin: bibliotecas adotadas pelo path com IDs preservados, conta inicial
  somente quando ausente, número de threads e permissão opcional de exclusão.
- Seerr: conexão Jellyfin, bibliotecas, Arr e perfis pelos IDs reais, preservando
  usuários/pedidos. Tokens descobertos autenticadamente são persistidos pelo CLI.

Contas existentes não têm senhas redefinidas silenciosamente. Nome de biblioteca
existente com path diferente exige adoção explícita. Perfil estrangeiro não é
renomeado/removido. Busca/RSS/grabs Arr autônomos devem permanecer desabilitados:
novos downloads passam pelo worker/gateway para checar tamanho real e capacidade.

## Painéis web nativos

Os cards do painel usam JELLYFIN_PUBLIC_URL, SEERR_PUBLIC_URL, SONARR_PUBLIC_URL,
RADARR_PUBLIC_URL, QBIT_MONITOR_PUBLIC_URL, PROWLARR_PUBLIC_URL e BAZARR_PUBLIC_URL.
Esses URLs e portas ficam no `.env`. Administre via Tailscale/loopback; não abrir
portas do roteador nem publicar a API nativa qBit na LAN.

O monitor qBit mantém autenticação nativa e permite UI/GET de leitura + login/
logout. Botões de mutação podem aparecer na UI, mas retornam 403. Use Sonarr/
Radarr para acompanhar importação, Prowlarr para fontes e qBit para velocidade,
progresso e peers; não adicionar ou retomar torrents contornando o controlador.

## Porta de peers

QBIT_PEER_PORT (padrão 6881) e QBIT_PEER_BIND_IP configuram TCP/UDP de transferência,
separados da API administrativa. Bind padrão é 127.0.0.1; escolha endereço LAN
explícito quando necessário. Publicação Docker não abre roteador e não garante
conexões externas ou velocidade: seeds disponíveis continuam sendo determinantes.

## Fontes e legendas

Após cinco minutos sem progresso ou abaixo do limiar configurado, o worker busca
alternativa mantendo a atual. Candidata precisa mesma qualidade/edição, tamanho
verificado, capacidade conjunta e medição suficiente. A promoção exige ETA
melhor; indisponibilidade/ambiguidade exige reconciliação. Arquivos parciais
anteriores não são apagados automaticamente. Séries mantêm sequência mesmo
quando filmes mais saudáveis passam à frente.

Prioridade de legenda é pt-BR mesma release, pt-BR edição compatível, inglês
mesma release e inglês edição compatível. Duração evita extended/director cut
incompatível, com margem para créditos. Original pt-BR pode dispensar legenda.
Use SUBDL_API_KEY e providers Bazarr declarados; provider desativado/sem chave
não prova disponibilidade. Validação/importação precedem publicação Jellyfin;
um 100% no qBit ainda pode aguardar legenda ou importação.

`AUDIO_LANGUAGES` prefere idiomas declarados nos metadados de release;
`original` exige contexto do Arr. Não deduza idioma ou região pelo nome.
`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` aceita `pt-BR`, ou vazio para desativar
a dispensa; não há detector de dispensa original en-US nesta versão.

Para os comandos por sintoma, consulte [troubleshooting](../troubleshooting.md).
Tags de desenvolvimento não são prova de segurança/compatibilidade: produção
usa manifesto validado com digests imutáveis e versão/SHA observada.
