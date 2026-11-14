# Validação dos três perfis — 2026-10-05

`Max Quality`, `Standard` e `Dublado` foram implementados e ativados para filmes
e séries. A ordem de qualidade está documentada no [README](../../README.md).
`Standard` é o padrão dos dois servidores configurados no Seerr. O mínimo de
seeds é 5, ajustável no `.env`, assim como o perfil padrão e a preferência de
áudio do Dublado.

## Verificações locais

- Suíte completa em Python 3.12 no Linux/WSL: **1.879 testes aprovados**, 109
  ignorados e três avisos de depreciação das dependências.
- Dos testes ignorados, 105 exigem um runtime Caddy e quatro exigem root e
  fixtures de propriedade de arquivos. Não foram apresentados como aprovados.
- `make lint PYTHON=.venv/bin/python` passou.
- Revisão da seleção inicial, trocas manuais, packs, áudio, legendas e
  sincronização da escolha do Seerr com títulos já cadastrados no Arr.

## Verificações no servidor

- Imagens de controle e telemetria construídas e serviços atualizados.
- Radarr e Sonarr contêm exatamente os três perfis. A leitura pelas APIs
  confirmou a ordem das qualidades e upgrades automáticos desativados.
- A API usada pelo seletor do Seerr retorna os três nomes para filmes e séries.
- Verificação posterior da configuração apresentou **zero divergências**.
- Migrados 132 cadastros de filmes, dois de séries e 25 coleções. As coleções
  conservaram monitoramento, diretório e opção de busca.
- Corrigidas 96 referências de perfil em 98 pedidos persistidos do Seerr, com
  verificação de que estados e registros de temporadas foram preservados.
- A escolha histórica do único título ainda aprovado foi registrada como já
  aplicada para evitar que o pedido antigo sobrescreva futuras escolhas manuais.
- Comparação antes/depois preservou os **108 torrents** (hashes, destinos,
  tamanhos e discos) e **161 itens de biblioteca** (IDs, tipos e caminhos).
  O restante dos downloads era zero no momento da verificação.
- Serviços de controle, worker, gateway, telemetria e Jellyfin passaram nos
  healthchecks. A inicialização do Compose terminou com código zero.

## Compatibilidade e observação separada

O Radarr recalcula `movieFile.qualityCutoffNotMet` ao mudar um perfil. A
comparação ignora exclusivamente esse booleano derivado, mantendo a validação
dos demais campos. A migração também verifica referências de coleções antes
de remover definições antigas; listas de importação não suportadas bloqueiam
a migração para impedir alterações de provedores sem validação.

Os logs ainda apontam erros de finalização em itens que aguardam legendas.
Uma inspeção somente de leitura em três exemplos encontrou legendas pt-BR já
existentes na biblioteca com conteúdo e inode diferentes das cópias no diretório
do torrent. As datas desses arquivos antecedem esta ativação. A biblioteca e
as legendas existentes foram preservadas; esta alteração de perfis não resolve
essa reconciliação de legendas. Isso não indica falha na criação dos perfis,
nem ausência dos filmes já publicados no Jellyfin.

Não foi iniciada uma nova transferência para testar reprodução ou disponibilidade
real de peers. Contagens informadas pelos indexadores continuam sendo indícios,
não uma garantia de velocidade ou conectividade.
