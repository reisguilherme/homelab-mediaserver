# Contenção nativa e restauração em ambiente isolado

## Objetivo e autorização

O proprietário autorizou a próxima etapa de preparação: implementar e ensaiar
contenção real dos serviços e backup com restauração verificável, mantendo o
servidor disponível. O ensaio usa somente configurações novas e mídia sintética
no Linux local. Publicar código revisado na `refactor`; não acessar produção,
capturar seus bancos, parar serviços reais, alterar mounts ou remover manutenção.

## Abordagem

Parar clones nativos por IDs completos e conferir identidade/estado permite
demonstrar ausência de execução durante a captura. Desabilitar somente realtime
ou um scheduler não cobre todos os caminhos de scan. Foi escolhida a parada
para esta etapa; retomada parcial com aplicações ativas continua bloqueada.

O checkpoint é um arquivo GNU tar/PAX com manifesto fechado, restaurado em
diretório Linux novo e comparado por conteúdo/metadados. Uma cópia solta no NTFS
não atende a esse objetivo. Não reintroduzir agendamento, Restic, serviço de
backup, CI/CD ou units. Nenhum código novo integra automaticamente o runtime.

## Contratos

- Python 3.12, biblioteca padrão e GNU tar local. Sem dependência Python nova.
- Implementação em worktree isolado; fixtures sempre sob diretório privado
  criado pelo ensaio em `/tmp`, nunca `.env`, bancos ou mídia reais.
- Biblioteca de checkpoint recebe origem quiescente e callback que valida
  contenção antes/depois dos efeitos. A callback é uma pré-condição confiada ao
  chamador, não prova universal de ausência de escritores externos.
- Captura fechada de diretórios, arquivos, hardlinks e symlinks relativos internos;
  preservar UID/GID, modos, mtime_ns, ACL e xattrs suportados. Inventariar antes
  e depois; mudança, link externo, FIFO/socket/device, alias ausente ou erro de
  metadados bloqueia. Não seguir symlinks para capturar arquivos externos.
- Criar diretórios/arquivos exclusivos e privados; não sobrescrever destinos.
  Conservar artefatos incompletos em falha. Arquivo final e manifesto só existem
  como evidência válida após fsync e verificações; não afirmar consistência por
  hashes isolados. Manifesto e caminhos detalhados são privados.
- Restore recebe hashes esperados do manifesto/arquivo obtidos na captura.
  Validar membros integralmente antes de extrair, inclusive traversal, duplicata,
  ancestry symlink, hardlink externo, tipos especiais e limites de tamanho.
  Usar o mesmo arquivo validado e diretório privado novo. Conferir árvore
  restaurada contra manifesto por bytes, metadados e topologia de hardlinks.
- SQLite: detectar por cabeçalho, além da lista explícita de bancos esperados;
  banco esperado inválido falha. Preservar WAL/journals. Integridade e leituras
  ocorrem em outra cópia descartável, nunca no original ou árvore restaurada
  de referência. Validar transação confirmada no WAL em fixture de processo.
- Resultados distinguem bytes, metadados e SQLite verificados. Validação de
  aplicação depende do ensaio nativo posterior. Não emitir `production_ready`.
- CLI nativa aceita apenas cenário nomeado; não aceita source, destination,
  container ID, Compose project, `.env`, Docker endpoint ou configuração externa.
- Criar clones Jellyfin/Sonarr/Radarr de imagens locais declaradas no Compose,
  resolvidas a IDs de imagem e versões reais; `--pull never`, `--network none`,
  `--restart no`, sem portas, dispositivos, modo privilegiado, PID host ou
  socket Docker. APIs via loopback dentro do clone. Limitar CPU/RAM/logs.
- Capturar IDs completos retornados pelo Docker e nonce/labels, imagem e binds
  exatos. Nenhuma operação Docker mutante pode receber container preexistente
  ou descoberto apenas por label/nome. Validar conjunto inteiro antes de parar.
- `docker stop --timeout -1`; erro/timeout não autoriza kill, remoção ou sucesso.
  Comparar Running/Restarting/Paused/PID/StartedAt/FinishedAt e identidade ao
  longo da captura. Restart entre duas leituras invalida a contenção.
- Cleanup remove somente clones próprios confirmados parados e depois a raiz
  da fixture; falha conserva recursos e relata hold. Nunca `rm -f`, SIGKILL ou
  limpeza automática de diretório ainda montado por consumidor possível.
- Ensaio normal: baseline HTTP nativo e catálogo Jellyfin sintético, parada real,
  checkpoint, cópia opaca do arquivo, restore em árvore nova, verificação e
  novos clones sobre outra cópia restaurada, com identidade nativa preservada.
  Arr deve preservar uma configuração/objeto criado por API, sem depender de
  indexadores/metadata externos; não declarar scan de catálogo Arr se não ocorreu.
- Ensaio de restart inesperado deve detectar perda e bloquear próximo efeito.
  Testes unitários cobrem substituição, bind extra, Docker ambíguo e cleanup.
- Saída pública apenas agregados/versões/booleans e códigos fixos. Nunca retornar
  segredos, corpo de banco, logs nativos ou inventário detalhado em erros.

## Limites explícitos

O checkpoint ensaiado é de appdata sintético; não é o checkpoint operacional de
todos os 21 serviços, overrides, imagens locais e estado de recuperação. Essa
lista e a disponibilidade independente das imagens continuam requisitos do
corte. Não extrair arquivo recebido de fonte não confiável; hashes esperados
precisam vir da captura confiável, não do próprio arquivo recebido.

Polling detecta mudanças observáveis, mas não impede um administrador externo
de reiniciar containers. O ambiente privado do ensaio fornece a exclusão de
escritores. Sem essa exclusão, um relatório positivo não é um fence operacional.

`scope=isolated_native_fixture`, `containment_kind=docker_stopped_native_applications`,
`partial_restart_safe=false`, `production_ready=false`. Uma rodada unitária
simulada ou teste Docker pulado não pode produzir evidência de execução nativa.
Perda de USB/energia, migração incremental e retomada com acervo parcial seguem
dependências separadas. As fontes reais permanecem intactas.
