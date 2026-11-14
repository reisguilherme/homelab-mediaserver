# Painéis administrativos e coleções — 06/10/2026

O proprietário autorizou administração completa nos painéis autenticados e a
substituição da coleção manual de James Bond pelas coleções automáticas.

## Alterações

- `qbit-monitor` encaminha a interface e API administrativas completas, mantendo
  autenticação e CSRF nativos. O acesso continua em loopback/Tailscale; os Arr
  continuam usando o gateway de capacidade para downloads automatizados.
- O usuário administrativo configurado do Jellyfin recebe as permissões de
  administração, coleções, legendas e letras. Os demais usuários são preservados.
- A mídia do Jellyfin é gravável, nos layouts simples e SSD/HD, para permitir
  edições e legendas pelo painel. Filmes, episódios e temporadas continuam com
  exclusão coordenada. Outros tipos seguem a exclusão administrativa nativa;
  apagar uma série inteira nativamente não garante limpeza em Arr/qBit/Seerr.
- Vínculos de coleções/playlists usam a API nativa. Excluir uma coleção moderna
  valida sua pasta de metadados e preserva os filmes. Exclusões em lote passam
  pelo mesmo tratamento por tipo. Ações nativas independem do worker.
- `HOMESERVER_JELLYFIN_AUTO_COLLECTIONS=true` configura a biblioteca gerenciada
  de filmes, preservando suas outras opções e as bibliotecas de séries.

## Verificação no servidor

- Removida somente a coleção manual de James Bond; a varredura criou 26 coleções.
  A nova coleção automática de James Bond contém os cinco filmes esperados pelo
  identificador de coleção do TMDb e possui identidade diferente da manual.
- Os 146 arquivos de filmes presentes antes da operação continuaram existentes
  e com os mesmos tamanhos após a configuração e a atualização dos containers.
- Pelo endereço Tailscale do qBit, uma tag temporária foi criada, consultada e
  removida. Sem sessão, a ação retornou 403; origem externa foi recusada com 401.
  Nenhum torrent foi modificado nesse teste.
- Pelo endereço Tailscale do Jellyfin, vínculo e desvínculo de filme retornaram
  204. Exclusão individual e em lote de coleções temporárias retornaram 204.
  Os filmes permaneceram presentes; não restou coleção temporária.
- Jellyfin confirmou escrita nas pastas de filmes e séries. Os 15 serviços
  permaneceram em execução; todos os healthchecks definidos estavam saudáveis.

## Verificação local

WSL Ubuntu 24.04, Python 3.12, Caddy 2.10.2 real para contratos do proxy:
1.994 testes passaram; quatro fixtures de ownership foram puladas por exigirem
root. Houve três avisos de depreciação de dependências. `make lint` passou.
Compose e Nginx também foram validados antes da ativação.

O roteamento `/nome` ainda não foi implementado. MagicDNS poderá resolver o nome
do servidor para esse acesso quando o proxy central for configurado. Não houve
teste de reprodução na TV nesta mudança.
