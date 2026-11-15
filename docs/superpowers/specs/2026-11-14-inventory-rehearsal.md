# Inventário e ensaio de publicação incremental

## Objetivo e autorização

Conectar metadados de inventário ao planejador de lotes e executar uma cópia e
publicação verificadas em fixtures Linux. O proprietário autorizou continuar a
preparação online e publicar código na branch `refactor`. A parada do servidor
continua dependendo de aviso explícito. Esta entrega não acessa o servidor.

## Contrato

- Python 3.12, biblioteca padrão, sem novas dependências ou hooks no runtime.
- Nenhuma leitura de `.env`, bancos, mídia, inventário privado ou helpers privados
  pela implementação pública. Nenhuma chamada de rede, Docker ou subprocesso.
- O adaptador recebe em memória um contrato normalizado v1, correspondente à
  estrutura do inventário privado de delta final: `version=1`,
  `inventory_generation`, `complete`, `files`, `directories`, `special`,
  `collisions`. As entradas de arquivo têm `pool`, `relative`, `device`, `inode`,
  `size`, `allocated`, `mtime_ns`, `mode`, `uid`, `gid`, `nlink`.
  O inventário inicial de lançamento não é entrada compatível. Campos ausentes
  não recebem valores presumidos; este adaptador não converte o formato inicial
  nem autentica evidências do servidor.
- Mapa explícito de pools para identidades lógicas e pool nativo do destino;
  nenhuma identificação automática por nome de device. Metadados importados
  continuam sendo declarações offline, sem comprovação de atualidade física.
- Validar todo o inventário, inclusive arquivos nativos do destino e grupos não
  selecionados. Arquivos duplicados/ancestrais, file/directory, aliases omitidos,
  assinaturas divergentes, special e colisões declaradas bloqueiam planejamento.
  Diretórios compartilhados entre pools são permitidos.
- Agrupar por inode físico declarado, não pelo hash. Uma seleção inclui todos
  os aliases e dependências. SHA de conteúdo nunca pode ser substituída pelo
  digest do inventário. Hashes declaradas ligam-se ao manifesto de metadados da
  inspeção; não promovem recibos ou indicadores de staging/publicação/readback.
- Nesta primeira versão, exigir hashes declaradas para todos os grupos de
  origem antes de emitir entradas para o planejador. Cobertura parcial aparece
  explicitamente no relatório e bloqueia o plano; não omitir grupos sem hash.
- O planejador existente e seus quatro flags operacionais falsos permanecem
  inalterados. Nenhuma declaração offline autoriza operação.
- O ensaio aceita somente um cenário nomeado. Cria seu próprio
  `TemporaryDirectory` privado em Linux, sem argumentos source/destination/apply.
  A fixture contém hardlinks, dependência, inodes distintos com bytes iguais e
  conteúdo não selecionado. Usa lstat e SHA reais, com verificação antes/depois.
- Cópia cria um novo inode por grupo em staging e reproduz os aliases; nunca
  liga origem diretamente ao staging. Confere conteúdo e topologia antes de
  publicar por hardlink exclusivo, recusando qualquer destino existente.
- Publicação é multietapa, não atômica. Interrupção conserva o estado parcial e
  staging dentro do ensaio até registrar o resultado; nenhum lote incompleto
  libera o consumidor simulado. O diretório temporário é removido ao terminar.
- Contenção Jellyfin/Sonarr/Radarr é explicitamente simulada: scans globais,
  tratamento destrutivo de ausências e caminhos externos ao lote são rejeitados.
  Somente a lista exata relida após publicação admite scan restrito simulado.
- Relatório agregado: `scope="synthetic_fixture"`,
  `containment_kind="simulated"`, `production_ready=False`. Sem paths reais ou
  recibos que aparentem provar comportamento nativo. Todas as permissões
  operacionais continuam falsas. Fontes da fixture são conferidas ao final.

## Limites e aceite

Demonstrar sucesso e bloqueios para corrupção, colisão, interrupção, mudança de
origem e perda de contenção. O ensaio não mede USB, capacidade, sparse,
ACL/xattrs, crash recovery, fencing ou contenção real de aplicativos. Nenhum
arquivo é publicado no acervo e nenhuma evidência histórica é reautenticada.
CLI e testes em WSL/Python 3.12, revisão independente e publicação em `refactor`.
