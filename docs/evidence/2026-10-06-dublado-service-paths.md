# Dublado e acesso por caminhos — verificação em 2026-10-06

## Entrega

- Busca Dublado ampliada somente em uIndex e 1337x, com aliases, consultas
  direcionadas e evidência de áudio do título, parser e descrição/MediaInfo.
- Categorias Other/Anime do uIndex incluídas. Radarr Dublado tem Custom Format
  de idioma português com score 1 para ampliar títulos pesquisados; Standard e
  Max Quality permanecem com score 0 nesse formato. Não exige português no
  mínimo de score, preservando o fallback original do controlador.
- Seleção continua sujeita à identidade nativa, qualidade, manifesto, capacidade
  e gateway. Fontes complementares passam por `release/push` do Arr.
- Busca incompleta não autoriza fallback original. Orçamento de 30 segundos,
  cache e cursores permitem continuar a investigação sem bloquear o ciclo por
  vários minutos. Downloads existentes não são trocados.
- Auditoria de áudio após ffprobe registra correspondência, divergência ou
  incerteza; não apaga mídia nem bloqueia importação por tags ambíguas.
- Proxy HTTP 80 com `/status/`, `/jellyfin/`, `/sonarr/`, `/radarr/`,
  `/prowlarr/`, `/bazarr/` e `/qbittorrent/`. `/seer` e `/seerr` redirecionam
  para o mesmo host na porta 5055, pois a versão do Seerr não suporta subpastas.
- Base URLs e conexões internas reconciliados. A porta antiga 8096 mantém
  compatibilidade e continua atravessando o proxy de exclusão coordenada.

## Verificações locais

- Suíte completa pytest em Python 3.12 Linux/WSL: exit 0.
- 85 testes de proxy com binários reais Caddy/nginx: aprovados, sem skips.
- 71 testes focados após as correções finais de deadline/configuração: aprovados.
- Ruff, compilação Python, sintaxe Bash e checks shell de montagem/layout/auditoria:
  aprovados. Os checks de filesystem usaram fixtures, nunca os dados do servidor.
- Compose validado no cliente e no servidor. WSL não tem Docker instalado;
  a validação Compose do cliente usou Docker CLI do Windows.
- Casos que exigem execução como root/UID 1000 permaneceram fora dessa execução
  local. Reprodução física na TV não foi testada nesta entrega.

## Verificações no servidor

- Build manual por Docker Compose concluído; 16 serviços em execução e todos os
  healthchecks definidos saudáveis.
- `operator config verify`: sete serviços `verified`, zero alterações pendentes.
- HTTP 200 no painel pelo IP Tailscale e por MagicDNS a partir do cliente.
- APIs autenticadas de Sonarr, Radarr, Prowlarr, Bazarr e Jellyfin: HTTP 200 pelos
  caminhos novos. Painel autenticado de downloads HomeServer: HTTP 200.
- Login qBit: HTTP 200, sessão com cookie em `/qbittorrent/` e leitura da fila.
- CSS/JavaScript dos seis painéis nativos testados: HTTP 200 pelos prefixos.
- Jellyfin público respondeu tanto em `/jellyfin/System/Info/Public` quanto no
  endereço legado `:8096/System/Info/Public`.
- Fila comparada por hash: 178 torrents antes/depois, quatro incompletos,
  zero removidos e zero adicionados pela implantação.
- Descoberta real após o ajuste de deadline: retornou em 30,01 segundos,
  com dois candidatos parciais e `complete=false`, sem grab.

## Busca real e limites da evidência

As consultas de diagnóstico foram somente de metadados, sem grab. Em Inside Out,
a release AlienVolt consultada declarou inglês e português genérico; isso não
comprova a variante brasileira. Em Perfect Blue, as amostras com `DUAL AUDIO`
também não comprovaram PT-BR. Consultas/inspeções incompletas permaneceram
explicitamente incompletas, sem virar autorização para fallback original.

O projeto melhora descoberta e classificação, mas não cria informação ausente
na fonte. A auditoria de trilhas reais só pode ocorrer quando um download termina;
downloads antigos sem snapshot de seleção não recebem um perfil presumido.

Foi identificado um bug de leitura no Jellyfin 10.10.7: o servidor grava a
permissão de letras, mas a omite ao montar o DTO do usuário. O configurador não
trata esse campo como drift; continua concedendo-o junto a mudanças do proprietário.
Administrador, coleções, legendas e exclusão continuam com read-back normal.
Referência: [UserManager da versão instalada](https://github.com/jellyfin/jellyfin/blob/v10.10.7/Jellyfin.Server.Implementations/Users/UserManager.cs).

## Pendência anterior observada nos logs

A checagem final encontrou `ValidationError` na conclusão de importações de
filmes. Duas amostras foram investigadas somente com leitura: E.T. e Spectre
estão em `subtitles_pending` desde 03/10 às 10:47 UTC. Seus vídeos já estão
importados com hardlinks corretos e passam na validação de mídia.

As legendas PT-BR já presentes na biblioteca diferem das legendas SubDL
selecionadas pelo controlador. A proteção contra sobrescrever uma legenda
existente deixa a conclusão pendente ao verificar o hardlink. Isso antecede
esta implantação e não passa pela nova auditoria de áudio. Nenhuma legenda,
mídia ou estado de importação foi alterado durante o diagnóstico; os demais
erros de importação não foram individualmente investigados nesta entrega.
