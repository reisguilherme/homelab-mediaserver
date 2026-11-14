# Legendas e capas de coleções — 09/10/2026

O servidor possui 175 filmes sem pendências de legenda pt-BR no Bazarr.
OpenSubtitles.com e SubDL estão habilitados e saudáveis. O login direto confirmou
VIP, limite de 1.000 downloads por dia e 895 disponíveis na verificação.
O DNS explícito do Bazarr foi mantido no Compose e no renderer, que usa a mesma
versão 1.6.2 já instalada no servidor.

## Legendas

- O finalizador de filmes reconhece legendas válidas instaladas pelo Bazarr
  depois da importação. Verifica origem do vídeo, idioma, conteúdo, raiz e pool
  físico; mantém a seleção do permit e a prioridade pt-BR.
- O proxy padroniza somente `MediaStreams[].DisplayTitle` no catálogo,
  informações de reprodução e notificações WebSocket. Autenticação continua
  sendo a do cliente, validada pelo Jellyfin nativo.
- Os nomes configurados são `Português BR` e `English - US`. Inglês sem região
  usa essa preferência visual; regiões declaradas diferentes são preservadas.
  Português genérico não é promovido a brasileiro. Forçada, SDH, padrão e
  detalhes úteis das fontes permanecem visíveis. Colisões usam formato, origem
  e índice para distinguir as faixas.
- A API nativa e a API pelo proxy retornaram 243 filmes/episódios e 2.472 faixas
  de legenda, com todos os campos nativos preservados fora de `DisplayTitle`.
  Foram observados 266 rótulos pt-BR, 445 de inglês e 173 identificações SDH.
- Catálogo, detalhe e informações de reprodução retornaram HTTP 200. A entrada
  central e a porta do proxy produziram os mesmos rótulos. Token inválido
  retornou 401; handshake WebSocket nativo foi aceito. Queries de autenticação
  são ocultadas nos logs. O encerramento normal foi repetido no servidor sem
  tracebacks. Fechamento normal e aborto de conexão também passaram em teste
  com Uvicorn real; os headers Date/Server do handshake são fornecidos pelo proxy.

## Coleções

O Jellyfin 10.10.7 cria coleções automáticas bloqueadas e sem `ProviderIds.Tmdb`.
O ID necessário para a capa foi obtido do `TmdbCollection` dos filmes associados,
sem desbloquear as coleções.

- As 32 coleções de franquias tinham identidades inequívocas e capas oficiais
  disponíveis. Todas receberam Primary e as 32 imagens foram acessadas pelo
  proxy com HTTP 200. A amostra visual inspecionada foi a coleção de Mulan.
- Nomes, `LockData` e `LockedFields` permaneceram iguais na conferência após
  a atualização. Uma segunda execução preservou as 32 capas e não fez reparos.
- Uma coleção administrativa de teste vazia permaneceu sem arte. A rotina não
  inventa identidade nem capa para coleções vazias ou ambíguas.
- O worker reconcilia a cada 300 segundos, depois da criação nativa das coleções
  na varredura. Capas existentes são preservadas e falhas de arte não impedem
  o processamento de pedidos.

## Verificação e limites

WSL Ubuntu 24.04, Python 3.12: `make lint` passou; a suíte completa passou com
2.453 testes, 84 pulados e três avisos de depreciação. Os contratos de caminhos
foram executados com Nginx real. Testes de nomes, autenticação, limites e
WebSocket também foram executados isoladamente após os ajustes finais.

Compose e Nginx foram validados no servidor. API, worker e Jellyfin ficaram
saudáveis; Bazarr e proxy permaneceram em execução.

Não houve substituição de mídia nesta tarefa nem teste físico de reprodução
na TV. Os testes de importação usaram fixtures temporárias. A existência de
legenda e a compatibilidade de metadados não comprovam sincronismo perfeito.
Clientes que constroem nomes localmente podem ignorar `DisplayTitle`; a
padronização foi verificada nas APIs e no contrato consumido pelo Jellyfin Web.
