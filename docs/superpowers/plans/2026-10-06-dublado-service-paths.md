# Dublado e acesso por caminhos — plano de implementação

**Objetivo:** encontrar áudio brasileiro nos indexadores já autorizados e abrir os painéis por nomes no mesmo host.

**Escopo aprovado:** uIndex e 1337x; Dublado mantém dual PT-BR + original, PT-BR, original; 1080p Blu-ray e WEB-DL. Downloads em andamento permanecem escolhidos até ação manual. Legendas, capacidade real e importação sequencial de séries continuam vigentes.

**Arquitetura:** enriquecer a descoberta existente com consultas limitadas, identidade nativa e evidência explícita de áudio. Caddy recebe os caminhos públicos; serviços com suporte usam Base URL nativa. Jellyseerr 2.7.3 não suporta subcaminho oficialmente: `/seer` será um atalho para a porta 5055 do mesmo hostname, sem manter um fork do aplicativo.

**Execução:** implementação autorizada pelo proprietário nesta conversa. Trabalho no checkout existente para preservar as alterações acumuladas ainda não commitadas; nenhum reset, limpeza de Git ou publicação. Agentes divididos por arquivos, integração e deploy pelo agente principal.

## Restrições

- Não adicionar indexadores, trocar torrents, apagar mídia ou iniciar buscas automáticas de upgrades.
- Não publicar qBittorrent nativo; encaminhar pelo proxy administrativo autenticado existente.
- Nenhum CI/CD, release, backup gerenciado, alteração de montagem ou abertura de porta no roteador.
- Credenciais no `.env`; caminhos e URLs técnicos no código/Compose.
- Testes em fixtures temporárias, Python 3.12 Linux/WSL; verificações no servidor separadas das locais.

## 1. Evidência de áudio

- [x] `worker/audio_evidence.py` concentra leitura contextual de título, descrição e MediaInfo.
- [x] `release_quality.py` consome `audioEvidence` explícita; `audioLanguagesInferred` impede que o idioma cadastrado do filme vire prova de faixa.
- [x] Regressões: SUBS não apaga áudio explícito, colchetes em listas, português genérico, PT-PT, comentários e legenda PT-BR com áudio inglês.
- [x] Auditoria após ffprobe por filme/episódio registra divergências sem apagar nem substituir automaticamente.

## 2. Descoberta Dublado

- [x] `worker/dubbed_discovery.py` consulta aliases e variantes PT-BR, com cache, limites, erros distintos de ausência e indexadores permitidos.
- [x] Consultar detalhes somente em hosts de indexadores autorizados, sem repassar credenciais a páginas externas.
- [x] Deduplicar infohash; validar título/ano e temporada/episódios; guardar origem da evidência.
- [x] `acquisition.py` e `series_acquisition.py` aplicam ranking áudio, qualidade, seeds; resultados novos passam pela decisão nativa `/release/push` depois de manifesto, gateway e capacidade.
- [x] Atualizar filtros uIndex para incluir resultados classificados como Other e filmes de anime, mantendo identificação estrita antes da aquisição.
- [x] `worker/__main__.py` injeta os clientes e credenciais existentes na descoberta.

## 3. Caminhos dos serviços

| Caminho | Destino |
|---|---|
| `/` | `/status/` |
| `/status/` | telemetry, removendo prefixo e mantendo Host para autenticação/CSRF |
| `/jellyfin/` | jellyfin-proxy, preservando exclusão coordenada |
| `/sonarr/`, `/radarr/`, `/prowlarr/`, `/bazarr/` | Base URL nativa |
| `/qbittorrent/` | qbit-monitor autenticado, removendo prefixo |
| `/seer`, `/seerr` | redirecionamento ao mesmo hostname, porta 5055 |

- [x] `deploy/service-proxy/Caddyfile`, Compose e render incluem o proxy sem acesso direto ao filesystem de mídia ou à rede de transferência.
- [x] Defaults internos incluem os prefixos; configuração inicial e reconciliação nativa preservam os demais campos.
- [x] Atualizar conexões Arr/Prowlarr/Seerr/Bazarr/Jellyfin com Base URL e manter o acesso Jellyfin anterior pelo proxy.
- [x] Cards do painel usam caminhos relativos; URLs funcionam por IP LAN, IP Tailscale e MagicDNS.
- [x] Testar redirecionamentos, cookies, assets, API, WebSocket e rotas administrativas.

## 4. Integração, documentação e implantação

- [x] Testes focados por componente, testes unitários/contrato/integração completos e lint.
- [x] Revisão independente das mudanças, especialmente permissão antes do grab, SSRF, parsing de idioma e bypass de exclusão.
- [x] Atualizar README, instalação, operação e busca de fontes; registrar limitações observadas.
- [x] Aplicar configurações nativas e containers necessários no servidor, sem parar qBit ou limpar a fila.
- [x] Verificar pesquisas sem grab, estado dos serviços, login e leitura pelos caminhos novos e compatibilidade das conexões internas.

## Riscos específicos a verificar

1. Busca por alias retornar outro filme ou uma temporada errada.
2. Descrição mencionar PT-BR apenas em legendas ou comentários.
3. Resultados Prowlarr não constarem no cache de decisões Arr.
4. Prefixos quebrarem callbacks, URLs internas, sessões ou captura DELETE do Jellyfin.
5. Instalação nova começar sem Base URL enquanto clientes já usam prefixos.

## Registro da execução

- Planejamento registrado durante a implementação autorizada; estado inicial contém trabalho anterior não commitado.
- Evidência anterior: Divertida Mente retorna 0 itens em categoria 2000 e 2 itens em 8000; não é ausência de fontes.

- Implementação, revisão e implantação concluídas. Evidência em [relatório de verificação](../../evidence/2026-10-06-dublado-service-paths.md).
